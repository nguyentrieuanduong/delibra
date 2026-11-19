# Third-party notices

Delibra uses the open-source software below. Each project keeps its own copyright and
licence; the licence texts ship inside each installed package (its `*.dist-info` folder) and
on the project pages linked here. Versions are the pins in `requirements.txt`; their own
dependencies are pinned in `requirements.lock`.

## Direct dependencies

| Package | Version | Licence | Copyright | Project |
|---|---|---|---|---|
| FastAPI | 0.139.2 | MIT | Sebastián Ramírez | https://github.com/fastapi/fastapi |
| HTTPX | 0.28.1 | BSD-3-Clause | Encode OSS Ltd | https://github.com/encode/httpx |
| Jinja2 | 3.1.6 | BSD-3-Clause | Pallets | https://github.com/pallets/jinja |
| markdown-it-py (with linkify-it-py and mdit-py-plugins) | 4.2.0 | MIT | Vitaly Puzrin, Alex Kocharin; tsutsu3; ExecutableBookProject | https://github.com/executablebooks/markdown-it-py |
| pytest | 9.1.1 | MIT | Holger Krekel and others | https://github.com/pytest-dev/pytest |
| pytest-asyncio | 1.4.0 | Apache-2.0 | pytest-asyncio contributors | https://github.com/pytest-dev/pytest-asyncio |
| python-multipart | 0.0.32 | Apache-2.0 | Andrew Dunham and contributors | https://github.com/Kludex/python-multipart |
| PyYAML | 6.0.3 | MIT | Ingy döt Net, Kirill Simonov | https://pyyaml.org/ |
| sse-starlette | 3.4.5 | BSD-3-Clause | sysid | https://github.com/sysid/sse-starlette |
| Uvicorn | 0.51.0 | BSD-3-Clause | Encode OSS Ltd | https://github.com/encode/uvicorn |
| watchfiles | 1.3.0 | MIT | Samuel Colvin | https://github.com/samuelcolvin/watchfiles |
| Playwright for Python | 1.62.0 | Apache-2.0 | Microsoft Corporation | https://github.com/microsoft/playwright-python |
| Magika | 1.0.3 | Apache-2.0 | Google LLC | https://github.com/google/magika |
| Docling (`docling[ocrmac]`) | 2.150.1 | MIT | Docling Project contributors (IBM Research; LF AI & Data) | https://github.com/docling-project/docling |
| pypdf | 7.1.0 | BSD-3-Clause | Mathieu Fenniak and pypdf contributors | https://github.com/py-pdf/pypdf |
| xlrd | 2.0.2 | BSD-3-Clause | Stephen John Machin, Lingfo Pty Ltd; Chris Withers | https://github.com/python-excel/xlrd |

## Document conversion

Delibra's document converter (`app/doc_convert.py`, `app/converter/worker.py`) runs these
in a sandboxed subprocess:

- **Magika** (Google, Apache-2.0) identifies each file's type from its content. Its model
  ships inside the Magika package under the same licence. It runs on **ONNX Runtime**
  (Microsoft Corporation, MIT; https://github.com/microsoft/onnxruntime).
- **Docling** (IBM Research / LF AI & Data, MIT) converts PDF, DOCX, XLSX and PPTX. It pulls
  in docling-core, docling-parse and docling-ibm-models (MIT) and **PyTorch** (BSD-3-Clause
  style licence, https://github.com/pytorch/pytorch).
- **ocrmac** (Maximilian Strauss, MIT; https://github.com/straussmaximilian/ocrmac) and
  **PyObjC** (Ronald Oussoren, MIT) connect Docling to the macOS Vision text recognition.
- **pypdf** (BSD-3-Clause) is the plain-text fallback for PDFs and counts pages.
- **xlrd** (BSD-3-Clause) reads legacy `.xls` workbooks.

### Docling models

Downloaded once, on request, into `$DELIBRA_HOME/models/docling`; not part of this
repository:

| Model | Licence | Source |
|---|---|---|
| Layout "Heron" (`docling-project/docling-layout-heron`, `docling-layout-heron-onnx`) | Apache-2.0 | https://huggingface.co/docling-project/docling-layout-heron |
| TableFormer (`docling-project/docling-models`) | CDLA-Permissive-2.0 | https://huggingface.co/docling-project/docling-models |

### System components

macOS Vision (text recognition) and `/usr/bin/textutil` (turns `.doc` into `.docx`) are
part of macOS. Delibra calls them; it does not redistribute them.
