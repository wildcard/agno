# Nimble × AgentOS playground

A local implementation spike that runs the published **`NimbleAgentTools`** toolkit inside
Agno's own native surface: an **AgentOS** backend plus the **official `agno-agi/agent-ui`**
frontend, behind a single protected origin.

The point is not to build a Nimble dashboard. It is to show a maintainer or a teammate
what Nimble's Web Search Agent looks like *in Agno's own interaction model* — the native
chat timeline narrating the `start → status → result` lifecycle and ending in a grounded,
cited answer.

> **Status: local spike.** Nothing here is deployed, committed, or published. It does not
> touch the existing Cloudflare Worker.

---

## What this exercises

The toolkit under test is the actual published file, imported unmodified:

- `libs/agno/agno/tools/nimble_agent.py`
- `libs/agno/tests/unit/tools/test_nimble_agent.py`

Nothing in this directory patches, subclasses, or wraps it. The per-request values the
playground varies (API key, agent id, effort) are constructor arguments the toolkit
already exposes.

---

## Architecture

```
                         ┌── the ONE origin the browser uses ──┐
  browser  ───────────►  │        edge shim  :8800             │
                         │  • authenticates the human (cookie) │
                         │  • OVERWRITES identity headers      │
                         │  • mints a short-lived assertion    │
                         └──────┬───────────────────┬──────────┘
                                │                   │
                    /  , /nimble/api/*         /ui, /_next
                                │                   │
                   ┌────────────▼────────┐   ┌──────▼─────────────────┐
                   │ AgentOS origin :8801│   │ official Agent UI :3000│
                   │ • trusts ONLY a     │   │ (Next.js, pinned SHA,  │
                   │   verified assertion│   │  2 config lines added) │
                   │ • AgentFactory      │   └────────────────────────┘
                   │   builds the agent  │
                   └──────────┬──────────┘
                              │  the REAL published NimbleAgentTools
                              │  the REAL nimble-python SDK
                   ┌──────────▼────────────────────────────────┐
        test mode  │ local fake Nimble Agent API V2  :9411      │
        live mode  │ https://sdk.nimbleway.com                  │
                   └───────────────────────────────────────────┘
```

Four processes, because the edge/origin split is the security boundary and collapsing it
would make the boundary untestable (see **Auth boundary** below).

### Why the official UI needs no changes

`agent-ui` issues `fetch()` with no `credentials` option, which defaults to `same-origin`.
Serving the UI and the API from one origin therefore means the HttpOnly session cookie is
attached automatically. The UI's own **Auth Token** field stays empty — the playground
never puts a token in the browser at all.

The console is a separate Nimble-branded shell that embeds the official UI from the same
origin. Upstream's chat timeline, event handling, and components are untouched.

---

## Setup

```bash
# 1. Vendor the official Agent UI at its pinned commit (gitignored clone)
./ui/setup.sh

# 2. Run
./scripts/run.sh test      # deterministic, non-billable (default)
./scripts/run.sh live      # real Nimble + real LLM

# 3. Open
open http://127.0.0.1:8800/
```

`ui/setup.sh` clones `agno-agi/agent-ui`, hard-resets to the pinned SHA, verifies it, and
applies **exactly two configuration lines** (recorded in `ui/applied-overlay.patch`):

| File | Change | Why |
|---|---|---|
| `next.config.ts` | `basePath: '/ui'` | the console owns `/`, the UI is embedded at `/ui` |
| `src/store.ts` | default endpoint → the edge origin | same-origin, so the session cookie applies |

Pinned upstream is recorded in [`ui/UPSTREAM.lock`](ui/UPSTREAM.lock):

```
agno-agi/agent-ui @ 6dad9593fca6756e1813e4f4b3b2620be6377691   (2026-05-08)
next 15.5.18 · react 18.3.1 · tailwind 3.4.1 · zustand 5.0.3
```

