from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from coordinator.profiles import (
    LocalProfileProvider,
    ProfileHydrationError,
    S3ProfileProvider,
    hydrate_profiles,
)


def test_local_provider_returns_directory_and_key(tmp_path: Path) -> None:
    auth_root = tmp_path / "auth_profiles"
    active_dir = auth_root / "active"
    active_dir.mkdir(parents=True)
    key_file = auth_root / "key.txt"
    key_file.write_text("abc\n", encoding="utf-8")
    (active_dir / "profile-a.json").write_text("{}", encoding="utf-8")

    provider = LocalProfileProvider(active_dir)
    result = provider.hydrate()

    assert result.profiles_dir == active_dir.resolve()
    assert result.key_file == key_file.resolve()


def test_local_provider_missing_directory_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    provider = LocalProfileProvider(missing)

    with pytest.raises(ProfileHydrationError):
        provider.hydrate()


def test_hydrate_profiles_uses_local_backend(tmp_path: Path) -> None:
    active_dir = tmp_path / "auth_profiles" / "active"
    active_dir.mkdir(parents=True)
    (active_dir / "profile.json").write_text("{}", encoding="utf-8")

    result = hydrate_profiles(
        backend="local",
        profile_dir=active_dir,
        bucket=None,
        prefix=None,
        region=None,
        cache_dir=None,
    )

    assert result.profiles_dir == active_dir.resolve()


@mock_aws
def test_s3_provider_downloads_profiles_and_key(tmp_path: Path) -> None:
    bucket = "test-bucket"
    prefix = "prod/coordinator"
    region = "us-east-1"

    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/active/profile-a.json", Body=b"{}")
    s3.put_object(Bucket=bucket, Key=f"{prefix}/key.txt", Body=b"secret\n")

    provider = S3ProfileProvider(
        bucket=bucket,
        prefix=prefix,
        cache_dir=tmp_path,
        region=region,
    )

    result = provider.hydrate()

    downloaded = sorted(p.name for p in result.profiles_dir.glob("*.json"))
    assert downloaded == ["profile-a.json"]
    assert result.key_file is not None
    assert result.key_file.read_text(encoding="utf-8") == "secret\n"


@mock_aws
def test_s3_provider_without_profiles_raises(tmp_path: Path) -> None:
    bucket = "empty-bucket"
    region = "us-east-1"

    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(Bucket=bucket)

    provider = S3ProfileProvider(
        bucket=bucket,
        prefix=None,
        cache_dir=tmp_path,
        region=region,
    )

    with pytest.raises(ProfileHydrationError):
        provider.hydrate()
