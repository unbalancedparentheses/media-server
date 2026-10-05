"""A stream check, separate from imported: for a file that was imported
(and checked by postimport), ask Jellyfin itself. It shows Jellyfin can
open and serve the file, not that every device decodes it smoothly
(postimport's own decode check and the player's choices cover more).

1. Jellyfin lists it and opens it: its playback information for the file
   (what a player asks for) comes back without an error, with a source.
2. Its tracks are usable: at least one audio track Jellyfin can read (and
   which subtitle tracks are text, which players show without converting).
3. A short stream can be produced: the first 256 KB of the file's stream
   arrive (what a player starts with).

A new file shows up in Jellyfin after its scan, so a file that isn't
listed yet is looked for again; after NOT_LISTED_AFTER it's a failure
("Jellyfin hasn't listed it"). Results go to postimport's status for the
dashboard: "verified" or "failed" with the reason.
"""
from __future__ import annotations

import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

NOT_LISTED_AFTER = 2 * 3600
PER_ROUND = 5
KEEP = 100


class Jellyfin:
    def __init__(self, state_dir: Path):
        self.url, self.auth = local("jellyfin"), c.jellyfin_auth(state_dir)
        self._items: dict | None = None
        self._user = ""

    def get(self, path: str):
        return c.get_json(f"{self.url}/{path}", self.auth)

    def items(self) -> dict:
        """path → item id, for every film and episode (once per round)"""
        if self._items is None:
            data = self.get("Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path")
            self._items = {i.get("Path"): i["Id"] for i in data.get("Items", []) if i.get("Path")}
        return self._items

    def user(self) -> str:
        if not self._user:
            self._user = (self.get("Users") or [{}])[0].get("Id", "")
        return self._user

    def user_data(self, item: str) -> list | None:
        """Every user's watch state of <item> (played, position); None when
        Jellyfin didn't answer"""
        try:
            users = self.get("Users") or []
            return [(self.get(f"Users/{u['Id']}/Items/{item}") or {}).get("UserData") or {} for u in users]
        except c.HTTP_ERRORS:
            return None

    def playback_info(self, item: str) -> dict:
        r = c.request(f"{self.url}/Items/{item}/PlaybackInfo?userId={self.user()}", "POST", self.auth, body={}, timeout=60)
        return r.json({}) if r.ok else {"ErrorCode": f"HTTP {r.status}" if r.status else "no answer"}

    def first_bytes(self, item: str, source: str) -> int:
        """HTTP status of the stream's start (0 when nothing came). At most
        256 KB is read, even from a server that ignores the range and sends
        the whole film"""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"{self.url}/Videos/{item}/stream?static=true&mediaSourceId={source}",
                                     headers=dict(self.auth, Range="bytes=0-262143"))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status if resp.read(262144) else 0
        except urllib.error.HTTPError as e:
            return e.code
        except (OSError, ValueError):
            return 0


def verify(jf: Jellyfin, path: str) -> dict | None:
    """{"status": "verified" | "failed", "detail"}; None when Jellyfin
    doesn't list the file (yet)"""
    item = jf.items().get(path)
    if not item:
        return None
    info = jf.playback_info(item)
    sources = info.get("MediaSources") or []
    if info.get("ErrorCode") or not sources:
        return {"status": "failed", "detail": f"Jellyfin can't open it ({info.get('ErrorCode') or 'no playable source'})"}
    source = next((s for s in sources if s.get("Path") == path), sources[0])
    streams = source.get("MediaStreams") or []
    video = next((s for s in streams if s.get("Type") == "Video"), None)
    audio = [s for s in streams if s.get("Type") == "Audio" and s.get("Codec")]
    text = sorted({s.get("Language") or "?" for s in streams if s.get("Type") == "Subtitle" and s.get("IsTextSubtitleStream")})
    if not audio:
        return {"status": "failed", "detail": "Jellyfin finds no audio track it can read"}
    status = jf.first_bytes(item, source.get("Id") or item)
    if status not in (200, 206):
        return {"status": "failed", "detail": f"the stream didn't start (HTTP {status or 'none'})"}
    parts = [(video or {}).get("Codec") or "?", "/".join(sorted({a["Codec"] for a in audio}))]
    if text:
        parts.append("subtitles " + ", ".join(text))
    how = "streams directly" if source.get("SupportsDirectPlay") else "streams (converted for some devices)"
    return {"status": "verified", "detail": f"{how}: " + " · ".join(parts)}


def run(state: dict, state_dir: Path, now: float | None = None) -> None:
    """Check up to PER_ROUND of the files waiting (oldest first)"""
    now = time.time() if now is None else now
    pending = state.setdefault("playback_pending", {})
    results = state.setdefault("playback", [])
    if not pending:
        return
    jf = Jellyfin(state_dir)
    for path in sorted(pending, key=lambda p: pending[p].get("since", 0))[:PER_ROUND]:
        entry = pending[path]
        if not Path(path).exists():
            pending.pop(path)   # replaced or deleted since
            continue
        try:
            result = verify(jf, path)
        except c.HTTP_ERRORS:
            return   # Jellyfin not answering: next round
        if result is None:
            if now - entry.get("since", now) < NOT_LISTED_AFTER:
                continue
            result = {"status": "failed", "detail": "Jellyfin hasn't listed it (is the library scan stuck?)"}
        pending.pop(path)
        results.insert(0, {"path": path, "title": entry.get("title", Path(path).stem), "at": int(now), **result})
        c.log(f"{entry.get('title', Path(path).stem)}: stream check {'passed' if result['status'] == 'verified' else 'failed'}: {result['detail']}")
    del results[KEEP:]


def queue(state: dict, path: "str | Path", title: str, now: float | None = None) -> None:
    """A file just imported (and checked): verify its playback soon"""
    state.setdefault("playback_pending", {})[str(path)] = {"title": title, "since": int(time.time() if now is None else now)}
