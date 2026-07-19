from __future__ import annotations

from hashlib import sha256

import pytest

from app.models import AutoArtifact, AutoParticipant, AutoRunRecord
from app.storage import ProjectStore


@pytest.fixture
def reserve_auto_run():
    """Return a test helper that publishes an active Auto reservation."""

    def reserve(
        store: ProjectStore,
        *,
        auto_id: str = "a" * 32,
    ) -> AutoRunRecord:
        topic = b"Original topic"
        baseline = b""
        sessions = store.list_sessions()
        record = AutoRunRecord(
            id=auto_id,
            project_id=store.project.id,
            status="preparing",
            agreement_policy="all_agree",
            max_cycles=3,
            current_cycle=0,
            next_participant=0,
            participants=[
                AutoParticipant(
                    session_id=session.id,
                    name=session.name,
                    agent=session.agent,
                    model=session.model,
                    effort=session.effort,
                )
                for session in sessions
            ],
            topic=AutoArtifact("topic.md", sha256(topic).hexdigest()),
            baseline=AutoArtifact("baseline.md", sha256(baseline).hexdigest()),
            baseline_entries=[],
            shared_context=None,
            shared_context_source=None,
            preparations=[],
            discussion=[],
            active_key=None,
            future_turn_timeout_seconds=900,
            active_timeout=None,
            stop_requested=False,
            created_at="2026-07-19T00:00:00Z",
            started_at=None,
            finished_at=None,
            terminal_reason=None,
        )
        store.create_auto_run(record, topic=topic, baseline=baseline)
        store.publish_auto_reservation(auto_id)
        return record

    return reserve
