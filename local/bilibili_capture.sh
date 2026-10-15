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
# Pin the admitted official release, including its maintained browser fallback.
# The slim image answers homepage health checks but cannot execute browser fallbacks.
RSSHUB_IMAGE="${RSSHUB_IMAGE:-diygod/rsshub:chromium-bundled-b4a7c36fbcb24e84452a74d772a59fd73d4588c3}"
MIDS=(73414544 519463151 1567748478)
MIN_BYTES=400

STATUS_FILE="${BILIBILI_STATUS_FILE:-$HOME/Library/Application Support/OpenClaw/FeedRuntime/bilibili-status.json}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
successful=()
failed=()
CAPTURE_REQUESTED=1
[[ "${1:-}" != "--start-container" ]] || CAPTURE_REQUESTED=0
write_capture_status() {
  (( CAPTURE_REQUESTED )) || return 0
  /opt/homebrew/bin/python3.11 - "$STATUS_FILE" "$STARTED_AT" "$1" "$2" "${(j:,:)successful}" "${(j:,:)failed}" <<'CAPTURE_PY'
import json,os,sys,tempfile
from pathlib import Path
from datetime import datetime,timezone
path=Path(sys.argv[1]);started=sys.argv[2];published=sys.argv[3]=='1';detail=sys.argv[4]
ok=[x for x in sys.argv[5].split(',') if x];failed=[x for x in sys.argv[6].split(',') if x]
try:prior=json.loads(path.read_text())
except (OSError,ValueError):prior={}
last=prior.get('lastFetchedAtBySource',{}) if isinstance(prior,dict) else {};since=prior.get('failedSinceBySource',{}) if isinstance(prior,dict) else {}
last=dict(last) if isinstance(last,dict) else {};since=dict(since) if isinstance(since,dict) else {}
now=datetime.now(timezone.utc)
for uid in ok:last[uid]=started;since.pop(uid,None)
for uid in failed:since.setdefault(uid,started)
stale=[]
for uid in failed:
 try:
  stamp=datetime.fromisoformat(since[uid].replace('Z','+00:00'))
  if stamp.utcoffset() is None or (now-stamp).total_seconds()>=72*3600:stale.append(uid)
 except (ValueError,TypeError):stale.append(uid)
result='fail' if not published or not ok or stale else 'warn' if failed else 'ok'
roots=[]
if detail.startswith('credential_'):roots=['feeds/credentials']
elif detail=='runtime_unavailable':roots=['feeds/runtime/openclaw-rsshub']
else:
 if not published and detail!='all_sources_failed':roots.append('feeds/publication')
 roots.extend('feeds/source/'+uid for uid in (failed if not ok else stale))
payload={'schemaVersion':1,'producer':'feeds-bilibili','result':result,'startedAt':started,'finishedAt':now.isoformat(),'published':published,'detail':detail,'fetchedSources':ok,'failedSources':failed,'lastFetchedAtBySource':last,'failedSinceBySource':since,'staleFailedSources':stale,'rootCauseIds':roots,'rootCauseIdsComplete':result=='fail' and bool(roots)}
path.parent.mkdir(parents=True,exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix='.bilibili-',dir=path.parent)
try:
 with os.fdopen(fd,'w') as f:json.dump(payload,f,ensure_ascii=False,indent=2);f.write('\n')
 os.replace(tmp,path)
finally:
 if os.path.exists(tmp):os.unlink(tmp)
CAPTURE_PY
}

if [[ ! -r "$ENV_FILE" ]]; then
  print -u2 "FAIL credential file missing or unreadable: $ENV_FILE"
  print -u2 "     This leg cannot run without it. See the setup block at the top of this script."
  print -u2 "     Nothing was written; the previously published feeds are untouched."
  failed=("${MIDS[@]}")
  write_capture_status 0 credential_file_unavailable
  exit 1
fi
set -a; source "$ENV_FILE"; set +a
if [[ -z "${BILIBILI_UID:-}" || -z "${BILIBILI_SESSDATA:-}" ]]; then
  print -u2 "FAIL required Bilibili session fields are missing"
  failed=("${MIDS[@]}")
  write_capture_status 0 credential_fields_unavailable
  exit 1
fi

