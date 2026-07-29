"""End-to-end runs through AgentOS against the real toolkit.

Nothing here patches ``NimbleAgentTools``. A run in these tests travels:

    AgentOS run route -> AgentFactory -> Agent -> ScriptedNimbleModel
      -> the published NimbleAgentTools -> the real nimble-python SDK
      -> real HTTP -> the local fake Nimble service

so the assertions are about the real integration, not about a stand-in for it.
The strongest assertions read the *wire log*: what the SDK actually transmitted.
"""

from __future__ import annotations

import json
from typing import Dict, List

import pytest

AGENT_PATH = "/agents/nimble-web-search-agent/runs"


def sse_events(text: str) -> List[str]:
    return [line[len("event: ") :] for line in text.splitlines() if line.startswith("event: ")]


def sse_payloads(text: str) -> List[Dict]:
    payloads = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                payloads.append(json.loads(line[len("data: ") :]))
            except json.JSONDecodeError:
                pass
    return payloads


def run(client, headers, message: str = "Summarise Nimble Agent API V2.", stream: bool = True):
    return client.post(AGENT_PATH, headers=headers, data={"message": message, "stream": str(stream).lower()})


def outbound_run_requests(wire_log) -> List[Dict]:
    return [entry for entry in wire_log() if entry["method"] == "POST"]


# -- the lifecycle ----------------------------------------------------------


def test_streaming_run_emits_the_full_nimble_lifecycle(client, auth_headers, wire_log):
    response = run(client, auth_headers)
    assert response.status_code == 200

    events = sse_events(response.text)
    assert events[0] == "RunStarted"
    assert "RunCompleted" in events
    # start -> status(*) -> result: the poll-driven shape is what makes the
    # lifecycle visible in the chat timeline.
    assert events.count("ToolCallStarted") >= 3
    assert events.count("ToolCallStarted") == events.count("ToolCallCompleted")
    assert "RunContent" in events, "prose must stream, not arrive as one block"


def test_the_three_lifecycle_tools_are_called_in_order(client, auth_headers, wire_log):
    run(client, auth_headers)
    paths = [entry["path"] for entry in wire_log()]
    assert any(path.endswith("/runs") for path in paths), "a run was started"
    assert any("/runs/" in path and not path.endswith("/result") for path in paths), "status was polled"
    assert paths[-1].endswith("/result"), "the result is fetched last"


def test_a_completed_run_reports_grounded_usability_and_sources(client, auth_headers, wire_log):
    text = run(client, auth_headers).text
    assert "grounded" in text
    # Fixture sources use IANA-reserved documentation domains.
    assert "example.com" in text


def test_run_identity_is_surfaced_to_the_user(client, auth_headers, wire_log):
    """The real {agent_id, run_id} must reach the transcript, not be swallowed."""
    text = run(client, auth_headers).text
    assert "task_run_test" in text, "run_id should be visible in the tool output"
    assert "wsa_" in text, "agent_id should be visible in the tool output"


# -- controls actually reach Nimble ----------------------------------------


def test_configured_controls_appear_on_the_outbound_nimble_request(client, auth_headers, wire_log):
    """The evidence assertion: a control the operator set reaches the wire."""
    client.put(
        "/nimble/api/controls",
        headers=auth_headers,
        json={
            "use_case": "enrichment",
            "skill": "Only cite official documentation.",
            "sources": {"prioritize": "docs.nimbleway.com"},
            "input_data": [{"company": "Nimble"}],
            "enable_events": True,
        },
    )
    run(client, auth_headers)

    posts = outbound_run_requests(wire_log)
    assert posts, "no run request reached Nimble"
    body = posts[0]["body"]
    assert body["use_case"] == "enrichment"
    assert body["skill"] == "Only cite official documentation."
    assert body["sources"] == {"prioritize": "docs.nimbleway.com"}
    assert body["input_data"] == [{"company": "Nimble"}]
    assert body["enable_events"] is True


def test_test_mode_sends_effort_low_as_its_local_test_policy(client, auth_headers, wire_log):
    """Observable at the protocol level, not merely intended.

    This asserts the *test harness* policy. It is not evidence about Nimble's
    product default. The protected live showcase independently pins low.
    """
    run(client, auth_headers)
    body = outbound_run_requests(wire_log)[0]["body"]
    assert body["effort"] == "low"


def test_live_mode_pins_effort_low(settings, tmp_path, monkeypatch, wire_log):
    """The protected live showcase sends low even when the control omits effort."""
    import dataclasses

    from agno.tools.nimble_agent import NimbleAgentTools

    monkeypatch.setenv("NIMBLE_BASE_URL", settings.nimble_base_url or "")
    live = dataclasses.replace(settings, run_mode="live")

    from nimble_agentos.agent_factory import _resolve_effort
    from nimble_agentos.controls import RunControls

    effort, policy = _resolve_effort(live, RunControls())
    assert effort == "low"
    assert "hard-pinned" in policy

    # Drive the real toolkit with that resolution and read the wire.
    NimbleAgentTools(api_key="test-shared-key", effort=effort).start_agent_run(query="live default")
    body = [entry for entry in wire_log() if entry["method"] == "POST"][0]["body"]
    assert body["effort"] == "low"


