"""Unit tests for the edge assertion contract.

These are the tests that matter most: every other guarantee in the playground
rests on an assertion being unforgeable, expiring, and being bound to one
deployment.
"""

from __future__ import annotations

import time

import pytest

from nimble_agentos.security import (
    DEFAULT_LEEWAY_SECONDS,
    AssertionError_,
    mint_edge_assertion,
    verify_edge_assertion,
)

SECRET = "unit-test-secret"
AUDIENCE = "unit-test-audience"


def mint(**overrides) -> str:
    params = {
        "secret": SECRET,
        "subject": "operator-1",
        "audience": AUDIENCE,
        "scopes": ["nimble:run"],
    }
    params.update(overrides)
    return mint_edge_assertion(**params)


def test_round_trip_preserves_subject_and_scopes():
    principal = verify_edge_assertion(mint(), secret=SECRET, audience=AUDIENCE)
    assert principal.subject == "operator-1"
    assert principal.has_scope("nimble:run")
    assert not principal.has_scope("nimble:admin")


def test_signature_is_rejected_under_a_different_secret():
    token = mint()
    with pytest.raises(AssertionError_, match="signature"):
        verify_edge_assertion(token, secret="not-the-edge-secret", audience=AUDIENCE)


def test_tampering_with_the_payload_invalidates_the_signature():
    header, payload, signature = mint().split(".")
    # Any edit to the claims changes the signing input.
    forged = f"{header}.{payload[:-2]}AA.{signature}"
    with pytest.raises(AssertionError_):
        verify_edge_assertion(forged, secret=SECRET, audience=AUDIENCE)


def test_audience_binds_an_assertion_to_one_deployment():
    with pytest.raises(AssertionError_, match="audience"):
        verify_edge_assertion(mint(), secret=SECRET, audience="a-different-deployment")


def test_expired_assertion_is_rejected():
    token = mint(ttl_seconds=1, now=time.time() - 3600)
    with pytest.raises(AssertionError_, match="expired"):
        verify_edge_assertion(token, secret=SECRET, audience=AUDIENCE)


def test_assertion_from_the_future_is_rejected():
    token = mint(now=time.time() + 3600)
    with pytest.raises(AssertionError_, match="not yet valid"):
        verify_edge_assertion(token, secret=SECRET, audience=AUDIENCE)


def test_clock_skew_within_leeway_is_tolerated():
    # An edge whose clock is a few seconds ahead must not lock everyone out.
    token = mint(now=time.time() + (DEFAULT_LEEWAY_SECONDS - 5))
    assert verify_edge_assertion(token, secret=SECRET, audience=AUDIENCE).subject == "operator-1"


def test_missing_token_is_rejected():
    with pytest.raises(AssertionError_, match="missing"):
        verify_edge_assertion("", secret=SECRET, audience=AUDIENCE)


def test_malformed_token_is_rejected():
    with pytest.raises(AssertionError_, match="malformed"):
        verify_edge_assertion("not-a-jws", secret=SECRET, audience=AUDIENCE)


def test_origin_without_a_secret_trusts_nothing():
    # Fail closed: an origin booted without a secret must not accept a token
    # that happens to be well-formed.
    with pytest.raises(AssertionError_, match="no edge secret"):
        verify_edge_assertion(mint(), secret="", audience=AUDIENCE)


def test_alg_none_downgrade_is_refused():
    """A forged header must not be able to select a weaker algorithm."""
    import base64
    import json

    def seg(obj) -> str:
        raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    forged = f"{seg({'alg': 'none', 'typ': 'nimble-edge+jws'})}.{seg({'sub': 'attacker', 'aud': AUDIENCE, 'exp': time.time() + 60})}."
    with pytest.raises(AssertionError_):
        verify_edge_assertion(forged, secret=SECRET, audience=AUDIENCE)


def test_reserved_claims_cannot_be_overridden_by_extra_claims():
    """extra_claims must not be able to forge a subject or extend an expiry."""
    token = mint_edge_assertion(
        secret=SECRET,
        subject="real-operator",
        audience=AUDIENCE,
        scopes=["nimble:run"],
        extra_claims={"sub": "attacker", "exp": 9_999_999_999, "scp": ["nimble:admin"]},
    )
    principal = verify_edge_assertion(token, secret=SECRET, audience=AUDIENCE)
    assert principal.subject == "real-operator"
    assert principal.scopes == frozenset({"nimble:run"})


def test_empty_subject_or_secret_is_refused_at_mint_time():
    with pytest.raises(ValueError):
        mint_edge_assertion(secret="", subject="x", audience=AUDIENCE)
    with pytest.raises(ValueError):
        mint_edge_assertion(secret=SECRET, subject="", audience=AUDIENCE)
