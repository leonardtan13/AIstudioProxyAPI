from __future__ import annotations

"""
Profile provider abstraction for hydrating authentication material.

The coordinator requires auth profile JSON files and, optionally, an API key list
before it can launch child processes. Profiles are always hydrated onto the local
filesystem so downstream components can operate on regular `Path` objects.

S3 Layout Expectations
----------------------
We assume the backing bucket contains the following structure:

    <prefix>/active/*.json   # per-child auth JSON payloads
    <prefix>/key.txt         # optional newline-delimited API keys

Only the `active/` directory is consumed today. When using AWS, ensure the
execution role grants `s3:ListBucket` and `s3:GetObject` permissions for the
target prefix. Hydrated files are cached under `AUTH_PROFILE_CACHE_DIR`
(`/tmp/auth_profiles` by default) and overwritten on each run so containers can
restart cleanly without leaving stale files behind.

Additional profile providers can be registered by implementing the
`ProfileProvider` protocol and returning a `ProfileHydrationResult`.
"""

import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict

LOGGER = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("/tmp/auth_profiles")


class ProfileHydrationError(RuntimeError):
    """Raised when auth profiles cannot be hydrated from the configured backend."""


class ProfileHydrationResult(BaseModel):
    """Return type describing hydrated auth material."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profiles_dir: Path
    key_file: Path | None = None


@runtime_checkable
class ProfileProvider(Protocol):
    """Protocol for hydrating auth profiles into a local directory."""

    backend_name: str

    def hydrate(self) -> ProfileHydrationResult: ...


def _clean_directory(directory: Path) -> None:
    if directory.exists():
        for child in directory.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        directory.mkdir(parents=True, exist_ok=True)


class LocalProfileProvider:
    """Reads profiles from an existing on-disk directory."""

    backend_name = "local"

    def __init__(self, profile_dir: Path) -> None:
        self._profile_dir = profile_dir.expanduser().resolve()

    def hydrate(self) -> ProfileHydrationResult:
        directory = self._profile_dir
        if not directory.exists():
            raise ProfileHydrationError(
                f"Profile directory does not exist: {directory}"
            )
        if not directory.is_dir():
            raise ProfileHydrationError(f"Profile path is not a directory: {directory}")

        key_candidate = directory.parent / "key.txt"
        key_path = key_candidate.resolve() if key_candidate.exists() else None
        return ProfileHydrationResult(profiles_dir=directory, key_file=key_path)


class S3ProfileProvider:
    """Downloads profiles from S3 into a local cache directory."""

    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str | None,
        *,
        cache_dir: Path | None = None,
        region: str | None = None,
    ) -> None:
        if not bucket:
            raise ProfileHydrationError(
                "AUTH_PROFILE_S3_BUCKET is required for S3 backend."
            )

        self.bucket = bucket
        self.prefix = prefix.strip("/") if prefix else ""
        self.cache_dir = (cache_dir or DEFAULT_CACHE_DIR).expanduser().resolve()
        self.region = region
        self._session = (
            boto3.session.Session(region_name=region)
            if region
            else boto3.session.Session()
        )

    def hydrate(self) -> ProfileHydrationResult:
        LOGGER.info(
            "Hydrating auth profiles from s3://%s/%s into %s",
            self.bucket,
            f"{self.prefix}/active" if self.prefix else "active",
            self.cache_dir,
        )
        profile_target_dir = self.cache_dir / "active"
        _clean_directory(profile_target_dir)

        s3 = self._session.client("s3")
        active_prefix = "/".join(filter(None, (self.prefix, "active")))
        key_file_key = "/".join(filter(None, (self.prefix, "key.txt")))

        downloaded = 0
        paginator = s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self.bucket, Prefix=f"{active_prefix}/"
            ):
                for entry in page.get("Contents", []):
                    key = entry["Key"]
                    if not key.endswith(".json"):
                        continue
                    filename = Path(key).name
                    destination = profile_target_dir / filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    s3.download_file(self.bucket, key, str(destination))
                    downloaded += 1
        except (ClientError, BotoCoreError) as exc:
            raise ProfileHydrationError(
                f"Failed to fetch auth profiles from S3: {exc}"
            ) from exc

        if downloaded == 0:
            raise ProfileHydrationError(
                f"No auth profiles found under s3://{self.bucket}/{active_prefix}/"
            )

        try:
            key_path = self.cache_dir / "key.txt"
            s3.download_file(self.bucket, key_file_key, str(key_path))
            resolved_key = key_path.resolve()
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                resolved_key = None
            else:
                raise ProfileHydrationError(
                    f"Failed to download API key file from s3://{self.bucket}/{key_file_key}: {exc}"
                ) from exc
        except (BotoCoreError, FileNotFoundError) as exc:
            raise ProfileHydrationError(
                f"Failed to stage API key file locally: {exc}"
            ) from exc

        return ProfileHydrationResult(
            profiles_dir=profile_target_dir.resolve(),
            key_file=resolved_key,
        )


def hydrate_profiles(
    *,
    backend: str,
    profile_dir: Path,
    bucket: str | None,
    prefix: str | None,
    region: str | None,
    cache_dir: Path | None,
) -> ProfileHydrationResult:
    """Instantiate the correct provider for the requested backend and hydrate profiles."""

    backend = (backend or LocalProfileProvider.backend_name).lower()
    if backend == LocalProfileProvider.backend_name:
        provider: ProfileProvider = LocalProfileProvider(profile_dir)
    elif backend == S3ProfileProvider.backend_name:
        provider = S3ProfileProvider(
            bucket=bucket or "",
            prefix=prefix,
            cache_dir=cache_dir,
            region=region,
        )
    else:
        raise ProfileHydrationError(f"Unsupported profile backend '{backend}'.")

    result = provider.hydrate()
    LOGGER.info(
        "Hydrated %s profiles using backend '%s' into %s",
        len(list(result.profiles_dir.glob("*.json"))),
        backend,
        result.profiles_dir,
    )
    if result.key_file:
        LOGGER.info("Hydrated API key file at %s", result.key_file)
    return result


def _parse_cli_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hydrate auth profiles for the coordinator."
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("PROFILE_BACKEND", LocalProfileProvider.backend_name),
        choices=("local", "s3"),
        help="Profile backend to use (defaults to PROFILE_BACKEND or 'local').",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("auth_profiles/active"),
        help="Local profile directory when using the 'local' backend.",
    )
    parser.add_argument("--bucket", default=os.environ.get("AUTH_PROFILE_S3_BUCKET"))
    parser.add_argument("--prefix", default=os.environ.get("AUTH_PROFILE_S3_PREFIX"))
    parser.add_argument("--region", default=os.environ.get("AUTH_PROFILE_S3_REGION"))
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("AUTH_PROFILE_CACHE_DIR", DEFAULT_CACHE_DIR)),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> ProfileHydrationResult:
    args = _parse_cli_args(argv)
    result = hydrate_profiles(
        backend=args.backend,
        profile_dir=args.profiles,
        bucket=args.bucket,
        prefix=args.prefix,
        region=args.region,
        cache_dir=args.cache_dir,
    )
    print(result.model_dump_json())
    return result


if __name__ == "__main__":
    main()
