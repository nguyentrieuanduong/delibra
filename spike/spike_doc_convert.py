"""Smoke test: Magika, Docling (+ Vision OCR), pypdf and xlrd inside Delibra's converter sandbox.

Usage: tmp/doc-spike/venv/bin/python -I spike/spike_doc_convert.py \
           tmp/doc-spike/inputs tmp/doc-spike/out tmp/doc-spike/models
Prints one summary line per file and step (stdout is this spike's interface).
Never prints document text: only labels, sizes, timings and return codes.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

PROFILE = (  # the same text as app.doc_convert.PROFILE (Task 6.5)
    "(version 1)(allow default)(deny network*)"
    '(deny file-write*)(allow file-write* (subpath (param "OUT")) (literal "/dev/null"))'
    '(deny file-read-data (subpath (param "PRIVATE")))'
    '(allow file-read-data (subpath (param "OUT")) (literal (param "SOURCE"))'
    ' (subpath (param "PREFIX")) (subpath (param "BASE_PREFIX"))'
    ' (subpath (param "WORKER_DIR")) (subpath (param "MODELS")))'
)
DOCLING = """
import sys
from io import BytesIO
from pathlib import Path
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
source, target, models = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
options = PdfPipelineOptions(artifacts_path=models, do_ocr=True, ocr_options=OcrMacOptions())
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
stream = DocumentStream(name="input" + source.suffix.lower(), stream=BytesIO(source.read_bytes()))
target.write_text(converter.convert(stream).document.export_to_markdown(), encoding="utf-8")
"""
PYPDF = """
import sys
from pypdf import PdfReader
reader = PdfReader(sys.argv[1])
open(sys.argv[2], "w", encoding="utf-8").write("\\n\\n".join(p.extract_text() or "" for p in reader.pages))
"""
XLRD = """
import sys
import xlrd
book = xlrd.open_workbook(sys.argv[1], on_demand=True)
lines = []
for sheet in book.sheets():
    lines.append("## " + sheet.name)
    lines.extend(" | ".join(str(sheet.cell_value(r, c)) for c in range(sheet.ncols)) for r in range(sheet.nrows))
open(sys.argv[2], "w", encoding="utf-8").write("\\n".join(lines))
"""
TEXTUTIL = """
import subprocess, sys
with open(sys.argv[2], "wb") as target:
    subprocess.run(["/usr/bin/textutil", "-convert", "docx", "-stdout", sys.argv[1]], stdout=target, check=True, timeout=60)
"""
MAGIKA = """
import sys
from magika import Magika
result = Magika().identify_path(sys.argv[1])
print(type(result.output.label).__name__, str(result.output.label), result.score, result.output.is_text)
"""
NET_PROBE = """
import socket, sys
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
except OSError as exc:
    print("blocked:", type(exc).__name__, exc)
else:
    print("CONNECTED")
