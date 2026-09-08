"""Polling fragment for account-wide provider quota."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.usage import USAGE_PERSISTENCE_WARNING, quota_verdict


router = APIRouter()

_PROVIDERS = ("claude", "codex")
# One row per provider, one column per window. The labels live here rather than
# in the template so the header and the cells cannot drift apart.
_WINDOWS = (("five_hour", "5-hour limit"), ("seven_day", "Weekly limit"))
_VERDICT_STRENGTH = {"unknown": 0, "ok": 1, "warning": 2, "pause": 3}


def usage_badge_context(request: Request, *, oob: bool = False) -> dict[str, Any]:
    """Render-ready quota state, including the interval its next poll will use.

    The interval is quota-dependent (5g): the badge is otherwise a request a
    minute per tab, forever, for state that only a finished turn can change.

    Every caller renders the badge itself rather than asking the browser to
    fetch it on load, so Codex's refresh is kicked off here -- scheduled, never
    awaited: Phase 8's account read takes 1.2-1.9 s, and a page must not wait
    on it. This render ships the monitor's current state and the next poll
    shows the newer figure.
    """

    request.app.state.manager.schedule_codex_quota_refresh()
    monitor = request.app.state.usage_monitor
    settings = request.app.state.settings
    providers: list[dict[str, Any]] = []
    strongest_verdict = "unknown"
    # A window with no percentage -- Claude reports none at all -- cannot be
    # learned about by polling faster, so it keeps the idle interval.
    low_quota = False
    for provider in _PROVIDERS:
        windows: list[dict[str, Any]] = []
        for window, _ in _WINDOWS:
            observation = monitor.report(provider, window)
            verdict = quota_verdict(observation, settings=settings)
            remaining_percent = (
                observation.remaining_percent if observation is not None else None
            )
            strongest_verdict = max(
                (strongest_verdict, verdict),
                key=lambda value: _VERDICT_STRENGTH[value],
            )
            if (
                remaining_percent is not None
                and remaining_percent < settings.usage_warn_remaining_percent
            ):
                low_quota = True
            windows.append(
                {
                    "key": window,
                    "observation": observation,
                    "remaining_percent": (
                        round(remaining_percent) if remaining_percent is not None else None
                    ),
                    "verdict": verdict,
                }
            )
        providers.append(
            {
                "name": provider,
                "label": provider.title(),
                "windows": windows,
            }
        )
    badge_state = {
        "pause": "paused",
        "warning": "warn",
        "ok": "ok",
        "unknown": "ok",
    }[strongest_verdict]
    return {
        "providers": providers,
        "window_labels": [label for _, label in _WINDOWS],
        "badge_state": badge_state,
        "durability_warning": (
            USAGE_PERSISTENCE_WARNING if monitor.durability_degraded else None
        ),
        "usage_poll_seconds": (
            settings.usage_low_quota_poll_seconds
            if low_quota
            else settings.usage_poll_seconds
        ),
        "usage_badge_oob": oob,
    }


@router.get("/usage/badge", response_class=HTMLResponse)
async def usage_badge(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="_usage_badge.html",
        context=usage_badge_context(request),
    )
