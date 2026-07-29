"""A deterministic model that drives the Nimble run lifecycle.

``NimbleAgentTools`` is poll-driven on purpose: the model starts a run, polls
status until it is terminal, then fetches the result. In live mode a real LLM
makes those decisions. In test mode this model makes them from a fixed script,
so a run costs nothing and produces the same transcript every time.

This is a *scripted driver*, not a stub of the toolkit. It decides which tool to
call next by reading the actual tool results already in the conversation -- so
the real toolkit runs, the real Nimble SDK runs, and the real HTTP round trip
happens against the local fake. If any of those layers changed shape, this model
would follow the ``error`` branch and the test would fail rather than pass on a
comfortable fiction.

The subclassing pattern (implement ``invoke``/``ainvoke``/``*_stream`` plus the
two ``_parse_provider_response`` hooks) follows the test doubles in agno's own
unit suite, e.g. ``tests/unit/learn/test_max_updates_per_run.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence, Tuple

from agno.models.base import Model
from agno.models.response import ModelResponse

from .settings import DEFAULT_POLL_DEADLINE_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS

# Hard ceiling on status polls, independent of the wall-clock deadline. Two
# independent bounds means neither a stopped clock nor a zero interval can turn
# the loop into a spin.
MAX_STATUS_POLLS = 60

# Terminal states, mirroring the toolkit's own vocabulary.
_TERMINAL = {"completed", "failed", "cancelled"}


def _tool_messages(messages: Sequence[Any]) -> List[Any]:
    """Tool results belonging to the *current* turn only.

    Scoped to everything after the last user message. As of agno 2.8.x the model
    is handed one turn at a time, so this is currently a no-op -- but relying on
    that would make correctness depend on someone else's implementation detail.
    Without the scoping, a cumulative history would make the driver find a
    previous turn's ``start_agent_run`` and ``get_agent_run_result``, skip
    straight to "answer", and replay the earlier run's result for a new
    question: a silent wrong answer, the worst failure mode for a harness whose
    job is trustworthy evidence.

    ``test_a_second_message_in_the_same_session_starts_a_new_run`` pins the
    behaviour either way.
    """
    ordered = list(messages or [])
    last_user = -1
    for index, message in enumerate(ordered):
        if getattr(message, "role", None) == "user":
            last_user = index
    return [m for m in ordered[last_user + 1 :] if getattr(m, "role", None) == "tool"]


def _last_user_text(messages: Sequence[Any]) -> str:
    for message in reversed(list(messages or [])):
        if getattr(message, "role", None) == "user":
            content = getattr(message, "content", "") or ""
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def _decode(payload: Any) -> Dict[str, Any]:
    """Parse a tool result. The toolkit always returns a JSON string."""
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str):
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_name(message: Any) -> str:
    # agno populates tool_name on tool result messages; fall back to the
    # payload shape when a provider omits it.
    return getattr(message, "tool_name", None) or ""


class ScriptedNimbleModel(Model):
    """Walks start -> status(*) -> result -> final answer, from tool output alone."""

    def __init__(
        self,
        *,
        model_id: str = "nimble-scripted-driver",
        start_arguments: Optional[Dict[str, Any]] = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        poll_deadline_seconds: float = DEFAULT_POLL_DEADLINE_SECONDS,
    ) -> None:
        super().__init__(id=model_id, name=model_id, provider="nimble-playground")
        # Status-poll pacing, per the workspace WSA polling convention: one poll
        # every 10 seconds by default, applied only after the run exists. A test
        # may pass 0 as an explicit test-only override.
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.poll_deadline_seconds = max(0.0, float(poll_deadline_seconds))
        # The operator's configured run controls, already resolved by the agent
        # factory. Passing them here is what makes test mode *prove* something:
        # the configured use_case / skill / sources / schema travel as real
        # ``start_agent_run`` arguments, through the real toolkit, onto the wire.
        # In live mode the LLM is told the same values via instructions instead.
        self.start_arguments: Dict[str, Any] = dict(start_arguments or {})

    def __deepcopy__(self, memo: Dict[int, Any]) -> "ScriptedNimbleModel":
        # Agents deep-copy their model; this driver holds only resolved config,
        # so sharing the instance keeps identity stable across copies.
        return self

    # -- poll pacing --------------------------------------------------------

    def _poll_budget(self) -> int:
        """Max status polls allowed by the bounded deadline.

        With a zero interval (the test-only override) the deadline cannot bound
        anything, so the hard poll ceiling is the only bound that applies.
        """
        if self.poll_interval_seconds <= 0:
            return MAX_STATUS_POLLS
        by_deadline = int(self.poll_deadline_seconds // self.poll_interval_seconds)
        return max(1, min(MAX_STATUS_POLLS, by_deadline))

    def _pace_before_poll(self, polls_so_far: int) -> float:
        """Seconds to wait before the next status poll.

        The first poll is immediate -- a run that is already complete should not
        be made to wait -- and each subsequent poll is spaced by the interval,
        which is what "one poll every N seconds" means in practice.
        """
        return 0.0 if polls_so_far == 0 else self.poll_interval_seconds

    # -- decision logic -----------------------------------------------------

    def _decide(self, messages: Sequence[Any]) -> Tuple[ModelResponse, float]:
        """Choose the next action and how long to wait before taking it.

        Returns ``(response, delay_seconds)``. The delay is non-zero only ahead
        of a *status poll*: run creation and result collection are never paced,
        which keeps the interval strictly a poll cadence.
        """
        history = _tool_messages(messages)

        started: Optional[Dict[str, Any]] = None
        statuses: List[Dict[str, Any]] = []
        result: Optional[Dict[str, Any]] = None

        for message in history:
            payload = _decode(getattr(message, "content", None))
            name = _tool_name(message)
            if name == "start_agent_run" or ("run_id" in payload and "is_active" in payload and not name):
                if started is None:
                    started = payload
            elif name == "get_agent_run_status":
                statuses.append(payload)
            elif name == "get_agent_run_result":
                # ``not_ready`` is a transient race, not an outcome: the toolkit
                # returns it when the result endpoint 409s even though status
                # already read ``completed``. Treating it as a collected result
                # would render "the run ended in state not_ready", which reports
                # a live run as finished. Count it as a poll instead and let the
                # loop come back for it.
                if payload.get("state") == "not_ready":
                    statuses.append({"status": "running", "run_id": payload.get("run_id")})
                else:
                    result = payload

        # 1. Nothing started yet -> start the run with the operator's controls.
        #    Never paced, and never retried: creating a run is billable and is
        #    not idempotent.
        if started is None:
            arguments: Dict[str, Any] = {"query": _last_user_text(messages)}
            # Configured controls are merged in, never allowed to displace the
            # query itself. ``effort`` is absent here: the toolkit carries it as
            # a constructor argument, and omits the field entirely when unset.
            arguments.update({k: v for k, v in self.start_arguments.items() if k != "query"})
            return self._call("start_agent_run", arguments, call_id="nimble-start"), 0.0

        # A start that errored is terminal for this turn: the toolkit already
        # produced an actionable, redacted message, so surface it rather than
        # burning polls against a run that does not exist.
        if "error" in started:
            return (
                ModelResponse(role="assistant", content=self._error_answer("start the Nimble run", started)),
                0.0,
            )

        agent_id = started.get("agent_id")
        run_id = started.get("run_id")
        identity = {"run_id": run_id, "agent_id": agent_id}

        # 2. Poll status until terminal, paced and bounded.
        latest = statuses[-1] if statuses else None
        latest_status = (latest or {}).get("status")

        if "error" in (latest or {}):
            return (
                ModelResponse(role="assistant", content=self._error_answer("poll the Nimble run", latest or {})),
                0.0,
            )

        if latest is None or latest_status not in _TERMINAL:
            budget = self._poll_budget()
            if len(statuses) >= budget:
                # Bounded deadline reached. Report the run as still active rather
                # than inventing a terminal state for it.
                return (
                    ModelResponse(
                        role="assistant",
                        content=(
                            f"The Nimble run is still `{latest_status or 'unstarted'}` after {len(statuses)} status "
                            f"checks at {self.poll_interval_seconds:g}s intervals, so I stopped at the "
                            f"{self.poll_deadline_seconds:g}s deadline. Run `{run_id}` on agent `{agent_id}` "
                            "is still active server-side and was not cancelled."
                        ),
                    ),
                    0.0,
                )
            return (
                self._call("get_agent_run_status", identity, call_id=f"nimble-status-{len(statuses) + 1}"),
                self._pace_before_poll(len(statuses)),
            )

        # 3. Terminal. Fetch the result exactly once, unpaced.
        if result is None:
            return self._call("get_agent_run_result", identity, call_id="nimble-result"), 0.0

        # 4. Compose the grounded answer from the result envelope.
        return ModelResponse(role="assistant", content=self._final_answer(result)), 0.0

    @staticmethod
    def _call(name: str, arguments: Dict[str, Any], *, call_id: str) -> ModelResponse:
        return ModelResponse(
            role="assistant",
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        )

    @staticmethod
    def _error_answer(what: str, payload: Dict[str, Any]) -> str:
        code = payload.get("code") or "error"
        message = payload.get("error") or "Unknown error."
        return f"I could not {what}.\n\n- **code**: `{code}`\n- **detail**: {message}"

    @staticmethod
    def _final_answer(result: Dict[str, Any]) -> str:
        """Render the toolkit's result envelope as the assistant's answer.

        Deliberately surfaces ``usability`` rather than only the prose: a run can
        reach ``completed`` with no citations, and the toolkit distinguishes that
        as ``degraded``. Hiding it in the UI would make an ungrounded answer look
        exactly like a grounded one.
        """
        state = result.get("state")
        if state != "completed":
            detail = result.get("error") or result.get("message") or ""
            return f"The Nimble run ended in state `{state}`. {detail}".strip()

        output = result.get("output") or {}
        trust = output.get("trust") or {}
        content = output.get("content")
        usability = output.get("usability", "unknown")

        body = content if isinstance(content, str) else json.dumps(content, indent=2, sort_keys=True)

        lines = [body.strip(), "", "---", ""]
        badge = "grounded" if usability == "grounded" else "degraded"
        lines.append(
            f"**Nimble trust:** `{badge}` · confidence `{trust.get('confidence', 'unknown')}` · "
            f"{trust.get('source_count', 0)} source(s) · {trust.get('claim_count', 0)} cited claim(s)"
        )
        sources = trust.get("sources") or []
        if sources:
            lines.append("")
            lines.append("**Sources**")
            for index, source in enumerate(sources, start=1):
                title = (source.get("title") or source.get("url") or "").strip()
                url = source.get("url") or ""
                lines.append(f"{index}. [{title}]({url})")
        if usability != "grounded":
            lines.append("")
            lines.append(
                "> This run completed but is **not** grounded: Nimble returned no cited claim, "
                "or reported low confidence. Treat the answer as unverified."
            )
        return "\n".join(lines)

    # -- Model interface ----------------------------------------------------

    @staticmethod
    def _messages_of(args: Sequence[Any], kwargs: Dict[str, Any]) -> Sequence[Any]:
        return kwargs.get("messages") or (args[0] if args else [])

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        response, delay = self._decide(self._messages_of(args, kwargs))
        if delay:
            time.sleep(delay)
        return response

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        response, delay = self._decide(self._messages_of(args, kwargs))
        if delay:
            # asyncio.sleep, not time.sleep: AgentOS serves the run over SSE on
            # this event loop, and blocking it would stall every other client's
            # stream for the whole poll interval.
            await asyncio.sleep(delay)
        return response

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        response, delay = self._decide(self._messages_of(args, kwargs))
        if delay:
            time.sleep(delay)
        yield from self._chunks(response)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        response, delay = self._decide(self._messages_of(args, kwargs))
        if delay:
            await asyncio.sleep(delay)
        for chunk in self._chunks(response):
            yield chunk

    @staticmethod
    def _chunks(response: ModelResponse) -> Iterator[ModelResponse]:
        """Split a decision into stream chunks.

        Tool calls go out whole -- a partially assembled tool call has no meaning
        to the executor -- while prose is split so the UI shows tokens arriving
        rather than one atomic block.
        """
        if response.tool_calls:
            yield response
            return

        text = response.content or ""
        if not text:
            yield response
            return
        for chunk in _chunk_text(text):
            yield ModelResponse(role="assistant", content=chunk)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def _chunk_text(text: str, size: int = 48) -> Iterator[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]
