#!/usr/bin/env bash
# Materialise the official Agno Agent UI at the pinned commit and apply the
# two configuration changes the playground needs.
#
# The clone lands in ./agent-ui/ and is gitignored, matching the workspace's
# vendored-nested-clone convention (integrations/*/upstream-repo/, sdks/*/checkout/).
#
# Exactly TWO lines of upstream source are changed, both configuration, both
# enumerated below and re-applied idempotently. Nothing in the chat UI, the API
# client, the event handling, or the components is touched.
#
#   1. next.config.ts  -> add basePath '/ui'
#        The Nimble console owns "/" on the protected origin and embeds the
#        official UI at "/ui". basePath makes Next serve its pages and assets
#        under that prefix.
#
#   2. src/store.ts    -> default selectedEndpoint becomes the protected origin
#        Upstream defaults to http://localhost:7777, a different origin from the
#        UI, which would make the browser omit the session cookie. Pointing the
#        default at the edge origin makes every API call same-origin, which is
#        what lets the unmodified UI authenticate at all.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE="$HERE/agent-ui"
REPO="https://github.com/agno-agi/agent-ui"
PINNED_SHA="6dad9593fca6756e1813e4f4b3b2620be6377691"
EDGE_ORIGIN="${NIMBLE_EDGE_ORIGIN:-http://127.0.0.1:8800}"

log() { printf '\033[36m[ui-setup]\033[0m %s\n' "$*"; }

if [ ! -d "$CLONE/.git" ]; then
  log "cloning $REPO"
  git clone --quiet "$REPO" "$CLONE"
fi

log "pinning to $PINNED_SHA"
git -C "$CLONE" fetch --quiet origin "$PINNED_SHA" 2>/dev/null || git -C "$CLONE" fetch --quiet origin
# Hard reset rather than checkout so a re-run always restores pristine upstream
# before the overlay is re-applied; the overlay is then never applied twice.
git -C "$CLONE" checkout --quiet --detach "$PINNED_SHA"
git -C "$CLONE" reset --hard --quiet "$PINNED_SHA"
git -C "$CLONE" clean -fdq -e node_modules -e .next

ACTUAL_SHA="$(git -C "$CLONE" rev-parse HEAD)"
if [ "$ACTUAL_SHA" != "$PINNED_SHA" ]; then
  echo "FATAL: expected $PINNED_SHA, got $ACTUAL_SHA" >&2
  exit 1
fi
log "verified HEAD = $ACTUAL_SHA"

# ---------------------------------------------------------------- overlay 1/2
log "overlay 1/2: next.config.ts basePath='/ui'"
cat > "$CLONE/next.config.ts" <<'EOF'
import type { NextConfig } from 'next'

// PLAYGROUND OVERLAY (agentos-nimble): basePath only.
// The Nimble console owns "/" on the protected origin and embeds this app at
// "/ui". Everything else is upstream.
const nextConfig: NextConfig = {
  devIndicators: false,
  basePath: '/ui'
}

export default nextConfig
EOF

# ---------------------------------------------------------------- overlay 2/2
log "overlay 2/2: default selectedEndpoint -> $EDGE_ORIGIN"
python3 - "$CLONE/src/store.ts" "$EDGE_ORIGIN" <<'PY'
import re, sys
path, origin = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
needle = "selectedEndpoint: 'http://localhost:7777',"
if needle not in src:
    # Fail loudly: silently skipping would leave the UI pointed at a foreign
    # origin, where the session cookie is not sent and every call 401s.
    raise SystemExit(f"FATAL: expected default endpoint line not found in {path}")
src = src.replace(
    needle,
    f"selectedEndpoint: '{origin}', // PLAYGROUND OVERLAY: same-origin so the session cookie applies",
)
open(path, "w", encoding="utf-8").write(src)
print(f"  patched {path}")
PY

# Record exactly what diverges from upstream, so a reviewer can audit it in one command.
git -C "$CLONE" diff --stat > "$HERE/applied-overlay.diffstat"
git -C "$CLONE" diff > "$HERE/applied-overlay.patch"
log "overlay recorded in ui/applied-overlay.patch"
cat "$HERE/applied-overlay.diffstat"

if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  # pnpm 10, not whatever is on PATH. pnpm 11 stopped reading the `pnpm.overrides`
  # field in package.json, which this commit uses to pin picomatch for a security
  # advisory; under pnpm 11 the frozen install aborts with
  # ERR_PNPM_LOCKFILE_CONFIG_MISMATCH. Pinning the client is better than passing
  # --no-frozen-lockfile, which would rewrite the upstream lockfile and lose the
  # exact resolutions this commit was published with.
  log "installing dependencies (pnpm 10, frozen lockfile)"
  ( cd "$CLONE" && npx --yes pnpm@10 install --frozen-lockfile )
fi

log "done. Start it with: cd ui/agent-ui && pnpm dev"
