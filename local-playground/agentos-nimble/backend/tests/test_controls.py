"""Control-plane behaviour, with an emphasis on what must never leak."""

from __future__ import annotations

import pytest

from nimble_agentos.controls import ControlStore, RunControls

SECRET_LOOKING_KEY = "abcdef0123456789abcdef0123456789abcdef01"


# -- model ------------------------------------------------------------------


def test_identity_modes_are_derived_not_stored():
    assert RunControls().identity_mode == "auto_provision"
    assert RunControls(agent_id="wsa_1").identity_mode == "existing_agent_id"
    assert RunControls(agent_name="my-agent").identity_mode == "named_agent"


def test_agent_id_and_agent_name_together_are_rejected():
    """Mirrors the toolkit's own rule, but fails at configuration time."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        RunControls(agent_id="wsa_1", agent_name="my-agent")


def test_unknown_fields_are_rejected():
    # extra="forbid" catches a typo'd control rather than silently ignoring it.
    with pytest.raises(ValueError):
        RunControls(use_kase="research")


def test_oversized_input_data_is_rejected():
    with pytest.raises(ValueError, match="capped"):
        RunControls(input_data=[{"i": n} for n in range(101)])


# -- store ------------------------------------------------------------------


def test_session_override_takes_precedence_over_the_shared_key():
    store = ControlStore()
    assert store.resolve_api_key("p1", "shared") == "shared"
    store.set_key_override("p1", "override")
    assert store.resolve_api_key("p1", "shared") == "override"


def test_blank_override_clears_rather_than_storing_an_unusable_key():
    store = ControlStore()
    store.set_key_override("p1", "override")
    store.set_key_override("p1", "   ")
    assert store.resolve_api_key("p1", "shared") == "shared"


def test_profiles_are_isolated_per_principal():
    store = ControlStore()
    store.set_key_override("alice", "alice-key")
    store.set_controls("alice", RunControls(use_case="research"))
    assert store.resolve_api_key("bob", None) is None
    assert store.get("bob").controls.use_case is None


def test_public_view_reports_key_presence_but_never_the_value():
    store = ControlStore()
    profile = store.set_key_override("p1", SECRET_LOOKING_KEY)
    view = profile.public_view(shared_key_present=True)
    assert view["nimble_key"]["source"] == "session_override"
    assert view["nimble_key"]["session_override_present"] is True
    assert SECRET_LOOKING_KEY not in str(view)
    # Not even a length, which would narrow which key is in use.
    assert "length" not in str(view)


# -- HTTP surface -----------------------------------------------------------


def test_controls_round_trip_over_http(client, auth_headers):
    response = client.put(
        "/nimble/api/controls",
        headers=auth_headers,
        json={
            "use_case": "dataset_building",
            "skill": "Only official sources.",
            "sources": {"prioritize": "docs.nimbleway.com"},
            "enable_events": True,
        },
    )
    assert response.status_code == 200
    controls = response.json()["profile"]["controls"]
    assert controls["use_case"] == "dataset_building"
    assert controls["sources"] == {"prioritize": "docs.nimbleway.com"}
    assert controls["enable_events"] is True


def test_conflicting_identity_is_rejected_over_http(client, auth_headers):
    response = client.put(
        "/nimble/api/controls",
        headers=auth_headers,
        json={"agent_id": "wsa_1", "agent_name": "nope"},
    )
    assert response.status_code == 422


def test_key_override_is_never_echoed_by_any_endpoint(client, auth_headers):
    posted = client.post("/nimble/api/key", headers=auth_headers, json={"api_key": SECRET_LOOKING_KEY})
    assert posted.status_code == 200
    assert SECRET_LOOKING_KEY not in posted.text

    fetched = client.get("/nimble/api/session", headers=auth_headers)
    assert SECRET_LOOKING_KEY not in fetched.text
    assert fetched.json()["profile"]["nimble_key"]["source"] == "session_override"

    cleared = client.delete("/nimble/api/key", headers=auth_headers)
    assert cleared.json()["profile"]["nimble_key"]["source"] == "shared_server_key"


def test_test_mode_advertises_effort_as_a_local_test_policy(client, auth_headers):
    """Test mode pins effort, and says why -- without claiming it is a Nimble default."""
    effort = client.get("/nimble/api/session", headers=auth_headers).json()["effort"]
    assert effort["mode"] == "test"
    assert effort["selectable"] is False
    assert effort["value"] == "low"
    assert effort["choices"] == ["low"]
    assert "test policy" in effort["rationale"]
    assert "not a nimble product default" in effort["rationale"].lower()


def test_effort_cannot_be_set_in_test_mode(client, auth_headers):
    """Refused rather than silently ignored, so the console cannot show a lie."""
    response = client.put("/nimble/api/controls", headers=auth_headers, json={"effort": "high"})
    assert response.status_code == 409
    assert "fixed to 'low'" in response.json()["detail"]


def test_x_high_is_rejected_by_the_model_itself():
    """Not merely absent from a menu: the control type refuses it.

    x-high is excluded by this playground's policy even though the SDK and the
    toolkit both accept it.
    """
    with pytest.raises(ValueError):
        RunControls(effort="x-high")


def test_max_is_accepted_by_the_model_but_never_runnable():
    """Deliberately different from x-high, and the distinction is the point.

    ``max`` must be answerable with a coming-soon engagement notice, which means
    it has to survive validation long enough for the API to explain itself. A
    bare 422 would be the "silently unavailable" outcome the policy forbids.
    The refusal happens in the control plane instead -- see
    test_sdk_alignment.py::test_selecting_max_returns_an_actionable_engagement_notice.
    """
    from nimble_agentos.settings import LIVE_EFFORT_CHOICES

    assert RunControls(effort="max").effort == "max"
    assert "max" not in LIVE_EFFORT_CHOICES


@pytest.mark.parametrize("tier", ["low", "medium", "high"])
def test_selectable_tiers_are_accepted_by_the_model(tier):
    assert RunControls(effort=tier).effort == tier


def test_omitted_effort_is_the_default():
    """None means "omit the field", preserving the agent/template default."""
    assert RunControls().effort is None


def test_live_mode_is_fixed_to_low(settings):
    """The protected live showcase cannot select a higher-cost tier."""
    import dataclasses

    live = dataclasses.replace(settings, run_mode="live")
    policy = live.effort_policy()
    assert policy["selectable"] is False
    assert policy["choices"] == ["low"]
    assert policy["value"] == "low"
    assert policy["nimble_documented_default"] == "high"
    assert "cost-control policy" in policy["rationale"]
    assert set(policy["excluded"]) == {"medium", "high", "x-high", "max"}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/nimble/api/session", None),
        ("put", "/nimble/api/controls", {"use_case": "research"}),
        ("post", "/nimble/api/key", {"api_key": "abc123def456"}),
        ("delete", "/nimble/api/key", None),
    ],
)
def test_every_control_plane_route_returns_the_same_shape(client, auth_headers, method, path, body):
    """Regression: the console re-renders from whatever a mutation returns.

    ``capabilities`` and ``scopes`` were originally only on the GET, so saving
    controls returned a payload the console could not render and the save
    appeared to do nothing. Caught in the browser, not by the unit tests --
    hence this test, which pins the shape on every route.
    """
    call = getattr(client, method)
    response = call(path, headers=auth_headers, **({"json": body} if body else {}))
    assert response.status_code == 200
    payload = response.json()
    for key in ("principal", "scopes", "capabilities", "agent", "runtime", "effort", "poll", "profile"):
        assert key in payload, f"{method.upper()} {path} is missing '{key}'"
    assert "can_run" in payload["capabilities"]


def test_two_principals_do_not_see_each_others_configuration(client, auth_headers, readonly_headers):
    client.put("/nimble/api/controls", headers=auth_headers, json={"use_case": "enrichment"})
    other = client.get("/nimble/api/session", headers=readonly_headers).json()
    assert other["principal"] == "test-viewer"
    assert other["profile"]["controls"].get("use_case") is None
