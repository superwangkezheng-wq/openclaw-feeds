#!/usr/bin/env python3
"""Mirror upstream feeds to static files, keeping the last good copy on failure.

Two of the sources this pipeline reads are unreliable in ways the pipeline cannot
do anything useful with. YouTube's `feeds/videos.xml` endpoint answers 200 from
some egress addresses and 404 from others, in the same hour. The 36kr feed is
served by a public third-party RSSHub instance that can simply be down. In both
cases the reader's only options are "parse it" or "fail", and a transient 404
becomes a source health failure and an alert.

Mirroring converts that into something better: on success the file is refreshed,
on failure the previous file stays exactly as it was. A reader gets slightly old
data instead of nothing, which is the correct trade for a daily digest.

What it must NOT do is let a permanent outage look like a slow news day. So a
miss is loud -- non-zero exit, named in the summary, and the per-target last
success time is recorded so age is always answerable.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "openclaw-feeds/1.0 (+https://github.com/superwangkezheng-wq/openclaw-feeds)"
REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state" / "mirror-state.json"

YOUTUBE_CHANNELS = {
    "UCXUPKJO5MZQN11PqgIvyuvQ": "Andrej Karpathy",
    "UCZHmQk67mSJgfCCTn7xBfew": "Yannic Kilcher",
    "UC_CzsS7UTjcxJ-xXp1ftxtA": "Sebastian Raschka",
    "UCawZsQWqfGSbCI5yjkdVkTA": "Matthew Berman",
    "UCcIXc5mJsHVYTZR1maL5l9w": "DeepLearningAI",
    "UCIDll3SRcbHwwcXbrwvBZNw": "橘鸦Juya",
    "UCbfYPyITQ-7l4upoX8nvctg": "Two Minute Papers",
    "UC1LpsuAUaKoMzzJSEt5WImw": "Asianometry",
    "UCRPdsCVuH53rcbTcEkuY4uQ": "Moore's Law Is Dead",
    "UC2LCc4VvMYj-6Kqe09avwow": "Semiconductor Engineering",
    "UCdp4_l1vPmpN-gDbUwhaRUQ": "Branch Education",
}

# Minimum plausible body. Anything smaller is an error page or an empty shell,
# and overwriting a good file with one of those is the failure this guards.
MIN_BYTES = 400


# 36kr killed its own RSS -- the direct feed is now an empty SPA shell -- so the
# only route left is a public RSSHub instance, and no single one of those is
# dependable: measured within four hours on 2026-09-08, rssforever went from
# serving 30 items to a 504, while three others served the same 30 items fine.
# Listing several and taking the first that answers is not a workaround for a
# flaky host, it is the correct shape for a resource that is only ever available
# from a rotating set of volunteer mirrors.
RSSHUB_36KR = [
    f"https://{host}/36kr/information/AI"
    for host in (
        "rss.injahow.cn",
        "hub.slarker.me",
        "rsshub.ktachibana.party",
        "rsshub.rssforever.com",
        "rsshub.pseudoyu.com",
    )
]


def targets() -> list[tuple[str, list[str], Path]]:
    items: list[tuple[str, list[str], Path]] = []
    for channel_id in YOUTUBE_CHANNELS:
        items.append((
            f"youtube/{channel_id}",
            [f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"],
            REPO / "feeds" / "youtube" / f"{channel_id}.xml",
        ))
    items.append(("36kr-ai", RSSHUB_36KR, REPO / "feeds" / "36kr-ai.xml"))
    return items


def fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--only", help="substring filter over target keys")
    args = parser.parse_args()

    state: dict = {}
    if STATE.is_file():
        try:
            loaded = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except json.JSONDecodeError as error:
            print(f"FAIL state file {STATE} is corrupt: {error}", file=sys.stderr)
            return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    refreshed: list[str] = []
    kept: list[str] = []
    missing: list[str] = []

    for key, urls, path in targets():
        if args.only and args.only not in key:
            continue
        entry = state.setdefault(key, {})
        body: bytes | None = None
        used = ""
        attempts: list[str] = []
        for url in urls:
            try:
                candidate = fetch(url, args.timeout)
                if len(candidate) < MIN_BYTES:
                    raise RuntimeError(f"body too small ({len(candidate)}B < {MIN_BYTES}B)")
            except Exception as error:  # noqa: BLE001 -- reported, never swallowed
                attempts.append(f"{url} -> {error}")
                continue
            body, used = candidate, url
            break

        if body is None:
            detail = "; ".join(attempts)
            entry["lastError"] = f"{now}: {detail}"
            if path.is_file():
                kept.append(key)
                print(f"MISS {key}: all {len(urls)} source(s) failed -- keeping the copy from "
                      f"{entry.get('lastSuccess', 'unknown')}. {detail}")
            else:
                missing.append(key)
                print(f"MISS {key}: all {len(urls)} source(s) failed and no previous copy exists. {detail}",
                      file=sys.stderr)
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.is_file() else b""
        if previous != body:
            path.write_bytes(body)
        refreshed.append(key)
        entry["lastSuccess"] = now
        entry["bytes"] = len(body)
        entry["via"] = used
        entry.pop("lastError", None)
        if attempts:
            print(f"OK   {key}: served by {used} after {len(attempts)} failure(s)")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"\nrefreshed {len(refreshed)}, kept-stale {len(kept)}, missing-entirely {len(missing)}")
    if kept:
        print("  kept stale: " + ", ".join(sorted(kept)))
    if missing:
        print("  MISSING:    " + ", ".join(sorted(missing)), file=sys.stderr)
    # A stale copy is survivable and reported; no copy at all is not.
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
