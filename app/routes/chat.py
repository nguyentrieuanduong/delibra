"""Project-level merged conversation view."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.routes.sessions import _round_views
from app.storage import ProjectStore


router = APIRouter()


def _timeline(request: Request, project_id: str, store: ProjectStore) -> list[dict]:
    items: list[dict] = []
    for session in store.list_sessions():
        active_key = request.app.state.manager.active_key(project_id, session.id)
        for round_view in _round_views(store, session.id):
            record = round_view["record"]
            if record is None:
                continue
            items.append(
                {
                    "session": session,
                    "round": round_view,
                    "key": (
                        active_key
                        if active_key is not None and active_key.round_n == record.n
                        else None
                    ),
                }
            )
    return sorted(
        items,
        key=lambda item: (
            item["round"]["record"].started_at,
            item["session"].created_at,
            item["session"].id,
            item["round"]["n"],
        ),
    )


@router.get("/projects/{project_id}/chat", response_class=HTMLResponse)
async def chat_page(request: Request, project_id: str):
    project = request.app.state.registry.get(project_id)
    store = ProjectStore(project)
    sessions = store.list_sessions()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "project": project,
            "sessions": sessions,
            "timeline": _timeline(request, project_id, store),
            "health": request.app.state.health,
        },
    )
