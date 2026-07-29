"""Status-poll pacing, per .claude/rules/wsa-polling-default.md.

The rule: a Nimble WSA / Agent API V2 integration polls run status once every
10 seconds by default; the interval is configurable; tests may shorten it only
as an explicit test-only override; the rule does not apply to SSE/WebSocket
delivery, UI animation, or browser waits; a bounded deadline and terminal-state
handling still apply; and a non-idempotent create is never retried.

The suite as a whole runs with a 0s test-only override (see conftest), so these
tests assert the *default* and the *pacing logic* directly rather than by
waiting on a clock.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nimble_agentos.scripted_model import MAX_STATUS_POLLS, ScriptedNimbleModel
from nimble_agentos.settings import (
    DEFAULT_POLL_DEADLINE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    Settings,
)


class FakeMessage:
    """Stands in for an agno tool-result message."""

    def __init__(self, tool_name: str, content: str) -> None:
        self.role = "tool"
        self.tool_name = tool_name
        self.content = content


def started(run_id: str = "task_run_test0001") -> FakeMessage:
    return FakeMessage(
        "start_agent_run",
        f'{{"agent_id": "wsa_1", "run_id": "{run_id}", "status": "queued", "is_active": true}}',
    )


def status(state: str = "running") -> FakeMessage:
    return FakeMessage(
        "get_agent_run_status",
        f'{{"run_id": "task_run_test0001", "agent_id": "wsa_1", "status": "{state}", "is_active": true}}',
    )


# -- the default ------------------------------------------------------------


def test_the_documented_default_is_ten_seconds():
    assert DEFAULT_POLL_INTERVAL_SECONDS == 10.0


def test_settings_default_to_the_convention():
    assert Settings().poll_interval_seconds == 10.0
    assert Settings().poll_deadline_seconds == DEFAULT_POLL_DEADLINE_SECONDS


def test_the_interval_is_configurable(monkeypatch):
    from nimble_agentos.settings import load_settings

    monkeypatch.setenv("NIMBLE_EDGE_SECRET", "x")
    monkeypatch.setenv("NIMBLE_POLL_INTERVAL_SECONDS", "3.5")
    assert load_settings().poll_interval_seconds == 3.5


def test_zero_is_accepted_as_an_explicit_test_only_override(monkeypatch):
    from nimble_agentos.settings import load_settings

    monkeypatch.setenv("NIMBLE_POLL_INTERVAL_SECONDS", "0")
    assert load_settings().poll_interval_seconds == 0.0


# -- what is paced, and what is not ----------------------------------------


def test_creating_a_run_is_never_paced():
    """The interval applies only after a run exists, so create is immediate."""
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    response, delay = model._decide([FakeMessage("user", "q")] and [])
    assert delay == 0.0
    assert response.tool_calls[0]["function"]["name"] == "start_agent_run"


def test_the_first_status_poll_is_immediate():
    """A run that is already finished should not be made to wait."""
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    response, delay = model._decide([started()])
    assert response.tool_calls[0]["function"]["name"] == "get_agent_run_status"
    assert delay == 0.0


def test_subsequent_status_polls_are_paced_at_the_interval():
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    _, delay = model._decide([started(), status("running")])
    assert delay == 10.0
    _, delay = model._decide([started(), status("running"), status("running")])
    assert delay == 10.0


def test_fetching_the_result_is_not_paced():
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    response, delay = model._decide([started(), status("completed")])
    assert response.tool_calls[0]["function"]["name"] == "get_agent_run_result"
    assert delay == 0.0


# -- bounded deadline and terminal states ----------------------------------


def test_the_deadline_bounds_the_number_of_polls():
    model = ScriptedNimbleModel(poll_interval_seconds=10.0, poll_deadline_seconds=30.0)
    assert model._poll_budget() == 3


def test_the_hard_ceiling_applies_when_the_interval_is_zero():
    """With no interval the deadline cannot bound anything, so the count does."""
    model = ScriptedNimbleModel(poll_interval_seconds=0.0)
    assert model._poll_budget() == MAX_STATUS_POLLS


def test_exceeding_the_deadline_stops_polling_without_inventing_a_terminal_state():
    model = ScriptedNimbleModel(poll_interval_seconds=10.0, poll_deadline_seconds=20.0)
    history = [started()] + [status("running")] * model._poll_budget()
    response, delay = model._decide(history)
    assert response.tool_calls is None or not response.tool_calls
    assert "still active server-side" in response.content
    assert "was not cancelled" in response.content
    assert delay == 0.0


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_terminal_states_end_the_poll_loop(terminal):
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    response, _ = model._decide([started(), status(terminal)])
    names = [call["function"]["name"] for call in (response.tool_calls or [])]
    assert "get_agent_run_status" not in names


# -- the pacing actually happens -------------------------------------------


# -- robustness of the state reconstruction --------------------------------


def user(text: str = "a question") -> FakeMessage:
    message = FakeMessage("", "")
    message.role = "user"
    message.content = text
    return message


def result(state: str = "completed") -> FakeMessage:
    return FakeMessage("get_agent_run_result", f'{{"state": "{state}", "run_id": "task_run_test0001"}}')


def test_a_previous_turns_run_is_not_mistaken_for_this_turns():
    """Cumulative history must not short-circuit a new question.

    agno currently hands the model one turn at a time, so this cannot happen
    today -- but the driver must not *depend* on that, because the failure mode
    is a silently replayed stale answer rather than an error.
    """
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    history = [
        user("first question"),
        started(),
        status("completed"),
        result("completed"),
        # A new turn begins here.
        user("second, different question"),
    ]
    response, delay = model._decide(history)
    names = [call["function"]["name"] for call in (response.tool_calls or [])]
    assert names == ["start_agent_run"], f"expected a fresh run, got {names or response.content[:80]!r}"
    assert delay == 0.0


def test_a_not_ready_result_is_treated_as_transient_not_terminal():
    """The documented race: status says completed, the result endpoint 409s.

    The toolkit surfaces that as {"state": "not_ready"}. Rendering it as a final
    answer would report a still-running run as finished.
    """
    model = ScriptedNimbleModel(poll_interval_seconds=10.0)
    response, _ = model._decide([user(), started(), status("completed"), result("not_ready")])
    names = [call["function"]["name"] for call in (response.tool_calls or [])]
    assert names, "a not_ready result must lead to another call, not a final answer"
    assert "not_ready" not in (response.content or "")


def test_the_async_path_sleeps_without_blocking_the_event_loop():
    """AgentOS streams the run on this loop; a blocking sleep would stall SSE."""
    model = ScriptedNimbleModel(poll_interval_seconds=0.25)

    async def scenario():
        ticks = 0

        async def other_client():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        started_at = time.monotonic()
        await asyncio.gather(model.ainvoke(messages=[started(), status("running")]), other_client())
        return time.monotonic() - started_at, ticks

    elapsed, ticks = asyncio.run(scenario())
    assert elapsed >= 0.25, "the poll interval was not honoured"
    # The concurrent task kept running, so the loop was never blocked.
    assert ticks == 5


def test_the_sync_path_also_honours_the_interval():
    model = ScriptedNimbleModel(poll_interval_seconds=0.2)
    started_at = time.monotonic()
    model.invoke(messages=[started(), status("running")])
    assert time.monotonic() - started_at >= 0.2
