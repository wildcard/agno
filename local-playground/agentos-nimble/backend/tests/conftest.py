"""Shared fixtures.

Every fixture here builds the *real* origin app against the *real* published
toolkit. The only substitution anywhere in this suite is the Nimble service
itself, which is replaced by the in-process fake. Nothing patches
``NimbleAgentTools``.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Dict, Iterator

import httpx
import pytest
import uvicorn

EDGE_SECRET = "test-edge-secret"
EDGE_AUDIENCE = "agentos-nimble-playground"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _BackgroundServer:
    """Runs an ASGI app on a real port, so the SDK makes real HTTP calls."""

    def __init__(self, app, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}"

    def __enter__(self) -> "_BackgroundServer":
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start in time")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture(scope="session")
def fake_nimble() -> Iterator[_BackgroundServer]:
    """The local Nimble Agent API V2 stand-in, on a real port."""
    # Zero delay: the browser demo wants visible progression, tests want speed.
    os.environ["FAKE_NIMBLE_POLL_DELAY"] = "0"
    import importlib

    from fake_nimble import server as server_module

    importlib.reload(server_module)
    with _BackgroundServer(server_module.create_fake_nimble_app(), _free_port()) as running:
        yield running


@pytest.fixture()
def settings(fake_nimble, tmp_path):
    from nimble_agentos.settings import Settings

    return Settings(
        run_mode="test",
        edge_secret=EDGE_SECRET,
        edge_audience=EDGE_AUDIENCE,
        nimble_api_key="test-shared-key",
        nimble_base_url=fake_nimble.base_url,
        # TEST-ONLY OVERRIDE of the 10s WSA status-poll default
        # (.claude/rules/wsa-polling-default.md). Zero here purely to avoid
        # wall-clock delay in the suite; the production default lives in
        # settings.DEFAULT_POLL_INTERVAL_SECONDS and is asserted in
        # test_poll_pacing.py.
        poll_interval_seconds=0.0,
    )


@pytest.fixture()
def origin_app(settings, tmp_path, monkeypatch):
    # NIMBLE_BASE_URL is how the unmodified toolkit's SDK client is redirected at
    # the fake. Setting it in the environment is exactly what the run scripts do.
    monkeypatch.setenv("NIMBLE_BASE_URL", settings.nimble_base_url or "")
    monkeypatch.setenv("NIMBLE_PLAYGROUND_DB", str(tmp_path / "agentos.db"))
    from nimble_agentos.app import build_origin_app

    return build_origin_app(settings)


@pytest.fixture()
def client(origin_app):
    from fastapi.testclient import TestClient

    with TestClient(origin_app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers() -> Dict[str, str]:
    from nimble_agentos.security import EDGE_ASSERTION_HEADER, mint_edge_assertion

    return {
        EDGE_ASSERTION_HEADER: mint_edge_assertion(
            secret=EDGE_SECRET,
            subject="test-operator",
            audience=EDGE_AUDIENCE,
            scopes=["nimble:run", "nimble:discover"],
        )
    }


@pytest.fixture()
def readonly_headers() -> Dict[str, str]:
    """A principal that may discover but must not start a billable run."""
    from nimble_agentos.security import EDGE_ASSERTION_HEADER, mint_edge_assertion

    return {
        EDGE_ASSERTION_HEADER: mint_edge_assertion(
            secret=EDGE_SECRET,
            subject="test-viewer",
            audience=EDGE_AUDIENCE,
            scopes=["nimble:discover"],
        )
    }


@pytest.fixture()
def wire_log(fake_nimble):
    """Read what the toolkit actually transmitted, and reset between tests."""
    httpx.post(f"{fake_nimble.base_url}/__fake/reset", timeout=5)

    def read(limit: int = 20):
        response = httpx.get(f"{fake_nimble.base_url}/__fake/requests", params={"limit": limit}, timeout=5)
        response.raise_for_status()
        return response.json()["requests"]

    return read
