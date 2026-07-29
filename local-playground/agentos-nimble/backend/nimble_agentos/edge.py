"""The local edge shim: a stand-in for Cloudflare.

In production this role belongs to a Cloudflare Worker (or Cloudflare Access)
in front of the origin. Locally it is this process. Either way the contract is
identical, which is the point:

* It is the **single origin the browser talks to**. The Nimble console, the
  official Agent UI, the AgentOS API, and the SSE stream are all same-origin,
  so the browser sends its session cookie automatically and the official UI
  needs no authentication code of its own.
* It **authenticates the human** (here: a development cookie; in production:
  Cloudflare Access SSO).
* It **overwrites** the identity header on every proxied request. Any value the
  browser supplied is discarded before the request leaves this process, so a
  crafted header cannot reach the origin at all.
* It **mints a short-lived signed assertion** the origin can verify on its own.

Nothing about the origin's safety depends on this shim being correct -- the
origin verifies the signature itself. The shim is defence in depth plus the
place where "who is this human" is answered.

This file is development scaffolding. It is not deployed, and it does not touch
the existing Cloudflare Worker.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .security import CLIENT_FORBIDDEN_HEADERS, EDGE_ASSERTION_HEADER, mint_edge_assertion
from .settings import SCOPE_DISCOVER, SCOPE_RUN, Settings, _env_flag, allow_insecure_bind, load_settings

# Interfaces on which an unauthenticated session selector is tolerable.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Paths the shim serves itself rather than proxying.
CONSOLE_PATH = "/"
LOGIN_PATH = "/__edge/login"
LOGOUT_PATH = "/__edge/logout"
WHOAMI_PATH = "/__edge/whoami"

# Prefix under which the official Agent UI is served. The vendored UI is
# configured with a matching Next.js ``basePath``, so it and the console share
# one origin without either one being rewritten.
# Keep the embedded Next.js document cache-busted with the deployed overlay.
# Its hashed assets remain cacheable, while the HTML shell must refresh when
# the protected same-origin endpoint policy changes.
UI_PREFIX = "/ui"
UI_DOCUMENT = f"{UI_PREFIX}?build=8ee82ac6"

# Hop-by-hop headers that must not be forwarded verbatim.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_FORBIDDEN = {h.lower() for h in CLIENT_FORBIDDEN_HEADERS}

# The browser's session cookie is the edge's business and nobody else's. The
# origin authenticates purely from the assertion, and the UI dev server has no
# use for it, so it is dropped at the boundary rather than forwarded upstream.
# Also drops any unrelated cookie the browser happens to hold for this host --
# cookies are shared across ports on a hostname, so another localhost app's
# cookie would otherwise be forwarded into our backend.
_STRIP_UPSTREAM = _FORBIDDEN | {"cookie"}


def _sanitised_request_headers(request: Request) -> Dict[str, str]:
    """Copy inbound headers, dropping hop-by-hop, identity, and cookie headers.

    This is the "overwrite" half of the contract. Identity headers are removed
    unconditionally *before* the shim adds its own, so there is no ordering in
    which a client-supplied value survives.
    """
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() not in _STRIP_UPSTREAM
    }


def _sanitised_response_headers(response: httpx.Response) -> Dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower() not in _HOP_BY_HOP}


def build_edge_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    trust_outer_edge = _env_flag("NIMBLE_TRUST_OUTER_EDGE", False)

    if not settings.edge_secret:
        raise RuntimeError("NIMBLE_EDGE_SECRET is not set; the edge shim cannot mint assertions without it.")

    # The shim's sign-in performs no authentication, so exposing it beyond
    # loopback would hand the shared Nimble key to anyone who can reach the
    # port. Refusing to build is better than a warning nobody reads.
    if settings.edge_host not in _LOOPBACK and not trust_outer_edge and not allow_insecure_bind():
        raise RuntimeError(
            f"Refusing to bind the edge shim to '{settings.edge_host}'. Its sign-in does not "
            "authenticate, so it is only safe on loopback. In production, Cloudflare replaces "
            "this shim entirely. Set NIMBLE_ALLOW_INSECURE_BIND=1 only if you understand this."
        )

    app = FastAPI(
        title="Nimble playground edge (local Cloudflare stand-in)",
        docs_url=None,
        redoc_url=None,
    )

    # Development session tokens. In-memory: restarting the shim logs everyone
    # out, and no session value is ever written to disk.
    sessions: Dict[str, Dict[str, Any]] = {}

    def current_session(request: Request) -> Optional[Dict[str, Any]]:
        if trust_outer_edge:
            subject = request.headers.get("x-agno-auth-email", "").strip()
            role = request.headers.get("x-agno-auth-role", "").strip()
            if subject and role in {"admin", "employee"}:
                return {"subject": subject, "scopes": [SCOPE_RUN, SCOPE_DISCOVER]}
            return None
        token = request.cookies.get(settings.session_cookie_name)
        return sessions.get(token) if token else None

    def mint_for(session: Dict[str, Any]) -> str:
        return mint_edge_assertion(
            secret=settings.edge_secret,
            subject=str(session["subject"]),
            audience=settings.edge_audience,
            scopes=session.get("scopes", ()),
        )

    # -- session ------------------------------------------------------------

    @app.post(LOGIN_PATH)
    async def login(request: Request) -> Response:
        """Development session selector. **This does not authenticate anyone.**

        Stated plainly because the distinction matters: in production
        *Cloudflare Access* authenticates the human and this endpoint does not
        exist. Locally there is no credential check at all, so anyone who can
        reach this port can obtain a session. Per-principal isolation downstream
        (control profiles, key overrides) is therefore an *organisational*
        boundary in local development, not a security one.

        Three things keep that from being worse than it sounds:

        * the shim binds to loopback and refuses to start off-loopback without
          an explicit opt-in (see ``build_edge_app``);
        * a caller-chosen ``subject`` requires an explicit opt-in, so the
          default is a single fixed local principal rather than "impersonate
          anyone";
        * a cross-site POST cannot select a subject, because doing so needs a
          JSON content type, which forces a preflight this app does not answer.

        The cookie itself is HttpOnly (page scripts cannot read it),
        SameSite=Lax, and carries an opaque random token rather than any claim,
        so a stolen cookie cannot be edited into a higher-privileged one.
        """
        # Reject a cross-site form post outright rather than relying on the
        # preflight side effect to save us -- that protection would silently
        # disappear the day someone adds CORS for a good reason.
        origin = request.headers.get("origin")
        if origin and origin not in {settings.edge_base_url, f"http://localhost:{settings.edge_port}"}:
            return JSONResponse({"error": "cross_origin_login_refused", "origin": origin}, status_code=403)

        body: Dict[str, Any] = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                body = {}

        requested_subject = body.get("subject")
        if requested_subject and not _env_flag("NIMBLE_ALLOW_SUBJECT_SELECTION"):
            # Multi-principal demos are useful, but they must be switched on
            # deliberately: an unauthenticated endpoint that mints any identity
            # on request is a footgun even on loopback.
            return JSONResponse(
                {
                    "error": "subject_selection_disabled",
                    "detail": (
                        "This endpoint does not authenticate. Choosing an arbitrary subject is "
                        "off by default; set NIMBLE_ALLOW_SUBJECT_SELECTION=1 to enable it for a "
                        "multi-principal demo."
                    ),
                },
                status_code=403,
            )

        subject = str(requested_subject or settings.local_edge_subject)
        # Read-only mode is offered so the scope gate can be demonstrated
        # without editing configuration.
        scopes = [SCOPE_DISCOVER] if body.get("read_only") else [SCOPE_RUN, SCOPE_DISCOVER]

        token = secrets.token_urlsafe(32)
        sessions[token] = {"subject": subject, "scopes": scopes}

        response = JSONResponse({"subject": subject, "scopes": scopes})
        response.set_cookie(
            settings.session_cookie_name,
            token,
            httponly=True,
            samesite="lax",
            # Secure is off only because local development is plain HTTP. In
            # production this must be True; see README "Auth boundary".
            secure=False,
            path="/",
        )
        return response

    @app.post(LOGOUT_PATH)
    async def logout(request: Request) -> Response:
        """End the session *and* clear the principal's Nimble key override.

        Dropping only the edge-side session would leave the override resident in
        the origin's memory: "log out" would not mean "my key is gone", and a
        later session for the same principal would silently inherit it. Since
        the origin has no session concept, the edge is the only place that knows
        a session ended, so it must do the clearing.
        """
        token = request.cookies.get(settings.session_cookie_name)
        session = sessions.pop(token, None) if token else None

        cleared = False
        if session:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    cleared_response = await client.delete(
                        f"{settings.origin_base_url}/nimble/api/key",
                        headers={EDGE_ASSERTION_HEADER: mint_for(session)},
                    )
                    cleared = cleared_response.status_code == 200
            except httpx.HTTPError:
                # Reported rather than raised: the session is already gone from
                # the edge, so the caller is logged out either way. Surfacing
                # cleared=false is more useful than a 502 that hides that.
                cleared = False

        response = JSONResponse({"ok": True, "key_override_cleared": cleared})
        response.delete_cookie(settings.session_cookie_name, path="/")
        return response

    @app.get(WHOAMI_PATH)
    async def whoami(request: Request) -> Response:
        session = current_session(request)
        if not session:
            return JSONResponse({"authenticated": False}, status_code=401)
        return JSONResponse(
            {
                "authenticated": True,
                "subject": session["subject"],
                "scopes": session["scopes"],
                "run_mode": settings.run_mode,
            }
        )

    # -- console ------------------------------------------------------------

    @app.get(CONSOLE_PATH, include_in_schema=False)
    async def console() -> HTMLResponse:
        from .console import render_console

        return HTMLResponse(render_console(ui_prefix=UI_DOCUMENT))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # -- proxy --------------------------------------------------------------

    async def proxy(request: Request, target_base: str, path: str, *, authenticate: bool) -> Response:
        session = current_session(request)
        if authenticate and not session:
            return JSONResponse(
                {"error": "unauthenticated", "hint": f"POST {LOGIN_PATH} first"},
                status_code=401,
            )

        headers = _sanitised_request_headers(request)
        if authenticate and session:
            # The overwrite: whatever the client sent is already gone, and this
            # is the only assertion the origin will ever see.
            headers[EDGE_ASSERTION_HEADER] = mint_for(session)

        url = urljoin(target_base.rstrip("/") + "/", path.lstrip("/"))
        body = await request.body()

        client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        upstream = client.build_request(
            request.method,
            url,
            headers=headers,
            params=request.query_params,
            content=body or None,
        )
        try:
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            return JSONResponse(
                {"error": "upstream_unreachable", "detail": type(exc).__name__, "url": url},
                status_code=502,
            )

        async def stream_body():
            # Streaming rather than buffering is what keeps SSE live: the agent
            # run's events must reach the browser as they are produced, not when
            # the response finishes.
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            headers=_sanitised_response_headers(response),
        )

    @app.api_route(f"{UI_PREFIX}{{path:path}}", methods=["GET", "POST", "HEAD", "OPTIONS"], include_in_schema=False)
    async def proxy_ui(request: Request, path: str) -> Response:
        """Serve the official Agent UI from this origin.

        Authenticated like everything else: an unauthenticated visitor should
        not be handed the app shell at all.
        """
        return await proxy(request, settings.ui_base_url, f"{UI_PREFIX}{path}", authenticate=True)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def proxy_origin(request: Request, path: str) -> Response:
        """Everything else is AgentOS or the Nimble control plane."""
        # Next.js dev assets live under /_next; route them to the UI server so
        # the vendored app can load its own chunks.
        if path.startswith("_next/") or path.startswith("__nextjs"):
            return await proxy(request, settings.ui_base_url, f"/{path}", authenticate=True)
        return await proxy(request, settings.origin_base_url, f"/{path}", authenticate=True)

    return app


def redirect_to_console() -> RedirectResponse:
    return RedirectResponse(CONSOLE_PATH)
