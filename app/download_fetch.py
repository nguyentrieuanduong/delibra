"""Fetch one requested URL outside the sandbox, from public addresses only."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import socket
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.download_rules import detect_type, is_executable, is_public_address, sanitize_filename, url_flags
from app.storage import StorageError, ensure_project_subdirectory, open_project_directory, writable_root_parts

FILE_MAX_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 600.0
CHUNK = 64 * 1024
HEAD_BYTES = 4096
USER_AGENT = "Delibra-Downloader/1"
NAME_ATTEMPTS = 1000
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

Resolver = Callable[[str, int], Awaitable[list[tuple[str, int]]]]


class FetchError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget(Protocol):
    async def reserve(self, amount: int) -> int: ...
    async def release(self, amount: int) -> None: ...


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    path: str
    sha256: str
    size: int
    detected_type: str


async def system_resolve(host: str, port: int) -> list[tuple[str, int]]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [(info[4][0], int(info[4][1])) for info in infos]


class _PartFile:
    """A `.part-<id>` file in a symlink-free project folder, renamed on commit."""

    def __init__(self, project_root: Path, folder: str, name: str, request_id: str) -> None:
        self.project_root, self.folder, self.name = project_root, folder, name
        self.part = f".part-{request_id}"
        self.directory: int | None = None
        self.descriptor: int | None = None
        self.committed: str | None = None

    def __enter__(self) -> "_PartFile":
        try:
            ensure_project_subdirectory(self.project_root, self.folder)
            self._context = open_project_directory(self.project_root, writable_root_parts(self.folder))
            self.directory = self._context.__enter__()
        except StorageError as exc:
            raise FetchError(f"download folder is unavailable: {exc}") from exc
        try:
            os.unlink(self.part, dir_fd=self.directory)
        except FileNotFoundError:
            pass
        self.descriptor = os.open(self.part, _CREATE, 0o644, dir_fd=self.directory)
        return self

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.descriptor, view):]

    def commit(self) -> str:
        os.close(self.descriptor)
        self.descriptor = None
        stem, suffix = PurePosixPath(self.name).stem, PurePosixPath(self.name).suffix
        # link() never replaces: EEXIST means another download or an agent
        # owns that name, so try the next suffix (plan review B2).
        for number in range(1, NAME_ATTEMPTS + 1):
            candidate = self.name if number == 1 else f"{stem}-{number}{suffix}"
            try:
                os.link(self.part, candidate, src_dir_fd=self.directory,
                        dst_dir_fd=self.directory, follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError as exc:
                raise FetchError(f"could not save the file: {exc.strerror or type(exc).__name__}") from exc
            break
        else:
            raise FetchError("could not save the file: no free name")
        self.committed = candidate
        try:
            os.unlink(self.part, dir_fd=self.directory)
        except OSError:
            pass  # the saved file is complete; a stray part file is harmless
        return candidate

    def __exit__(self, *exc: object) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
        if self.committed is None and self.directory is not None:
            try:
                os.unlink(self.part, dir_fd=self.directory)
            except FileNotFoundError:
                pass
        if self.directory is not None:
            self._context.__exit__(None, None, None)


class Fetcher:
    def __init__(self, resolve: Resolver = system_resolve,
                 address_allowed: Callable[[str], bool] = is_public_address) -> None:
        self._resolve = resolve
        self._allowed = address_allowed

    async def _vetted(self, host: str, port: int) -> tuple[str, int]:
        try:
            pairs = await self._resolve(host, port)
        except OSError as exc:
            raise FetchError(f"could not resolve {host}") from exc
        if not pairs:
            raise FetchError(f"could not resolve {host}")
        if not all(self._allowed(address) for address, _ in pairs):
            raise FetchError(f"{host} resolves to a non-public address")
        return pairs[0]

    async def fetch(self, url: str, *, check_first: bool, project_root: Path, folder: str,
                    filename: str | None, request_id: str, budget: Budget) -> FetchResult:
        # httpx's timeout bounds each read; this bounds the whole download, so
        # a slow-drip server cannot hold a download slot for ever.
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                return await self._fetch(url, check_first, project_root, folder, filename, request_id, budget)
        except TimeoutError as exc:
            raise FetchError("download timed out") from exc

    async def _fetch(self, url: str, check_first: bool, project_root: Path, folder: str,
                     filename: str | None, request_id: str, budget: Budget) -> FetchResult:
        current = url
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout,
                                     headers={"User-Agent": USER_AGENT}) as client:
            for hop in range(MAX_REDIRECTS + 1):
                if hop or check_first:
                    flags = url_flags(current)
                    if flags:
                        raise FetchError(("redirect refused: " if hop else "refused: ") + ", ".join(flags))
                parts = urlsplit(current)
                host = (parts.hostname or "").lower()
                https = parts.scheme == "https"
                address, port = await self._vetted(host, parts.port or (443 if https else 80))
                shown = f"[{address}]" if ":" in address else address
                target = f"{parts.scheme}://{shown}:{port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
                request = client.build_request(
                    "GET", target, headers={"Host": host},
                    extensions={"sni_hostname": host} if https else {},
                )
                client.cookies.clear()  # no cookie follows a redirect to another host
                try:
                    response = await client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    raise FetchError(f"connection failed: {type(exc).__name__}") from exc
                try:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("redirect without a Location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"HTTP {response.status_code}")
                    name = sanitize_filename(filename, current)
                    return await self._save(response, current, project_root, f"{folder}/{host}", name, request_id, budget)
                finally:
                    await response.aclose()
            raise FetchError("too many redirects")

    async def _save(self, response: httpx.Response, final_url: str, project_root: Path,
                    folder: str, name: str, request_id: str, budget: Budget) -> FetchResult:
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > FILE_MAX_BYTES:
            raise FetchError("file is over the 100 MB limit")
        reserved = await budget.reserve(min(int(length), FILE_MAX_BYTES) if length.isdigit() else FILE_MAX_BYTES)
        written, digest, head = 0, sha256(), b""
        try:
            with _PartFile(project_root, folder, name, request_id) as part:
                try:
                    async for chunk in response.aiter_bytes(CHUNK):
                        if written + len(chunk) > FILE_MAX_BYTES:
                            raise FetchError("file is over the 100 MB limit")
                        if written + len(chunk) > reserved:
                            reserved += await budget.reserve(written + len(chunk) - reserved)
                            if written + len(chunk) > reserved:
                                raise FetchError("run download budget exhausted")
                        if len(head) < HEAD_BYTES:
                            head += chunk[: HEAD_BYTES - len(head)]
                            if len(head) >= 4 and is_executable(head):
                                raise FetchError("executable content refused")
                        part.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                except httpx.HTTPError as exc:
                    raise FetchError(f"download failed: {type(exc).__name__}") from exc
                if is_executable(head):
                    raise FetchError("executable content refused")
                saved = part.commit()
        finally:
            await budget.release(reserved - written)
        return FetchResult(final_url, f"{folder}/{saved}", digest.hexdigest(), written, detect_type(head))
