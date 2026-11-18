import hashlib
import json
from pathlib import Path
import pytest
from app.download_rules import (
    MASKED_URL, RequestError, converted_output, detect_type, downloads_prompt, executable_extension, is_executable,
    convert_prompt, is_public_address, parse_convert_request, parse_request, safe_host, safe_name,
    sanitize_filename, url_flags,
)

def request(**fields) -> bytes:
    return json.dumps(fields).encode()

def test_a_request_parses_and_trims() -> None:
    proposal = parse_request(request(url=" https://arxiv.org/pdf/1706.03762 ", purpose="paper"))
    assert (proposal.url, proposal.purpose, proposal.filename) == ("https://arxiv.org/pdf/1706.03762", "paper", None)

@pytest.mark.parametrize("data", [
    b"x" * (16 * 1024 + 1),
    b"\xff",
    b"[]",
    request(purpose="no url"),
    request(url="https://a.org", extra=1),
    request(url="https://a.org/" + "a" * 5000),
    request(url="https://a.org", purpose="p" * 501),
    request(url="https://a.org", filename="f" * 129),
    request(url=7),
])
def test_bad_requests_are_refused(data: bytes) -> None:
    with pytest.raises(RequestError):
        parse_request(data)

@pytest.mark.parametrize("url", [
    "https://arxiv.org/pdf/1706.03762",
    "https://www.nature.com/articles/s41586-021-03819-2.pdf",
    "https://github.com/o/r/commit/" + "a" * 40,
    "https://example.com/attention_is_all_you_need_transformer_architecture_paper.pdf",
    "http://legacy.example.edu/~smith/thesis.pdf",
])
def test_ordinary_urls_pass(url: str) -> None:
    assert url_flags(url) == []

@pytest.mark.parametrize(("url", "rule"), [
    ("ftp://a.org/x", "scheme"),
    ("https://a.org:8443/x", "port"),
    ("https://u:p@a.org/x", "userinfo"),
    ("https://10.0.0.1/x", "host"),
    ("https://localhost/x", "host"),
    ("https://a.org/x?" + "q=" + "a" * 1100, "query-length"),
    ("https://a.org/x?k=ghp_" + "A1b2" * 9, "secret"),
    ("https://a.org/x?k=AKIA" + "ABCDEFGHIJKLMNOP", "secret"),
    ("https://a.org/x?to=me%40corp.com", "email"),
    ("https://a.org/x?d=" + "QmFzZTY0IGVuY29kZWQgc2VjcmV0IGRhdGEgZm9yIHRlc3Q1" + "Ab1" * 4, "encoded-blob"),
    ("https://a.org/" + "f" * 65, "encoded-blob"),
    ("https://" + "m" * 40 + ".evil.org/x", "encoded-blob"),
])
def test_risky_urls_are_flagged(url: str, rule: str) -> None:
    assert rule in url_flags(url)

def test_the_mask_names_nothing_from_the_url() -> None:
    # Plan review B3: a host label can carry the encoded data itself.
    assert MASKED_URL == "(URL hidden)"

@pytest.mark.parametrize(("hint", "url", "name"), [
    (None, "https://a.org/papers/x%20y.pdf", "x_y.pdf"),
    ("../../etc/passwd", "https://a.org/", "etc_passwd"),
    ("x" * 100 + ".pdf", "https://a.org/", "x" * 52 + ".pdf"),
    (None, "https://a.org/", "download"),
    (".hidden", "https://a.org/", "hidden"),
])
def test_filenames_are_sanitised(hint, url, name) -> None:
    assert sanitize_filename(hint, url) == name

def test_types_are_sniffed_and_executables_refused() -> None:
    assert detect_type(b"%PDF-1.7") == "pdf"
    assert detect_type(b"  <!DOCTYPE html><html>") == "html"
    assert detect_type("tiếng Việt".encode()) == "text"
    assert detect_type(b"\x00\x01\x02\xff") == "binary"
    assert all(is_executable(head) for head in (b"\x7fELF..", b"MZ\x90\x00", b"#!/bin/sh", b"\xcf\xfa\xed\xfe"))
    assert not is_executable(b"%PDF-1.7")

