from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping

from .config import DEFAULT_LOG_DIR
from .types import AuthProfile, ChildPorts, ChildProcess

_LOG_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 5


def _configure_logger(
    profile: AuthProfile, log_dir: Path
) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"CoordinatorChild.{profile.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = log_dir / f"{profile.name}.log"
    if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        handler = RotatingFileHandler(
            log_path, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger, log_path


def _pump_stream(stream, logger: logging.Logger, prefix: str) -> None:
    for line in iter(stream.readline, ""):
        if not line:
            break
        logger.info("%s%s", prefix, line.rstrip())
    stream.close()


def launch_child(
    profile: AuthProfile,
    ports: ChildPorts,
    env: Mapping[str, str],
    *,
    headless: bool = True,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> ChildProcess:
    """Launch a Camoufox proxy child process for the given profile."""

    if not profile.path.exists():
        raise FileNotFoundError(f"Auth profile not found: {profile.path}")

    logger, log_path = _configure_logger(profile, log_dir)
    repo_root = Path(__file__).resolve().parent.parent
    launcher_script = repo_root / "launch_camoufox.py"
    if not launcher_script.exists():
        raise FileNotFoundError(
            f"Unable to locate launch_camoufox.py at {launcher_script}"
        )

    cmd = [
        sys.executable,
        str(launcher_script),
        "--server-port",
        str(ports.api_port),
        "--stream-port",
        str(ports.stream_port),
        "--camoufox-debug-port",
        str(ports.camoufox_port),
        "--active-auth-json",
        str(profile.path),
    ]

    if headless:
        cmd.append("--headless")

    env_vars = os.environ.copy()
    env_vars.update(env)

    process = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env_vars,
    )

    if process.stdout:
        stdout_thread = threading.Thread(
            target=_pump_stream, args=(process.stdout, logger, "[stdout] "), daemon=True
        )
        stdout_thread.start()

    if process.stderr:
        stderr_thread = threading.Thread(
            target=_pump_stream, args=(process.stderr, logger, "[stderr] "), daemon=True
        )
        stderr_thread.start()

    logger.info(
        "Launched child process PID=%s for profile '%s' on ports api=%s stream=%s camoufox=%s",
        process.pid,
        profile.name,
        ports.api_port,
        ports.stream_port,
        ports.camoufox_port,
    )

    return ChildProcess(
        profile=profile,
        ports=ports,
        process=process,
        log_path=log_path,
    )
