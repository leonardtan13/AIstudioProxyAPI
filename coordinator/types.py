from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class AuthProfile:
    """Represents a single authentication profile JSON file."""

    name: str
    path: Path


@dataclass(frozen=True)
class ChildPorts:
    """Port assignments for a child proxy process."""

    api_port: int
    stream_port: int
    camoufox_port: int


@dataclass
class ChildProcess:
    """Tracking metadata for a launched child process."""

    profile: AuthProfile
    ports: ChildPorts
    process: subprocess.Popen[str]
    ready: bool = False
    log_path: Optional[Path] = None


@dataclass(frozen=True)
class CancelResult:
    """Outcome of broadcasting a cancellation request to child processes."""

    success: bool
    responders: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
