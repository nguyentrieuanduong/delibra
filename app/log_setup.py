"""Delibra's log file (design 2026-11-16): private, size-bounded, installed per app lifespan."""

from __future__ import annotations

from collections.abc import Callable
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time

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
    """A rotating file whose current and rotated files are readable only by the owner."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=False)
        self.setFormatter(_formatter())

    def _open(self):  # type: ignore[override]
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def install_logging(home: Path, level_name: str | None) -> Callable[[], None]:
    """Attach the file and terminal handlers; returns the function that removes them."""

    level = LEVELS.get((level_name or "INFO").upper())
    file_handler = PrivateRotatingFileHandler(home / LOG_DIRNAME / LOG_FILENAME)
    file_handler.setLevel(level or logging.INFO)
    terminal = logging.StreamHandler(sys.stderr)
    terminal.setLevel(logging.WARNING)
    terminal.setFormatter(_formatter())
    app_logger = logging.getLogger("app")
    previous_level = app_logger.level
    app_logger.setLevel(level or logging.INFO)
    attached: list[tuple[logging.Logger, logging.Handler]] = [(app_logger, terminal)]
    for name in FILE_LOGGERS:
        attached.append((logging.getLogger(name), file_handler))
    for logger, handler in attached:
        logger.addHandler(handler)
    if level is None:
        logging.getLogger("app.log_setup").warning(
            "DELIBRA_LOG_LEVEL=%s is not a level; using INFO", level_name
        )

    def uninstall() -> None:
        for logger, handler in attached:
            logger.removeHandler(handler)
        app_logger.setLevel(previous_level)
        file_handler.close()

    return uninstall
