"""Builds the Nimble agent for each request.

This is the join point: the verified principal (from the edge assertion), the
operator's run controls (from the control store), and the *actual published*
``agno.tools.nimble_agent.NimbleAgentTools`` come together here.

Two properties are worth stating plainly, because they are the reason this file
exists rather than a module-level singleton agent:

*Authorization reads only trusted context.* Whether a caller may start a
billable run is decided from ``ctx.trusted.scopes`` -- which agno populates
exclusively from middleware-set ``request.state`` -- never from
``ctx.input``. A browser that posts its own ``factory_input`` cannot grant
itself the run scope.

*The toolkit is used, not wrapped.* No subclass, no monkey-patch, no shim. The
per-request values the playground needs to vary (API key, agent id) are
constructor arguments the published toolkit already exposes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agno.agent import Agent
from agno.factory import RequestContext
from agno.tools.nimble_agent import NimbleAgentTools

from .controls import ControlProfile, ControlStore, RunControls
from .scripted_model import ScriptedNimbleModel
from .settings import SCOPE_RUN, TEST_MODE_EFFORT, Settings

# The AgentOS component id. The official Agent UI lists agents by this id, so it
# is also what an operator sees in the agent picker.
AGENT_ID = "nimble-web-search-agent"
AGENT_NAME = "Nimble Web Search Agent (Agent API V2)"
AGENT_DESCRIPTION = (
    "Runs Nimble's Web Search Agent lifecycle: start a run, poll its status, "
    "then return the grounded, cited result."
)

# Arguments of ``start_agent_run`` that the operator may configure. ``effort`` is
# absent because the toolkit owns it; ``query`` is absent because it comes from
# the chat message.
CONFIGURABLE_START_ARGS = (
    "agent_name",
    "use_case",
    "skill",
    "input_data",
    "output_schema",
    "sources",
    "enable_events",
)


@dataclass
class ResolvedRunConfig:
    """What the factory actually built, for display and for evidence.

    Deliberately records the *effective* values rather than the requested ones,
    and records the key by origin rather than by value. This is what the console
    shows, so it must never carry a secret.
    """

    principal: str
    identity_mode: str = "auto_provision"
    toolkit_kwargs: Dict[str, Any] = field(default_factory=dict)
    start_arguments: Dict[str, Any] = field(default_factory=dict)
    key_source: str = "none"
    run_lifecycle_enabled: bool = False
    discovery_enabled: bool = True
    # None means the toolkit omits the field, so Nimble applies the
    # agent/template default. Recorded as-is rather than substituting a label,
    # so the readout matches what actually goes on the wire.
    effort: Optional[str] = None
    effort_policy: str = "live: optional override, omitted by default"
    model: str = ""
    denied_reason: Optional[str] = None

    def public_view(self) -> Dict[str, Any]:
        return {
            "principal": self.principal,
            "identity_mode": self.identity_mode,
            "effort": self.effort,
            "effort_sent_to_nimble": self.effort is not None,
            "effort_policy": self.effort_policy,
            "model": self.model,
            "key_source": self.key_source,
            "run_lifecycle_enabled": self.run_lifecycle_enabled,
            "discovery_enabled": self.discovery_enabled,
            "toolkit_kwargs": self.toolkit_kwargs,
            "start_arguments": self.start_arguments,
            "denied_reason": self.denied_reason,
        }


class ResolvedConfigRecorder:
    """Keeps the most recent resolved config per principal.

    The console reads this to show the operator what their controls actually
    became. It is a display aid; the authoritative evidence that a control
    reached Nimble is the wire log in the fake server (test mode) or the run's
    own identity (live mode).
    """

    def __init__(self) -> None:
        self._latest: Dict[str, ResolvedRunConfig] = {}

    def record(self, config: ResolvedRunConfig) -> None:
        self._latest[config.principal] = config

    def latest(self, principal: str) -> Optional[ResolvedRunConfig]:
        return self._latest.get(principal)


def _resolve_effort(settings: Settings, controls: RunControls) -> Tuple[Optional[str], str]:
    """Decide the effort passed to the toolkit, and say which policy decided it.

    Two genuinely different policies, kept apart so neither can be mistaken for
    the other in a readout or a screenshot:

    * **test** -- hard-pinned to ``low``. This is a *local test policy* for the
      deterministic, non-billable evidence mode. It says nothing about Nimble's
      product defaults.
    * **live** -- hard-pinned to ``low`` by the protected showcase's
      cost-control policy. This is deliberately narrower than the reusable
      integration's supported effort contract.
    """
    if settings.is_test_mode:
        return TEST_MODE_EFFORT, (
            f"test: hard-pinned to '{TEST_MODE_EFFORT}' as a local, non-billable test policy "
            "(not a Nimble product default)"
        )
    return TEST_MODE_EFFORT, (
        "live: hard-pinned to 'low' by the protected playground cost-control policy"
    )


def _start_arguments_from(controls: RunControls) -> Dict[str, Any]:
    """Project run controls onto ``start_agent_run`` keyword arguments."""
    payload = controls.model_dump(exclude_none=True)
    arguments = {key: payload[key] for key in CONFIGURABLE_START_ARGS if key in payload}
    # enable_events defaults to False and is meaningful even when False, but
    # sending it only when True keeps the recorded call minimal and readable.
    if not arguments.get("enable_events"):
        arguments.pop("enable_events", None)
    return arguments


def _instructions(controls: RunControls, run_allowed: bool, poll_interval_seconds: float) -> List[str]:
    """Operating instructions for the live LLM.

    In test mode the scripted driver ignores these and uses the resolved
    arguments directly. In live mode they are how the operator's controls reach
    the tool call, so the configured values are embedded literally.
    """
    lines = [
        "You answer questions using Nimble's Web Search Agent (Agent API V2).",
        "Follow this lifecycle exactly and never skip a step:",
        "1. Call `start_agent_run` once with the user's question as `query`.",
        "2. Call `get_agent_run_status` with the returned `run_id` and `agent_id`, and repeat "
        "until `status` is `completed`, `failed`, or `cancelled`. Never call `start_agent_run` again.",
        f"   Pace those status checks at roughly one every {poll_interval_seconds:g} seconds. "
        "Do not poll in a tight loop.",
        "3. Once the run is `completed`, call `get_agent_run_result` exactly once.",
        "4. Answer from the returned content, and always state the `usability` flag: a run can be "
        "`completed` yet `degraded`, which means it is not grounded. List the sources you were given.",
        "Never retry a failed `start_agent_run`: a run is billable and is not idempotent.",
    ]
    if not run_allowed:
        lines = [
            "You have read-only access to Nimble. You may list agents and templates, "
            "but you may not start runs. If asked to research something, explain that "
            "this principal lacks the run scope.",
        ]
        return lines

    configured = _start_arguments_from(controls)
    if configured:
        lines.append(
            "The operator has configured these exact `start_agent_run` arguments. "
            "Pass every one of them verbatim, in addition to `query`:"
        )
        for key, value in configured.items():
            rendered = value if isinstance(value, (str, bool, int)) else json.dumps(value, sort_keys=True)
            lines.append(f"- `{key}` = {rendered}")
    if controls.agent_id:
        lines.append(
            f"Runs target the existing Nimble agent `{controls.agent_id}`, which is already configured "
            "as the toolkit default. Do not pass `agent_name`."
        )
    elif controls.agent_name:
        lines.append(f"Pass `agent_name` = `{controls.agent_name}` so Nimble creates or reuses that agent.")
    else:
        lines.append("Pass neither `agent_id` nor `agent_name`; Nimble will auto-provision a one-off agent.")
    return lines


def build_agent_factory(
    *,
    settings: Settings,
    store: ControlStore,
    recorder: ResolvedConfigRecorder,
):
    """Return the callable AgentOS invokes per request."""

    def factory(ctx: RequestContext) -> Agent:
        # -- identity: trusted context only ---------------------------------
        principal = str(ctx.trusted.claims.get("sub") or ctx.user_id or "unknown")
        scopes = ctx.trusted.scopes
        run_allowed = SCOPE_RUN in scopes

        profile: ControlProfile = store.get(principal)
        controls = profile.controls

        effective_key = profile.nimble_api_key_override or settings.nimble_api_key
        key_source = (
            "session_override"
            if profile.nimble_api_key_override
            else ("shared_server_key" if settings.nimble_api_key else "none")
        )

        # An operator-configured agent_id becomes the toolkit default, which is
        # exactly how the published toolkit expects an "existing agent" run to
        # be expressed.
        toolkit_agent_id = controls.agent_id or settings.nimble_default_agent_id

        effort, effort_policy = _resolve_effort(settings, controls)

        toolkit = NimbleAgentTools(
            api_key=effective_key,
            agent_id=toolkit_agent_id,
            # None means the toolkit omits `effort` entirely, so Nimble applies
            # the selected agent/template default rather than this playground
            # silently choosing a tier.
            effort=effort,
            # Read-only discovery stays available to every principal; only the
            # billable lifecycle is gated.
            enable_run_lifecycle=run_allowed,
            enable_discovery=True,
        )

        start_arguments = _start_arguments_from(controls)

        recorder.record(
            ResolvedRunConfig(
                principal=principal,
                identity_mode=controls.identity_mode,
                toolkit_kwargs={
                    # Note the absence of api_key: recording it, even redacted,
                    # would put a secret-shaped field in a UI-facing payload.
                    "agent_id": toolkit_agent_id,
                    "effort": effort,
                    "enable_run_lifecycle": run_allowed,
                    "enable_discovery": True,
                },
                start_arguments=start_arguments,
                key_source=key_source,
                effort=effort,
                effort_policy=effort_policy,
                run_lifecycle_enabled=run_allowed,
                discovery_enabled=True,
                model=("scripted" if settings.uses_scripted_driver else settings.openai_model),
                denied_reason=None if run_allowed else "principal lacks the nimble:run scope",
            )
        )

        model = _build_model(settings, start_arguments)

        return Agent(
            id=AGENT_ID,
            name=AGENT_NAME,
            description=AGENT_DESCRIPTION,
            model=model,
            tools=[toolkit],
            instructions=_instructions(controls, run_allowed, settings.poll_interval_seconds),
            markdown=True,
            # The UI renders tool calls from the event stream; storing them keeps
            # a reloaded session showing the same lifecycle.
            store_events=True,
        )

    return factory


def _build_model(settings: Settings, start_arguments: Dict[str, Any]):
    """Pick the driver for the current run mode.

    Test mode never constructs a provider client, so a missing or wrong LLM key
    cannot affect a deterministic run.
    """
    if settings.uses_scripted_driver:
        return ScriptedNimbleModel(
            start_arguments=start_arguments,
            # The scripted driver owns the poll loop, so this is where the
            # workspace's 10s WSA cadence is actually enforced rather than
            # merely requested of an LLM.
            poll_interval_seconds=settings.poll_interval_seconds,
            poll_deadline_seconds=settings.poll_deadline_seconds,
        )

    from agno.models.openai import OpenAIChat

    return OpenAIChat(id=settings.openai_model, api_key=settings.openai_api_key)
