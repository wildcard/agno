"""The local edge shim's half of the trust contract.

The origin's tests prove it refuses anything unsigned. These prove the *other*
half: that the shim removes client-supplied identity before it adds its own, so
a crafted header never reaches the origin at all.

Both halves matter. The origin's signature check is what makes the system safe;
the shim's stripping is what makes a misconfigured or downgraded origin less
catastrophic.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from nimble_agentos.edge import (
    UI_DOCUMENT,
    UI_PREFIX,
    _HOP_BY_HOP,
    _STRIP_UPSTREAM,
    _sanitised_request_headers,
    build_edge_app,
)
from nimble_agentos.security import CLIENT_FORBIDDEN_HEADERS, EDGE_ASSERTION_HEADER


class FakeRequest:
    """Minimal stand-in exposing the one attribute the sanitiser reads."""

    def __init__(self, headers: dict) -> None:
        self.headers = Headers(headers)


def test_ui_route_prefix_never_contains_cache_busting_query() -> None:
    """FastAPI route patterns are paths; query strings belong only in iframe URLs."""
    assert UI_PREFIX == "/ui"
    assert "?" not in UI_PREFIX
    assert UI_DOCUMENT.startswith(f"{UI_PREFIX}?")


@pytest.mark.parametrize("header", CLIENT_FORBIDDEN_HEADERS)
def test_every_identity_header_is_stripped_before_forwarding(header):
    sanitised = _sanitised_request_headers(FakeRequest({header: "attacker-supplied", "accept": "*/*"}))
    assert header not in {key.lower() for key in sanitised}
    # An ordinary header still passes through, so this is stripping, not blanking.
    assert sanitised.get("accept") == "*/*"


def test_the_assertion_header_itself_cannot_be_supplied_by_the_client():
    """The one that matters most: it is removed, then re-added by the shim."""
    sanitised = _sanitised_request_headers(FakeRequest({EDGE_ASSERTION_HEADER: "forged.jws.token"}))
    assert EDGE_ASSERTION_HEADER not in {key.lower() for key in sanitised}


def test_the_session_cookie_is_not_forwarded_upstream():
    """The cookie is the edge's business; the origin authenticates on the assertion.

    Cookies are shared across ports on a hostname, so forwarding would also hand
    any unrelated localhost app's cookie to our backend.
    """
    sanitised = _sanitised_request_headers(FakeRequest({"cookie": "nimble_edge_session=abc; other=1"}))
    assert "cookie" not in {key.lower() for key in sanitised}


@pytest.mark.parametrize("header", sorted(_HOP_BY_HOP))
def test_hop_by_hop_headers_are_not_proxied(header):
    sanitised = _sanitised_request_headers(FakeRequest({header: "whatever"}))
    assert header not in {key.lower() for key in sanitised}


def test_strip_set_covers_identity_and_cookie():
    assert {h.lower() for h in CLIENT_FORBIDDEN_HEADERS} <= _STRIP_UPSTREAM
    assert "cookie" in _STRIP_UPSTREAM


def test_the_shim_refuses_to_start_without_a_secret(settings):
    """It cannot mint assertions, so starting would be a silent no-auth proxy."""
    import dataclasses

    with pytest.raises(RuntimeError, match="NIMBLE_EDGE_SECRET"):
        build_edge_app(dataclasses.replace(settings, edge_secret=""))


def test_unauthenticated_requests_are_refused_before_proxying(settings):
    """The shim must not forward at all without a session.

    Asserted via the response body rather than only the status, so this cannot
    pass because of an unrelated upstream failure.
    """
    from fastapi.testclient import TestClient

    with TestClient(build_edge_app(settings)) as client:
        response = client.get("/agents")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthenticated"


def test_signing_in_issues_an_httponly_cookie(settings):
    """HttpOnly is what keeps page scripts from reading the session."""
    from fastapi.testclient import TestClient

    with TestClient(build_edge_app(settings)) as client:
        response = client.post("/__edge/login", json={})
        assert response.status_code == 200
        cookie_header = response.headers.get("set-cookie", "")
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()
        # The cookie carries an opaque token, never the identity itself, so a
        # stolen cookie cannot be edited into a higher-privileged one.
        assert response.json()["subject"] not in cookie_header


def test_read_only_sign_in_yields_a_principal_without_the_run_scope(settings):
    from fastapi.testclient import TestClient

    from nimble_agentos.settings import SCOPE_RUN

    with TestClient(build_edge_app(settings)) as client:
        scopes = client.post("/__edge/login", json={"read_only": True}).json()["scopes"]
        assert SCOPE_RUN not in scopes
