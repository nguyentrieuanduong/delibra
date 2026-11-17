"""Delibra's log file (design 2026-11-16): private, size-bounded, installed per app lifespan."""

from __future__ import annotations

from collections.abc import Callable
import errno
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import sys
import time
from typing import TextIO

from app.storage import OwnershipError, ensure_owned_directory

LOG_DIRNAME = "logs"
LOG_FILENAME = "delibra.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 4
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
FILE_LOGGERS = ("app", "uvicorn.error")


class _UtcFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 - logging's name
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        return f"{stamp}.{int(record.msecs):03d}Z"


def _formatter() -> logging.Formatter:
    return _UtcFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


class PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating file under ``home/logs`` whose current and rotated files only the owner can read.

    A symlinked home is resolved first. Below it symlinks are refused, never followed: a symlinked
    ``logs`` directory or log file raises OwnershipError, and a symlinked rotated file is left alone.
    """

    def __init__(self, home: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        root = home.resolve(strict=True)
        logs = ensure_owned_directory(root / LOG_DIRNAME, root)
        os.chmod(logs, 0o700)
        path = logs / LOG_FILENAME
        for index in range(1, backups + 1):
            _make_private_if_regular(Path(f"{path}.{index}"))
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=False)
        self.setFormatter(_formatter())

    def _open(self) -> TextIO:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(self.baseFilename, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OwnershipError(f"symlinked log file rejected: {self.baseFilename}") from exc
            raise
        try:
            # O_CREAT's mode applies only to a new file; an existing one is tightened here.
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(descriptor)
            raise


def _make_private_if_regular(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    # Already private needs no open, so an owner-unreadable (0o000) backup never stops startup.
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 == 0:
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def install_logging(home: Path, level_name: str | None) -> Callable[[], None]:
    """Attach the file and terminal handlers; returns the function that removes them.

    ``home`` must already exist (RegistryStore creates it at 0700).
    """

    requested = (level_name or "INFO").upper()
    level = LEVELS.get(requested, logging.INFO)
    file_handler = PrivateRotatingFileHandler(home)
    file_handler.setLevel(level)
    terminal = logging.StreamHandler(sys.stderr)
    terminal.setLevel(logging.WARNING)
    terminal.setFormatter(_formatter())
    app_logger = logging.getLogger("app")
    previous_level = app_logger.level
    app_logger.setLevel(level)
    attached: list[tuple[logging.Logger, logging.Handler]] = [(app_logger, terminal)]
    for name in FILE_LOGGERS:
        attached.append((logging.getLogger(name), file_handler))
    for logger, handler in attached:
        logger.addHandler(handler)
    if requested not in LEVELS:
        logging.getLogger("app.log_setup").warning(
            "DELIBRA_LOG_LEVEL=%s is not a level; using INFO", level_name
        )

    def uninstall() -> None:
        for logger, handler in attached:
            logger.removeHandler(handler)
        app_logger.setLevel(previous_level)
        file_handler.close()

    return uninstall
