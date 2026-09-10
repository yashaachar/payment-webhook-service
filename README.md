# Payment Webhook Integration Service

A backend service that receives signed payment webhooks (Stripe-style),
verifies and processes them **exactly once** even under retries/duplicate
delivery, and pushes live updates to a dashboard over WebSocket.

Built to demonstrate third-party/enterprise webhook integration patterns:
signature verification, replay protection, and idempotent event processing
backed by the database rather than in-memory state.

## Architecture

```
 Payment Provider (simulated)
        |
        | POST /webhooks/payments
        | header: X-Webhook-Signature: t=<ts>,v1=<hmac>
        v
 +-------------------+
 |   FastAPI app     |
 |-------------------|
 | 1. Verify HMAC     |---> reject (400) if invalid or stale (replay window)
 | 2. INSERT event_id |---> DB unique constraint = idempotency guard
 |    (dup? -> ignore)|
 | 3. Update ledger   |
 | 4. Broadcast (WS)  |
 +-------------------+
        |         \
        v          v
  PostgreSQL    Dashboard (WebSocket clients)
  (events +
   ledger)
```

## Why it's built this way

**Signature verification (`app/signature.py`)**
Mirrors Stripe's real scheme: the header carries `t=<timestamp>,v1=<hmac>`,
and the signed content is `"<timestamp>.<raw_body>"` — not just the body.
Binding the timestamp into what's signed is what lets us reject *replayed*
requests (a captured, still-validly-signed request sent again later)
by checking the timestamp is within a tolerance window, even though HMAC
signatures themselves don't expire on their own.

Comparison uses `hmac.compare_digest`, not `==`. A naive string comparison
short-circuits on the first mismatched byte, which leaks timing
information an attacker can use to forge a valid signature one byte at a
time. Constant-time comparison closes that side channel.

**Idempotency (`app/main.py`)**
The temptation is to check "have I seen this event_id?" with a `SELECT`
before inserting — but that has a race condition if two copies of the
same webhook arrive concurrently (real providers do send concurrent
retries). Instead, `event_id` is the **primary key**, and we just try to
`INSERT`. The database's uniqueness constraint is the single source of
truth for "have I seen this" — atomic, and correct even under concurrent
delivery, without needing an explicit lock.

**Separating `WebhookEvent` from `PaymentLedger`**
`WebhookEvent` is an append-only audit log of every raw event received —
useful for debugging and replay. `PaymentLedger` is the current-state
business record per order. Keeping them separate means the "did we
already process this" question and "what's the current state of this
order" question don't get tangled — a later event (e.g. a refund) can
update the ledger without needing to touch or reinterpret the original
event row.

**WebSocket dashboard, not polling**
Ledger updates broadcast to connected clients the moment they're
processed, so the dashboard reflects payment state in real time without
the client polling an endpoint on a timer.

## Quick start

```bash
docker-compose up --build
```

Then open **http://localhost:8000** for the live dashboard.

Interactive API docs (Swagger): **http://localhost:8000/docs**

## Simulating webhook events

Since there's no real payment provider account behind this, run the
included simulator — it signs requests exactly the way a real provider
would:

```bash
pip install requests
python simulate_webhook.py succeed          # one successful payment -> appears on dashboard
python simulate_webhook.py fail             # one failed payment
python simulate_webhook.py retry            # sends the SAME event_id twice
python simulate_webhook.py bad-signature    # tampered signature -> 400
```

**To verify idempotency actually works:** run `retry` and watch the logs —
the first delivery returns `"processed"`, the second (identical) delivery
returns `"duplicate_ignored"`. Then check `GET /ledger` and confirm the
order appears exactly once despite the payment event being sent twice.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/webhooks/payments` | POST | Receives a signed payment event |
| `/ledger` | GET | Current state of every order |
| `/events` | GET | Raw audit log of received webhook events |
| `/ws/dashboard` | WebSocket | Live ledger updates |
| `/` | GET | Dashboard UI |
| `/docs` | GET | Swagger/OpenAPI docs |

## What I'd change at higher scale

- **Broker instead of in-process broadcast**: the `ConnectionManager` only
  works within a single app instance. At scale, I'd back it with Redis
  pub/sub (or a proper message queue) so ledger updates fan out correctly
  across multiple app replicas.
- **Dead-letter handling**: currently a processing failure after signature
  verification just raises an error. A production version would move
  failed events to a dead-letter table/queue for manual review and retry,
  rather than relying on the sender's retry behavior alone.
- **Outbox pattern**: the DB write and the WebSocket broadcast currently
  happen in the same request. If the broadcast step ever needs to survive
  process restarts or become an at-least-once guarantee, I'd write to an
  outbox table and have a separate dispatcher poll and publish it.
