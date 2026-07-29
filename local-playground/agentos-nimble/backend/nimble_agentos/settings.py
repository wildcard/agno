"""Configuration for the AgentOS x Nimble playground.

Two decisions are made here and nowhere else, because getting either of them
wrong silently is the expensive kind of mistake:

``run_mode``
    ``test`` points the Nimble SDK at a local fake and uses a scripted model, so
    a run costs nothing and always produces the same transcript. ``live`` uses
    the real Nimble service and a real LLM. There is no "mostly live" state, and
    the mode is echoed in the control-plane response so a screenshot can never
    be mistaken for the other one.

``edge_secret``
    The origin refuses every request when this is unset. Failing closed is the
    only safe default for a process whose whole job is to sit behind an edge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional

RunMode = Literal["test", "live"]

# -- effort policy ---------------------------------------------------------
#
# ``NimbleAgentTools`` takes ``effort`` as an *optional* per-run override and
# omits the field entirely when it is None, so Nimble applies the selected
# agent/template default. That default is documented as ``high`` -- it is not
# ``medium``, and this playground must never imply otherwise.
#
# Two different policies apply, and conflating them would be misleading:
#
# ``live``  A real, billable run. This protected showcase deliberately pins
#           every run to the cheapest ``low`` tier.
# ``test``  A local, non-billable, deterministic evidence run against the fake.
#           Effort is hard-pinned to "low" as a *local test policy*. That is a
#           property of this harness, not a statement about Nimble's product.
TEST_MODE_EFFORT = "low"
LIVE_MODE_EFFORT = "low"
TEST_MODE_EFFORT_RATIONALE = (
    "Local test policy for the deterministic, non-billable evidence mode. "
    "Not a Nimble product default."
)

# Sentinel for "omit the field". Distinct from the string "default" so it can
# never be mistaken for a tier name on the wire.
EFFORT_DEFAULT = None

# The reusable SDK supports other tiers, but this live showcase intentionally
# exposes only low so an operator cannot accidentally start a more expensive
# run from the protected UI or its backing control plane.
#
# nimble-python 1.2.0 types effort as
# Literal['low','medium','high','x-high','max'], so the SDK will send any of
# them. The narrowing below is a deliberate product decision in our layer, not
# something the SDK enforces.
#
# "x-high" is omitted because this team has explicitly asked that it never be
# run in this playground. The exclusion is enforced by the control model's own
# type, not merely by the UI omitting an <option>.
LIVE_EFFORT_CHOICES = (LIVE_MODE_EFFORT,)

# "max" gets the coming-soon / enterprise treatment rather than silent absence.
#
# The rule it must satisfy: max may be surfaced only alongside a positive,
# actionable Nimble-engagement notice AND an explicit degradation policy. It
# must never be silently sent, and never silently downgraded.
#
# Because this playground forbids x-high, the only honest degradation policy
# here is REJECT. Selecting max therefore produces an explicit, actionable
# refusal naming the path forward -- not a quiet substitution of a cheaper tier,
# which would misrepresent what the run actually did.
EFFORT_COMING_SOON = "max"
EFFORT_COMING_SOON_POLICY = "reject"  # the alternative, "degrade-to-x-high", is barred here
EFFORT_COMING_SOON_NOTICE = (
    "Nimble's 'max' effort tier is coming soon and runs on a custom budget agreed per account. "
    "It is not enabled for this playground. To evaluate 'max' for your use case, contact your "
    "Nimble representative to arrange access and a budget; once enabled, re-run with the tier "
    "selected. This playground will not silently downgrade a 'max' request to a cheaper tier."
)

EFFORT_EXCLUSIONS = {
    "x-high": "Excluded by this playground's policy; never run here.",
}
NIMBLE_DOCUMENTED_DEFAULT_EFFORT = "high"

# -- SDK floor -------------------------------------------------------------
#
# 1.2.0 promoted agent_name / use_case / skill from extra_body passthrough to
# typed parameters on agents.run() and agents.runs.create(). This playground
# reports the resolved version so a stale environment is visible rather than
# silently changing how run options are transmitted.
REQUIRED_NIMBLE_SDK = "1.2.0"

# -- status-poll pacing ----------------------------------------------------
#
# Workspace convention (.claude/rules/wsa-polling-default.md): a Nimble WSA /
# Agent API V2 integration polls run status once every 10 seconds by default.
#
# Scope, stated explicitly because it is easy to over-apply: this paces the
# *status-poll loop only*, and only after a run has been created and identified.
# It does not touch SSE or WebSocket delivery, UI animation, or browser
# automation waits -- those are event-driven and must stay immediate.
#
# Run creation is never retried (the toolkit builds its write client with
# max_retries=0), so this interval can never turn into a retry storm on a
# billable, non-idempotent create.
DEFAULT_POLL_INTERVAL_SECONDS = 10.0

# Bounded overall deadline for the poll loop, so a run that never reaches a
# terminal state fails loudly instead of polling forever.
DEFAULT_POLL_DEADLINE_SECONDS = 300.0

# Scope required to start a billable Nimble run. Read-only discovery is allowed
# without it, so a viewer principal can explore an account without spending.
SCOPE_RUN = "nimble:run"
SCOPE_DISCOVER = "nimble:discover"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float, allowing 0 as a deliberate value.

    Zero matters here: it is how a test opts out of poll pacing, and it must be
    distinguishable from "unset".
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration. Built once, at process start."""

    run_mode: RunMode = "test"

    # -- the protected edge -------------------------------------------------
    edge_secret: str = field(default="", repr=False)
    edge_audience: str = "agentos-nimble-playground"
    edge_host: str = "127.0.0.1"
    edge_port: int = 8800

    # -- the origin (AgentOS) ----------------------------------------------
    origin_host: str = "127.0.0.1"
    origin_port: int = 8801

    # -- the official Agent UI (Next.js dev server) ------------------------
    ui_host: str = "127.0.0.1"
    ui_port: int = 3000

    # -- Nimble ------------------------------------------------------------
    # Shared fallback key. A per-session override, when present, wins over this.
    # Never logged, never returned by any endpoint, never sent to the browser.
    #
    # repr=False matters more than it looks: without it, a plain
    # ``logging.info("%s", settings)``, an f-string, or a debugger's ``pp`` would
    # print the raw key. Guarding only pydantic serialisation would leave the
    # much likelier accident -- someone logging the settings object -- wide open.
    nimble_api_key: Optional[str] = field(default=None, repr=False)
    nimble_base_url: Optional[str] = None
    nimble_default_agent_id: Optional[str] = None

    # -- status-poll pacing -------------------------------------------------
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    poll_deadline_seconds: float = DEFAULT_POLL_DEADLINE_SECONDS

    # -- live-mode model ---------------------------------------------------
    openai_api_key: Optional[str] = field(default=None, repr=False)
    openai_model: str = "gpt-4o-mini"
    scripted_driver: bool = False

    # -- local edge shim ---------------------------------------------------
    # The shim stands in for Cloudflare. It is only ever safe on a loopback
    # interface, so it refuses to bind anywhere else unless explicitly forced.
    local_edge_subject: str = "local-operator"
    session_cookie_name: str = "nimble_edge_session"

    @property
    def origin_base_url(self) -> str:
        return f"http://{self.origin_host}:{self.origin_port}"

    @property
    def ui_base_url(self) -> str:
        return f"http://{self.ui_host}:{self.ui_port}"

    @property
    def edge_base_url(self) -> str:
        return f"http://{self.edge_host}:{self.edge_port}"

    @property
    def is_test_mode(self) -> bool:
        return self.run_mode == "test"

    @property
    def uses_scripted_driver(self) -> bool:
        return self.is_test_mode or self.scripted_driver

    def effort_policy(self) -> dict:
        """How effort behaves in the current mode, in a form the console renders.

        Returned rather than hard-coded in the UI so the console cannot offer a
        tier the server would not honour, and cannot mislabel which mode's
        policy is in force.
        """
        if self.is_test_mode:
            return {
                "mode": "test",
                "selectable": False,
                "value": TEST_MODE_EFFORT,
                "choices": [TEST_MODE_EFFORT],
                "rationale": TEST_MODE_EFFORT_RATIONALE,
                "excluded": EFFORT_EXCLUSIONS,
            }
        return {
            "mode": "live",
            "selectable": False,
            "value": LIVE_MODE_EFFORT,
            "choices": [LIVE_MODE_EFFORT],
            "nimble_documented_default": NIMBLE_DOCUMENTED_DEFAULT_EFFORT,
            "rationale": (
                "Protected live-playground policy: every billable run is pinned to 'low'. "
                "This is a showcase cost-control policy, not Nimble's product default."
            ),
            "excluded": {
                "medium": "Disabled by this protected playground's low-only policy.",
                "high": "Disabled by this protected playground's low-only policy.",
                "x-high": "Disabled by this protected playground's low-only policy.",
                "max": "Not generally available and disabled by this protected playground.",
            },
        }

    def describe(self) -> dict:
        """A redaction-safe summary, suitable for the UI banner and for logs.

        Credentials are reported as presence only. This method is the reason no
        other code needs to decide what is safe to print.
        """
        return {
            "run_mode": self.run_mode,
            "edge_audience": self.edge_audience,
            "nimble_base_url": self.nimble_base_url or "https://sdk.nimbleway.com",
            "shared_nimble_key_present": bool(self.nimble_api_key),
            "default_agent_id_present": bool(self.nimble_default_agent_id),
            "llm": ("scripted" if self.uses_scripted_driver else self.openai_model),
        }