Dependencies install with **pnpm 10**, not whatever is on `PATH`: pnpm 11 stopped reading
the `pnpm.overrides` field this commit uses, which aborts a frozen install. Pinning the
client keeps the upstream lockfile byte-identical.

---

## The controls

Everything is configurable from the console by the authenticated human, and every control
is resolved server-side by the `AgentFactory` at request time.

| Control | Values |
|---|---|
| **Agent identity** | agentless auto-provision · existing `agent_id` · named agent (`agent_name`) |
| **use_case** | unset · `research` · `enrichment` · `dataset_building` |
| **effort** | see below |
| **skill** | free text, one-time instructions |
| **input_data** | JSON object or array of rows |
| **output_schema** | JSON — switches the result to `type: "json"` |
| **sources** | JSON: `allow` / `block` / `avoid` / `prioritize` |
| **enable_events** | boolean |
| **Nimble API key** | per-session override, server-side, with shared-key fallback |

`agent_id` and `agent_name` are mutually exclusive and rejected together — at configuration
time, so the failure is an obvious 422 rather than a confusing mid-chat tool error.

### effort — two different policies, deliberately not conflated

`NimbleAgentTools` takes `effort` as an **optional per-run override** and **omits the field
entirely** when it is `None`, so Nimble applies the selected agent/template default. That
default is documented as **`high`** — it is *not* `medium`, and this playground never says
otherwise.

| Mode | Behaviour |
|---|---|
| **live** | Selectable: **Default (omit)** · `low` · `medium` · `high`. Default is *omit*, preserving the agent/template default. |
| **test** | Hard-pinned to `low`. This is a **local test policy** for the deterministic, non-billable evidence mode — not a Nimble product default. |

Not runnable in this playground:

- **`x-high`** — the SDK and the toolkit both accept it, but this team has asked that it
  never be run here. Excluded by the control model's own type, so a hand-crafted request is
  rejected by validation rather than merely being absent from a menu.
- **`max`** — coming soon / custom budget. Handled differently from `x-high` on purpose:
  it is **offered in the console**, and selecting it returns a **409 with a positive,
  actionable engagement notice** naming the next step. It is **never silently sent** and
  **never silently downgraded** to a cheaper tier — because reporting a `max` request as if
  it ran, or quietly running it at `high`, both misrepresent what happened. The degradation
  policy is `reject` (not degrade-to-`x-high`), since `x-high` is barred here.

  ```
  409  {"code": "effort_tier_coming_soon", "tier": "max",
        "degradation_policy": "reject",
        "message": "…coming soon and runs on a custom budget agreed per account…",
        "next_step": "Contact your Nimble representative to arrange access and a budget.",
        "runnable_tiers": [...]}
  ```

Setting `effort` in test mode returns **409**, so the console can never display a tier that
would not reach Nimble.

### SDK floor: nimble-python ≥ 1.2.0

1.2.0 promoted `agent_name`, `use_case`, and `skill` from `extra_body` passthrough to
**typed parameters** on both `agents.run()` (agentless) and `agents.runs.create()`. The
published toolkit now sends them as typed fields, so a typo in `use_case` fails against a
`Literal` locally instead of at the API.

The wire format is unchanged — `extra_body` merged into the same JSON body — so this is a
type-safety upgrade, not a behavioural one. `tests/test_sdk_alignment.py` asserts the floor
and that each field is a real parameter, so a downgraded environment fails loudly here
rather than silently changing how run options are transmitted.

Verified against the running stack:

| Invariant | Result |
|---|---|
| create is never auto-retried | `max_retries=0` in the toolkit's write client |
| effort omitted unless set | no `effort` key on the wire; body is `['enable_events','input']` |
| agentless runs work | auto-provisioned `wsa_…` returned |
| returned identity fail-closed | both `agent_id` and `run_id` present, or an error |
| typed run options on the wire | `use_case` / `skill` / `agent_name` all transmitted |

### Status-poll cadence

