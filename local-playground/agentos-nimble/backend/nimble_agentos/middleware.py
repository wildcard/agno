"""Origin-side enforcement of the edge trust contract.

This is written as raw ASGI rather than a Starlette ``BaseHTTPMiddleware``
because ``BaseHTTPMiddleware`` only sees ``http`` scopes. AgentOS also serves a
WebSocket router, and an auth boundary that silently does not apply to
WebSockets is not an auth boundary. Raw ASGI sees ``http``, ``websocket``, and
``lifespan`` alike, so one implementation covers every surface the browser can
reach.

The middleware does exactly three things:

1. Refuses any request that carries a client-forgeable identity header other
   than the assertion itself. The edge strips these, so their presence means
   either a misconfigured edge or someone talking to the origin directly.
2. Verifies the edge assertion, failing closed on every error.
3. Publishes the verified principal on ``request.state`` in the two places
   agno reads: ``claims``/``scopes`` (which become ``RequestContext.trusted``)
   and ``user_id`` (which AgentOS prefers over any client-supplied form field).

Step 3 is what makes the browser unable to act as another user: AgentOS's run
router takes ``request.state.user_id`` over the ``user_id`` form value, so
session ownership follows the assertion rather than the request body.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, Iterable, MutableMapping, Optional, Sequence, Tuple

from .security import (
    CLIENT_FORBIDDEN_HEADERS,
    EDGE_ASSERTION_HEADER,
    AssertionError_,
    EdgePrincipal,
    verify_edge_assertion,
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Liveness path served by the origin itself, deliberately outside the AgentOS
# route table so that "is the process up" never requires a credential and can
# never be confused with the UI-facing ``/health`` route.
ORIGIN_LIVENESS_PATH = "/__origin/live"

# Headers that must not survive to the application, minus the assertion header
# which is consumed here.
_STRIPPED_HEADERS = tuple(h for h in CLIENT_FORBIDDEN_HEADERS if h != EDGE_ASSERTION_HEADER)


def _header_value(headers: Sequence[Tuple[bytes, bytes]], name: str) -> Optional[str]:
    wanted = name.lower().encode("latin-1")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def _present_headers(headers: Sequence[Tuple[bytes, bytes]], names: Iterable[str]) -> list:
    lowered = {name.lower().encode("latin-1") for name in names}
    return sorted({key.decode("latin-1").lower() for key, _ in headers if key.lower() in lowered})


class EdgeAssertionMiddleware:
    """Require a verified edge assertion on every request."""

    def __init__(
        self,
        app: Callable,
        *,
        secret: str,
        audience: str,
        public_paths: Iterable[str] = (ORIGIN_LIVENESS_PATH,),
    ) -> None:
        self.app = app
        self._secret = secret
        self._audience = audience
        self._public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._public_paths:
            await self.app(scope, receive, send)
            return

        headers: Sequence[Tuple[bytes, bytes]] = scope.get("headers") or []

        smuggled = _present_headers(headers, _STRIPPED_HEADERS)
        if smuggled:
            await self._reject(
                scope,
                send,
                reason="client-supplied identity headers are not accepted",
                detail={"rejected_headers": smuggled},
            )
            return

        token = _header_value(headers, EDGE_ASSERTION_HEADER)
        try:
            principal = verify_edge_assertion(
                token or "",
                secret=self._secret,
                audience=self._audience,
            )
        except AssertionError_ as exc:
            await self._reject(scope, send, reason=str(exc))
            return

        self._publish(scope, principal)
        await self.app(scope, receive, send)

    @staticmethod
    def _publish(scope: Scope, principal: EdgePrincipal) -> None:
        """Put the verified principal where agno and the control plane read it."""
        state: Dict[str, Any] = scope.setdefault("state", {})
        # agno's build_request_context() reads these two to populate
        # RequestContext.trusted, which factories use for authorization.
        state["claims"] = dict(principal.claims)
        state["scopes"] = frozenset(principal.scopes)
        # AgentOS's run router prefers request.state.user_id over the form field,
        # so this is what binds a session to its owner.
        state["user_id"] = principal.subject
        # Convenience handle for our own control-plane routes.
        state["edge_principal"] = principal

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        *,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Refuse the request on whichever protocol it arrived over."""
        if scope["type"] == "websocket":
            # 1008 = policy violation. Closing before accept means the handshake
            # never completes, so no application code runs for this connection.
            await send({"type": "websocket.close", "code": 1008, "reason": "unauthorized"})
            return

        body: Dict[str, Any] = {"error": "unauthorized", "reason": reason}
        if detail:
            body.update(detail)
        payload = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                    # No challenge scheme is advertised: authentication happens at
                    # the edge, and prompting the browser here would be wrong.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def principal_from_request(request: Any) -> EdgePrincipal:
    """Read the verified principal off a FastAPI request.

    Raises if absent. Any route that calls this is, by construction, unreachable
    without a verified assertion -- but raising rather than returning ``None``
    means a future route cannot accidentally treat "no principal" as "anonymous
    is fine".
    """
    principal = getattr(request.state, "edge_principal", None)
    if principal is None:
        raise RuntimeError("no verified edge principal on request; middleware is not installed")
    return principal
