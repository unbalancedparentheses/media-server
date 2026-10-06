"""Pausing the automation for a while, from the dashboard: "searches"
(stuck.py's searches, diagnosis looks, Japanese-audio looks and upgrade
searches; Sonarr and Radarr's own RSS keeps going) or "repairs"
(postimport's file fixes and MP4 repackaging; new downloads are still
checked). Each resumes on its own at its time, in .state/pause.json."""
from __future__ import annotations

import time
from pathlib import Path

from mediaserver import common as c

KINDS = ("searches", "repairs")
MORNING_HOUR = 7


def file(state: Path) -> Path:
    return state / "pause.json"


def until(state: Path, what: str, now: float | None = None) -> float:
    """When <what> resumes; 0 when it isn't paused"""
    now = time.time() if now is None else now
    data = c.read_json(file(state), {}) or {}
    t = data.get(what, 0) if isinstance(data, dict) else 0
    return t if isinstance(t, (int, float)) and t > now else 0


def paused(state: Path, what: str, now: float | None = None) -> bool:
    return until(state, what, now) > 0


def next_morning(now: float | None = None) -> float:
    """MORNING_HOUR local time, tomorrow if it's already past"""
    now = time.time() if now is None else now
    t = time.localtime(now)
    morning = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, MORNING_HOUR, 0, 0, 0, 0, -1))
    return morning if morning > now else time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, MORNING_HOUR, 0, 0, 0, 0, -1))


def set_pause(state: Path, what: str, resume_at: float) -> None:
    """resume_at 0: resume now"""
    data = c.read_json(file(state), {}) or {}
    data = data if isinstance(data, dict) else {}
    if resume_at:
        data[what] = int(resume_at)
    else:
        data.pop(what, None)
    c.write_json(file(state), data)


def status(state: Path, now: float | None = None) -> dict:
    """{kind: resume time} for what's paused now"""
    return {k: int(t) for k in KINDS if (t := until(state, k, now))}