if [[ "${1:-}" == "--start-container" ]]; then
  # Resolve the image before touching a serving container, and retain rollback.
  docker image inspect "$RSSHUB_IMAGE" >/dev/null 2>&1 || docker pull "$RSSHUB_IMAGE"
  # Functional preflight: a landing-page 200 cannot prove the browser can run.
  docker run --rm --entrypoint /bin/sh "$RSSHUB_IMAGE" -c '
    browser="$(sed -n "s/^CHROMIUM_EXECUTABLE_PATH=//p" /app/.env | head -n 1)"
    test -n "$browser" && test -x "$browser" &&
      "$browser" --headless --no-sandbox --disable-gpu --dump-dom about:blank >/dev/null 2>&1
  '
  previous=""
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    previous="${CONTAINER}-rollback-$(date +%Y%m%d%H%M%S)"
    docker stop "$CONTAINER" >/dev/null
    if ! docker rename "$CONTAINER" "$previous"; then
      docker start "$CONTAINER" >/dev/null
      exit 1
    fi
  fi
  # Bound to loopback on purpose: this service holds a session cookie and has no
  # business being reachable from anywhere but this machine.
  #
  # --health-*: the image ships with no HEALTHCHECK of its own, which is what let
  # this container run undetected for six days after 2026-09-08 with no restart
  # signal at all -- the production guards that watch the other five containers
  # on this host all key off dockerHealthcheck, and an entry with no healthcheck
  # to report is indistinguishable from one that was never declared. Curl against
  # the container's own internal port (1200, not the host-mapped 1201): verified
  # both return 200 with RSSHub's landing page, but the healthcheck has to probe
  # the same interface `docker inspect`'s Health block reports on, which is the
  # container's own network namespace. start-period 30s covers the ~20s boot this
  # script already documented above.
  if ! docker run -d --name "$CONTAINER" --restart unless-stopped \
    -p "127.0.0.1:$RSSHUB_PORT:1200" \
    -e "BILIBILI_COOKIE_${BILIBILI_UID}=SESSDATA=${BILIBILI_SESSDATA}" \
    --health-cmd 'browser="$(sed -n "s/^CHROMIUM_EXECUTABLE_PATH=//p" /app/.env | head -n 1)"; test -n "$browser" && test -x "$browser" && curl -sf --max-time 5 -o /dev/null http://127.0.0.1:1200/'  \
    --health-interval 30s \
    --health-timeout 10s \
    --health-start-period 30s \
    --health-retries 3 \
    "$RSSHUB_IMAGE"; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [[ -n "$previous" ]]; then
      docker rename "$previous" "$CONTAINER"
      docker start "$CONTAINER" >/dev/null
    fi
    exit 1
  fi
  [[ -z "$previous" ]] || print "rollback retained: $previous"
  print "started $CONTAINER; give it ~20s to boot, then run this script with no arguments"
  exit 0
fi

if ! curl -fsS --max-time 10 "$RSSHUB_URL/" >/dev/null 2>&1; then
  print -u2 "FAIL RSSHub is not answering at $RSSHUB_URL"
  print -u2 "     Start it with: $0 --start-container"
  failed=("${MIDS[@]}")
  write_capture_status 0 runtime_unavailable
  exit 1
fi

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
      rm -f "$tmp.err"
      print "OK   $mid ($size bytes)"
      successful+=("$mid")
      continue
    fi
    print -u2 "MISS $mid: response has no items (${size}B) -- cookie expired, or the route broke"
  else
    print -u2 "MISS $mid: RSSHub request failed after 2 attempts: $(tr -d '\n' < "$tmp.err" | tail -c 200)"
  fi
  rm -f "$tmp" "$tmp.err"
  failed+=("$mid")
done

if (( ${#failed[@]} == ${#MIDS[@]} )); then
  print -u2 "FAIL all bilibili sources failed; retained the previously validated feeds"
  print -u2 "     Check the shared runtime, current session and upstream response; no credential failure is assumed."
  write_capture_status 0 all_sources_failed
  exit 1
elif (( ${#failed[@]} )); then
  print -u2 "WARN ${#failed[@]}/${#MIDS[@]} sources unavailable (${failed[*]}); publishing validated sources and retaining their last good peers"
fi

cd "$REPO"
if [[ -n "$(git status --porcelain feeds/bilibili)" ]]; then
  git add feeds/bilibili
  if ! git -c user.name="openclaw-feeds" -c user.email="tian1617@sohu.com" commit -q -m "chore(bilibili): refresh validated uploads feeds"; then
    write_capture_status 0 git_commit_failed
    exit 1
  fi
fi
# A previously failed push remains pending even if this capture has no new diff.
if ! git pull --rebase --autostash -q; then
  print -u2 "FAIL cannot rebase onto origin/main; validated feeds retained locally"
  write_capture_status 0 git_pull_failed
  exit 1
fi
if ! pending="$(git rev-list --count '@{upstream}..HEAD')"; then
  write_capture_status 0 git_upstream_unavailable
  exit 1
fi
if (( pending > 0 )); then
  if ! git push -q; then
    write_capture_status 0 git_push_failed
    exit 1
  fi
  print "pushed"
else
  print "already published"
fi
write_capture_status 1 validated_sources_published
