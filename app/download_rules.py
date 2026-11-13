"""Pure rules for Delibra downloads: request files, URL checks, names, types."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qsl, unquote, urlsplit

# Per build turn, in the turn's temp dir: the agent writes request files in
# downloads/requests/; Delibra writes downloads/status.json.
DOWNLOADS_DIR = "downloads"
DOWNLOAD_SPOOL = "requests"
STATUS_NAME = "status.json"
REQUEST_FILE_MAX_BYTES = 16 * 1024
URL_MAX_CHARS = 4_096
PURPOSE_MAX_CHARS = 500
FILENAME_MAX_CHARS = 128
QUERY_MAX_CHARS = 1_024
TURN_MAX_REQUESTS = 64
_REQUEST_KEYS = frozenset({"url", "purpose", "filename"})
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SECRETS = tuple(re.compile(pattern) for pattern in (
    r"sk-[A-Za-z0-9_-]{20,}",
    r"gh[opsu]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"AIza[0-9A-Za-z_-]{35}",
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.",
    r"-----BEGIN",
))
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_HEX = re.compile(r"[0-9a-fA-F]{65,}")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_EXECUTABLE = (
    b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"MZ", b"#!",
)
_MAGIC = (
    (b"%PDF-", "pdf"), (b"PK\x03\x04", "zip"), (b"\x1f\x8b", "gzip"),
    (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"),
)


class RequestError(ValueError):
    """A spool file Delibra will not turn into a request."""


@dataclass(frozen=True)
class ProposedDownload:
    url: str
    purpose: str
    filename: str | None


def parse_request(data: bytes) -> ProposedDownload:
    if len(data) > REQUEST_FILE_MAX_BYTES:
        raise RequestError("request file is over 16 KiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError("request file is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or "url" not in value or set(value) - _REQUEST_KEYS:
        raise RequestError('request must be {"url", "purpose", "filename"}')
    url, purpose, filename = value["url"], value.get("purpose", ""), value.get("filename")
    if type(url) is not str or type(purpose) is not str or (
        filename is not None and type(filename) is not str
    ):
        raise RequestError("request fields must be text")
    url, purpose = url.strip(), purpose.strip()
    filename = filename.strip() if filename else None
    if not url or len(url) > URL_MAX_CHARS:
        raise RequestError(f"url must be 1-{URL_MAX_CHARS} characters")
    if len(purpose) > PURPOSE_MAX_CHARS:
        raise RequestError(f"purpose must be at most {PURPOSE_MAX_CHARS} characters")
    if filename is not None and len(filename) > FILENAME_MAX_CHARS:
        raise RequestError(f"filename must be at most {FILENAME_MAX_CHARS} characters")
    return ProposedDownload(url, purpose, filename or None)


def _dns_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split(".")
    return (
        len(host) <= 253 and len(labels) >= 2 and not labels[-1].isdigit()
        and host != "localhost" and not host.endswith(".localhost")
        and all(_LABEL.fullmatch(label) for label in labels)
    )


def _base64_like(run: str) -> bool:
    return (
        any(c.isupper() for c in run) and any(c.islower() for c in run)
        and any(c.isdigit() for c in run)
    )


def _blob(path: str, query: str, host: str) -> bool:
    pieces = [segment for segment in unquote(path).split("/")]
    pieces += [value for _key, value in parse_qsl(query, keep_blank_values=True)]
    for piece in pieces:
        if any(_base64_like(match.group()) for match in _RUN.finditer(piece)):
            return True
        if _HEX.search(piece):
            return True
    return any(len(label) >= 40 for label in host.split("."))


def url_flags(url: str) -> list[str]:
    """Names of the pattern rules this URL breaks; empty when it passes."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ["scheme"]
    host = (parts.hostname or "").lower()
    flags: list[str] = []
    if parts.scheme not in ("http", "https"):
        flags.append("scheme")
    if port not in (None, 80, 443):
        flags.append("port")
    if "@" in parts.netloc:
        flags.append("userinfo")
    if not _dns_host(host):
        flags.append("host")
    if len(parts.query) + len(parts.fragment) > QUERY_MAX_CHARS:
        flags.append("query-length")
    decoded = unquote(url)
    if any(pattern.search(url) or pattern.search(decoded) for pattern in _SECRETS):
        flags.append("secret")
    if _EMAIL.search(unquote(f"{parts.path}?{parts.query}#{parts.fragment}")):
        flags.append("email")
    if _blob(parts.path, parts.query, host):
        flags.append("encoded-blob")
    return flags


def url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


# Shown instead of any URL a check refused (plan review B3): neither the
# host (a long label can be the payload) nor any other part.
MASKED_URL = "(URL hidden)"


def sanitize_filename(hint: str | None, url: str) -> str:
    try:
        last = PurePosixPath(unquote(urlsplit(url).path)).name
    except ValueError:
        last = ""
    name = _UNSAFE_NAME.sub("_", hint or last).lstrip(".")[:FILENAME_MAX_CHARS]
    return name or "download"


def is_executable(head: bytes) -> bool:
    return head.startswith(_EXECUTABLE)


def detect_type(head: bytes) -> str:
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    start = head.lstrip()[:20].lower()
    if start.startswith((b"<!doctype html", b"<html")):
        return "html"
    sample = head[:4096]
    try:
        sample.decode("utf-8")
        return "text"
    except UnicodeDecodeError as exc:
        # A multi-byte character cut by the sample end is still text.
        if exc.start >= len(sample) - 3 and b"\x00" not in sample:
            return "text"
    return "binary"


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast
