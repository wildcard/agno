"""A local stand-in for the Nimble Agent API V2.

This is a *fake*, not a mock. It speaks the real wire protocol on the real
paths, and the real ``nimble-python`` SDK parses its responses with the real
Pydantic models. That means the published ``NimbleAgentTools`` runs completely
unmodified against it: same client construction, same ``max_retries=0`` write
path, same ``with_options`` read path, same response parsing, same rendering.

The only substituted component is the upstream Nimble service itself.

Two consequences worth being explicit about:

* If a response here drifts from the published schema, the SDK raises rather
  than quietly passing -- so this file cannot silently diverge into fiction.
* Nothing produced here is evidence of live Nimble behaviour. Every payload is
  clearly marked as test-mode fixture data, and the console labels the mode.

The request log exists so the console can show what the toolkit *actually put
on the wire*. That is the honest way to demonstrate that a control the operator
changed reached Nimble, rather than asking the reader to trust a self-report.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# How many status polls a run spends active before completing. Counting polls
# rather than seconds keeps the transcript identical on a fast or slow machine.
POLLS_BEFORE_COMPLETE = int(os.getenv("FAKE_NIMBLE_POLLS_BEFORE_COMPLETE", "2"))

# Small delay per poll so a browser shows the run progressing rather than
# snapping to done. Tests set this to 0.
POLL_DELAY_SECONDS = float(os.getenv("FAKE_NIMBLE_POLL_DELAY", "0.6"))

# A key value the fake rejects, so the toolkit's 401 mapping can be exercised
# without anyone needing a real (or a really invalid) credential.
UNAUTHORIZED_SENTINEL = "test-unauthorized-key"

# Bounded request log for the console's wire view.
MAX_LOGGED_REQUESTS = 25

_ISO = "%Y-%m-%dT%H:%M:%S.%f%z"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_ISO)


def _fixture_sources() -> List[Dict[str, Any]]:
    # example.com / example.org are IANA-reserved documentation domains, so this
    # fixture can never be mistaken for a real retrieved source.
    return [
        {
            "type": "primary",
            "url": "https://example.com/test-mode-primary-source",
            "title": "TEST MODE fixture: primary source",
            "source_category": "official",
        },
        {
            "type": "secondary",
            "url": "https://example.org/test-mode-secondary-source",
            "title": "TEST MODE fixture: secondary source",
            "source_category": "news",
        },
    ]


class RunRecord:
    """One fake run's lifecycle state."""

    def __init__(self, *, run_id: str, agent_id: str, prompt: str, body: Dict[str, Any]) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.prompt = prompt
        self.body = body
        self.polls = 0
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.completed_at: Optional[str] = None

    @property
    def status(self) -> str:
        if self.polls == 0:
            return "queued"
        if self.polls <= POLLS_BEFORE_COMPLETE:
            return "running"
        return "completed"

    @property
    def is_active(self) -> bool:
        return self.status in {"queued", "running"}

    def envelope(self) -> Dict[str, Any]:
        return {
            "id": self.run_id,
            "created_at": self.created_at,
            "effort": self.body.get("effort", "low"),
            "interaction_id": f"interaction_{self.run_id}",
            "is_active": self.is_active,
            "status": self.status,
            "web_search_agent_id": self.agent_id,
            "prompt": self.prompt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def output(self) -> Dict[str, Any]:
        """Result payload, shaped by whether an output_schema was requested.

        The ``sources`` and ``skill`` the operator configured are echoed into the
        prose so a browser screenshot shows the configured control affecting the
        result, not just the request.
        """
        wants_json = isinstance(self.body.get("output_schema"), dict)

        # Report what was actually on the request, never a substituted default.
        # An earlier version printed "use_case=research" when the field was
        # absent, which made an unset control look like a chosen one -- exactly
        # the kind of small dishonesty that ends up in a screenshot.
        def observed(field: str) -> str:
            value = self.body.get(field)
            if value is None:
                return f"{field}=unset"
            # Short scalars are echoed so the effect of a control is legible;
            # long or structured values report presence to keep the line short.
            if isinstance(value, str) and len(value) <= 32:
                return f"{field}={value}"
            return f"{field}=set"

        detail_lines = [observed(field) for field in ("use_case", "skill", "sources", "input_data")]
        # effort is reported by presence, so an omitted effort is visibly omitted
        # rather than silently rendered as some default tier.
        detail_lines.append(
            f"effort={self.body['effort']}" if "effort" in self.body else "effort=omitted (agent/template default)"
        )

        if wants_json:
            content: Any = {
                "test_mode": True,
                "answer": f"TEST MODE fixture answer for: {self.prompt[:160]}",
                "observed_run_configuration": detail_lines,
            }
            claims = [
                {
                    "path": "$.answer",
                    "citations": [
                        {
                            "url": "https://example.com/test-mode-primary-source",
                            "title": "TEST MODE fixture: primary source",
                            "excerpts": ["Deterministic excerpt produced by the local fake."],
                        }
                    ],
                    "confidence": "high",
                    "reasoning": "Fixture claim emitted by the local fake Nimble server.",
                }
            ]
        else:
            content = (
                f"**TEST MODE fixture answer.** This text came from the local fake Nimble server, "
                f"not from the live Nimble service.\n\n"
                f"Question: {self.prompt[:300]}\n\n"
                f"Observed run configuration on the wire: {', '.join(detail_lines)}. [1]"
            )
            claims = [
                {
                    "callout": 1,
                    "citations": [
                        {
                            "url": "https://example.com/test-mode-primary-source",
                            "title": "TEST MODE fixture: primary source",
                            "excerpts": ["Deterministic excerpt produced by the local fake."],
                        }
                    ],
                    "confidence": "high",
                    "reasoning": "Fixture claim emitted by the local fake Nimble server.",
                }
            ]

        return {
            "type": "json" if wants_json else "text",
            "content": content,
            "trust": {
                "confidence": "high",
                "reasoning": "Deterministic fixture trust envelope.",
                "sources": _fixture_sources(),
                "claims": claims,
            },
        }


def create_fake_nimble_app() -> FastAPI:
    app = FastAPI(title="Fake Nimble Agent API V2", docs_url=None, redoc_url=None)

    runs: Dict[str, RunRecord] = {}
    wire_log: List[Dict[str, Any]] = []
    # Separate counters so an auto-provisioned agent does not consume a run
    # number: the first run of a session should read task_run_test0001 whichever
    # identity mode produced it.
    counter = {"runs": 0, "agents": 0}

    def log_request(method: str, path: str, body: Optional[Dict[str, Any]]) -> None:
        """Record the request for the console's wire view.

        The Authorization header is never captured -- not redacted, simply never
        read into the log -- so there is no code path that could leak it.
        """
        wire_log.append(
            {
                "at": _now(),
                "method": method,
                "path": path,
                "body": body,
            }
        )
        del wire_log[:-MAX_LOGGED_REQUESTS]

    def require_key(authorization: Optional[str]) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(status_code=401, detail="empty bearer token")
        if token == UNAUTHORIZED_SENTINEL:
            raise HTTPException(status_code=401, detail="invalid api key")
        return token

    def new_run(agent_id: str, body: Dict[str, Any]) -> RunRecord:
        counter["runs"] += 1
        run_id = f"task_run_test{counter['runs']:04d}"
        record = RunRecord(
            run_id=run_id,
            agent_id=agent_id,
            prompt=str(body.get("input") or ""),
            body=body,
        )
        runs[run_id] = record
        return record

    # -- run lifecycle ------------------------------------------------------

    @app.post("/v2/agents/{agent_id}/runs")
    async def create_run(agent_id: str, request: Request, authorization: Optional[str] = Header(None)):
        require_key(authorization)
        body = await request.json()
        log_request("POST", f"/v2/agents/{agent_id}/runs", body)
        return JSONResponse(new_run(agent_id, body).envelope())

    @app.post("/v2/agents/runs")
    async def create_run_autoprovision(request: Request, authorization: Optional[str] = Header(None)):
        """Generic route: Nimble provisions an agent and returns its id."""
        require_key(authorization)
        body = await request.json()
        log_request("POST", "/v2/agents/runs", body)
        counter["agents"] += 1
        agent_id = f"wsa_autoprovisioned_{counter['agents']:04d}"
        return JSONResponse(new_run(agent_id, body).envelope())

    @app.get("/v2/agents/{agent_id}/runs/{run_id}")
    async def get_run(agent_id: str, run_id: str, authorization: Optional[str] = Header(None)):
        require_key(authorization)
        log_request("GET", f"/v2/agents/{agent_id}/runs/{run_id}", None)
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        if record.is_active and POLL_DELAY_SECONDS:
            await asyncio.sleep(POLL_DELAY_SECONDS)
        record.polls += 1
        if record.polls == 1:
            record.started_at = _now()
        if not record.is_active and record.completed_at is None:
            record.completed_at = _now()
        return JSONResponse(record.envelope())

    @app.get("/v2/agents/{agent_id}/runs/{run_id}/result")
    async def get_result(agent_id: str, run_id: str, authorization: Optional[str] = Header(None)):
        require_key(authorization)
        log_request("GET", f"/v2/agents/{agent_id}/runs/{run_id}/result", None)
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        if record.is_active:
            # Matches the documented contract: 409 while queued or running.
            raise HTTPException(status_code=409, detail="run still active")
        return JSONResponse({"run": record.envelope(), "output": record.output()})

    # -- read-only discovery ------------------------------------------------

    @app.get("/v2/agents")
    async def list_agents(limit: int = 20, authorization: Optional[str] = Header(None)):
        require_key(authorization)
        log_request("GET", "/v2/agents", {"limit": limit})
        items = [
            {
                "id": "wsa_test_research_0001",
                "agent_name": "test-research-agent",
                "created_at": _now(),
                "updated_at": _now(),
                "description": "TEST MODE fixture agent.",
                "display_name": "Test Research Agent",
                "effort": "low",
                "goals": [],
                "icon": "search",
                "is_active": True,
                "skill": "Answer research questions with citations.",
                "sources": {},
                "suggested_questions": [],
                "use_case": "research",
            }
        ][:limit]
        return JSONResponse({"items": items, "total": len(items), "limit": limit, "offset": 0})

    @app.get("/v2/agents/templates")
    async def list_templates(limit: int = 20, authorization: Optional[str] = Header(None)):
        require_key(authorization)
        log_request("GET", "/v2/agents/templates", {"limit": limit})
        items = [
            {
                "id": "tpl_test_0001",
                "template_name": "test-research-template",
                "created_at": _now(),
                "updated_at": _now(),
                "description": "TEST MODE fixture template.",
                "display_name": "Test Research Template",
                "effort": "low",
                "goals": [],
                "icon": "search",
                "skill": "Answer research questions with citations.",
                "sources": [],
                "suggested_questions": [],
                "use_case": "research",
            }
        ][:limit]
        return JSONResponse({"items": items, "total": len(items), "limit": limit, "offset": 0})

    # -- inspection ---------------------------------------------------------

    @app.get("/__fake/requests")
    async def inspect_requests(limit: int = 10):
        """What the toolkit actually sent. Never includes credentials."""
        return {"mode": "test", "requests": wire_log[-limit:]}

    @app.post("/__fake/reset")
    async def reset():
        runs.clear()
        wire_log.clear()
        counter["n"] = 0
        return {"ok": True}

    @app.get("/__fake/live")
    async def live():
        return {"ok": True, "polls_before_complete": POLLS_BEFORE_COMPLETE}

    return app


app = create_fake_nimble_app()
