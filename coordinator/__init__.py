"""
Coordinator package for managing multi-process Camoufox proxy instances.

This package exposes typed helpers for profile discovery, process launching,
and FastAPI routing that collectively implement the multi-profile coordinator.
"""

from __future__ import annotations

__all__ = [
    "api",
    "config",
    "health",
    "launcher",
    "manager",
    "routing",
    "types",
]
