"""nix run .#trash-sync [-- COMMIT]: refresh the TRaSH Guides custom formats
in custom-formats/ from upstream (its latest commit unless one is given),
for a reviewed change: a weekly GitHub job runs it and opens a pull request.

Each file with a "trash_id" is matched to TRaSH's file by that id (not by
name), its rule (specifications) is taken from upstream, and what's ours
stays: the mediaServer* settings, and the name (Sonarr and Radarr know the
format by its name: a rename would leave the old one behind, so it's only
reported). Files without a trash_id are ours and untouched. The commit
goes into custom-formats/README.md.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UPSTREAM = "TRaSH-Guides/Guides"
API = f"https://api.github.com/repos/{UPSTREAM}"
RAW = f"https://raw.githubusercontent.com/{UPSTREAM}"
HEADERS = {"User-Agent": "media-server-trash-sync", "Accept": "application/vnd.github+json"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as r:
        return r.read()


def latest_commit() -> str:
    return json.loads(fetch(f"{API}/commits/master"))["sha"]


def upstream_formats(commit: str, app: str) -> dict:
    """trash_id → TRaSH's custom format, for one app at <commit>"""
    tree = json.loads(fetch(f"{API}/git/trees/{commit}?recursive=1"))["tree"]
    prefix = f"docs/json/{app}/cf/"
    paths = [t["path"] for t in tree if t["path"].startswith(prefix) and t["path"].endswith(".json")]

    def one(path: str):
        try:
            return json.loads(fetch(f"{RAW}/{commit}/{path}"))
        except (OSError, ValueError):
            return None
    with ThreadPoolExecutor(16) as pool:
        formats = [f for f in pool.map(one, paths) if isinstance(f, dict) and f.get("trash_id")]
    return {f["trash_id"]: f for f in formats}


def merged(ours: dict, theirs: dict) -> dict:
    """TRaSH's rule with our settings and name kept"""
    # Our files' own fields and order (so a sync with no rule change changes
    # nothing); TRaSH's extra fields (trash_scores, descriptions) only where
    # the file had them
    out = {k: (ours[k] if k == "name" or k.startswith("mediaServer") or k not in theirs else theirs[k]) for k in ours}
    out.update({k: v for k, v in theirs.items() if k not in out and not k.startswith("trash_")})
    return out


def sync(folder: Path, commit: str, fetched: dict | None = None) -> list[str]:
    """Rewrite the files that changed; what happened, one line each"""
    report = []
    for app in ("sonarr", "radarr"):
        files = sorted((folder / app).glob("*.json"))
        mine = {f: json.loads(f.read_text()) for f in files}
        if not any("trash_id" in d for d in mine.values()):
            continue
        upstream = fetched[app] if fetched is not None else upstream_formats(commit, app)
        for f, ours in mine.items():
            tid = ours.get("trash_id")
            if not tid:
                continue
            theirs = upstream.get(tid)
            if theirs is None:
                report.append(f"{app}/{f.name}: no longer in TRaSH Guides (kept as it is; remove it if it's retired)")
                continue
            new = merged(ours, theirs)
            if theirs.get("name") != ours["name"]:
                report.append(f"{app}/{f.name}: TRaSH calls it {theirs.get('name')!r} now (kept {ours['name']!r})")
            if new != ours:
                f.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
                report.append(f"{app}/{f.name}: updated")
    if not any(line.endswith(": updated") for line in report):
        return report   # the README's commit only moves with a change
    readme = folder / "README.md"
    text = readme.read_text()
    updated = re.sub(r"commit [0-9a-f]{7,40}\)", f"commit {commit[:12]})", text, count=1)
    if updated != text:
        readme.write_text(updated)
    return report


def main(argv: list[str]) -> int:
    folder = Path.cwd() / "custom-formats"
    if not folder.is_dir():
        print("Run this from the media-server checkout (custom-formats/ not found)")
        return 2
    commit = argv[0] if argv else latest_commit()
    print(f"TRaSH Guides at {commit[:12]}")
    report = sync(folder, commit)
    print("\n".join(f"  {line}" for line in report) if report else "  Nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
