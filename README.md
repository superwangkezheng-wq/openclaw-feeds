# openclaw-feeds

Static feed artifacts for the OpenClaw v11.6 intelligence pipeline.

## Why this repository exists

The pipeline that reads these files is a **sealed release**. Its manifest is
verified on every run, so any in-place edit makes it refuse to start, and a real
change costs a full re-seal: rebuild, eight evidence items, seal, activate. That
ceremony is appropriate for changing how the pipeline *thinks*. It is absurdly
expensive for reacting to YouTube moving an endpoint.

Its transport layer also only accepts `https`, port 443, and publicly routable
addresses — a self-hosted service on `localhost` cannot be reached at all without
exposing it to the internet first.

`raw.githubusercontent.com` satisfies the transport rules, and this repository is
not sealed. So the arms race with the platforms lives **here**, and the pipeline
side is one URL per source. When a platform breaks, we fix a script and push.
Nothing downstream re-seals.

## The four legs

| Leg | What it does | Credentials | Runs in |
| --- | --- | --- | --- |
| `feed-x.json` | 96-hour rolling window over the upstream X feed | none | Actions |
| `feeds/youtube/*.xml` | Mirrors each channel's live RSS | none | Actions |
| `feeds/36kr-ai.xml` | Mirrors 36kr AI via public RSSHub instances | none | Actions |
| `feeds/bilibili/*.xml` | Uploads via a locally hosted RSSHub | SESSDATA, local only | this Mac |

### X — why a rolling window, not a copy

Upstream (`zarazhangrui/follow-builders`) publishes a **24-hour slice** once a day
at 06:17 UTC. Our pipeline reads at 01:30 UTC, so it always sees *yesterday's*
file — and on a Monday morning, that file covers Saturday.

Measured over two weeks, that slice swings between 5 and 19 builders purely from
this effect, with an empty `errors` array every single day. Upstream is healthy.
A 24-hour window is simply the wrong shape for a reader on a different clock.

So this accumulates slices into a **96-hour window**, matching the pipeline's own
`maxAgeHours.builder = 96`. It spans any weekend, and it is exactly as much
history as the reader can still use.

**A new mirror takes ~4 daily runs to fill.** The first runs report a short span
and say so explicitly — that is expected, not a fault.

Staleness is a **hard failure**: if upstream's `generatedAt` is older than 48h the
run exits non-zero rather than republishing a frozen file. A mirror that quietly
keeps serving old data is indistinguishable from a working one at the reading end,
which is the single worst thing this could do.

### YouTube — why mirror an endpoint that works

`feeds/videos.xml?channel_id=…` answers **200 from some egress addresses and 404
from others, in the same hour**. All 11 channels fetched cleanly on 2026-09-08;
other attempts from a different network path had returned 404.

The reader can only parse or fail, so a transient 404 becomes a source health
failure and an alert. Mirroring turns that into "slightly old data", which for a
daily digest is obviously the better trade. **No API key is used.** If the no-key
mirror proves unreliable, the documented upgrade is the YouTube Data API v3
`playlistItems.list` (1 unit per channel; 11 channels twice daily is 22 of the
10,000 free daily units) — but do not add a credential before the free path fails.

### 36kr — the most fragile leg

36kr's own feed is a dead SPA shell, so the only route is a public RSSHub
instance, and no single one is dependable: within four hours on 2026-09-08,
`rsshub.rssforever.com` went from serving 30 items to a 504 while three other
instances served the same 30 items. The script tries several in order and takes
the first that answers. Expect to add or remove hosts from that list over time.

### Bilibili — why it is local

The space API needs a WBI signature and a logged-in cookie. The cookie must not
leave this machine, so this leg does not run in Actions. `local/bilibili_capture.sh`
talks to a loopback-bound RSSHub container and pushes the result here.

Placeholder feeds are committed so the pipeline reads a parseable **empty** feed
rather than a 404 before the credential lands — "no new items" is something a
reader can act on; a transport failure is not.

Setup is in the header comment of `local/bilibili_capture.sh`. SESSDATA lives
about a month; when it expires the script fails loudly and names the file to fix.

## Failure behaviour

Every script fails loudly and never silently. Specifically:

- **X**: upstream unreachable, malformed, or older than 48h → non-zero exit, nothing published.
- **Mirrors**: a target that fails but has a previous copy keeps it and is reported as
  `MISS … keeping the copy from <time>` (exit 0 — a stale copy is survivable).
  A target that fails with *no* previous copy exits non-zero.
- **Bilibili**: missing credential file, dead container, or a response with no
  `<item>` (which is what an expired cookie looks like — not an HTTP error) all fail
  loudly without overwriting a good feed.

`state/` records per-target last-success times so "how old is this?" is always answerable.

## Consumed by

`config/ai_ict_news_sources_v11_6.json` in the sealed release. Each source there
pins an explicit `id`, so these URL changes did not alter any source identity and
no health history was lost.
