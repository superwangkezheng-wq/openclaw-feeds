#!/usr/bin/env python3
"""Mirror the upstream X feed into a 96-hour rolling window.

Upstream (`zarazhangrui/follow-builders`) publishes a 24-hour slice once a day at
06:17 UTC. The pipeline that consumes it reads at 01:30 UTC, so it always sees
yesterday's file -- and on a Monday morning that file covers Saturday. Measured
over two weeks the slice swings between 5 and 19 builders for that reason alone,
with an empty `errors` array every single day. Nothing is broken upstream; a
24-hour window is simply the wrong shape for a reader that runs on a different
clock.

So we accumulate. The pipeline ages builder items out at 96 hours
(`maxAgeHours.builder`), so a 96-hour window is exactly what it can still use,
and it spans any weekend. Four consecutive daily slices are needed before the
window is full -- a first run is expected to look thin, and says so.

The one thing this must never do is go quiet. A mirror that keeps serving a
frozen file is indistinguishable from a working one at the reading end, which is
why staleness is a hard failure here rather than a warning.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UPSTREAM = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json"
WINDOW_HOURS = 96
MAX_UPSTREAM_AGE_HOURS = 48
USER_AGENT = "openclaw-feeds/1.0 (+https://github.com/superwangkezheng-wq/openclaw-feeds)"

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state" / "x-window.json"
OUTPUT = REPO / "feed-x.json"


def fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def tweet_key(tweet: dict) -> str | None:
    """Identity of a tweet, preferring the id upstream already assigns."""
    for field in ("id", "url"):
        value = tweet.get(field)
        if isinstance(value, str) and value.strip():
            return f"{field}:{value.strip()}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="ISO8601 override, for tests")
    args = parser.parse_args()

    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"FAIL --now is not a parseable timestamp: {args.now}", file=sys.stderr)
        return 2

    try:
        payload = json.loads(fetch(UPSTREAM))
    except Exception as error:  # noqa: BLE001 -- the reason is reported, not swallowed
        print(f"FAIL cannot fetch upstream {UPSTREAM}: {error}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict) or not isinstance(payload.get("x"), list):
        print("FAIL upstream payload is not an object with an 'x' list", file=sys.stderr)
        return 1

    generated_at = parse_time(payload.get("generatedAt"))
    if generated_at is None:
        print("FAIL upstream has no parseable generatedAt", file=sys.stderr)
        return 1
    age_hours = (now - generated_at).total_seconds() / 3600.0
    if age_hours > MAX_UPSTREAM_AGE_HOURS:
        print(
            f"FAIL upstream is stale: generatedAt={payload['generatedAt']} "
            f"age={age_hours:.1f}h max={MAX_UPSTREAM_AGE_HOURS}h. "
            "Refusing to republish a frozen mirror as if it were fresh.",
            file=sys.stderr,
        )
        return 1

    # Load the accumulator. A missing one is a first run, not an error.
    state: dict = {"handles": {}}
    if STATE.is_file():
        try:
            loaded = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("handles"), dict):
                state = loaded
        except json.JSONDecodeError as error:
            print(f"FAIL state file {STATE} is corrupt: {error}", file=sys.stderr)
            return 1

    handles: dict = state["handles"]
    added = 0
    for row in payload["x"]:
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "").strip()
        if not handle:
            continue
        slot = handles.setdefault(handle.lower(), {"handle": handle, "name": "", "tweets": {}})
        # Upstream is authoritative for display fields; refresh them every run.
        slot["handle"] = handle
        for field in ("name", "bio", "source"):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                slot[field] = value
        for tweet in row.get("tweets") or []:
            if not isinstance(tweet, dict):
                continue
            key = tweet_key(tweet)
            if key is None:
                continue
            if key not in slot["tweets"]:
                added += 1
            slot["tweets"][key] = tweet

    # Age out. A tweet with no parseable timestamp is dropped rather than kept
    # forever: an item that can never expire would accumulate without bound.
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    kept_handles: dict = {}
    oldest: datetime | None = None
    newest: datetime | None = None
    dropped = 0
    for key, slot in handles.items():
        fresh = {}
        for tkey, tweet in slot["tweets"].items():
            created = parse_time(tweet.get("createdAt"))
            if created is None or created < cutoff:
                dropped += 1
                continue
            fresh[tkey] = tweet
            oldest = created if oldest is None or created < oldest else oldest
            newest = created if newest is None or created > newest else newest
        if fresh:
            kept = dict(slot)
            kept["tweets"] = fresh
            kept_handles[key] = kept
    state["handles"] = kept_handles

    # Emit in the shape the sealed parser reads (collector.py `_builder_items`):
    # an object with "x", each row carrying "handle"/"name" and a "tweets" list.
    rows = []
    for key in sorted(kept_handles):
        slot = kept_handles[key]
        tweets = sorted(
            slot["tweets"].values(),
            key=lambda item: (str(item.get("createdAt") or ""), str(item.get("id") or "")),
            reverse=True,
        )
        row = {"handle": slot.get("handle", key), "name": slot.get("name", ""), "tweets": tweets}
        for field in ("source", "bio"):
            if slot.get(field):
                row[field] = slot[field]
        rows.append(row)

    total_tweets = sum(len(row["tweets"]) for row in rows)
    span_hours = (newest - oldest).total_seconds() / 3600.0 if oldest and newest else 0.0
    output = {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "lookbackHours": WINDOW_HOURS,
        "upstream": {"url": UPSTREAM, "generatedAt": payload.get("generatedAt"), "ageHours": round(age_hours, 2)},
        "x": rows,
        "stats": {"xBuilders": len(rows), "xTweets": total_tweets, "windowSpanHours": round(span_hours, 2)},
    }

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"OK upstream {len(payload['x'])} builders (age {age_hours:.1f}h) "
        f"-> window {len(rows)} builders / {total_tweets} tweets, "
        f"+{added} new, -{dropped} aged out, span {span_hours:.1f}h of {WINDOW_HOURS}h"
    )

    # An upstream that still has rows while our window comes out empty is not a slow
    # news day -- it is this script failing to understand the data. A field rename in
    # `createdAt`, `id` or `url` would send every tweet through the skip path or the
    # age-out path, and the result publishes with a fresh generatedAt and an empty `x`.
    # Downstream cannot catch that: builder_feed declares no minimumArtifacts, so the
    # consumer grades zero artifacts as a healthy fetch. This is the only place that
    # can tell the difference, so it has to be the place that refuses.
    if payload["x"] and total_tweets == 0:
        print(
            f"FAIL upstream carried {len(payload['x'])} builder row(s) but the window is empty. "
            "Refusing to publish a hollow feed -- most likely the upstream field names "
            "(createdAt / id / url) moved and every tweet is being skipped.",
            file=sys.stderr,
        )
        return 1
    if span_hours < WINDOW_HOURS - 24:
        print(
            f"NOTE the window is not full yet ({span_hours:.1f}h of {WINDOW_HOURS}h). "
            "Upstream publishes 24h slices, so it takes ~4 daily runs to fill. This is expected on a new mirror."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
