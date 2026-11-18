"""Convert one document to Markdown. Run only by app.doc_convert, inside sandbox-exec.

Lives in its own directory so app/ modules (markdown.py, models.py, ...) are not on
sys.path when Python puts this script's folder first. Usage: worker.py <in> <out> <step>.
Step `identify` writes JSON {"label", "score", "pages", "is_text"} (Magika; pages only
for PDFs). The parent sets a hard RLIMIT_FSIZE before this process starts.
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


def docling_markdown(source: Path, extension: str) -> str:
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
    return converter.convert(stream).document.export_to_markdown()


def xls_markdown(source: Path) -> str:
    import xlrd

    book = xlrd.open_workbook(str(source), on_demand=True)
    parts: list[str] = []
    for sheet in book.sheets():
        parts.append(f"## {sheet.name}\n")
        rows = [
            [str(sheet.cell_value(r, c)).replace("|", "\\|").replace("\n", " ") for c in range(sheet.ncols)]
            for r in range(sheet.nrows)
        ]
        if not rows or not sheet.ncols:
            parts.append("(empty sheet)\n")
            continue
        header, *body = rows
        parts.append("| " + " | ".join(header) + " |")
        parts.append("|" + " --- |" * sheet.ncols)
        parts.extend("| " + " | ".join(row) + " |" for row in body)
        parts.append("")
    return "\n".join(parts)


def pdf_text(source: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    return "\n\n".join(
        f"<!-- page {number} -->\n{page.extract_text() or ''}"
        for number, page in enumerate(reader.pages, start=1)
    )


def convert(source: Path, kind: str, work: Path) -> str:
    if kind == "identify":
        return identify(source)
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
        text = convert(source, kind, output.parent)
    except Exception as exc:  # the parent records one reason line; nothing else to do here
        (output.parent / "error.txt").write_text(f"{type(exc).__name__}: {exc}"[:300], encoding="utf-8")
        return 3
    output.write_bytes(text.encode("utf-8")[: OUTPUT_MAX_BYTES + 1])  # never more than the parent reads
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
