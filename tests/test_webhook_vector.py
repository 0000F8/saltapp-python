# Proves saltapp.webhook byte-compatible with salt-agent-sdk (TypeScript):
# tests/fixtures/webhook_signature_vector.json was produced by
# scripts/generate_ts_vector.cjs, which fed each of these exact
# (secret, agent_id, signature_header, body) tuples through the REAL
# compiled TS SDK's own createWebhookServer and recorded whether IT
# accepted (200) or rejected (401) each one. If saltapp.webhook's verdict
# ever disagrees with `expect_valid` for one of these vectors, the two
# SDKs have drifted apart on the wire.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from saltapp.webhook import WebhookVerificationError, verify_signature

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "webhook_signature_vector.json"


def load_vectors():
    data = json.loads(FIXTURE_PATH.read_text())
    return data["vectors"], data


VECTORS, FIXTURE = load_vectors()


def test_fixture_was_generated_and_self_consistent():
    assert FIXTURE["algorithm"].startswith("HMAC-SHA256")
    assert len(VECTORS) >= 5
    for vector in VECTORS:
        # The fixture generator itself asserted this before writing the file
        # (see generate_ts_vector.cjs's `failed` check) -- re-assert it here
        # so a hand-edited fixture can't silently drift from its own claim.
        assert (vector["ts_sdk_status"] == 200) == vector["expect_valid"], vector["name"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_python_sdk_agrees_with_ts_sdk(vector):
    raw_body = vector["body"].encode("utf-8")
    # Replay at the exact moment the TS SDK evaluated it (`now_unix`,
    # captured by the generator immediately before/around signing) rather
    # than the real wall clock -- a "fresh" signature is only fresh for
    # 300s, so pinning `now` is what keeps this fixture meaningful whether
    # the suite runs seconds or years after generation.
    now = vector["now_unix"]
    if vector["expect_valid"]:
        # Must not raise.
        verify_signature(
            raw_body, vector["signature_header"], vector["secret"],
            tolerance_seconds=FIXTURE["tolerance_seconds"], now=now,
        )
    else:
        with pytest.raises(WebhookVerificationError):
            verify_signature(
                raw_body, vector["signature_header"], vector["secret"],
                tolerance_seconds=FIXTURE["tolerance_seconds"], now=now,
            )
