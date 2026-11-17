from __future__ import annotations

import logging
import os
import re
import stat

import pytest

from app.log_setup import PrivateRotatingFileHandler, install_logging


@pytest.fixture
def home(tmp_path):
    yield tmp_path / "home"


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_install_creates_a_private_file_and_writes_app_info(home) -> None:
    uninstall = install_logging(home, None)
    try:
        logging.getLogger("app.example").info("hello %s", "file")
        logging.getLogger("app.example").debug("too quiet")
    finally:
        uninstall()

    log = home / "logs" / "delibra.log"
    text = log.read_text(encoding="utf-8")
    assert mode(home / "logs") == 0o700 and mode(log) == 0o600
    assert "too quiet" not in text
    line = next(line for line in text.splitlines() if "hello file" in line)
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO app\.example: hello file", line
    )


def test_warnings_reach_the_terminal_with_a_timestamp(home, capsys) -> None:
    uninstall = install_logging(home, None)
    try:
        logging.getLogger("app.example").warning("careful")
        logging.getLogger("app.example").info("file only")
    finally:
        uninstall()

    err = capsys.readouterr().err
    assert " WARNING app.example: careful" in err
    assert "file only" not in err


def test_access_lines_never_reach_the_file_but_server_messages_do(home) -> None:
    uninstall = install_logging(home, None)
    try:
        logging.getLogger("uvicorn.access").warning('127.0.0.1 - "GET / HTTP/1.1" 200')
        logging.getLogger("uvicorn.error").warning("Uvicorn running")
    finally:
        uninstall()

    text = (home / "logs" / "delibra.log").read_text(encoding="utf-8")
    assert "GET / HTTP/1.1" not in text
    assert "Uvicorn running" in text


def test_uninstall_removes_every_handler_and_a_second_install_does_not_double(home) -> None:
    before = (list(logging.getLogger("app").handlers), list(logging.getLogger("uvicorn.error").handlers))
    first = install_logging(home, None)
    first()
    second = install_logging(home, None)
    try:
        logging.getLogger("app.example").info("once")
    finally:
        second()

    after = (list(logging.getLogger("app").handlers), list(logging.getLogger("uvicorn.error").handlers))
    assert after == before
    assert (home / "logs" / "delibra.log").read_text(encoding="utf-8").count("once") == 1


@pytest.mark.parametrize("value, level", [("DEBUG", logging.DEBUG), ("warning", logging.WARNING), (None, logging.INFO)])
def test_the_level_comes_from_the_setting(home, value, level) -> None:
    uninstall = install_logging(home, value)
    try:
        assert logging.getLogger("app").level == level
    finally:
        uninstall()


def test_the_level_also_filters_uvicorn_lines_in_the_file(home) -> None:
    uninstall = install_logging(home, "WARNING")
    try:
        logging.getLogger("uvicorn.error").info("Started server process")
        logging.getLogger("uvicorn.error").warning("Uvicorn warning kept")
        logging.getLogger("app.example").info("app info dropped")
    finally:
        uninstall()

    text = (home / "logs" / "delibra.log").read_text(encoding="utf-8")
    assert "Uvicorn warning kept" in text
    assert "Started server process" not in text
    assert "app info dropped" not in text


def test_an_unknown_level_falls_back_to_info_with_a_warning(home) -> None:
    uninstall = install_logging(home, "bogus")
    try:
        assert logging.getLogger("app").level == logging.INFO
    finally:
        uninstall()
    text = (home / "logs" / "delibra.log").read_text(encoding="utf-8")
    assert "DELIBRA_LOG_LEVEL=bogus is not a level; using INFO" in text


def test_rotation_keeps_five_private_files(home) -> None:
    handler = PrivateRotatingFileHandler(home / "logs" / "delibra.log", max_bytes=200, backups=4)
    logger = logging.getLogger("app.rotation-test")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        for index in range(200):
            logger.warning("line %03d %s", index, "x" * 40)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.propagate = True

    files = sorted(path.name for path in (home / "logs").iterdir())
    assert files == ["delibra.log", "delibra.log.1", "delibra.log.2", "delibra.log.3", "delibra.log.4"]
    assert all(mode(home / "logs" / name) == 0o600 for name in files)
