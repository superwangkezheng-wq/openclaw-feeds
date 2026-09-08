#!/bin/zsh
# Capture Bilibili uploads through a locally hosted RSSHub and publish them here.
#
# Why this leg is local rather than a GitHub Action: the space API needs a WBI
# signature and a logged-in cookie, and that cookie must not leave this machine.
# It is read from a file outside the repo and never committed.
#
# Why through RSSHub rather than talking to Bilibili directly: the signing scheme
# is an actively moving target. RSSHub tracks it; we do not want to. A container
# we can `docker pull` is the cheapest possible way to inherit that maintenance.
#
# Setup, once:
#   mkdir -p ~/.config/openclaw-feeds
#   cat > ~/.config/openclaw-feeds/bilibili.env <<'EOF'
#   BILIBILI_UID=<the number in your space.bilibili.com/<N> URL>
#   BILIBILI_SESSDATA=<the SESSDATA cookie value>
#   EOF
#   chmod 600 ~/.config/openclaw-feeds/bilibili.env
#   ./local/bilibili_capture.sh --start-container
#
set -euo pipefail

REPO="${0:A:h:h}"
ENV_FILE="${BILIBILI_ENV_FILE:-$HOME/.config/openclaw-feeds/bilibili.env}"
# Not 1200: this host already maps that port to the ragflow stack's
# Elasticsearch (docker-es01-1, 1200->9200), which answers 401 and would
# otherwise look like an RSSHub that refuses us.
RSSHUB_PORT="${RSSHUB_PORT:-1201}"
RSSHUB_URL="${RSSHUB_URL:-http://127.0.0.1:$RSSHUB_PORT}"
CONTAINER="${RSSHUB_CONTAINER:-openclaw-rsshub}"
MIDS=(73414544 519463151 1567748478)
MIN_BYTES=400

if [[ ! -r "$ENV_FILE" ]]; then
  print -u2 "FAIL credential file missing or unreadable: $ENV_FILE"
  print -u2 "     This leg cannot run without it. See the setup block at the top of this script."
  print -u2 "     Nothing was written; the previously published feeds are untouched."
  exit 1
fi
set -a; source "$ENV_FILE"; set +a
: "${BILIBILI_UID:?FAIL BILIBILI_UID is not set in $ENV_FILE}"
: "${BILIBILI_SESSDATA:?FAIL BILIBILI_SESSDATA is not set in $ENV_FILE}"

if [[ "${1:-}" == "--start-container" ]]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # Bound to loopback on purpose: this service holds a session cookie and has no
  # business being reachable from anywhere but this machine.
  docker run -d --name "$CONTAINER" --restart unless-stopped \
    -p "127.0.0.1:$RSSHUB_PORT:1200" \
    -e "BILIBILI_COOKIE_${BILIBILI_UID}=SESSDATA=${BILIBILI_SESSDATA}" \
    diygod/rsshub:latest
  print "started $CONTAINER; give it ~20s to boot, then run this script with no arguments"
  exit 0
fi

if ! curl -fsS --max-time 10 "$RSSHUB_URL/" >/dev/null 2>&1; then
  print -u2 "FAIL RSSHub is not answering at $RSSHUB_URL"
  print -u2 "     Start it with: $0 --start-container"
  exit 1
fi

failed=()
for mid in "${MIDS[@]}"; do
  dest="$REPO/feeds/bilibili/$mid.xml"
  tmp="$(mktemp)"
  # RSSHub initialises a route on its first request, so a cold one can exceed the
  # timeout while the same route answers in milliseconds a moment later -- measured
  # here at 1.14s cold against 0.02s warm. One retry distinguishes a cold start from
  # a real outage; anything past that is still reported as a failure.
  fetched=0
  for attempt in 1 2; do
    if curl -fsS --max-time 60 -o "$tmp" "$RSSHUB_URL/bilibili/user/video/$mid" 2>"$tmp.err"; then
      fetched=1; break
    fi
    (( attempt == 1 )) && sleep 3
  done
  if (( fetched )); then
    size=$(stat -f%z "$tmp")
    # A cookie that has expired comes back as a small error document, not as an
    # HTTP failure. Overwriting a good feed with one of those is the whole risk.
    if (( size >= MIN_BYTES )) && grep -q "<item>" "$tmp"; then
      mv "$tmp" "$dest"
      print "OK   $mid ($size bytes)"
      continue
    fi
    print -u2 "MISS $mid: response has no items (${size}B) -- cookie expired, or the route broke"
  else
    print -u2 "MISS $mid: RSSHub request failed after 2 attempts: $(tr -d '\n' < "$tmp.err" | tail -c 200)"
  fi
  rm -f "$tmp" "$tmp.err"
  failed+=("$mid")
done

if (( ${#failed[@]} )); then
  print -u2 "FAIL ${#failed[@]}/${#MIDS[@]} bilibili source(s) failed: ${failed[*]}"
  # Which diagnosis is right is decided by the other sources, not guessed. If any
  # of them returned items the cookie demonstrably works, and blaming it would send
  # the reader off to fix something that is not broken. A confident wrong diagnosis
  # costs more than none.
  if (( ${#failed[@]} == ${#MIDS[@]} )); then
    print -u2 "     Every source failed, so the shared cause is most likely the credential:"
    print -u2 "     refresh SESSDATA in $ENV_FILE (~1 month lifetime), then $0 --start-container && $0"
  else
    print -u2 "     $(( ${#MIDS[@]} - ${#failed[@]} )) other source(s) returned items, so the credential works."
    print -u2 "     This is per-source: a cold RSSHub route, an upstream hiccup, or a space with"
    print -u2 "     no videos. Re-run once before investigating further."
  fi
  exit 1
fi

cd "$REPO"
if [[ -n "$(git status --porcelain feeds/bilibili)" ]]; then
  git add feeds/bilibili
  git -c user.name="openclaw-feeds" -c user.email="tian1617@sohu.com" \
    commit -q -m "chore(bilibili): refresh uploads feeds"
  # The Actions harvester pushes to this same branch twice a day. Without a rebase
  # the first such push makes every later run here a non-fast-forward rejection,
  # stacking commits on a stale base until a human intervenes -- the leg would go
  # quiet from that moment on.
  if ! git pull --rebase --autostash -q; then
    print -u2 "FAIL cannot rebase onto origin/main; the feeds were captured but not published"
    exit 1
  fi
  git push -q
  print "pushed"
else
  print "no change"
fi