def load_settings() -> Settings:
    """Read configuration from the environment.

    ``NIMBLE_BASE_URL`` deserves a note: the published ``NimbleAgentTools`` never
    passes ``base_url`` to the SDK, and the Nimble SDK falls back to this
    environment variable. That is precisely how test mode redirects the *real*
    toolkit and the *real* SDK at a local fake without patching either one.
    """
    run_mode: RunMode = "live" if os.getenv("NIMBLE_PLAYGROUND_MODE", "test").strip().lower() == "live" else "test"

    return Settings(
        run_mode=run_mode,
        edge_secret=os.getenv("NIMBLE_EDGE_SECRET", "").strip(),
        edge_audience=os.getenv("NIMBLE_EDGE_AUDIENCE", "agentos-nimble-playground").strip(),
        edge_host=os.getenv("NIMBLE_EDGE_HOST", "127.0.0.1").strip(),
        edge_port=_env_int("NIMBLE_EDGE_PORT", 8800),
        origin_host=os.getenv("NIMBLE_ORIGIN_HOST", "127.0.0.1").strip(),
        origin_port=_env_int("NIMBLE_ORIGIN_PORT", 8801),
        ui_host=os.getenv("NIMBLE_UI_HOST", "127.0.0.1").strip(),
        ui_port=_env_int("NIMBLE_UI_PORT", 3000),
        nimble_api_key=os.getenv("NIMBLE_API_KEY") or None,
        nimble_base_url=os.getenv("NIMBLE_BASE_URL") or None,
        nimble_default_agent_id=os.getenv("NIMBLE_AGENT_ID") or None,
        # Configurable per the workspace convention. Setting this to 0 is a
        # test-only override; it must never be the default in a real deployment.
        poll_interval_seconds=_env_float("NIMBLE_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS),
        poll_deadline_seconds=_env_float("NIMBLE_POLL_DEADLINE_SECONDS", DEFAULT_POLL_DEADLINE_SECONDS),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        scripted_driver=_env_flag("NIMBLE_SCRIPTED_DRIVER", False),
        local_edge_subject=os.getenv("NIMBLE_LOCAL_EDGE_SUBJECT", "local-operator").strip(),
    )


def allow_insecure_bind() -> bool:
    """Escape hatch for binding the local edge shim off loopback.

    The shim authenticates with a development cookie, so exposing it on a LAN
    interface would hand the shared Nimble key to anyone who can reach the port.
    Requiring an explicit opt-in keeps that from happening by accident.
    """
    return _env_flag("NIMBLE_ALLOW_INSECURE_BIND", False)