@pytest.mark.parametrize(("address", "public"), [
    ("151.101.3.42", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.1", False),
    ("169.254.1.1", False), ("100.64.0.1", False), ("::1", False), ("::ffff:127.0.0.1", False),
    ("fe80::1%en0", False), ("224.0.0.1", False), ("2606:4700::1111", True),
])
def test_public_addresses(address: str, public: bool) -> None:
    assert is_public_address(address) is public

@pytest.mark.parametrize("field", ["url", "purpose", "filename"])
def test_text_that_cannot_be_utf8_is_refused(field: str) -> None:
    # Review 2026-11-15: a lone surrogate (JSON "\ud800") in a stored record
    # made every page rendering it fail to encode, taking Chat down.
    fields = {"url": "https://arxiv.org/pdf/1", "purpose": "p", "filename": "f.pdf"}
    data = json.dumps(fields).replace(f'"{fields[field]}"', f'"{fields[field]}\\ud800"', 1).encode()
    with pytest.raises(RequestError, match="text"):
        parse_request(data)

@pytest.mark.parametrize("address, public", [
    ("::127.0.0.1", False), ("::8.8.8.8", False),          # IPv4-compatible: deprecated
    ("::ffff:0:7f00:1", False), ("::ffff:0:808:808", False),  # IPv4-translated
    ("64:ff9b::a00:1", False), ("64:ff9b::7f00:1", False),    # NAT64 to private
    ("64:ff9b::808:808", True),                             # NAT64 (DNS64) to public
])
def test_ipv6_forms_that_embed_ipv4_are_judged_by_it(address: str, public: bool) -> None:
    assert is_public_address(address) is public


@pytest.mark.parametrize(("text", "name"), [
    ("Báo cáo tài chính.pdf", "Bao_cao_tai_chinh.pdf"),
    ("Đề án ưu tiên.docx", "De_an_uu_tien.docx"),
    ("名前.pdf", "download.pdf"),
    ("con.pdf", "con_.pdf"),
    ("con.tar.gz", "con_.tar.gz"),
    ("LPT1", "LPT1_"),
    ("report.", "report"),
    ("-rf.txt", "rf.txt"),
    ("a  b__c.pdf", "a_b_c.pdf"),
    ("x" * 100 + ".pdf", "x" * 60 + ".pdf"),
    ("", "download"),
])
def test_saved_names_are_portable_ascii_of_at_most_64_characters(text, name) -> None:
    assert safe_name(text) == name
    assert len(name) <= 64 and name.isascii()


def test_host_folders_are_ascii() -> None:
    assert safe_host("files.test") == "files.test"
    assert safe_host("2606:4700::1111") == "2606_4700_1111"
    unicode_host = safe_host("tài-liệu.vn")
    assert unicode_host.isascii() and unicode_host.startswith("xn--") and unicode_host.endswith(".vn")


def test_converted_outputs_are_safe_and_never_shared() -> None:
    assert converted_output("raw", "docs/a.pdf") == "raw/converted/docs/a.pdf.md"
    digest = hashlib.sha256("Tài liệu/Báo cáo.docx".encode()).hexdigest()[:8]
    assert converted_output("raw", "Tài liệu/Báo cáo.docx") == f"raw/converted/Tai_lieu/Bao_cao.docx-{digest}.md"
    assert converted_output("raw", "Tai lieu/Bao cao.docx") != converted_output("raw", "Tai_lieu/Bao_cao.docx")
    long_leaf = converted_output("raw", "y" * 70 + ".pdf").rsplit("/", 1)[1]
    assert len(long_leaf) == 64


def test_installers_and_scripts_are_refused_by_name_and_content() -> None:
    assert executable_extension("Setup.EXE") == "exe"
    assert executable_extension("tool.exe. ") == "exe"
    assert executable_extension("paper.pdf") is None
    assert executable_extension("script.sh") is None
    assert all(is_executable(head) for head in (b"xar!\x00\x1c", b"!<arch>\ndebian", b"\xed\xab\xee\xdb\x03"))
    with pytest.raises(RequestError, match=r"executable file type refused \(\.msi\)"):
        parse_request(json.dumps({"url": "https://a.org/x", "purpose": "p", "filename": "x.msi"}).encode())
    with pytest.raises(RequestError, match=r"executable file type refused \(\.dmg\)"):
        parse_request(json.dumps({"url": "https://a.org/App.dmg", "purpose": "p"}).encode())


def test_any_text_becomes_a_portable_name() -> None:
    import random
    import re

    rng = random.Random(20261118)
    alphabet = "abcXYZ019 .-_/\\:*?\"<>|\t\nàáạđĐườ名前ß́​\x00"
    allowed = re.compile(r"[A-Za-z0-9._-]+")
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        limit = rng.choice((52, 56, 64))  # converted leaf, download, any saved name
        name = safe_name(text, max_chars=limit)
        assert allowed.fullmatch(name), (text, name)
        assert len(name) <= limit and not name.startswith((".", "-", "_"))
        assert name.split(".")[0].casefold() not in {"con", "prn", "aux", "nul"}


def test_the_no_shell_download_note_has_no_wait_loop() -> None:
    status, spool = Path("/w/downloads/status.json"), Path("/w/downloads/requests")
    shell = downloads_prompt(status, spool, manual=False, shell=True)
    reread = downloads_prompt(status, spool, manual=False, shell=False)
    manual = downloads_prompt(status, spool, manual=True, shell=False)

    assert "until grep" in shell
    assert "until grep" not in reread
    assert (
        "To see a request's result, read the status file again later in your turn; a download "
        "that finishes after your turn ends is listed there in your next turn."
    ) in reread
    assert "Approval is manual: do not wait for it" in manual
    assert downloads_prompt(status, spool, manual=True, shell=True) == manual


def test_convert_requests_parse_one_path() -> None:
    assert parse_convert_request(b'{"path": "docs/a.pdf"}') == "docs/a.pdf"
    for bad in (b"[]", b'{"path": 3}', b'{"path": ""}', b'{"path": "a.pdf", "x": 1}', b"x" * 5000):
        with pytest.raises(RequestError):
            parse_convert_request(bad)


def test_any_bytes_either_parse_to_a_path_or_are_refused() -> None:
    import random

    rng = random.Random(7)
    pieces = [b'{"path": "', b'"}', b"docs/a.pdf", b"\\u0000", b"\xff", b"{", b"}", b'"', b"[", b"null", b" ", b"x" * 900]
    for _ in range(3000):
        data = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        try:
            path = parse_convert_request(data)
        except RequestError:
            continue
        assert isinstance(path, str) and path.strip() and len(path) <= 1024


def test_the_convert_note_names_spool_status_and_output() -> None:
    note = convert_prompt(Path("/w/convert/status.json"), Path("/w/convert/requests"),
                          "docs/source/raw/converted", shell=False)
    assert "/w/convert/requests/ named like c1.json" in note
    assert '{"path": "' in note
    assert "docs/source/raw/converted" in note
    assert "until grep" not in note
