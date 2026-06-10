"""Atomic file writes and pidfile locking.

Prevents:
  - Corrupt JSON if a process crashes mid-write
  - Two main.py cycles running simultaneously
  - Dashboard reading partial writes
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def atomic_write_text(path: Path | str, content: str) -> None:
    """Write via temp file + rename. POSIX-safe, Windows-best-effort."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)  # atomic on POSIX, best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path | str, data, **kwargs) -> None:
    atomic_write_text(path, json.dumps(data, **kwargs))


@contextmanager
def single_instance(pidfile: str | Path, max_age_sec: int = 900):
    """File-based lock. If another process holds it, raise RuntimeError.

    Stale locks (pid no longer running, or file older than max_age_sec) are
    reclaimed automatically.
    """
    p = Path(pidfile)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        try:
            existing_pid = int(p.read_text().strip())
            age = time.time() - p.stat().st_mtime
            pid_alive = False
            try:
                os.kill(existing_pid, 0)
                pid_alive = True
            except (OSError, ProcessLookupError):
                pid_alive = False

            if pid_alive and age < max_age_sec:
                raise RuntimeError(
                    f"Another instance is running (pid={existing_pid}, lock={p}). "
                    f"If stuck, delete the lockfile."
                )
        except (ValueError, FileNotFoundError):
            # Malformed or disappeared between exists() and read — treat as no lock
            pass

    try:
        atomic_write_text(p, str(os.getpid()))
        yield
    finally:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
