"""Polling fragment for account-wide provider quota."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.usage import quota_verdict


router = APIRouter()

_PROVIDERS = ("claude", "codex")
_WINDOWS = (("five_hour", "5h"), ("seven_day", "weekly"))
_VERDICT_STRENGTH = {"unknown": 0, "ok": 1, "warning": 2, "pause": 3}


def _badge_context(request: Request) -> dict[str, Any]:
    monitor = request.app.state.usage_monitor
    settings = request.app.state.settings
    providers: list[dict[str, Any]] = []
    strongest_verdict = "unknown"
    for provider in _PROVIDERS:
        windows: list[dict[str, Any]] = []
        for window, label in _WINDOWS:
            observation = monitor.report(provider, window)
            verdict = quota_verdict(observation, settings=settings)
            remaining_percent = (
                observation.remaining_percent if observation is not None else None
            )
            strongest_verdict = max(
                (strongest_verdict, verdict),
                key=lambda value: _VERDICT_STRENGTH[value],
            )
            windows.append(
                {
                    "label": label,
                    "observation": observation,
                    "remaining_percent": (
                        round(remaining_percent) if remaining_percent is not None else None
                    ),
                    "verdict": verdict,
                }
            )
        providers.append(
            {"name": provider, "label": provider.title(), "windows": windows}
        )
    badge_state = {
        "pause": "paused",
        "warning": "warn",
        "ok": "ok",
        "unknown": "ok",
    }[strongest_verdict]
    return {
        "providers": providers,
        "badge_state": badge_state,
        "usage_poll_seconds": settings.usage_poll_seconds,
    }


@router.get("/usage/badge", response_class=HTMLResponse)
async def usage_badge(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_usage_badge.html",
        context=_badge_context(request),
    )
