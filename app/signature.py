import hmac
import hashlib
import time
from fastapi import HTTPException, Request

# In Stripe's real scheme this lives in your dashboard; here it's a shared
# secret between the "payment provider" (our simulator script) and us.
WEBHOOK_SECRET = "whsec_test_secret_change_me"

# Reject webhooks whose timestamp is older than this — protects against
# replay attacks where someone captures a valid signed request and
# resends it later.
TOLERANCE_SECONDS = 300


def verify_signature(payload_body: bytes, sig_header: str, secret: str = WEBHOOK_SECRET) -> None:
    """
    Verifies a Stripe-style webhook signature.

    Header format: "t=<timestamp>,v1=<hex_hmac_sha256>"

    The signed payload is "<timestamp>.<raw_body>", NOT just the raw body.
    Binding the timestamp into the signed content is what lets us reject
    old/replayed requests even though the signature itself never expires.

    Raises HTTPException(400) on any failure. Never leaks *why* verification
    failed beyond "invalid signature" — don't help an attacker debug their
    forgery attempt.
    """
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing signature header")

    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        timestamp = parts["t"]
        received_sig = parts["v1"]
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail="Malformed signature header")

    # Replay protection
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed timestamp")

    if abs(time.time() - ts) > TOLERANCE_SECONDS:
        raise HTTPException(status_code=400, detail="Timestamp outside tolerance — possible replay")

    # Recompute the expected signature server-side
    signed_content = f"{timestamp}.".encode() + payload_body
    expected_sig = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()

    # constant-time comparison — never use `==` on secrets/signatures,
    # it leaks timing information an attacker can exploit to forge one
    # byte at a time.
    if not hmac.compare_digest(expected_sig, received_sig):
        raise HTTPException(status_code=400, detail="Invalid signature")


async def get_verified_payload(request: Request) -> bytes:
    """Dependency that reads the raw body and verifies it before the route sees it."""
    body = await request.body()
    sig_header = request.headers.get("X-Webhook-Signature", "")
    verify_signature(body, sig_header)
    return body
