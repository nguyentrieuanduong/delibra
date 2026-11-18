"""Docling's PDF models, downloaded once into $DELIBRA_HOME/models/docling (design 2026-11-16).

The converter sandbox has no network, so the models are fetched here, outside it,
when the user asks, and passed to the worker as Docling's artifacts_path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys

from app.storage import atomic_write_bytes, utc_now

STATUS_NAME = "docling-status.json"
READY_NAME = ".delibra-ready"
DOWNLOAD_SECONDS = 30 * 60
# What the PDF pipeline uses (layout Heron + TableFormer, about 0.67 GB, Task 1).
# Docling's default set adds code-formula, picture-classifier and RapidOCR models
# that Delibra never runs: OCR is macOS Vision.
MODELS = ("layout", "tableformer")


@dataclass(frozen=True)
class ModelsState:
    status: str
    error: str | None = None
    size_bytes: int = 0


def models_dir(home: Path) -> Path:
    return home / "models" / "docling"


def _status_path(home: Path) -> Path:
    return home / "models" / STATUS_NAME


def _write_status(home: Path, status: str, error: str | None = None) -> None:
    (home / "models").mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"status": status, "error": error, "updated_at": utc_now()}
    atomic_write_bytes(_status_path(home), json.dumps(payload).encode("utf-8"))


def _size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file() and path.name != READY_NAME)


def ready_models_dir(home: Path) -> Path | None:
    target = models_dir(home)
    return target if (target / READY_NAME).is_file() else None


def models_state(home: Path) -> ModelsState:
    if ready_models_dir(home) is not None:
        return ModelsState("ready", None, _size(models_dir(home)))
    try:
        data = json.loads(_status_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ModelsState("absent")
    status = data.get("status") if isinstance(data, dict) else None
    if status in ("downloading", "failed"):
        return ModelsState(status, data.get("error"))
    return ModelsState("absent")


def mark_interrupted(home: Path) -> None:
    """Startup: a download that was running when the server stopped did not finish."""

    if models_state(home).status == "downloading":
        _write_status(home, "failed", "interrupted")


async def download_models(home: Path, *, tools: Path | None = None) -> None:
    """Fetch Docling's PDF models into a temporary folder, then move it into place."""

    tools = tools or Path(sys.executable).with_name("docling-tools")
    target = models_dir(home)
    partial = target.with_name("docling.partial")
    # Hugging Face's cache would otherwise go to ~/.cache; it is dropped afterwards.
    hf_home = target.with_name("docling.hf-cache")
    for leftover in (partial, hf_home):
        shutil.rmtree(leftover, ignore_errors=True)
    _write_status(home, "downloading")
    try:
        process = await asyncio.create_subprocess_exec(
            str(tools), "models", "download", "-o", str(partial), *MODELS,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            # Every Hugging Face cache variable, so a user's own setting cannot send files elsewhere.
            env={**os.environ, "HF_HOME": str(hf_home), "HF_HUB_CACHE": str(hf_home / "hub"),
                 "HF_XET_CACHE": str(hf_home / "xet")},
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), DOWNLOAD_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("download did not finish within 30 minutes") from None
        except asyncio.CancelledError:  # shutdown: the next start marks it interrupted
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no message"]
            raise RuntimeError(f"docling-tools exited with {process.returncode}: {tail[0][:200]}")
        (partial / READY_NAME).write_text(utc_now(), encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(partial, target)
        _write_status(home, "ready")
    except (OSError, RuntimeError) as exc:
        shutil.rmtree(partial, ignore_errors=True)
        _write_status(home, "failed", str(exc))
    finally:
        shutil.rmtree(hf_home, ignore_errors=True)
