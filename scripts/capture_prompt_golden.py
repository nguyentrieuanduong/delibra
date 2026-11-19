"""Record what every built-in prompt renders to today (editable-prompts plan, Task 1).

Usage: envs/bin/python scripts/capture_prompt_golden.py > tests/fixtures/prompts/golden.json
Stdout is this script's interface. Run it on the commit BEFORE prompts become templates;
afterwards tests/test_prompts_golden.py compares these cases with the golden file.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.claude import ClaudeAdapter  # noqa: E402
from app.agents.codex import CodexAdapter  # noqa: E402
from app.auto import (  # noqa: E402
    _untrusted_section, build_turn_prompt, compaction_prompt, disagreement_note, discussion_prompt, docker_note,
    guidance_gate_note, preparation_prompt, skills_turn_note,
)
from app.build_handover import render_handover  # noqa: E402
from app.coding import DownloadPolicy  # noqa: E402
from app.doc_convert import HEADER  # noqa: E402
from app.download_manager import turn_spool_notes  # noqa: E402
from app.download_model_check import check_prompt  # noqa: E402
from app.download_rules import convert_prompt, downloads_prompt  # noqa: E402
from app.download_store import render_index  # noqa: E402
from app.notes import notes_prompt  # noqa: E402
from app.pass_prompts import BUILT_IN_PASS_PROMPT_TEMPLATE, render_pass_prompt  # noqa: E402
from app.prompts import BUILT_IN  # noqa: E402
from app.questions import questions_prompt  # noqa: E402
from app.skill_folders import chat_skills_note  # noqa: E402

from tests.prompt_cases import stateless_cases  # noqa: E402

W = Path("/w/sessions/alpha/workspace")
STAGED = (W / "inputs/round-07/auto-context.md").as_posix()


def cases() -> dict[str, str]:
    out = {
        "pass": render_pass_prompt(BUILT_IN_PASS_PROMPT_TEMPLATE, source_path="inputs/round-03/source.md",
                                   source_session="Alpha", source_round=2),
        "compact": BUILT_IN.render("compact"),
        "chat_skills": chat_skills_note(W.parent / ".delibra/skills/chat-index.md"),
        "auto_preparation": preparation_prompt(STAGED),
        "auto_discussion": discussion_prompt(STAGED),
        "auto_compaction": compaction_prompt(STAGED),
        "build_turn": build_turn_prompt(STAGED),
        "docker_lima": docker_note("lima"),
        "docker_local": docker_note("local"),
        "build_handover": render_handover("Alpha", "timed out", b"partial text", ["a.py", "b.md"]).decode(),
        "skills_turn": skills_turn_note(Path("/w/.delibra/skills/build"), STAGED),
        "guidance_gate/ask": guidance_gate_note(True),
        "guidance_gate/report": guidance_gate_note(False),
        "disagreement/ask": disagreement_note(True),
        "disagreement/report": disagreement_note(False),
        "questions/read_only": questions_prompt(Path("/w/questions.json"), None, pause_enabled=True),
        "questions/pause": questions_prompt(Path("/w/questions.json"), W / "questions/outbox", pause_enabled=True),
        "questions/no_pause": questions_prompt(Path("/w/questions.json"), W / "questions/outbox", pause_enabled=False),
        "notes/live_marks": notes_prompt(W / "notes.json", live=True, marks_path=W / "notes-read.json"),
        "notes/copy_no_marks": notes_prompt(W / "notes.json", live=False, marks_path=None),
        "notes/chat": notes_prompt(W / "delibra-notes.json", live=True, marks_path=W / "delibra-notes-read.json",
                                   sent="to you in this conversation", overrides="the message"),
        "downloads/manual": downloads_prompt(W / "downloads/status.json", W / "downloads/requests", manual=True,
                                             shell=True),
        "downloads/shell": downloads_prompt(W / "downloads/status.json", W / "downloads/requests", manual=False,
                                            shell=True),
        "downloads/no_shell": downloads_prompt(W / "downloads/status.json", W / "downloads/requests",
                                               manual=False, shell=False),
        "convert/shell": convert_prompt(W / "convert/status.json", W / "convert/requests", "raw/converted",
                                        shell=True),
        "convert/no_shell": convert_prompt(W / "convert/status.json", W / "convert/requests", "raw/converted",
                                           shell=False),
        "spool_notes/build": "".join(turn_spool_notes(
            W, DownloadPolicy(enabled=True, folder="raw", approval="automatic", convert=True),
            agent="claude", build=True,
        )),
        "model_check": check_prompt([("D1", "https://example.org/a.pdf")]),
        "global_header": BUILT_IN.render("global_header"),
        # Framing that stays non-editable (design non-goal); guarded so the refactor cannot change it.
        "framing/untrusted_section": _untrusted_section("Topic", b"Body.").decode(),
        "framing/converted_header": HEADER,
        "framing/downloads_index": render_index([]).decode(),
    }
    out.update(stateless_cases(ClaudeAdapter(), CodexAdapter()))
    return out


if __name__ == "__main__":
    json.dump(cases(), sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")