"""


MODELS: Path = Path("/nonexistent")  # set in main()


def sandboxed(out: Path, *argv: str, source: Path | None = None) -> tuple[subprocess.CompletedProcess[str], float]:
    params = {
        "OUT": out, "SOURCE": (source or out).resolve(), "PRIVATE": Path.home().resolve(),
        "PREFIX": Path(sys.prefix).resolve(), "BASE_PREFIX": Path(sys.base_prefix).resolve(),
        "WORKER_DIR": out, "MODELS": MODELS,
    }
    defines = [arg for name, value in params.items() for arg in ("-D", f"{name}={value}")]
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["/usr/bin/time", "-l", "/usr/bin/sandbox-exec", *defines, "-p", PROFILE, *argv],
            capture_output=True, text=True, timeout=1800, cwd=out,  # getcwd() needs a readable cwd
            env={"PATH": "/usr/bin:/bin", "HOME": str(out), "TMPDIR": str(out),
                 "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "OMP_NUM_THREADS": "4"},
        )
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(argv, -9, "", "timed out after 1800 s")
    return result, time.monotonic() - started


def peak_rss(stderr: str) -> str:
    for line in stderr.splitlines():
        if "maximum resident set size" in line:
            return f"{int(line.split()[0]) // (1024 * 1024)} MiB"
    return "?"


def usage(out: Path) -> tuple[int, int]:
    """(total bytes, largest file) under out; measures what a step wrote in scratch."""

    sizes = [entry.stat().st_size for entry in out.rglob("*") if entry.is_file() and not entry.is_symlink()]
    return sum(sizes), max(sizes, default=0)


def wrote(before: tuple[int, int], out: Path) -> str:
    total, largest = usage(out)
    return f"scratch +{(total - before[0]) / 1048576:.1f} MiB, largest file {largest / 1048576:.1f} MiB"


def tail(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if "resident" not in line and line.strip()]
    errors = [line for line in lines if "Error" in line or "error" in line]
    return repr((errors or lines)[-1][-240:]) if lines else "''"


def main(inputs: Path, out: Path, models: Path, only: str = "") -> int:
    global MODELS
    MODELS = models
    out.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    steps = (("docling_step.py", DOCLING), ("pypdf_step.py", PYPDF), ("magika_step.py", MAGIKA),
             ("xlrd_step.py", XLRD), ("textutil_step.py", TEXTUTIL), ("probe.py", NET_PROBE))
    for name, body in steps:
        (out / name).write_text(body, encoding="utf-8")
    for index, source in enumerate(sorted(inputs.iterdir())):
        if only and only not in source.name:
            continue
        suffix = source.suffix.lower()
        shown = f"input{index}{suffix}"  # never the file's own name: one sample is personal
        bare = out / f"noext-{index}"  # same bytes, no extension: content must decide
        bare.write_bytes(source.read_bytes())
        for probe, tag in ((source, shown), (bare, f"noext-{index}")):
            result, took = sandboxed(out, python, "-I", "-B", str(out / "magika_step.py"), str(probe), source=probe)
            print(f"magika {tag}: rc={result.returncode} {took:.1f}s {result.stdout.strip()} {tail(result.stderr) if result.returncode else ''}", flush=True)
        if suffix == ".doc":
            docx = out / f"input{index}.docx"
            # -stdout: textutil's own file save goes through a temporary folder outside OUT and is denied.
            done, took = sandboxed(out, python, "-I", "-B", str(out / "textutil_step.py"), str(source), str(docx), source=source)
            print(f"textutil {shown}: rc={done.returncode} {took:.1f}s {tail(done.stderr) if done.returncode else ''}", flush=True)
            source = docx
        if suffix in (".pdf", ".docx", ".xlsx", ".pptx", ".doc"):
            target = out / f"input{index}.docling.md"
            before = usage(out)
            result, took = sandboxed(out, python, "-I", "-B", str(out / "docling_step.py"), str(source), str(target), str(models), source=source)
            size = target.stat().st_size if target.exists() else 0
            print(f"docling {shown}: rc={result.returncode} {took:.1f}s peak {peak_rss(result.stderr)} {size} bytes; {wrote(before, out)} {tail(result.stderr) if result.returncode else ''}", flush=True)
        if suffix == ".pdf":
            target = out / f"input{index}.pypdf.md"
            before = usage(out)
            result, took = sandboxed(out, python, "-I", "-B", str(out / "pypdf_step.py"), str(source), str(target), source=source)
            size = target.stat().st_size if target.exists() else 0
            print(f"pypdf {shown}: rc={result.returncode} {took:.1f}s peak {peak_rss(result.stderr)} {size} bytes; {wrote(before, out)}", flush=True)
        if suffix == ".xls":
            target = out / f"input{index}.xlrd.md"
            result, took = sandboxed(out, python, "-I", "-B", str(out / "xlrd_step.py"), str(source), str(target), source=source)
            size = target.stat().st_size if target.exists() else 0
            print(f"xlrd {shown}: rc={result.returncode} {took:.1f}s {size} bytes {tail(result.stderr) if result.returncode else ''}", flush=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    result, _ = sandboxed(out, python, "-I", "-B", str(out / "probe.py"), str(listener.getsockname()[1]))
    print("network probe:", result.stdout.strip() or tail(result.stderr))
    outside, _ = sandboxed(out, "/usr/bin/touch", str(Path.home() / "delibra-sandbox-probe"))
    print("write outside OUT:", "blocked" if outside.returncode != 0 else "ALLOWED")
    sentinel = out.parent / "read-probe-sentinel.txt"  # under the home, outside every allowed path
    sentinel.write_text("sentinel", encoding="utf-8")
    read, _ = sandboxed(out, "/bin/cat", str(sentinel))
    print("read under home:", "blocked" if read.returncode != 0 else "ALLOWED")
    if os.path.exists(Path.home() / "delibra-sandbox-probe"):
        print("WARNING: the write probe left a file in the home")
    return 0


if __name__ == "__main__":
    paths = [Path(arg).resolve() for arg in sys.argv[1:4]]
    raise SystemExit(main(*paths, *(sys.argv[4:5])))