Per [`.claude/rules/wsa-polling-default.md`](../../../../../../.claude/rules/wsa-polling-default.md):
run-status polling defaults to **one poll every 10 seconds**, configurable via
`NIMBLE_POLL_INTERVAL_SECONDS`, with a bounded deadline (`NIMBLE_POLL_DEADLINE_SECONDS`,
default 300s) and a hard poll ceiling.

Scope, stated because it is easy to over-apply:

- **Applies to:** Nimble run-status polling, and only after a run has been created.
- **Does not apply to:** SSE/WebSocket delivery, UI animation, or browser automation waits.
- **Run creation is never retried** — the toolkit builds its write client with
  `max_retries=0`, and the interval only starts once a run exists and is identified.
- Tests use `0` as an **explicit test-only override** (labelled in `tests/conftest.py`);
  the production default is asserted in `tests/test_poll_pacing.py`.

In test mode the scripted driver owns the loop, so the cadence is *enforced*. In live mode
an LLM drives the loop, so the cadence is *instructed* — see **Known gaps**.

---

## Auth boundary (the Cloudflare contract)

**One protected origin.** The browser only ever talks to `:8800`. The console, the official
UI, the AgentOS REST API, and the SSE stream are all same-origin.

**The origin trusts exactly one thing:** a short-lived assertion (HMAC-SHA256, ≤120s,
audience-bound) signed with a secret only the edge holds. No cookies, no sessions, no
client headers.

**The edge overwrites, it does not pass through.** Identity headers are stripped from every
inbound request *before* the edge adds its own, so a browser-supplied header cannot reach
the origin. The origin does not depend on that for safety — it verifies the signature
itself.

Two independent defences, and both are tested:

| Attack | Result |
|---|---|
| anonymous → origin | `401` |
| forged assertion → origin | `401` (signature) |
| assertion for another audience | `401` |
| expired assertion | `401` |
| `alg: none` downgrade | `401` (algorithm is pinned, not read from the token) |
| `X-Nimble-User-Id: admin` → origin | `401` (client identity headers refused outright) |
| `X-Nimble-User-Id: admin` → *through the edge* | `200`, but as the real principal — the edge discarded it |
| `user_id=somebody-else` in the run form | ignored; AgentOS prefers `request.state.user_id`, which only middleware sets |
| WebSocket without an assertion | closed, code `1008` |

**The local shim does not authenticate.** Stated plainly because it would be easy to read
the diagram and assume otherwise: `POST /__edge/login` has no credential check. In
production *Cloudflare Access* authenticates and this endpoint does not exist. Locally,
per-principal isolation is an organisational boundary, not a security one. Three things
keep that bounded: the shim refuses to bind off-loopback without an explicit opt-in;
choosing an arbitrary `subject` requires `NIMBLE_ALLOW_SUBJECT_SELECTION=1`; and a
cross-origin login is refused outright rather than relying on a CORS side effect.

**Logging out clears the key override.** The edge is the only component that knows a
session ended (the origin has no session concept), so logout calls the origin's
`DELETE /nimble/api/key` for that principal and reports `key_override_cleared`. Otherwise
"log out" would leave the override resident and a later session would silently inherit it.

**Secrets.** No API key or OS security key is stored in browser storage, in a URL, in a
log, in a screenshot, or in source:

- The per-session Nimble key is POSTed once over the protected origin, held in process
  memory, and never returned by any endpoint — not even as a length, which would narrow
  which key is in use. It lives outside the serialisable model by construction.
- Secret-bearing fields are `field(repr=False)`, so `logging.info("%s", settings)`, an
  f-string, or a debugger's `pp` cannot print them. Guarding only pydantic serialisation
  would have left the likelier accident — someone logging the object — wide open.
- The console clears the key input **synchronously, before** the network call, not after:
  a `type="password"` input masks the value visually, not programmatically.
- The session cookie is **HttpOnly** (verified: absent from `document.cookie`).
- `localStorage` contains only the endpoint URL (verified in the browser).
- The fake Nimble server never reads the `Authorization` header into its request log.

