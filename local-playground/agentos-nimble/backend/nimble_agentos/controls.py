"""Playground run controls, held server-side and keyed by verified principal.

The official Agent UI sends a chat message and nothing else. Everything the
Nimble playground needs to vary per run -- identity mode, use case, skill,
input data, output schema, source guidance, events -- therefore lives here and
is resolved by the agent factory at request time.

Keying by *principal* rather than by session is deliberate. A brand new chat has
no session id until the first run returns one, so a session-keyed profile could
not be configured before the first message. The principal is known from the
first request onward, and it comes from the verified edge assertion, so it
cannot be spoofed by the browser.

The per-session Nimble API key override is held in this module too, and it is
the reason ``RunControls`` and ``ControlProfile`` are separate types: the key is
not a field of the serialisable model at all. There is no code path that can
serialise it by accident, because it is not there to serialise.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

UseCase = Literal["research", "enrichment", "dataset_building"]

# Effort tiers this playground will RUN.
#
# nimble-python 1.2.0 types effort as Literal['low','medium','high','x-high','max'],
# so the SDK would send any of them. The narrowing is ours:
#
#   x-high  excluded by this team's explicit policy; never run here.
#   max     coming soon / custom budget. Handled separately below rather than
#           silently dropped, because "not offered" and "offered then quietly
#           downgraded" are both worse than an explicit, actionable refusal.
#
# Encoding the runnable set as the field's *type* means a hand-crafted request is
# rejected by validation, not merely absent from a menu.
LiveEffort = Literal["low", "medium", "high"]

# Accepted by the API so it can be answered with a real explanation, but never
# runnable. Kept distinct from LiveEffort so the two can never be confused.
ComingSoonEffort = Literal["max"]

# Bounds on operator-supplied structures. These are not security boundaries --
# the operator is authenticated -- but they keep one bad paste from producing a
# request that the Nimble API would reject with an opaque error.
MAX_SKILL_CHARS = 4000
MAX_INPUT_ROWS = 100


class RunControls(BaseModel):
    """Everything the operator can vary about a Nimble run.

    Mirrors the argument surface of ``NimbleAgentTools.start_agent_run`` so the
    factory can hand values straight through without reshaping them. Effort is
    absent on purpose: it is pinned to ``low`` and is not an operator choice.
    """

    model_config = {"extra": "forbid"}

    # -- effort -------------------------------------------------------------
    # None means "omit the field", which is the default and preserves the
    # selected agent/template default (Nimble documents that default as "high").
    # Honoured in live mode only; test mode pins effort as a local test policy.
    effort: Optional[Union[LiveEffort, ComingSoonEffort]] = Field(
        default=None,
        description=(
            "Optional per-run effort override. Omit to preserve the agent/template default. "
            "'max' is accepted only to be answered with an engagement notice — it is never run. "
            "'x-high' is not selectable in this playground."
        ),
    )

    # -- identity: exactly one of three modes -------------------------------
    agent_id: Optional[str] = Field(
        default=None,
        description="Existing Nimble agent id (wsa_...). Mutually exclusive with agent_name.",
    )
    agent_name: Optional[str] = Field(
        default=None,
        description="Stable name Nimble creates or reuses server-side. Mutually exclusive with agent_id.",
    )

    # -- run shaping --------------------------------------------------------
    use_case: Optional[UseCase] = Field(
        default=None,
        description="Creation-time mode. Existing agents reject a different locked value.",
    )
    skill: Optional[str] = Field(default=None, max_length=MAX_SKILL_CHARS)
    input_data: Optional[Union[List[Dict[str, Any]], Dict[str, Any]]] = None
    output_schema: Optional[Dict[str, Any]] = None
    sources: Optional[Dict[str, Any]] = None
    enable_events: bool = False

    @model_validator(mode="after")
    def _one_identity_mode(self) -> "RunControls":
        # The toolkit already refuses this combination and returns a structured
        # error, but catching it here turns a confusing mid-chat tool failure
        # into an immediate, obvious 422 on the control-plane call.
        if self.agent_id and self.agent_name:
            raise ValueError(
                "agent_id and agent_name are mutually exclusive: pass an existing agent_id, "
                "pass an agent_name for Nimble to create or reuse, or pass neither to auto-provision."
            )
        if isinstance(self.input_data, list) and len(self.input_data) > MAX_INPUT_ROWS:
            raise ValueError(f"input_data is capped at {MAX_INPUT_ROWS} rows in this playground")
        return self

    @property
    def identity_mode(self) -> str:
        """Which of the three identity modes these controls select."""
        if self.agent_id:
            return "existing_agent_id"
        if self.agent_name:
            return "named_agent"
        return "auto_provision"


@dataclass
class ControlProfile:
    """A principal's active controls plus their optional key override.

    ``nimble_api_key_override`` is a plain attribute rather than a model field so
    that no serialiser, log formatter, or response model can reach it. The only
    reader is the agent factory.
    """

    controls: RunControls = field(default_factory=RunControls)
    # repr=False as well as being outside the pydantic model: keeping it off the
    # model stops a serialiser reaching it, but only repr=False stops the far
    # likelier accident of someone logging or printing the profile object.
    nimble_api_key_override: Optional[str] = field(default=None, repr=False)

    def public_view(self, *, shared_key_present: bool) -> Dict[str, Any]:
        """The shape the browser is allowed to see.

        Reports key *presence and origin*, never a value, never a prefix, never
        a length -- a length is a meaningful hint about which key is in use.
        """
        return {
            "controls": self.controls.model_dump(exclude_none=True),
            "identity_mode": self.controls.identity_mode,
            "nimble_key": {
                "source": (
                    "session_override"
                    if self.nimble_api_key_override
                    else ("shared_server_key" if shared_key_present else "none")
                ),
                "session_override_present": bool(self.nimble_api_key_override),
                "shared_key_present": shared_key_present,
            },
        }


class ControlStore:
    """In-memory, per-principal control profiles.

    In-memory is the right call for a spike *and* a security property: a paste
    of a Nimble key never touches disk, so there is no file to leak and nothing
    to scrub on shutdown. Restarting the process forgets every override.
    """

    def __init__(self) -> None:
        self._profiles: Dict[str, ControlProfile] = {}
        # AgentOS serves requests concurrently, so profile mutation is guarded.
        self._lock = threading.Lock()

    def get(self, principal: str) -> ControlProfile:
        with self._lock:
            return self._profiles.get(principal) or ControlProfile()

    def set_controls(self, principal: str, controls: RunControls) -> ControlProfile:
        with self._lock:
            profile = self._profiles.setdefault(principal, ControlProfile())
            profile.controls = controls
            return profile

    def set_key_override(self, principal: str, api_key: Optional[str]) -> ControlProfile:
        """Store or clear a principal's key override.

        An empty or whitespace-only value clears rather than stores, so "delete
        the text and save" does the obvious thing instead of installing a key
        that can never authenticate.
        """
        normalised = (api_key or "").strip() or None
        with self._lock:
            profile = self._profiles.setdefault(principal, ControlProfile())
            profile.nimble_api_key_override = normalised
            return profile

    def clear(self, principal: str) -> None:
        with self._lock:
            self._profiles.pop(principal, None)

    def resolve_api_key(self, principal: str, shared_key: Optional[str]) -> Optional[str]:
        """Effective key for a run: session override first, shared key second."""
        return self.get(principal).nimble_api_key_override or shared_key
