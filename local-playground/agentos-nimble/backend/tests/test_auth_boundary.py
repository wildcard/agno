"""The origin's auth boundary, tested against the origin alone.

These tests deliberately talk to the origin with no edge in front of it. That is
the threat model: someone who can reach the origin directly must still be
refused. If these passed only through the edge shim, they would be testing
middleware ordering rather than the origin's own guarantee.
"""

from __future__ import annotations

import pytest

from nimble_agentos.security import EDGE_ASSERTION_HEADER, mint_edge_assertion

# Surfaces the browser can reach: AgentOS API, the UI's status probe, and the
# Nimble control plane.
PROTECTED_PATHS = ["/config", "/health", "/agents", "/nimble/api/session"]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_anonymous_requests_are_refused(client, path):
    response = client.get(path)
    assert response.status_code == 401, f"{path} must not be readable anonymously"
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_verified_assertion_is_admitted(client, auth_headers, path):
    assert client.get(path, headers=auth_headers).status_code == 200


def test_unauthenticated_run_is_refused(client):
    """The billable surface is protected, not just the read surface."""
    response = client.post(
        "/agents/nimble-web-search-agent/runs",
        data={"message": "should never execute", "stream": "false"},
    )
    assert response.status_code == 401


def test_liveness_is_public_and_says_nothing_useful(client):
    response = client.get("/__origin/live")
    assert response.status_code == 200
    body = response.json()
    # A liveness probe must not become a configuration oracle.
    assert set(body) == {"ok", "component"}


def test_assertion_signed_with_another_secret_is_refused(client):
    forged = mint_edge_assertion(
        secret="attacker-secret",
        subject="attacker",
        audience="agentos-nimble-playground",
        scopes=["nimble:run"],
    )
    response = client.get("/agents", headers={EDGE_ASSERTION_HEADER: forged})
    assert response.status_code == 401


def test_assertion_for_another_audience_is_refused(client):
    from tests.conftest import EDGE_SECRET

    wrong_audience = mint_edge_assertion(
        secret=EDGE_SECRET,
        subject="operator",
        audience="some-other-deployment",
        scopes=["nimble:run"],
    )
    assert client.get("/agents", headers={EDGE_ASSERTION_HEADER: wrong_audience}).status_code == 401


@pytest.mark.parametrize("header", ["x-nimble-user-id", "x-nimble-scopes", "x-forwarded-user"])
def test_client_supplied_identity_headers_are_refused(client, auth_headers, header):
    """Even alongside a *valid* assertion.

    Their presence can only mean a misconfigured edge, so the safe response is
    to refuse rather than to guess which identity was intended.
    """
    response = client.get("/agents", headers={**auth_headers, header: "administrator"})
    assert response.status_code == 401
    assert header in response.json()["rejected_headers"]


def test_browser_cannot_impersonate_another_user_via_the_form_field(client, auth_headers):
    """AgentOS prefers request.state.user_id, which only the middleware sets.

    A client that posts ``user_id`` for somebody else must not have it honoured.
    """
    response = client.post(
        "/agents/nimble-web-search-agent/runs",
        headers=auth_headers,
        data={
            "message": "hello",
            "stream": "false",
            "user_id": "somebody-else",
        },
    )
    assert response.status_code == 200
    # The run is recorded against the asserted principal, not the posted value.
    assert response.json().get("user_id") in (None, "test-operator")


def test_websocket_without_an_assertion_is_closed_with_a_policy_violation(client):
    """The boundary covers WebSocket, not only HTTP.

    Asserting the specific close code (1008, policy violation) rather than
    "some exception" matters: a missing route would also raise, and that would
    make this test pass for entirely the wrong reason. 1008 is the code this
    playground's middleware sends, so it identifies the refusal as ours.
    """
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()
    assert caught.value.code == 1008


def test_websocket_with_a_valid_assertion_is_not_closed_by_our_middleware(client, auth_headers):
    """Control for the test above: the refusal must be about the credential.

    Without this, 1008 could simply mean "this route always closes". A verified
    caller must get past our boundary; whatever AgentOS does next is its own
    business, so the assertion is only that the close reason is not ours.
    """
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect("/ws", headers=auth_headers) as websocket:
            websocket.send_json({"action": "ping"})
    except WebSocketDisconnect as exc:
        assert exc.code != 1008, "a verified principal was refused by the edge boundary"


def test_origin_refuses_to_start_without_a_secret(settings, tmp_path, monkeypatch):
    """Fail closed at boot rather than silently trusting everyone."""
    import dataclasses

    from nimble_agentos.app import build_origin_app

    monkeypatch.setenv("NIMBLE_PLAYGROUND_DB", str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError, match="NIMBLE_EDGE_SECRET"):
        build_origin_app(dataclasses.replace(settings, edge_secret=""))