### Porting to Cloudflare (not done in this turn)

Replace the local shim with a Worker that does the same three things:

1. Authenticate (Cloudflare Access SSO or your own session).
2. **Set** (never append) `X-Nimble-Edge-Assertion` on every proxied request, using
   `mint_edge_assertion`'s algorithm and a secret shared with the origin.
3. Proxy `/`, `/ui`, `/_next`, and the API to the origin, streaming SSE.

Origin-side changes: none — it already trusts only the assertion.

Before deploying, additionally: set the session cookie `Secure`, put the origin on a
private network or mTLS so it is not reachable directly, and rotate the assertion secret
out of band. The local shim's cookie is `Secure: false` only because local dev is plain
HTTP.

---

## Local test matrix

```bash
# Backend — 92 tests
cd backend && PYTHONPATH=. python -m pytest tests/

# Frontend — the vendored official UI
cd ui/agent-ui && npx pnpm@10 run typecheck && npx pnpm@10 run lint && npx pnpm@10 run build
```

| Suite | Covers |
|---|---|
| `test_security.py` | assertion mint/verify: signature, audience, expiry, skew, `alg:none`, reserved-claim override, fail-closed with no secret |
| `test_auth_boundary.py` | anonymous rejection on HTTP + WebSocket, forged/foreign assertions, smuggled identity headers, form-field impersonation, boot-time fail-closed |
| `test_controls.py` | control model validation, identity exclusivity, effort policy per mode, excluded tiers, per-principal isolation, key never echoed, uniform response shape |
| `test_run_lifecycle.py` | full run through AgentOS against the real toolkit: event sequence, tool ordering, controls on the wire, identity modes, scope gate, no credential in any readout |
| `test_poll_pacing.py` | the 10s default, configurability, what is and is not paced, bounded deadline, terminal states, non-blocking async sleep |

**These tests are not mocks of the toolkit.** They redirect `NIMBLE_BASE_URL` at a local
fake, so the real toolkit, the real Stainless SDK, real HTTP, and real Pydantic response
validation all run. Only the upstream Nimble service is substituted.

---

## Live evidence checklist

Test-mode output is **fixture data** and is labelled as such in the UI, in the payloads,
and in the fixture text itself. It is never evidence about live Nimble behaviour.

Before claiming the integration works against live Nimble, capture:

