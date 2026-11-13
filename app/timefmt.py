"""Timestamps as the page shows them before the browser localises them.

app.js rewrites each one in the reader's timezone with the same rules
(formatLocalStamp, formatLocalRange); this text is what a page without
JavaScript, or before it runs, shows.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def utc_stamp(value: object) -> str:
    """``2026-11-13 07:05:09 +00``; a value that is not an ISO time stays as is."""

    moment = _moment(value)
    if moment is None:
        return value if isinstance(value, str) else ""
    return moment.strftime("%Y-%m-%d %H:%M:%S") + " +00"


def utc_stamp_range(start: object, finish: object) -> str:
    """One day names its date and offset once; a range across midnight
    names both days; without a readable finish, the start alone."""

    begun = _moment(start)
    if begun is None:
        return utc_stamp(start)
    ended = _moment(finish)
    if ended is None:
        return utc_stamp(start)
    if begun.date() == ended.date():
        return f"{begun:%Y-%m-%d %H:%M:%S} – {ended:%H:%M:%S} +00"
    return f"{utc_stamp(start)} – {utc_stamp(finish)}"
