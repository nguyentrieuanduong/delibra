"""Read-only session/history routes; CRUD is added in M2."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.storage import ProjectStore


router = APIRouter()


def _round_views(store: ProjectStore, session_id: str) -> list[dict]:
    config = store.load_session(session_id)
    rounds_dir = store.rounds_dir(session_id)
    views: list[dict] = []
    known = {record.n for record in config.rounds}
    for record in sorted(config.rounds, key=lambda item: item.n):
        prompt = rounds_dir / f"round-{record.n:02d}.prompt.md"
        output = rounds_dir / f"round-{record.n:02d}.md"
        partial = rounds_dir / f"round-{record.n:02d}.partial.md"
        views.append(
            {
                "n": record.n,
                "record": record,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": False,
            }
        )
    scan = store.scan_round_files(session_id)
    for number in sorted({item.n for item in scan.orphans} - known):
        prompt = rounds_dir / f"round-{number:02d}.prompt.md"
        output = rounds_dir / f"round-{number:02d}.md"
        partial = rounds_dir / f"round-{number:02d}.partial.md"
        views.append(
            {
                "n": number,
                "record": None,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": True,
            }
        )
    return sorted(views, key=lambda item: item["n"])


@router.get("/projects/{project_id}/sessions/{session_id}")
async def session_page(request: Request, project_id: str, session_id: str):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    session = store.load_session(session_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "project": project,
            "session": session,
            "rounds": _round_views(store, session_id),
            "health": request.app.state.health,
        },
    )


@router.get("/projects/{project_id}/sessions/{session_id}/rounds/{round_n}")
async def round_fragment(
    request: Request,
    project_id: str,
    session_id: str,
    round_n: int,
):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    view = next(
        item for item in _round_views(store, session_id) if item["n"] == round_n
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_round.html",
        context={"project": project, "session_id": session_id, "round": view},
    )
