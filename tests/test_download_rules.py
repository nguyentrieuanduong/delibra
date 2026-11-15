import json
import pytest
from app.download_rules import (
    MASKED_URL, RequestError, detect_type, is_executable, is_public_address,
    parse_request, sanitize_filename, url_flags,
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
    ("../../etc/passwd", "https://a.org/", "_.._etc_passwd"),
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
