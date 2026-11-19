import asyncio, hashlib, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from app.download_fetch import FILE_MAX_BYTES, FetchError, Fetcher

# Every server a test starts; the autouse fixture below stops them, so a
# worker does not keep a listening thread per test for the rest of the run.
_SERVERS: list[ThreadingHTTPServer] = []


@pytest.fixture(autouse=True)
def _stop_servers():
    yield
    while _SERVERS:
        httpd = _SERVERS.pop()
        httpd.shutdown()
        httpd.server_close()


class Server:
    def __init__(self, routes):
        hits = self.hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                status, headers, body = routes[self.path]
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        # A short poll so the teardown's shutdown() returns at once, not after 0.5 s.
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        _SERVERS.append(self.httpd)
    def resolve(self, mapping):
        async def resolve(host, port):
            if host not in mapping: raise OSError("unknown")
            return [(mapping[host], self.port)]
        return resolve

class Budget:
    def __init__(self, left): self.left, self.released = left, 0
    async def reserve(self, n):
        granted = min(n, self.left); self.left -= granted; return granted
    async def release(self, n): self.left += n; self.released += n

async def fetch(fetcher, project, url, budget=None, check_first=True, name=None):
    return await fetcher.fetch(url, check_first=check_first, project_root=project,
        folder="raw", filename=name, request_id="D1", budget=budget or Budget(10**9))

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"; root.mkdir(); return root

@pytest.mark.asyncio
async def test_a_pdf_downloads_with_hash_and_type(project):
    body = b"%PDF-1.7 hello"
    server = Server({"/p.pdf": (200, {}, body)})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    result = await fetch(fetcher, project, "http://files.test/p.pdf")
    assert result.path == "raw/files.test/p.pdf"
    assert (project / result.path).read_bytes() == body
    assert result.sha256 == hashlib.sha256(body).hexdigest() and result.detected_type == "pdf"

