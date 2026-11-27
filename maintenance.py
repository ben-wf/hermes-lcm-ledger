"""Backup and rotate maintenance operations for the LCM store.

These are the data-layer maintenance primitives behind ``/lcm backup`` and
``/lcm rotate``: they flush the engine's SQLite connections and snapshot the
store to a timestamped or rolling backup file. They are pure functions that
take the engine so the command layer (``command.py``) keeps only the text
formatting, and the store/dag/lifecycle connection handling lives in one place.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Any

from .sqlite_util import (
    _SQLITE_SIDECAR_SUFFIXES,
    _prepare_private_sqlite_file,
    private_file_lock as _rotation_lock,
    _restrict_existing_sqlite_artifacts,
)


_FCHMOD = getattr(os, "fchmod", None)


def _prepare_private_backup_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    expected = os.lstat(path)
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise OSError(f"backup directory is not a real directory: {path}")

    if os.name != "posix":  # pragma: no cover - Windows compatibility fallback
        path.chmod(0o700)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise OSError(f"backup directory changed during validation: {path}")
        if callable(_FCHMOD):
            _FCHMOD(fd, 0o700)
        elif os.chmod in getattr(os, "supports_fd", ()):
            os.chmod(fd, 0o700)
        else:
            raise OSError("descriptor-based directory chmod is unavailable")
    finally:
        os.close(fd)


def flush_engine_connections(engine) -> None:
    """Flush buffered work through its owners before opening the snapshot.

    Shared by ``backup_database`` (timestamped backup) and
    ``rotate_backup_database`` (rolling backup) so the connection-flush
    contract stays in one place.
    """
    engine._store.commit()
    # DAG and lifecycle methods already commit their own work. A commit on
    # either shared handle here could publish another thread's partial work.
    assertion_store = getattr(engine, "_assertions", None)
    if assertion_store is not None:
        # AssertionStore owns a multi-statement publication transaction. Its
        # lock-taking API must serialize this flush with publish_source() so a
        # backup cannot commit a half-written receipt behind the publisher.
        assertion_store.commit()
    query_views = getattr(engine, "_query_views", None)
    if query_views is not None:
        query_views.commit()


def backup_database(engine) -> dict[str, Any]:
    db_path = Path(engine._store.db_path)
    if not db_path.exists():
        return {
            "ok": False,
            "db_path": db_path,
            "error": "database file does not exist",
        }

    backup_dir = engine.backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        _prepare_private_backup_directory(backup_dir)
        flush_engine_connections(engine)
        # Concurrent timestamped backups in the same second get separate files.
        fd, name = tempfile.mkstemp(
            prefix=f"{db_path.stem}-{timestamp}-", suffix=".sqlite3.tmp", dir=backup_dir
        )
        os.close(fd)
        staging = Path(name)
        backup_path = staging.with_suffix("")
        try:
            backup_size = _copy_snapshot(engine, staging)
            # A new name avoids replacing any earlier timestamped backup.
            os.link(staging, backup_path)
        finally:
            _discard_snapshot(staging)
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "db_path": db_path, "error": str(exc)}

    return {"ok": True, "db_path": db_path, "backup_path": backup_path, "backup_size": backup_size}


def _copy_snapshot(engine, path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as dest:
        engine._store.backup(dest)
        # Publish a self-contained database, including when the source uses WAL.
        dest.execute("PRAGMA journal_mode=DELETE").fetchone()
    _restrict_existing_sqlite_artifacts(path)
    size = path.stat().st_size
    if size == 0:
        raise sqlite3.DatabaseError("empty SQLite backup")
    return size


def _discard_snapshot(path: Path) -> None:
    for suffix in ("", *_SQLITE_SIDECAR_SUFFIXES):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


@contextmanager
def rotation_backup_slot(engine):
    """Serialize the preflight, rolling backup and frontier update across engines.

    Profiles can share a live database with different backup directories. Two
    databases with the same stem can also share a destination. Acquire source
    first and destination second throughout, without locking the live SQLite DB.
    """
    source = Path(engine._store.db_path).resolve()
    slot = engine.rotate_backup_path()
    with _rotation_lock(source.with_name(source.name + ".rotate.lock")):
        _prepare_private_backup_directory(slot.parent)
        with _rotation_lock(slot.with_name(slot.name + ".slot.lock")):
            yield lambda: _write_rotate_snapshot(engine, slot)


def _write_rotate_snapshot(engine, backup_path: Path) -> dict[str, Any]:
    """Replace the rolling slot while the caller holds rotation_backup_slot."""
    db_path = Path(engine._store.db_path)
    tmp_path = backup_path.with_name(backup_path.name + ".tmp")
    try:
        if not db_path.exists():
            raise FileNotFoundError("database file does not exist")
        _restrict_existing_sqlite_artifacts(backup_path)
        flush_engine_connections(engine)
        # With exclusive slot ownership, this can only be an interrupted copy.
        _discard_snapshot(tmp_path)
        _prepare_private_sqlite_file(tmp_path)
        try:
            size = _copy_snapshot(engine, tmp_path)
            tmp_path.replace(backup_path)
            if os.name == "posix":
                fd = os.open(backup_path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            _discard_snapshot(tmp_path)
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "db_path": db_path, "backup_path": backup_path, "error": str(exc)}
    return {"ok": True, "db_path": db_path, "backup_path": backup_path, "backup_size": size}


def rotate_backup_database(engine) -> dict[str, Any]:
    """Write a serialized, atomic rolling snapshot without advancing the frontier."""
    try:
        with rotation_backup_slot(engine) as write_snapshot:
            return write_snapshot()
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "db_path": Path(engine._store.db_path), "error": str(exc)}
