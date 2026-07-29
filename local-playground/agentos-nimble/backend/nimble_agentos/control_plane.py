"""The Nimble control plane: the API behind the visible configuration console.

Every route here sits behind the edge-assertion middleware, so ``principal`` is
always a verified identity and a profile can never be read or written on another
principal's behalf.

The key-override endpoint is the one to read carefully. A Nimble API key is
accepted over a single POST on the protected origin, held in memory, and never
returned again -- not in this response, not by ``GET /session``, not in any log
line. The browser is told only *whether* an override exists and which source a
run would use. That is what lets the console show a truthful key state without
the key ever being retrievable from the browser.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .agent_factory import AGENT_ID, AGENT_NAME, ResolvedConfigRecorder
from .controls import ControlStore, RunControls
from .middleware import principal_from_request
from .settings import (
    EFFORT_COMING_SOON,
    EFFORT_COMING_SOON_NOTICE,
    EFFORT_COMING_SOON_POLICY,
    SCOPE_DISCOVER,
    SCOPE_RUN,
    Settings,
)


class KeyOverrideRequest(BaseModel):
    """Carrier for a per-session Nimble key.

    ``api_key`` is write-only by construction: no response model in this module
    includes it, and the store keeps it outside the serialisable model.
    """

    model_config = {"extra": "forbid"}

    api_key: Optional[str] = Field(default=None, description="Nimble API key. Empty or null clears the override.")


def build_control_plane(
    *,
    settings: Settings,
    store: ControlStore,
    recorder: ResolvedConfigRecorder,
) -> APIRouter:
    router = APIRouter(prefix="/nimble/api", tags=["nimble-playground"])

    def session_payload(principal: Any) -> Dict[str, Any]:
        """The single response shape every control-plane route returns.

        One shape, not several: the console re-renders from whatever a mutation
        returns, so a route that omitted a field would break rendering the moment
        an operator saved something. That is precisely the bug this consolidation
        fixes -- ``capabilities`` used to exist only on the GET.
        """
        subject = principal.subject
        profile = store.get(subject)
        resolved = recorder.latest(subject)
        return {
            "principal": subject,
            "scopes": sorted(principal.scopes),
            "capabilities": {
                "can_run": SCOPE_RUN in principal.scopes,
                "can_discover": SCOPE_DISCOVER in principal.scopes or SCOPE_RUN in principal.scopes,
            },
            "agent": {"id": AGENT_ID, "name": AGENT_NAME},
            "runtime": settings.describe(),
            # The effort policy is served, not hard-coded in the console, so the
            # UI can only offer what this mode actually honours. In test mode it
            # is a fixed local test policy; in live mode it is an optional
            # per-run override that defaults to being omitted.
            "effort": {**settings.effort_policy(), "selected": profile.controls.effort},
            "poll": {
                "interval_seconds": settings.poll_interval_seconds,
                "deadline_seconds": settings.poll_deadline_seconds,
                "applies_to": "Nimble run status polling only, after a run has been created.",
            },
            "profile": profile.public_view(shared_key_present=bool(settings.nimble_api_key)),
            "resolved_run_config": resolved.public_view() if resolved else None,
        }

    @router.get("/session")
    async def get_session(request: Request) -> Dict[str, Any]:
        """Everything the console needs to render, for the calling principal."""
        principal = principal_from_request(request)
        return session_payload(principal)

    @router.put("/controls")
    async def put_controls(controls: RunControls, request: Request) -> Dict[str, Any]:
        """Replace this principal's run controls.

        A full replace rather than a patch: partial updates of a structure that
        includes mutually exclusive identity fields invite states where the
        stored profile means something the operator never selected.
        """
        principal = principal_from_request(request)

        # "max" is coming soon / custom budget. It is answered with a positive,
        # actionable engagement notice and an explicit degradation policy --
        # never silently sent, and never silently downgraded to a cheaper tier,
        # which would misreport what the run actually did.
        if controls.effort == EFFORT_COMING_SOON:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "effort_tier_coming_soon",
                    "tier": EFFORT_COMING_SOON,
                    "degradation_policy": EFFORT_COMING_SOON_POLICY,
                    "message": EFFORT_COMING_SOON_NOTICE,
                    "next_step": "Contact your Nimble representative to arrange access and a budget.",
                    "runnable_tiers": list(settings.effort_policy().get("choices") or []),
                },
            )

        if controls.effort not in (None, "low"):
            # Refuse rather than silently ignore. Accepting an effort the test
            # or live harness will not honour would let the console display a tier that
            # never reaches Nimble -- exactly the kind of misleading state a
            # screenshot would then carry.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Effort is fixed to 'low' in this protected playground. "
                    "Higher-cost effort tiers are disabled here."
                ),
            )
        store.set_controls(principal.subject, controls)
        return session_payload(principal)

    @router.post("/key")
    async def set_key(payload: KeyOverrideRequest, request: Request) -> Dict[str, Any]:
        """Install or clear the per-session Nimble key override.

        The value is consumed here and never leaves the process. The response
        deliberately reports only presence and source.
        """
        principal = principal_from_request(request)
        store.set_key_override(principal.subject, payload.api_key)
        return session_payload(principal)

    @router.delete("/key")
    async def clear_key(request: Request) -> Dict[str, Any]:
        principal = principal_from_request(request)
        store.set_key_override(principal.subject, None)
        return session_payload(principal)

    @router.get("/wire")
    async def wire_log(request: Request, limit: int = 10) -> Dict[str, Any]:
        """What the toolkit actually sent to Nimble, in test mode.

        Available only in test mode, and only because the peer is a local fake
        that never receives a real credential. There is no equivalent view of
        live traffic: proxying live request bodies into a browser would be a way
        to leak exactly the things this playground is careful not to leak.
        """
        principal_from_request(request)
        if not settings.is_test_mode:
            raise HTTPException(
                status_code=409,
                detail="The wire inspector is test-mode only. In live mode, verify a run by its "
                "agent_id/run_id in the chat transcript and in the Nimble dashboard.",
            )
        base = settings.nimble_base_url
        if not base:
            raise HTTPException(status_code=409, detail="No NIMBLE_BASE_URL configured; fake server not in use.")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{base.rstrip('/')}/__fake/requests", params={"limit": limit})
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Fake Nimble server unreachable: {type(exc).__name__}")

    @router.post("/wire/reset")
    async def wire_reset(request: Request) -> Dict[str, Any]:
        principal_from_request(request)
        if not settings.is_test_mode or not settings.nimble_base_url:
            raise HTTPException(status_code=409, detail="Test-mode only.")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{settings.nimble_base_url.rstrip('/')}/__fake/reset")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Fake Nimble server unreachable: {type(exc).__name__}")

    return router