@pytest.mark.asyncio
async def test_collisions_get_a_suffix(project):
    server = Server({"/p.pdf": (200, {}, b"%PDF-1")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    first = await fetch(fetcher, project, "http://files.test/p.pdf")
    second = await fetch(fetcher, project, "http://files.test/p.pdf")
    assert (first.path, second.path) == ("raw/files.test/p.pdf", "raw/files.test/p-2.pdf")

@pytest.mark.asyncio
async def test_same_name_downloads_and_existing_files_are_never_overwritten(project):
    # Plan review B2: the final name is taken with an atomic no-replace link.
    server = Server({"/p.pdf": (200, {}, b"%PDF-1")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    (project / "raw" / "files.test").mkdir(parents=True)
    (project / "raw" / "files.test" / "p.pdf").write_bytes(b"mine")
    results = await asyncio.gather(*(
        fetcher.fetch("http://files.test/p.pdf", check_first=True, project_root=project, folder="raw",
                      filename=None, request_id=f"D{n}", budget=Budget(10**9))
        for n in range(3)
    ))
    assert sorted(r.path for r in results) == [f"raw/files.test/p-{n}.pdf" for n in (2, 3, 4)]
    assert (project / "raw" / "files.test" / "p.pdf").read_bytes() == b"mine"
    assert not any(p.name.startswith(".part") for p in (project / "raw" / "files.test").iterdir())

@pytest.mark.asyncio
async def test_loopback_is_refused_by_default_and_never_contacted(project):
    server = Server({"/p.pdf": (200, {}, b"x")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}))
    with pytest.raises(FetchError, match="non-public") as refused:
        await fetch(fetcher, project, "http://files.test/p.pdf", check_first=False)
    assert server.hits == []
    assert "files.test" not in refused.value.reason

@pytest.mark.asyncio
async def test_a_redirect_to_a_private_host_is_refused_after_override(project):
    server = Server({"/r": (302, {"Location": "http://inside.test/x"}, b""), "/x": (200, {}, b"x")})
    mapping = {"files.test": "127.0.0.1", "inside.test": "10.0.0.5"}
    fetcher = Fetcher(resolve=server.resolve(mapping), address_allowed=lambda a: a == "127.0.0.1")
    with pytest.raises(FetchError, match="non-public"):
        await fetch(fetcher, project, "http://files.test/r", check_first=False)
    assert server.hits == ["/r"]

@pytest.mark.asyncio
async def test_a_redirect_carrying_a_secret_is_refused(project):
    server = Server({"/r": (302, {"Location": "/x?k=ghp_" + "A1b2" * 9}, b"")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    with pytest.raises(FetchError, match="secret"):
        await fetch(fetcher, project, "http://files.test/r")

@pytest.mark.asyncio
async def test_executables_and_oversized_files_are_refused(project, monkeypatch):
    server = Server({"/e": (200, {}, b"\x7fELF...."), "/big": (200, {}, b"a" * 2048)})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    with pytest.raises(FetchError, match="executable"):
        await fetch(fetcher, project, "http://files.test/e")
    monkeypatch.setattr("app.download_fetch.FILE_MAX_BYTES", 1024)
    with pytest.raises(FetchError, match="over"):
        await fetch(fetcher, project, "http://files.test/big")
    assert not any(p.name.startswith(".part") for p in (project / "raw" / "files.test").iterdir())

@pytest.mark.asyncio
async def test_the_budget_stops_a_download_and_returns_the_rest(project):
    server = Server({"/p": (200, {}, b"a" * 5000)})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    budget = Budget(1000)
    with pytest.raises(FetchError, match="budget"):
        await fetch(fetcher, project, "http://files.test/p", budget=budget)
    # Nothing was written, so the whole reservation goes back.
    assert (budget.left, budget.released) == (1000, 1000)

@pytest.mark.asyncio
async def test_a_symlinked_host_folder_is_never_written_through(project, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (project / "raw").mkdir()
    (project / "raw" / "files.test").symlink_to(outside, target_is_directory=True)
    server = Server({"/p.pdf": (200, {}, b"%PDF-1")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    with pytest.raises(FetchError, match="folder"):
        await fetch(fetcher, project, "http://files.test/p.pdf")
    assert list(outside.iterdir()) == []


class SlowDripServer:
    """Sends a byte every 0.1 s: each read is quick, the whole download never ends."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                try:
                    for _ in range(600):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        # A short poll so the teardown's shutdown() returns at once, not after 0.5 s.
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        _SERVERS.append(self.httpd)


@pytest.mark.asyncio
async def test_a_download_is_bounded_in_total_not_only_per_read(project, monkeypatch):
    server = SlowDripServer()

    async def resolve(host, port):
        return [("127.0.0.1", server.port)]

    monkeypatch.setattr("app.download_fetch.DOWNLOAD_TIMEOUT", 0.5)
    fetcher = Fetcher(resolve=resolve, address_allowed=lambda a: True)
    budget = Budget(10**9)
    with pytest.raises(FetchError, match="timed out"):
        await fetch(fetcher, project, "http://files.test/slow", budget=budget)
    assert 10**9 - budget.left < 100  # only the bytes received stay charged
    assert not any(p.name.startswith(".part") for p in (project / "raw" / "files.test").iterdir())


@pytest.mark.asyncio
async def test_a_redirect_to_an_installer_is_refused_before_it_is_requested(project):
    server = Server({"/r": (302, {"Location": "/setup.exe"}, b""), "/setup.exe": (200, {}, b"%PDF-1.7 not really")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    with pytest.raises(FetchError, match=r"executable file type refused \(\.exe\)"):
        await fetch(fetcher, project, "http://files.test/r")
    assert server.hits == ["/r"]

@pytest.mark.asyncio
async def test_an_accented_name_is_saved_as_ascii(project):
    server = Server({"/p": (200, {}, b"%PDF-1.7 hello")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    result = await fetch(fetcher, project, "http://files.test/p", name="Báo cáo.pdf")
    assert result.path == "raw/files.test/Bao_cao.pdf"


@pytest.mark.asyncio
async def test_a_redirect_whose_saved_name_is_an_installer_is_refused(project):
    server = Server({"/r": (302, {"Location": "/setup.%EF%BD%85%EF%BD%98%EF%BD%85"}, b""),
                     "/setup.%EF%BD%85%EF%BD%98%EF%BD%85": (200, {}, b"%PDF-1.7 not really")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    with pytest.raises(FetchError, match=r"executable file type refused \(\.exe\)"):
        await fetch(fetcher, project, "http://files.test/r")
    assert server.hits == ["/r"]


@pytest.mark.asyncio
async def test_downloads_are_quarantined_for_gatekeeper(project):
    server = Server({"/p": (200, {}, b"%PDF-1.7 hello")})
    fetcher = Fetcher(resolve=server.resolve({"files.test": "127.0.0.1"}), address_allowed=lambda a: True)
    result = await fetch(fetcher, project, "http://files.test/p", name="paper.pdf")
    import subprocess
    shown = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(project / result.path)],
                           capture_output=True, text=True)
    assert shown.returncode == 0 and shown.stdout.startswith("0081;") and ";Delibra;" in shown.stdout
