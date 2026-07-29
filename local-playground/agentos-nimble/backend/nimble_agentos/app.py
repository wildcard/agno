"""The AgentOS origin.

This process is what would sit *behind* Cloudflare. It has no notion of login,
no cookies, and no session of its own: it accepts a verified edge assertion or
it accepts nothing. Running it as its own process is what makes that testable --
a forged request can be aimed straight at it, with no edge in the way, and the
refusal is then a property of the origin rather than of middleware ordering.

Composition order matters and is asserted by tests:

    EdgeAssertionMiddleware   <- outermost; nothing runs without a principal
      AgentOS routes          <- /config /health /agents /sessions ...
      Nimble control plane    <- /nimble/api/*
"""

from __future__ import annotations

import os
from typing import Optional

from agno.agent import AgentFactory
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS
from fastapi import FastAPI

from .agent_factory import AGENT_ID, AGENT_NAME, ResolvedConfigRecorder, build_agent_factory
from .control_plane import build_control_plane
from .controls import ControlStore
from .middleware import ORIGIN_LIVENESS_PATH, EdgeAssertionMiddleware
from .settings import Settings, load_settings


def build_origin_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the origin ASGI app."""
    settings = settings or load_settings()

    if not settings.edge_secret:
        # Failing at boot rather than at first request: an origin that starts
        # without a secret and refuses everything looks like a bug, whereas one
        # that refuses to start states the cause once, clearly.
        raise RuntimeError(
            "NIMBLE_EDGE_SECRET is not set. The origin trusts only edge-signed assertions "
            "and will not start without the verification secret."
        )

    store = ControlStore()
    recorder = ResolvedConfigRecorder()

    # Session storage. AgentOS requires a db for factories so the UI can list
    # and resume sessions; sqlite keeps the spike to a single process.
    db_path = os.getenv("NIMBLE_PLAYGROUND_DB", "./.data/agentos_nimble.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = SqliteDb(db_file=db_path)

    base_app = FastAPI(
        title="AgentOS x Nimble Agent API V2 playground (origin)",
        description="Behind-the-edge origin. Requires a verified edge assertion on every request.",
    )

    @base_app.get(ORIGIN_LIVENESS_PATH, include_in_schema=False)
    async def origin_liveness() -> dict:
        """Unauthenticated liveness. Deliberately says nothing about config."""
        return {"ok": True, "component": "origin"}

    base_app.include_router(build_control_plane(settings=settings, store=store, recorder=recorder))

    factory = AgentFactory(
        id=AGENT_ID,
        name=AGENT_NAME,
        db=db,
        factory=build_agent_factory(settings=settings, store=store, recorder=recorder),
    )

    agent_os = AgentOS(
        id="agentos-nimble-playground",
        name="Nimble Web Search Playground",
        description="AgentOS playground for Nimble's Agent API V2 toolkit.",
        agents=[factory],
        base_app=base_app,
        db=db,
        telemetry=False,
    )
    app = agent_os.get_app()

    # Added last so it wraps every route AgentOS registered. Raw-ASGI middleware
    # added via add_middleware runs outside the router, covering WebSocket
    # upgrades as well as HTTP.
    app.add_middleware(
        EdgeAssertionMiddleware,
        secret=settings.edge_secret,
        audience=settings.edge_audience,
    )

    # Handles for tests and for the edge shim's introspection.
    app.state.nimble_settings = settings
    app.state.nimble_store = store
    app.state.nimble_recorder = recorder
    return app


# Intentionally no module-level ``app``: building one at import time would make
# importing this module for a test fail whenever the secret is unset, and would
# hide the boot-time check behind an ImportError. Serve with uvicorn's factory
# mode instead:
#
#     uvicorn nimble_agentos.app:build_origin_app --factory
