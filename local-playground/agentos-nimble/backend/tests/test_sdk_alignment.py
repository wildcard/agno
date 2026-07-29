"""nimble-python 1.2.0 alignment, and the 'max' coming-soon policy.

1.2.0 promoted ``agent_name`` / ``use_case`` / ``skill`` from ``extra_body``
passthrough to typed parameters on both ``agents.run()`` and
``agents.runs.create()``. These tests assert the SDK actually exposes them, so
a downgraded environment fails loudly here rather than silently changing how run
options are transmitted.

They also pin the ``max`` policy: accepted by the API only so it can be answered
with an actionable engagement notice, never sent, and never silently downgraded.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging

import pytest

from nimble_agentos.controls import ControlProfile, RunControls
from nimble_agentos.settings import (
    EFFORT_COMING_SOON,
    EFFORT_COMING_SOON_POLICY,
    LIVE_EFFORT_CHOICES,
    REQUIRED_NIMBLE_SDK,
    Settings,
)

TYPED_RUN_FIELDS = ("agent_name", "use_case", "skill", "effort", "input_data", "output_schema", "sources")


# -- SDK floor --------------------------------------------------------------


def test_installed_sdk_meets_the_required_floor():
    from importlib.metadata import version

    installed = tuple(int(part) for part in version("nimble-python").split(".")[:3])
    required = tuple(int(part) for part in REQUIRED_NIMBLE_SDK.split(".")[:3])
    assert installed >= required, (
        f"nimble-python {'.'.join(map(str, installed))} is below the {REQUIRED_NIMBLE_SDK} floor. "
        "Below 1.2.0, agent_name/use_case/skill are not typed parameters and must be smuggled "
        "through extra_body."
    )


@pytest.mark.parametrize("field", TYPED_RUN_FIELDS)
def test_run_options_are_typed_parameters_not_extra_body(field):
    """The whole point of the 1.2.0 floor: these are first-class, not passthrough."""
    from nimble_python.resources.agents.agents import AgentsResource
    from nimble_python.resources.agents.runs import RunsResource

    assert field in inspect.signature(RunsResource.create).parameters, f"{field} missing from runs.create"
    # agents.run() is the agentless / auto-provision route and must expose the
    # same surface, or auto-provisioned runs would silently lose the option.
    assert field in inspect.signature(AgentsResource.run).parameters, f"{field} missing from agents.run"


def test_use_case_is_a_literal_so_a_typo_fails_locally():
    from nimble_python.resources.agents.runs import RunsResource

    annotation = str(inspect.signature(RunsResource.create).parameters["use_case"].annotation)
    for expected in ("research", "enrichment", "dataset_building"):
        assert expected in annotation


# -- the 'max' coming-soon policy ------------------------------------------


def test_max_is_accepted_by_the_model_so_it_can_be_answered():
    """Accepted at the type level, precisely so the API can explain itself.

    Rejecting it at validation would produce a bare 422 with no engagement
    notice, which is the "silently unavailable" outcome the policy forbids.
    """
    assert RunControls(effort=EFFORT_COMING_SOON).effort == EFFORT_COMING_SOON


def test_max_is_never_a_runnable_tier():
    assert EFFORT_COMING_SOON not in LIVE_EFFORT_CHOICES


def test_the_degradation_policy_is_reject_not_silent_downgrade():
    """x-high is barred here, so degrading to it is not an option."""
    assert EFFORT_COMING_SOON_POLICY == "reject"


def test_selecting_max_returns_an_actionable_engagement_notice(client, auth_headers):
    response = client.put("/nimble/api/controls", headers=auth_headers, json={"effort": "max"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "effort_tier_coming_soon"
    assert detail["degradation_policy"] == "reject"
    # Positive and actionable: it must say what to do next, not just "no".
    assert "contact" in (detail["message"] + detail["next_step"]).lower()
    assert "budget" in detail["message"].lower()
    # And it must not imply a quiet substitution happened.
    assert "downgrade" in detail["message"].lower()


def test_selecting_max_never_stores_it_or_reaches_the_wire(client, auth_headers, wire_log):
    client.put("/nimble/api/controls", headers=auth_headers, json={"effort": "max"})
    stored = client.get("/nimble/api/session", headers=auth_headers).json()
    assert stored["profile"]["controls"].get("effort") is None, "a rejected tier must not be stored"

    client.post(
        "/agents/nimble-web-search-agent/runs",
        headers=auth_headers,
        data={"message": "should not run at max", "stream": "false"},
    )
    for entry in wire_log():
        assert (entry.get("body") or {}).get("effort") != "max"


def test_x_high_remains_unreachable_even_now_that_max_is_accepted():
    """Widening the type for max must not have widened it for x-high."""
    with pytest.raises(ValueError):
        RunControls(effort="x-high")


# -- secrets must not leak through repr ------------------------------------


def test_settings_repr_does_not_expose_credentials():
    """Guards the likeliest accident: logging or printing the settings object."""
    secret = "0123456789abcdef0123456789abcdef01234567"
    settings = Settings(edge_secret=secret, nimble_api_key=secret, openai_api_key=secret)
    for rendered in (repr(settings), str(settings), f"{settings}"):
        assert secret not in rendered


def test_control_profile_repr_does_not_expose_the_key_override():
    secret = "abcdef0123456789abcdef0123456789abcdef01"
    profile = ControlProfile(nimble_api_key_override=secret)
    assert secret not in repr(profile)
    assert secret not in str(profile)


def test_logging_the_settings_object_does_not_write_the_key(caplog):
    secret = "fedcba9876543210fedcba9876543210fedcba98"
    settings = Settings(nimble_api_key=secret)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("settings=%s", settings)
    assert secret not in caplog.text


def test_dataclasses_asdict_still_carries_the_key_by_design(monkeypatch):
    """repr=False hides it from printing, not from deliberate extraction.

    Documented so nobody mistakes repr=False for encryption: code that
    explicitly asks for the field still gets it, which is what the factory needs.
    """
    secret = "11112222333344445555666677778888aaaabbbb"
    settings = Settings(nimble_api_key=secret)
    assert dataclasses.asdict(settings)["nimble_api_key"] == secret
    # But the redaction-safe summary never carries it.
    assert secret not in json.dumps(settings.describe())
