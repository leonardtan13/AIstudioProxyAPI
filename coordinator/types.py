from __future__ import annotations

import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


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


@dataclass
class ProfileSlot:
    """Represents a fixed port assignment that can host a child process."""

    ports: ChildPorts
    profile: AuthProfile | None = None
    process: ChildProcess | None = None

    @property
    def occupied(self) -> bool:
        return self.process is not None


@dataclass
class ProfileQueue:
    """Queue of idle auth profiles available for rotation."""

    _profiles: deque[AuthProfile] = field(default_factory=deque)

    @classmethod
    def from_iterable(cls, profiles: Iterable[AuthProfile]) -> "ProfileQueue":
        return cls(deque(profiles))

    def push(self, profile: AuthProfile) -> None:
        self._profiles.append(profile)

    def pop(self) -> AuthProfile | None:
        if not self._profiles:
            return None
        return self._profiles.popleft()

    def push_front(self, profile: AuthProfile) -> None:
        self._profiles.appendleft(profile)

    def extend(self, profiles: Iterable[AuthProfile]) -> None:
        self._profiles.extend(profiles)

    def snapshot(self) -> list[AuthProfile]:
        return list(self._profiles)

    def clear(self) -> None:
        self._profiles.clear()

    def __len__(self) -> int:
        return len(self._profiles)

    def __bool__(self) -> bool:
        return bool(self._profiles)