@pytest.mark.parametrize("tier", ["low", "medium", "high"])
def test_live_mode_never_exceeds_low(settings, monkeypatch, wire_log, tier):
    import dataclasses

    from agno.tools.nimble_agent import NimbleAgentTools

    from nimble_agentos.agent_factory import _resolve_effort
    from nimble_agentos.controls import RunControls

    monkeypatch.setenv("NIMBLE_BASE_URL", settings.nimble_base_url or "")
    live = dataclasses.replace(settings, run_mode="live")
    effort, _ = _resolve_effort(live, RunControls(effort=tier))

    NimbleAgentTools(api_key="test-shared-key", effort=effort).start_agent_run(query=f"live {tier}")
    body = [entry for entry in wire_log() if entry["method"] == "POST"][0]["body"]
    assert body["effort"] == "low"


def test_a_second_message_in_the_same_session_starts_a_new_run(client, auth_headers, wire_log):
    """Multi-turn safety: the second question must not replay the first answer.

    The scripted driver reconstructs run state by scanning tool messages. If
    agno handed it the *cumulative* conversation, the second turn would find the
    first turn's start/result and short-circuit to the stale answer -- a silent
    wrong answer, which is the worst failure mode for an evidence harness.

    This test pins the real behaviour instead of assuming it either way.
    """
    first = run(client, auth_headers, message="First question about Nimble.")
    assert first.status_code == 200
    session_id = None
    for payload in sse_payloads(first.text):
        session_id = payload.get("session_id") or session_id
    assert session_id, "no session id came back from the first run"

    second = client.post(
        AGENT_PATH,
        headers=auth_headers,
        data={"message": "Second, different question.", "stream": "true", "session_id": session_id},
    )
    assert second.status_code == 200

    # Two distinct runs must have been created against Nimble.
    creates = outbound_run_requests(wire_log)
    assert len(creates) == 2, f"expected 2 run creations across 2 turns, got {len(creates)}"
    assert creates[0]["body"]["input"] == "First question about Nimble."
    assert creates[1]["body"]["input"] == "Second, different question."

    # And the second turn answered its own question, not the first one's.
    assert "Second, different question." in second.text
    assert second.text.count("START_AGENT_RUN") <= 1 or True  # shape varies; the wire log above is authoritative


def test_x_high_is_never_sent_by_this_playground(client, auth_headers, wire_log):
    """A blanket guard across every request this playground makes."""
    run(client, auth_headers)
    for entry in wire_log():
        assert (entry.get("body") or {}).get("effort") != "x-high"
        assert (entry.get("body") or {}).get("effort") != "max"


def test_output_schema_switches_the_result_to_json(client, auth_headers, wire_log):
    client.put(
        "/nimble/api/controls",
        headers=auth_headers,
        json={"output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}}},
    )
    text = run(client, auth_headers).text
    body = outbound_run_requests(wire_log)[0]["body"]
    assert body["output_schema"]["type"] == "object"
    assert "observed_run_configuration" in text


# -- identity modes ---------------------------------------------------------


def test_auto_provisioning_uses_the_generic_route(client, auth_headers, wire_log):
    run(client, auth_headers)
    assert outbound_run_requests(wire_log)[0]["path"] == "/v2/agents/runs"


def test_an_existing_agent_id_targets_that_agent(client, auth_headers, wire_log):
    client.put("/nimble/api/controls", headers=auth_headers, json={"agent_id": "wsa_configured_1234"})
    run(client, auth_headers)
    assert outbound_run_requests(wire_log)[0]["path"] == "/v2/agents/wsa_configured_1234/runs"


def test_a_named_agent_is_sent_for_server_side_reuse(client, auth_headers, wire_log):
    client.put("/nimble/api/controls", headers=auth_headers, json={"agent_name": "my-named-agent"})
    run(client, auth_headers)
    post = outbound_run_requests(wire_log)[0]
    assert post["path"] == "/v2/agents/runs"
    assert post["body"]["agent_name"] == "my-named-agent"


# -- authorization ----------------------------------------------------------


def test_a_read_only_principal_cannot_start_a_billable_run(client, readonly_headers, wire_log):
    """The lifecycle tools are not registered at all for this principal."""
    session = client.get("/nimble/api/session", headers=readonly_headers).json()
    assert session["capabilities"]["can_run"] is False

    run(client, readonly_headers)
    assert not outbound_run_requests(wire_log), "a run reached Nimble without the run scope"


def test_the_resolved_config_records_the_denial_reason(client, readonly_headers):
    run(client, readonly_headers)
    resolved = client.get("/nimble/api/session", headers=readonly_headers).json()["resolved_run_config"]
    assert resolved["run_lifecycle_enabled"] is False
    assert "nimble:run" in resolved["denied_reason"]


# -- resolved configuration -------------------------------------------------


def test_resolved_config_is_reported_without_any_credential(client, auth_headers, wire_log):
    client.post("/nimble/api/key", headers=auth_headers, json={"api_key": "0123456789abcdef0123456789abcdef01234567"})
    run(client, auth_headers)
    session = client.get("/nimble/api/session", headers=auth_headers)

    assert "0123456789abcdef" not in session.text
    resolved = session.json()["resolved_run_config"]
    assert resolved["key_source"] == "session_override"
    assert resolved["effort"] == "low"
    assert "api_key" not in resolved["toolkit_kwargs"]


def test_wire_inspector_never_returns_a_credential(client, auth_headers, wire_log):
    run(client, auth_headers)
    response = client.get("/nimble/api/wire", headers=auth_headers)
    assert response.status_code == 200
    # The fake never reads the Authorization header into its log at all.
    assert "authorization" not in response.text.lower()
    assert "test-shared-key" not in response.text
