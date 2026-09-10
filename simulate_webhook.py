"""
Simulates a payment provider (Stripe-style) sending signed webhook events
to our service. This exists because we don't have a real Stripe account —
but it signs requests exactly the way a real provider would, so the
receiving code is genuinely tested end to end.

Usage:
    python simulate_webhook.py succeed          # one successful payment
    python simulate_webhook.py fail             # one failed payment
    python simulate_webhook.py retry            # sends the SAME event twice
                                                 # -> proves idempotency
    python simulate_webhook.py bad-signature    # tampered signature -> 400
"""
import sys
import json
import time
import uuid
import hmac
import hashlib
import requests

BASE_URL = "http://localhost:8000"
WEBHOOK_SECRET = "whsec_test_secret_change_me"


def sign_payload(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    timestamp = str(int(time.time()))
    signed_content = f"{timestamp}.".encode() + payload_bytes
    sig = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def send_event(event: dict, tamper: bool = False):
    payload_bytes = json.dumps(event).encode()
    sig_header = sign_payload(payload_bytes)
    if tamper:
        sig_header = sig_header[:-4] + "0000"  # corrupt the signature

    resp = requests.post(
        f"{BASE_URL}/webhooks/payments",
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig_header,
        },
    )
    print(f"-> {resp.status_code} {resp.json() if resp.headers.get('content-type','').startswith('application/json') else resp.text}")
    return resp


def make_event(event_type: str, order_id: str, amount: float, event_id: str = None):
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "type": event_type,
        "data": {
            "order_id": order_id,
            "amount": amount,
            "currency": "usd",
        },
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "succeed"

    if cmd == "succeed":
        event = make_event("payment_intent.succeeded", f"order_{uuid.uuid4().hex[:8]}", 49.99)
        print(f"Sending {event['type']} for {event['data']['order_id']}...")
        send_event(event)

    elif cmd == "fail":
        event = make_event("payment_intent.payment_failed", f"order_{uuid.uuid4().hex[:8]}", 19.99)
        print(f"Sending {event['type']} for {event['data']['order_id']}...")
        send_event(event)

    elif cmd == "retry":
        # Same event_id sent twice — this is what a real provider does
        # when it doesn't get a fast-enough 200 back the first time.
        event = make_event("payment_intent.succeeded", f"order_{uuid.uuid4().hex[:8]}", 99.50)
        print(f"First delivery of {event['id']}...")
        send_event(event)
        print(f"Simulated retry of the SAME event {event['id']}...")
        send_event(event)
        print("Expect: first call 'processed', second call 'duplicate_ignored'.")
        print("Check GET /ledger — the order should appear exactly once.")

    elif cmd == "bad-signature":
        event = make_event("payment_intent.succeeded", f"order_{uuid.uuid4().hex[:8]}", 10.00)
        print("Sending event with a deliberately corrupted signature...")
        send_event(event, tamper=True)
        print("Expect: 400 Invalid signature.")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
