#!/usr/bin/env bash
# One command for the whole demo video: serve the site (design/landing-1: the overview and the
# recorded runs), capture every scene, synthesize the narration, assemble the MP4s, stop the server.
#
#   tools/video/render.sh                       # full render
#   VOICE=Daniel RATE=175 tools/video/render.sh  # another macOS voice / speaking rate
#   ASSEMBLE_ONLY=1 tools/video/render.sh        # re-run ffmpeg on the last capture (no server)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/out"
SITE="$REPO/design/landing-1"
LOGS="$OUT/logs"
APP_PORT=3074

export VOICE="${VOICE:-Samantha}"
export RATE="${RATE:-180}"
ASSEMBLE_ONLY="${ASSEMBLE_ONLY:-0}"

mkdir -p "$OUT/work/tmp" "$LOGS"
# everything this script and its children print also goes to out/logs/render.log (a fresh one per run)
: >"$LOGS/render.log"
exec > >(tee -a "$LOGS/render.log") 2>&1
echo "[video] render started $(date '+%Y-%m-%d %H:%M:%S') (VOICE=${VOICE:-Samantha} RATE=${RATE:-180} ASSEMBLE_ONLY=${ASSEMBLE_ONLY:-0})"
# keep every temporary file of ffmpeg, say, node and Chromium inside out/
export TMPDIR="$OUT/work/tmp"

say_step() { printf '\n[video] == %s\n' "$*"; }

# The capture is real time: an idle sleep in the middle ruins the scene, so keep the Mac awake
# for as long as this script runs (caffeinate exits with it).
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -dims -w $$ &
fi
die() { printf '[video] ERROR: %s\n' "$*" >&2; exit 1; }

for tool in node pnpm python3 ffmpeg ffprobe say curl lsof; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
[ -f "$SITE/run.html" ] || die "the site is missing: $SITE/run.html"

# ---------------------------------------------------------------- dependencies (own pnpm project)
if [ ! -d "$HERE/node_modules/playwright" ]; then
  say_step "installing tools/video dependencies (playwright, pinned)"
  (cd "$HERE" && pnpm install --frozen-lockfile)
fi

if [ "$ASSEMBLE_ONLY" = 1 ]; then
  say_step "assembling the last capture"
  node "$HERE/src/render.mjs" assemble
  exit 0
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
port_busy "$APP_PORT" && die "port $APP_PORT is already in use; stop that process first (this script only stops what it starts)"

# ---------------------------------------------------------------- servers, stopped on any exit
PIDS=()
descendants() {
  local kid
  for kid in $(pgrep -P "$1" 2>/dev/null); do
    descendants "$kid"
    echo "$kid"
  done
}
stop_servers() {
  local pid all p
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    all="$(descendants "$pid") $pid"
    for p in $all; do kill -TERM "$p" 2>/dev/null || true; done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    for p in $all; do kill -KILL "$p" 2>/dev/null || true; done
  done
  PIDS=()
}
trap stop_servers EXIT
trap 'exit 130' INT TERM

# ---------------------------------------------------------------- the site, served as files
say_step "serving design/landing-1 on :$APP_PORT"
python3 -m http.server "$APP_PORT" --bind 127.0.0.1 --directory "$SITE" >"$LOGS/site.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 60); do
  [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/run.html")" = 200 ] && break
  sleep 0.25
done
[ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/run.html")" = 200 ] || { tail -20 "$LOGS/site.log"; die "the site did not come up"; }

say_step "narration + capture (the engine is logged below)"
node "$HERE/src/render.mjs" capture

say_step "stopping the server"
stop_servers

say_step "assembling"
node "$HERE/src/render.mjs" assemble
echo "[video] render finished $(date '+%Y-%m-%d %H:%M:%S')"
