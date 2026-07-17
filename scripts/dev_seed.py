"""Create a local project and one session for manual M1 browser testing."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from uuid import uuid4

from app.config import Settings
from app.models import SessionConfig
from app.storage import ProjectStore, RegistryStore, utc_now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--agent", choices=["claude", "codex"], default="claude")
    args = parser.parse_args()
    project_path = args.project.expanduser().resolve()
    registry = RegistryStore(Settings.from_env().home)
    project = registry.register("Delibra M1 seed", project_path)
    model = "sonnet" if args.agent == "claude" else "gpt-5.4"
    effort = "high"
    session = SessionConfig(
        id=uuid4().hex,
        name=f"{args.agent} researcher",
        agent=args.agent,
        model=model,
        effort=effort,
        role_instructions="You are a rigorous research partner.",
        cli_session_id=None,
        status="idle",
        created_at=utc_now(),
        rounds=[],
    )
    ProjectStore(project).create_session(session)
    sys.stdout.write(
        f"Seeded /projects/{project.id}/sessions/{session.id} in {project.path}\n"
    )


if __name__ == "__main__":
    main()
