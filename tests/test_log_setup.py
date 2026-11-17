from __future__ import annotations

import logging
import os
import re
import stat

import pytest

from app.log_setup import PrivateRotatingFileHandler, install_logging
from app.storage import OwnershipError


@pytest.fixture
def home(tmp_path):
    # Production installs after RegistryStore has created the home at 0700.
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    yield path


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
    handler = PrivateRotatingFileHandler(home, max_bytes=200, backups=4)
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


def test_install_makes_an_existing_directory_and_files_private(home) -> None:
    logs = home / "logs"
    logs.mkdir()
    os.chmod(logs, 0o755)
    for name in ("delibra.log", "delibra.log.1"):
        (logs / name).write_text("old\n", encoding="utf-8")
        os.chmod(logs / name, 0o644)

    install_logging(home, None)()

    assert mode(logs) == 0o700
    assert mode(logs / "delibra.log") == 0o600
    assert mode(logs / "delibra.log.1") == 0o600


def test_a_symlinked_logs_directory_is_refused(home, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)
    (home / "logs").symlink_to(elsewhere)
    before = list(logging.getLogger("app").handlers)

    with pytest.raises(OwnershipError):
        install_logging(home, None)

    assert mode(elsewhere) == 0o755 and list(elsewhere.iterdir()) == []
    assert list(logging.getLogger("app").handlers) == before


def test_a_symlinked_log_file_is_refused(home, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    (home / "logs").mkdir(mode=0o700)
    (home / "logs" / "delibra.log").symlink_to(outside)

    with pytest.raises(OwnershipError):
        install_logging(home, None)

    assert outside.read_text(encoding="utf-8") == "keep\n" and mode(outside) == 0o644


def test_a_symlinked_rotated_file_is_left_alone(home, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    (home / "logs").mkdir(mode=0o700)
    (home / "logs" / "delibra.log.1").symlink_to(outside)

    install_logging(home, None)()

    assert outside.read_text(encoding="utf-8") == "keep\n" and mode(outside) == 0o644


def test_a_symlinked_home_is_followed_to_its_real_directory(tmp_path) -> None:
    real = tmp_path / "real-home"
    real.mkdir(mode=0o700)
    link = tmp_path / "home-link"
    link.symlink_to(real)

    uninstall = install_logging(link, None)
    try:
        logging.getLogger("app.example").info("through the link")
    finally:
        uninstall()

    log = real / "logs" / "delibra.log"
    assert "through the link" in log.read_text(encoding="utf-8")
    assert mode(real / "logs") == 0o700 and mode(log) == 0o600


def test_an_already_private_backup_is_left_as_it_is(home) -> None:
    logs = home / "logs"
    logs.mkdir(mode=0o700)
    (logs / "delibra.log.1").write_text("old\n", encoding="utf-8")
    os.chmod(logs / "delibra.log.1", 0o000)
    try:
        install_logging(home, None)()
        assert mode(logs / "delibra.log.1") == 0o000
    finally:
        os.chmod(logs / "delibra.log.1", 0o600)
