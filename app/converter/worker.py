"""Convert one document to Markdown. Run only by app.doc_convert, inside sandbox-exec.

Lives in its own directory so app/ modules (markdown.py, models.py, ...) are not on
sys.path when Python puts this script's folder first. Usage: worker.py <in> <out> <step>.
Step `identify` writes JSON {"label", "score", "pages", "is_text"} (Magika; pages only
for PDFs). A conversion step also writes stats.json {"text_chars"}: the document's own
text, without the Markdown this script adds (page markers, table rules, headings), so
the parent can tell an empty scan from a real document. The parent sets a hard
RLIMIT_FSIZE before this process starts.
The models directory comes from DELIBRA_DOCLING_MODELS. Exit codes: 0 done, 2 bad
arguments, 3 conversion error (reason in error.txt beside the output).
"""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys

OUTPUT_MAX_BYTES = 20 * 1024**2  # same as app.doc_convert; the parent marks the cut
KINDS = frozenset({"identify", "pdf", "docx", "xlsx", "pptx", "xls", "pdf-text"})  # .doc: textutil step first


def identify(source: Path) -> str:
    from magika import Magika

    # Content decides, never the name; default (high-confidence) mode returns a generic
    # label when unsure, which the parent treats as unsupported.
    result = Magika().identify_path(source)
    label = str(result.output.label)  # ContentTypeLabel is a str enum: str() is the bare label
    pages = None
    if label == "pdf":
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(source)).pages)
        except Exception:  # the count only sizes the time limit; Docling reports real errors
            pages = None
    return json.dumps({
        "label": label, "score": float(result.score), "pages": pages,
        "is_text": bool(result.output.is_text),
    })


def _chars(text: str) -> int:
    return sum(not ch.isspace() for ch in text)


def docling_text_chars(document) -> int:
    """Text of the document's items and table cells; image placeholders do not count."""

    count = 0
    for item, _level in document.iterate_items():
        text = getattr(item, "text", None)
        if isinstance(text, str):
            count += _chars(text)
        for cell in getattr(getattr(item, "data", None), "table_cells", None) or ():
            count += _chars(cell.text)
    return count


def docling_markdown(source: Path, extension: str) -> tuple[str, int]:
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # OCR always on, macOS Vision, Docling's default thresholds (user decision 2026-11-16).
    options = PdfPipelineOptions(
        artifacts_path=os.environ.get("DELIBRA_DOCLING_MODELS") or None,
        do_ocr=True,
        ocr_options=OcrMacOptions(),
    )
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    stream = DocumentStream(name=f"input.{extension}", stream=BytesIO(source.read_bytes()))
    document = converter.convert(stream).document
    return document.export_to_markdown(), docling_text_chars(document)


def xls_markdown(source: Path) -> tuple[str, int]:
    import xlrd

    book = xlrd.open_workbook(str(source), on_demand=True)
    parts: list[str] = []
    chars = 0
    for sheet in book.sheets():
        parts.append(f"## {sheet.name}\n")
        rows = [
            [str(sheet.cell_value(r, c)).replace("|", "\\|").replace("\n", " ") for c in range(sheet.ncols)]
            for r in range(sheet.nrows)
        ]
        chars += sum(_chars(cell) for row in rows for cell in row)
        if not rows or not sheet.ncols:
            parts.append("(empty sheet)\n")
            continue
        header, *body = rows
        parts.append("| " + " | ".join(header) + " |")
        parts.append("|" + " --- |" * sheet.ncols)
        parts.extend("| " + " | ".join(row) + " |" for row in body)
        parts.append("")
    return "\n".join(parts), chars


def pdf_text(source: Path) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(f"<!-- page {number} -->\n{page}" for number, page in enumerate(pages, start=1))
    return text, sum(_chars(page) for page in pages)


def convert(source: Path, kind: str, work: Path) -> tuple[str, int | None]:
    """(output, the document's own text characters; None for identify)."""

    if kind == "identify":
        return identify(source), None
    if kind == "pdf-text":
        return pdf_text(source)
    if kind == "xls":
        return xls_markdown(source)
    return docling_markdown(source, kind)


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[3] not in KINDS:
        return 2
    source, output, kind = Path(argv[1]), Path(argv[2]), argv[3]
    try:
        text, chars = convert(source, kind, output.parent)
    except Exception as exc:  # the parent records one reason line; nothing else to do here
        (output.parent / "error.txt").write_text(f"{type(exc).__name__}: {exc}"[:300], encoding="utf-8")
        return 3
    output.write_bytes(text.encode("utf-8")[: OUTPUT_MAX_BYTES + 1])  # never more than the parent reads
    if chars is not None:
        (output.parent / "stats.json").write_text(json.dumps({"text_chars": chars}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
