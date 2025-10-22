from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

import uvicorn

from .api import create_app
from .config import (
    DEFAULT_CHILD_BASE_API_PORT,
    DEFAULT_CHILD_BASE_CAMOUFOX_PORT,
    DEFAULT_CHILD_BASE_STREAM_PORT,
    DEFAULT_COORDINATOR_HOST,
    DEFAULT_COORDINATOR_PORT,
    DEFAULT_LOG_DIR,
    DEFAULT_PORT_INCREMENT,
    CoordinatorCLIArgs,
)
from .health import wait_for_ready
from .launcher import launch_child
from .manager import ChildRegistry
from .types import AuthProfile, ChildPorts, ChildProcess

LOGGER = logging.getLogger("Coordinator")


def parse_args(argv: Sequence[str] | None = None) -> CoordinatorCLIArgs:
    parser = argparse.ArgumentParser(
        description="Launch the AI Studio proxy coordinator."
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("auth_profiles/active"),
        help="Directory containing auth profile JSON files.",
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
    )


def discover_profiles(directory: Path) -> list[AuthProfile]:
    if not directory.exists():
        raise FileNotFoundError(f"Profile directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Profile path is not a directory: {directory}")

    profiles: list[AuthProfile] = []
    for json_path in sorted(directory.glob("*.json")):
        profiles.append(AuthProfile(name=json_path.stem, path=json_path.resolve()))
    return profiles


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
    timeout: float = 30.0,
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


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    args = parse_args(argv)

    try:
        profiles = discover_profiles(args.profile_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        LOGGER.error(str(exc))
        return 1

    if not profiles:
        LOGGER.error("No auth profiles found in %s", args.profile_dir)
        return 1

    ports = assign_ports(
        len(profiles),
        args.base_api_port,
        args.base_stream_port,
        args.base_camoufox_port,
        args.port_increment,
    )

    children: list[ChildProcess] = []
    try:
        for profile, port_assignment in zip(profiles, ports):
            child = launch_child(
                profile,
                port_assignment,
                env={},
                headless=args.headless,
                log_dir=args.log_dir,
            )
            children.append(child)
    except Exception as exc:
        LOGGER.exception("Failed to launch child processes: %s", exc)
        graceful_shutdown(children)
        return 1

    LOGGER.info("Launched %s child process(es).", len(children))

    registry = ChildRegistry(children)

    try:
        asyncio.run(_initialize_children(children, registry))
    except KeyboardInterrupt:
        LOGGER.info("Startup interrupted. Beginning shutdown.")
        graceful_shutdown(children)
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
        try:
            asyncio.run(registry.shutdown())
        except RuntimeError:
            pass
        graceful_shutdown(children)

    LOGGER.info("Coordinator shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
