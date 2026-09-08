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
RSSHUB_URL="${RSSHUB_URL:-http://127.0.0.1:1200}"
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
    -p 127.0.0.1:1200:1200 \
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
  if curl -fsS --max-time 60 -o "$tmp" "$RSSHUB_URL/bilibili/user/video/$mid" 2>/dev/null; then
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
    print -u2 "MISS $mid: RSSHub request failed"
  fi
  rm -f "$tmp"
  failed+=("$mid")
done

if (( ${#failed[@]} )); then
  print -u2 "FAIL ${#failed[@]}/${#MIDS[@]} bilibili source(s) failed: ${failed[*]}"
  print -u2 "     Most likely the SESSDATA in $ENV_FILE has expired (~1 month lifetime)."
  print -u2 "     Refresh it, then: $0 --start-container && $0"
  exit 1
fi

cd "$REPO"
if [[ -n "$(git status --porcelain feeds/bilibili)" ]]; then
  git add feeds/bilibili
  git -c user.name="openclaw-feeds" -c user.email="tian1617@sohu.com" \
    commit -q -m "chore(bilibili): refresh uploads feeds"
  git push -q
  print "pushed"
else
  print "no change"
fi
