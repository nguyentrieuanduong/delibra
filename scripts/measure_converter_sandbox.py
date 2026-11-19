"""Measure which mach services the document converter's sandboxed steps use.

Run from the repository root with your own sample documents (none ship with Delibra):

    envs/bin/python -I scripts/measure_converter_sandbox.py [--models DIR] [--repeat N]
        [--without SERVICE ...] FILE [FILE ...]

Each file goes through the real converter (app.doc_convert.convert_file) with the
mach-lookup allows marked `(with report)`; afterwards the script reads the kernel's
sandbox reports with `/usr/bin/log show` (no sudo needed; in zsh a bare `log` is a
shell builtin) and prints, per step program, the services that were looked up and any
mach-lookup the profile denied. `--without` drops a service from both allow-lists, to
check whether conversions still pass without it. stdout is this CLI's interface; nothing is written.
Use it to update app.doc_convert.MACH_SERVICES if a macOS update breaks conversion.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import app.doc_convert as converter  # noqa: E402

REPORT = re.compile(r"Sandbox: (python3[.\d]*|textutil)\(\d+\) (allow|deny)\(?\d?\)? mach-lookup (\S+)")


def patch_profiles(without: set[str]) -> None:
    for kind, services in list(converter.MACH_SERVICES.items()):
        converter.MACH_SERVICES[kind] = tuple(name for name in services if name not in without)
    original = converter.profile_for

    def reported(kind: str) -> str:
        return original(kind).replace("(allow mach-lookup ", "(allow mach-lookup (with report) ", 1)

    converter.profile_for = reported


async def convert_all(files: list[Path], models: Path | None, repeat: int, work: Path) -> bool:
    passed = True
    for round_number in range(1, repeat + 1):
        for index, source in enumerate(files):
            staged = work / f"in-{round_number}-{index}{source.suffix.lower()}"
            shutil.copyfile(source, staged)
            started = time.monotonic()
            result = await converter.convert_file(
                staged, display_path=f"sample{index}{source.suffix.lower()}", source_sha256="0" * 64,
                scratch_root=work / "scratch", models_dir=models,
            )
            passed = passed and result.state == "converted"
            print(f"round {round_number} sample{index}{source.suffix.lower()}: {result.state} "
                  f"{time.monotonic() - started:.1f}s {result.reason or ''}", flush=True)
    return passed


def reports(since: str) -> dict[tuple[str, str], set[str]]:
    time.sleep(2)  # let the kernel's reports reach the log
    shown = subprocess.run(
        ["/usr/bin/log", "show", "--start", since, "--style", "compact",
         "--predicate", 'eventMessage CONTAINS "mach-lookup"'],
        capture_output=True, text=True, check=True,
    ).stdout
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    for program, verdict, service in REPORT.findall(shown):
        found[("textutil" if program == "textutil" else "python", verdict)].add(service)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--without", action="append", default=[])
    args = parser.parse_args()
    patch_profiles(set(args.without))
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with tempfile.TemporaryDirectory(prefix="delibra-measure-") as work:
        passed = asyncio.run(convert_all(
            [path.resolve() for path in args.files], args.models.resolve() if args.models else None,
            args.repeat, Path(work),
        ))
    for (program, verdict), services in sorted(reports(since).items()):
        print(f"{program} {verdict}: {', '.join(sorted(services))}")
    print("all converted" if passed else "SOME CONVERSIONS FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
