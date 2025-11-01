from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

from dataclasses import replace

import uvicorn

from .api import create_app
from .config import (
    DEFAULT_AUTH_PROFILE_CACHE_DIR,
    DEFAULT_CHILD_BASE_API_PORT,
    DEFAULT_CHILD_BASE_CAMOUFOX_PORT,
    DEFAULT_CHILD_BASE_STREAM_PORT,
    DEFAULT_COORDINATOR_HOST,
    DEFAULT_COORDINATOR_PORT,
    DEFAULT_LOG_DIR,
    DEFAULT_PORT_INCREMENT,
    DEFAULT_PROFILE_BACKEND,
    CoordinatorCLIArgs,
)
from .health import wait_for_ready
from .launcher import launch_child
from .manager import ChildRegistry, SlotManager
from .profiles import (
    ProfileHydrationError,
    discover_profiles,
    hydrate_profiles,
)
from .types import ChildPorts, ChildProcess, ProfileQueue, ProfileSlot

PROFILE_POOL_SIZE = 2

LOGGER = logging.getLogger("Coordinator")


def parse_args(argv: Sequence[str] | None = None) -> CoordinatorCLIArgs:
    parser = argparse.ArgumentParser(
        description="Launch the AI Studio proxy coordinator."
    )
    env_profile_backend = os.environ.get("PROFILE_BACKEND", DEFAULT_PROFILE_BACKEND)
    env_s3_bucket = os.environ.get("AUTH_PROFILE_S3_BUCKET")
    env_s3_prefix = os.environ.get("AUTH_PROFILE_S3_PREFIX")
    env_s3_region = os.environ.get("AUTH_PROFILE_S3_REGION")
    env_cache_dir = os.environ.get("AUTH_PROFILE_CACHE_DIR")
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("auth_profiles/active"),
        help="Directory containing auth profile JSON files.",
    )
    parser.add_argument(
        "--profile-backend",
        choices=("local", "s3"),
        default=env_profile_backend,
        help=(
            "Profile source backend to hydrate before launch. "
            "Defaults to PROFILE_BACKEND environment variable or 'local'."
        ),
    )
    parser.add_argument(
        "--auth-profile-s3-bucket",
        default=env_s3_bucket,
        help="S3 bucket containing auth profiles when using the 's3' backend.",
    )
    parser.add_argument(
        "--auth-profile-s3-prefix",
        default=env_s3_prefix,
        help="S3 prefix containing auth profiles (e.g. 'prod/coordinator').",
    )
    parser.add_argument(
        "--auth-profile-s3-region",
        default=env_s3_region,
        help="AWS region for the auth profile bucket. Falls back to default boto3 config.",
    )
    parser.add_argument(
        "--auth-profile-cache-dir",
        default=env_cache_dir,
        help=(
            "Local directory used to cache hydrated auth profiles. "
            "Defaults to AUTH_PROFILE_CACHE_DIR or '/tmp/auth_profiles'."
        ),
    )
    parser.add_argument(
        "--base-api-port",
        type=int,
        default=DEFAULT_CHILD_BASE_API_PORT,
        help="Starting FastAPI port for child processes.",
    )
    parser.add_argument(
        "--base-stream-port",
        type=int,
        default=DEFAULT_CHILD_BASE_STREAM_PORT,
        help="Starting stream proxy port for child processes.",
    )
    parser.add_argument(
        "--base-camoufox-port",
        type=int,
        default=DEFAULT_CHILD_BASE_CAMOUFOX_PORT,
        help="Starting Camoufox debug port for child processes.",
    )
    parser.add_argument(
        "--coordinator-host",
        default=DEFAULT_COORDINATOR_HOST,
        help="Host interface for the coordinator HTTP server.",
    )
    parser.add_argument(
        "--coordinator-port",
        type=int,
        default=DEFAULT_COORDINATOR_PORT,
        help="Port for the coordinator HTTP server.",
    )
    parser.add_argument(
        "--port-step",
        type=int,
        default=DEFAULT_PORT_INCREMENT,
        help="Increment applied between successive child port assignments.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for coordinator-managed child log files.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Disable headless mode when launching child processes.",
    )

    args = parser.parse_args(argv)
    default_cache_dir = DEFAULT_AUTH_PROFILE_CACHE_DIR.expanduser().resolve()
    resolved_cache_dir = (
        Path(args.auth_profile_cache_dir).expanduser().resolve()
        if args.auth_profile_cache_dir
        else default_cache_dir
    )
    return CoordinatorCLIArgs(
        profile_dir=args.profiles.resolve(),
        base_api_port=args.base_api_port,
        base_stream_port=args.base_stream_port,
        base_camoufox_port=args.base_camoufox_port,
        coordinator_host=args.coordinator_host,
        coordinator_port=args.coordinator_port,
        log_dir=args.log_dir.expanduser().resolve(),
        port_increment=args.port_step,
        headless=not args.no_headless,
        profile_backend=args.profile_backend,
        auth_profile_s3_bucket=args.auth_profile_s3_bucket,
        auth_profile_s3_prefix=args.auth_profile_s3_prefix,
        auth_profile_s3_region=args.auth_profile_s3_region,
        auth_profile_cache_dir=resolved_cache_dir,
    )


def assign_ports(
    count: int,
    base_api: int,
    base_stream: int,
    base_camoufox: int,
    step: int = DEFAULT_PORT_INCREMENT,
) -> list[ChildPorts]:
    if count < 0:
        raise ValueError("Child count must be non-negative.")
    if step <= 0:
        raise ValueError("Port step must be a positive integer.")

    assignments: list[ChildPorts] = []
    for index in range(count):
        offset = index * step
        assignments.append(
            ChildPorts(
                api_port=base_api + offset,
                stream_port=base_stream + offset,
                camoufox_port=base_camoufox + offset,
            )
        )
    return assignments


