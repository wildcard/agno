#!/usr/bin/env bash
# Start the playground.
#
#   ./scripts/run.sh test     deterministic, non-billable (default)
#   ./scripts/run.sh live     real Nimble + real LLM
#
# Processes, and why there are four:
#
#   edge   :8800  the ONE origin the browser uses. Authenticates the human,
#                 overwrites the identity header, proxies everything else.
#   origin :8801  AgentOS. Trusts only a signed edge assertion. Never exposed.
#   ui     :3000  the official Agent UI (Next.js dev), reached only via the edge.
#   fake   :9411  test mode only: the local Nimble Agent API V2 stand-in.
#
# Only :8800 is meant to be opened in a browser. The others are reachable on
# loopback for debugging, and the origin refuses anonymous requests anyway.
set -euo pipefail

MODE="${1:-test}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$HERE/backend"
UI_DIR="$HERE/ui/agent-ui"
LOGS="$HERE/.logs"
mkdir -p "$LOGS"

: "${NIMBLE_EDGE_PORT:=8800}"
: "${NIMBLE_ORIGIN_PORT:=8801}"
: "${NIMBLE_UI_PORT:=3000}"
: "${FAKE_NIMBLE_PORT:=9411}"

log() { printf '\033[36m[run]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[run] %s\033[0m\n' "$*" >&2; exit 1; }

# Resolve the interpreter that has agno + nimble-python. The repo venv already
# resolves `agno` to this checkout's libs/agno, which is what makes the
# playground exercise the local published toolkit rather than a released wheel.
PY="${NIMBLE_PLAYGROUND_PYTHON:-$HERE/../../.venv/bin/python}"
[ -x "$PY" ] || die "python not found at $PY (set NIMBLE_PLAYGROUND_PYTHON)"

# The edge secret is generated per run unless supplied. Generating it means a
# forgotten export cannot leave a well-known secret in place.
if [ -z "${NIMBLE_EDGE_SECRET:-}" ]; then
  NIMBLE_EDGE_SECRET="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  log "generated a per-run edge secret (not printed, not persisted)"
fi
export NIMBLE_EDGE_SECRET NIMBLE_EDGE_PORT NIMBLE_ORIGIN_PORT NIMBLE_UI_PORT

PIDS=()
cleanup() {
  log "stopping"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for() { # url, label
  for _ in $(seq 1 120); do
    curl -sf -o /dev/null "$1" && return 0
    sleep 0.5
  done
  die "$2 did not come up: $1 (see $LOGS)"
}

case "$MODE" in
  test)
    export NIMBLE_PLAYGROUND_MODE=test
    export NIMBLE_BASE_URL="http://127.0.0.1:${FAKE_NIMBLE_PORT}"
    # Any non-empty value satisfies the fake; no real credential is involved.
    export NIMBLE_API_KEY="${NIMBLE_API_KEY:-test-mode-local-key}"
    log "TEST MODE — deterministic, no billable Nimble call, scripted driver"
    ( cd "$BACKEND" && PYTHONPATH="$BACKEND" "$PY" -m uvicorn fake_nimble.server:app \
        --host 127.0.0.1 --port "$FAKE_NIMBLE_PORT" --log-level warning \
        > "$LOGS/fake-nimble.log" 2>&1 ) &
    PIDS+=($!)
    wait_for "http://127.0.0.1:${FAKE_NIMBLE_PORT}/__fake/live" "fake Nimble"
    log "fake Nimble up on :${FAKE_NIMBLE_PORT}"
    ;;
  live)
    export NIMBLE_PLAYGROUND_MODE=live
    [ -n "${NIMBLE_API_KEY:-}" ] || die "live mode needs NIMBLE_API_KEY (or set a per-session override in the console)"
    [ -n "${OPENAI_API_KEY:-}" ] || die "live mode needs OPENAI_API_KEY for the driving model"
    unset NIMBLE_BASE_URL 2>/dev/null || true
    log "LIVE MODE — real Nimble runs are billable"
    log "effort: omitted by default, preserving the agent/template default"
    ;;
  *) die "unknown mode '$MODE' (expected: test | live)" ;;
esac

log "status-poll interval: ${NIMBLE_POLL_INTERVAL_SECONDS:-10}s (Nimble run status only; SSE is not throttled)"

( cd "$BACKEND" && PYTHONPATH="$BACKEND" "$PY" -m uvicorn nimble_agentos.app:build_origin_app \
    --factory --host 127.0.0.1 --port "$NIMBLE_ORIGIN_PORT" --log-level info \
    > "$LOGS/origin.log" 2>&1 ) &
PIDS+=($!)
wait_for "http://127.0.0.1:${NIMBLE_ORIGIN_PORT}/__origin/live" "AgentOS origin"
log "AgentOS origin up on :${NIMBLE_ORIGIN_PORT} (refuses anonymous requests)"

[ -d "$UI_DIR/node_modules" ] || die "official Agent UI not installed — run ui/setup.sh first"
( cd "$UI_DIR" && npx --yes pnpm@10 dev --port "$NIMBLE_UI_PORT" > "$LOGS/agent-ui.log" 2>&1 ) &
PIDS+=($!)

( cd "$BACKEND" && PYTHONPATH="$BACKEND" "$PY" -m uvicorn nimble_agentos.edge:build_edge_app \
    --factory --host 127.0.0.1 --port "$NIMBLE_EDGE_PORT" --log-level info \
    > "$LOGS/edge.log" 2>&1 ) &
PIDS+=($!)
wait_for "http://127.0.0.1:${NIMBLE_EDGE_PORT}/" "edge"

log ""
log "Open  ->  http://127.0.0.1:${NIMBLE_EDGE_PORT}/"
log "Logs  ->  $LOGS/{edge,origin,agent-ui,fake-nimble}.log"
log "Ctrl-C to stop."
wait
