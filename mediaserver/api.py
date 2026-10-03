"""Calls to the services' APIs that must not be mistaken for "nothing
there": any failure (no answer, an HTTP error, an unreadable answer)
raises ApiError, so a step never treats a failed read as an empty list."""
from __future__ import annotations

import time
from typing import Any

from mediaserver import common as c


class ApiError(Exception):
    pass


def call(method: str, url: str, headers: dict | None = None, body: Any = None, form: dict | None = None,
         timeout: float = 60) -> Any:
    """The decoded JSON answer (None for an empty one). An answer that isn't
    JSON (an HTML error page from a proxy, a half-started service) is an
    ApiError too, not text a caller would take for data."""
    r = c.request(url, method, headers, body=body, form=form, timeout=timeout)
    if not r.ok:
        raise ApiError(f"{method} {url}: " + (f"HTTP {r.status}" if r.status else "no answer"))
    if not r.body.strip():
        return None
    try:
        return c.json.loads(r.body)
    except ValueError:
        raise ApiError(f"{method} {url}: not JSON ({r.body[:60].decode(errors='replace')!r})") from None


def get(url: str, headers: dict | None = None, timeout: float = 60) -> Any:
    return call("GET", url, headers, timeout=timeout)


def wait_for(name: str, url: str, max_seconds: int = 120) -> bool:
    """Wait until <url> answers 2xx/3xx, printing "Waiting for <name>... up";
    False after max_seconds"""
    print(f"   Waiting for {name + '...':<15}", end="", flush=True)
    start = time.time()
    while True:
        status = c.request(url, timeout=10, follow=False).status
        if 200 <= status < 400:
            print(" up", flush=True)
            return True
        if time.time() - start >= max_seconds:
            print(" timeout!", flush=True)
            return False
        time.sleep(1)