def graceful_shutdown(children: Iterable[ChildProcess], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    for child in children:
        process = child.process
        if process.poll() is None:
            LOGGER.info(
                "Terminating child '%s' (pid=%s)...", child.profile.name, process.pid
            )
            process.terminate()

    for child in children:
        process = child.process
        if process.poll() is not None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
            LOGGER.info(
                "Child '%s' stopped with code %s.",
                child.profile.name,
                process.returncode,
            )
        except subprocess.TimeoutExpired:
            LOGGER.warning(
                "Child '%s' did not exit within timeout. Sending kill.",
                child.profile.name,
            )
            process.kill()
            process.wait()


async def _initialize_children(
    children: Iterable[ChildProcess],
    registry: ChildRegistry,
    *,
    timeout: float = 60.0,
) -> None:
    for child in children:
        try:
            ready = await wait_for_ready(child, timeout=timeout)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning(
                "Initial health check for '%s' failed: %s",
                child.profile.name,
                exc,
            )
            ready = False

        if ready:
            registry.mark_ready(child)
        else:
            registry.mark_unhealthy(child, "Startup health check failed.")


def _monitor_child_processes(
    registry: ChildRegistry,
    slot_manager: SlotManager,
    stop_event: threading.Event,
) -> None:
    notified: set[tuple[str, int | None]] = set()
    while not stop_event.is_set():
        for slot in slot_manager.slots():
            child = slot.process
            if child is None:
                continue
            process = child.process
            pid = getattr(process, "pid", None)
            key = (child.profile.name, pid)
            if process.poll() is None:
                notified.discard(key)
                continue
            if key in notified:
                continue
            exit_code = process.returncode
            log_hint = f" (log file: {child.log_path})" if child.log_path else ""
            LOGGER.error(
                "Child '%s' exited with code %s.%s",
                child.profile.name,
                exit_code,
                log_hint,
            )
            notified.add(key)
            registry.evict_child(
                child,
                f"Process exit (code {exit_code})",
            )
        stop_event.wait(1.0)

    # Final sweep to catch anything that exited just before stop_event was set.
    for slot in slot_manager.slots():
        child = slot.process
        if child is None:
            continue
        process = child.process
        pid = getattr(process, "pid", None)
        key = (child.profile.name, pid)
        if process.poll() is not None and key not in notified:
            exit_code = process.returncode
            log_hint = f" (log file: {child.log_path})" if child.log_path else ""
            LOGGER.error(
                "Child '%s' exited with code %s.%s",
                child.profile.name,
                exit_code,
                log_hint,
            )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    args = parse_args(argv)

    try:
        hydration = hydrate_profiles(
            backend=args.profile_backend,
            profile_dir=args.profile_dir,
            bucket=args.auth_profile_s3_bucket,
            prefix=args.auth_profile_s3_prefix,
            region=args.auth_profile_s3_region,
            cache_dir=args.auth_profile_cache_dir,
        )
    except ProfileHydrationError as exc:
        LOGGER.error("Failed to hydrate auth profiles: %s", exc)
        return 1

    args = replace(args, profile_dir=hydration.profiles_dir)
    if hydration.key_file:
        os.environ["AUTH_KEY_FILE_PATH"] = str(hydration.key_file)
    else:
        os.environ.pop("AUTH_KEY_FILE_PATH", None)

    try:
        profiles = discover_profiles(
            args.profile_dir, pool_size=PROFILE_POOL_SIZE
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        LOGGER.error(str(exc))
        return 1

    if not profiles:
        LOGGER.error("No auth profiles found in %s", args.profile_dir)
        return 1

    active_count = min(PROFILE_POOL_SIZE, len(profiles))
    ports = assign_ports(
        active_count,
        args.base_api_port,
        args.base_stream_port,
        args.base_camoufox_port,
        args.port_increment,
    )

    slots = [ProfileSlot(ports=assignment) for assignment in ports]
    queue = ProfileQueue.from_iterable(profiles[active_count:])
    if queue:
        LOGGER.debug(
            "Profiles staged for rotation: %s",
            [profile.name for profile in queue.snapshot()],
        )

    slot_manager = SlotManager(
        slots,
        profile_queue=queue,
        headless=args.headless,
        log_dir=args.log_dir,
        env={},
    )
    try:
        children = slot_manager.bootstrap(profiles[:active_count])
    except Exception as exc:
        LOGGER.exception("Failed to launch child processes: %s", exc)
        slot_manager.shutdown("bootstrap failure")
        return 1

    LOGGER.info("Launched %s child process(es).", len(children))

    registry = ChildRegistry(children, slot_manager=slot_manager)

    monitor_stop = threading.Event()
    monitor_thread = threading.Thread(
        target=_monitor_child_processes,
        args=(registry, slot_manager, monitor_stop),
        name="ChildProcessMonitor",
        daemon=True,
    )
    monitor_thread.start()

    try:
        asyncio.run(_initialize_children(children, registry))
    except KeyboardInterrupt:
        LOGGER.info("Startup interrupted. Beginning shutdown.")
        graceful_shutdown(slot_manager.live_children())
        slot_manager.clear_queue()
        return 0

    ready_names = [child.profile.name for child in registry.ready_children()]
    LOGGER.info("Ready children: %s", ready_names or "(none)")

    app = create_app(registry)
    config = uvicorn.Config(
        app,
        host=args.coordinator_host,
        port=args.coordinator_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        server.run()
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested by user.")
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=5)
        try:
            asyncio.run(registry.shutdown())
        except RuntimeError:
            pass
        graceful_shutdown(slot_manager.live_children())

    LOGGER.info("Coordinator shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