- [ ] `./scripts/run.sh live` with a real `NIMBLE_API_KEY` and `OPENAI_API_KEY`
- [ ] A real `{agent_id, run_id}` pair visible in the chat transcript (`wsa_…` / `task_run_…`)
- [ ] The `grounded` usability flag with a non-`low` confidence and ≥1 cited claim
- [ ] Real source URLs (not `example.com` — that is the fixture's reserved documentation domain)
- [ ] A screenshot of the completed run, uncropped
- [ ] Confirmation that no key appears in the screenshot, the URL, or the console
- [ ] Each identity mode exercised at least once: auto-provision, existing `agent_id`, named agent
- [ ] An effort override (`low`/`medium`/`high`) *and* a default (omitted) run, with the
      distinction stated

The wire inspector is **test-mode only** and returns 409 in live mode: proxying live
request bodies into a browser would leak exactly what this playground is careful not to.

---

## Browser evidence captured (test mode)

In [`docs/evidence/`](docs/evidence/). All from real runs against the running stack;
nothing synthesised.

| File | Shows |
|---|---|
| `01-unauthenticated-gate.png` | the protected origin refusing an unauthenticated visitor |
| `02-console-signed-in.png` | the control surface: identity modes, use_case, effort pinned with its rationale and exclusions, skill/input_data/output_schema/sources/events |
| `03-official-ui-connected.png` | the official Agent UI connected to the protected origin, **Auth Token: NO TOKEN SET**, our agent discovered in its picker |
| `04-run-in-flight.png` | native AgentOS timeline: prompt → `START_AGENT_RUN` → 3× `GET_AGENT_RUN_STATUS` → `GET_AGENT_RUN_RESULT` → grounded answer with sources |
| `05-wire-evidence.png` | resolved run configuration **and** the request body the SDK actually transmitted, carrying the operator's `use_case` / `skill` / `sources` / `input_data` / `enable_events` |
| `06-mid-run-liveness.png` | mid-flight at the 10s default: two tool chips so far, streaming indicator active |
| `07-completed-grounded-output.png` | the completed output, uncropped |
| `08-key-override-server-side.png` | a per-session key override applied: the field self-clears, the console reports `session_override`, and the value is nowhere in the browser |

`05` is the one that matters for "controls change the real run configuration": the console
shows the *actual outbound request*, not a summary the server wrote about itself.

### Secret-handling check, run in the browser

A sentinel key was applied through the console and then searched for everywhere it could
plausibly land:

| Location | Result |
|---|---|
| `localStorage` | absent (contains only `endpoint-storage`) |
| `sessionStorage` | absent (empty) |
| DOM / page source | absent (the input self-clears after the POST) |
| URL | absent |
| `document.cookie` | absent — and the session cookie itself is invisible to JS (HttpOnly) |
| edge / origin / UI / fake-Nimble logs | absent |
| `GET /nimble/api/session` response | absent; reports only `{source, session_override_present, shared_key_present}` |

---

## Known gaps

1. **Live-mode poll cadence is instructed, not enforced.** In test mode the scripted driver
   owns the loop and honours the interval exactly. In live mode an LLM chooses when to call
   `get_agent_run_status`, so the 10s cadence is stated in the agent's instructions. Enforcing
   it would need pacing inside the toolkit's status tool — a change to the published file,
   deliberately out of scope this turn.
2. **The edge shim does not proxy WebSocket.** Next.js dev HMR logs a 403 upgrade failure in
   the browser console; it is cosmetic and dev-only. AgentOS's WebSocket route *is* covered
   by the origin's auth boundary (tested), and the official UI at this commit uses SSE, not
   WebSocket, for runs. A production Worker should proxy WS.
3. **In-memory stores.** Control profiles and key overrides do not survive a restart. That is
   deliberate for a spike — a pasted key never touches disk — but a real deployment needs a
   decision about where per-user configuration lives.
4. **Session cookie is `Secure: false`** for local plain-HTTP development. Must be `True`
   behind Cloudflare.

---

## Porting into the private integration workspace

Exact steps, in order:

1. **Copy the directory** to `integrations/agno-agent-api-v2/local-playground/agentos-nimble/`.
   It is self-contained; nothing outside it was modified.
2. **Keep the two-layer split.** `ui/agent-ui/` is a vendored upstream clone and stays
   gitignored, exactly like `integrations/*/upstream-repo/` and `sdks/*/checkout/`. Add
   `local-playground/*/ui/agent-ui/` to the workspace `.gitignore` if it is not already
   covered.
3. **Point the Python at the workspace's vendored agno**, or keep resolving `agno` from the
   upstream clone so the playground continues to exercise the *local* toolkit rather than a
   released wheel. Set `NIMBLE_PLAYGROUND_PYTHON` if the venv path differs.
4. **Move the keys to the workspace convention** — the gitignored root `.env`, loaded with
   `set -a; . ./.env; set +a`, per `.claude/rules/local-env-keys.md`. Do not add a second
   `.env` here; delete `.env.example` if it would duplicate the root one.
5. **Record the live evidence** in `integrations/agno-agent-api-v2/api-validation.md` once the
   live checklist above is captured, and reference the screenshots from
   `docs/evidence/<feature-slug>/` per `.claude/rules/visual-evidence.md`.
6. **Do not port this README's Cloudflare section into any public PR.** It describes internal
   deployment topology. The upstream PR covers the toolkit only; this playground is private
   workspace machinery.
7. **Before any stakeholder demo**, run `./scripts/run.sh live` and capture the evidence — a
   test-mode screenshot must never be presented as live proof.
