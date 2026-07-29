"""Edge-issued identity assertions.

This module is the whole trust contract between the protected edge (Cloudflare in
production, the local edge shim in development) and the AgentOS origin.

The rule the origin enforces is deliberately small enough to audit in one sitting:

    The origin trusts an identity if, and only if, it arrives as a short-lived
    assertion signed with a secret that only the edge holds.

Everything else follows from that. A browser cannot mint an assertion because it
does not have the secret; a stale assertion is refused because it carries an
expiry; an assertion minted for a different deployment is refused because it
carries an audience. The edge additionally *overwrites* the assertion header on
every proxied request, so a browser-supplied header never even reaches the
origin -- but the origin does not depend on that for its safety.

Nothing here reads or writes a Nimble API key. Credentials never travel in an
assertion; the assertion only names a principal and its scopes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Optional

# Header the edge overwrites on every proxied request. Named with an explicit
# ``Edge`` segment so that it reads, at any call site, as "this came from the
# edge" rather than "the client asked for this".
EDGE_ASSERTION_HEADER = "x-nimble-edge-assertion"

# Headers a client must never be able to influence. The edge strips these from
# the inbound request before it adds its own; the origin refuses any request
# that still carries one alongside a valid assertion, because that combination
# can only mean a misconfigured edge.
CLIENT_FORBIDDEN_HEADERS = (
    EDGE_ASSERTION_HEADER,
    "x-nimble-user-id",
    "x-nimble-scopes",
    "x-forwarded-user",
)

# An assertion is minted per proxied request, so it only has to outlive the hop
# from edge to origin. Keeping this small bounds the value of a stolen one.
DEFAULT_TTL_SECONDS = 120

# Tolerance for edge/origin clock drift when validating ``iat``/``exp``.
DEFAULT_LEEWAY_SECONDS = 30

_ALGORITHM = "HS256"


class AssertionError_(Exception):
    """Raised when an assertion cannot be trusted.

    Deliberately carries a coarse reason. The reason is safe to log and safe to
    return to the caller: it never contains the presented token, the expected
    signature, or the secret.
    """


@dataclass(frozen=True)
class EdgePrincipal:
    """The verified identity of a caller, as asserted by the edge."""

    subject: str
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    claims: Dict[str, Any] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, signing_input: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def mint_edge_assertion(
    *,
    secret: str,
    subject: str,
    audience: str,
    scopes: Iterable[str] = (),
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra_claims: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> str:
    """Mint an assertion. Only the edge ever calls this.

    The origin imports this function for tests only; in a real deployment the
    secret lives at the edge and the origin holds the verification half.
    """
    if not secret:
        raise ValueError("edge assertion secret must not be empty")
    if not subject:
        raise ValueError("edge assertion subject must not be empty")

    issued_at = int(now if now is not None else time.time())
    payload: Dict[str, Any] = dict(extra_claims or {})
    # Reserved claims are written last so a caller cannot override them via
    # extra_claims -- a factory must never see a spoofed sub/exp.
    payload.update(
        {
            "sub": subject,
            "aud": audience,
            "scp": sorted(set(scopes)),
            "iat": issued_at,
            "exp": issued_at + int(ttl_seconds),
        }
    )

    header = {"alg": _ALGORITHM, "typ": "nimble-edge+jws"}
    header_segment = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_segment = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _b64url_encode(_sign(secret, signing_input))
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def verify_edge_assertion(
    token: str,
    *,
    secret: str,
    audience: str,
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    now: Optional[float] = None,
) -> EdgePrincipal:
    """Verify an assertion and return the principal it names.

    Raises ``AssertionError_`` for every failure mode. The caller should treat
    any exception as "anonymous" and refuse the request; there is no partial
    trust.
    """
    if not secret:
        # Failing closed here matters: an origin booted without a secret must
        # refuse everything rather than silently trust every caller.
        raise AssertionError_("origin has no edge secret configured")
    if not token:
        raise AssertionError_("missing assertion")

    parts = token.split(".")
    if len(parts) != 3:
        raise AssertionError_("malformed assertion")
    header_segment, payload_segment, signature_segment = parts

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    try:
        presented = _b64url_decode(signature_segment)
    except Exception as exc:  # noqa: BLE001 - any decode failure is untrusted input
        raise AssertionError_("malformed assertion signature") from exc

    expected = _sign(secret, signing_input)
    # Constant-time comparison: a timing oracle here would leak the secret one
    # byte at a time.
    if not hmac.compare_digest(presented, expected):
        raise AssertionError_("bad assertion signature")

    try:
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError_("malformed assertion body") from exc

    if not isinstance(payload, dict) or not isinstance(header, dict):
        raise AssertionError_("malformed assertion body")
    # Pin the algorithm rather than reading it from the token, so a forged
    # header cannot downgrade us to "none".
    if header.get("alg") != _ALGORITHM:
        raise AssertionError_("unsupported assertion algorithm")

    current = float(now if now is not None else time.time())
    expires_at = payload.get("exp")
    issued_at = payload.get("iat")
    if not isinstance(expires_at, (int, float)):
        raise AssertionError_("assertion has no expiry")
    if current > float(expires_at) + leeway_seconds:
        raise AssertionError_("assertion expired")
    if isinstance(issued_at, (int, float)) and current + leeway_seconds < float(issued_at):
        raise AssertionError_("assertion not yet valid")

    if payload.get("aud") != audience:
        raise AssertionError_("assertion audience mismatch")

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AssertionError_("assertion has no subject")

    raw_scopes = payload.get("scp") or []
    if not isinstance(raw_scopes, list) or any(not isinstance(scope, str) for scope in raw_scopes):
        raise AssertionError_("assertion has malformed scopes")

    return EdgePrincipal(subject=subject, scopes=frozenset(raw_scopes), claims=dict(payload))
