from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_COORDINATOR_HOST = "0.0.0.0"
DEFAULT_COORDINATOR_PORT = 2048
DEFAULT_CHILD_BASE_API_PORT = 3100
DEFAULT_CHILD_BASE_STREAM_PORT = 3200
DEFAULT_CHILD_BASE_CAMOUFOX_PORT = 9222
DEFAULT_PORT_INCREMENT = 1
DEFAULT_LOG_DIR = Path("logs/coordinator")


@dataclass(frozen=True)
class CoordinatorCLIArgs:
    """Typed representation of CLI arguments used to boot the coordinator."""

    profile_dir: Path
    base_api_port: int
    base_stream_port: int
    base_camoufox_port: int
    coordinator_host: str = DEFAULT_COORDINATOR_HOST
    coordinator_port: int = DEFAULT_COORDINATOR_PORT
    log_dir: Path = DEFAULT_LOG_DIR
    port_increment: int = DEFAULT_PORT_INCREMENT
    headless: bool = True
