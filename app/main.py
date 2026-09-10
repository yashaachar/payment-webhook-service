import json
import logging
from decimal import Decimal
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .database import engine, get_db, Base
from .models import WebhookEvent, PaymentLedger, EventStatus
from .signature import get_verified_payload
from .dashboard import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook-service")

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Payment Webhook Integration Service",
    description=(
        "Receives payment-provider webhooks, verifies their signature, "
        "processes them idempotently, and pushes live updates to a "
        "dashboard over WebSocket."
    ),
    version="1.0.0",
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/webhooks/payments")
async def receive_payment_webhook(
    payload: bytes = Depends(get_verified_payload),
    db: Session = Depends(get_db),
):
    """
    Receives a signed payment event, verifies it (see signature.py),
    and processes it exactly once even if the sender retries.

    Idempotency strategy: we try to INSERT the event_id first. The
    database's primary-key uniqueness constraint is the source of
    truth for "have we seen this before" — not an in-memory set,
    which wouldn't survive a restart or work across multiple app
    instances. If the insert fails on a duplicate key, we know this
    exact event already came through and we short-circuit safely.
    """
    event = json.loads(payload)
    event_id = event["id"]
    event_type = event["type"]
    data = event["data"]

    new_event = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        order_id=data.get("order_id"),
        amount=Decimal(str(data.get("amount", 0))),
        currency=data.get("currency", "usd"),
        status=EventStatus.RECEIVED,
        raw_payload=payload.decode(),
    )

    db.add(new_event)
    try:
        db.commit()
    except IntegrityError:
        # This event_id already exists — it's a retry/duplicate delivery.
        # Per webhook best practice, we still return 200 so the sender
        # stops retrying, but we do NOT process it again.
        db.rollback()
        logger.info(f"Duplicate webhook received and ignored: {event_id}")
        return {"status": "duplicate_ignored", "event_id": event_id}

    # First time seeing this event — actually process it.
    await process_event(db, new_event, event_type, data)

    return {"status": "processed", "event_id": event_id}


async def process_event(db: Session, event: WebhookEvent, event_type: str, data: dict):
    order_id = data.get("order_id")
    amount = Decimal(str(data.get("amount", 0)))
    currency = data.get("currency", "usd")

    status_map = {
        "payment_intent.succeeded": "paid",
        "payment_intent.payment_failed": "failed",
        "charge.refunded": "refunded",
    }
    new_status = status_map.get(event_type, "unknown")

    ledger_row = db.get(PaymentLedger, order_id)
    if ledger_row is None:
        ledger_row = PaymentLedger(
            order_id=order_id,
            amount=amount,
            currency=currency,
            payment_status=new_status,
            last_event_id=event.event_id,
        )
        db.add(ledger_row)
    else:
        ledger_row.payment_status = new_status
        ledger_row.last_event_id = event.event_id

    event.status = EventStatus.PROCESSED
    from sqlalchemy.sql import func
    event.processed_at = func.now()

    db.commit()
    db.refresh(ledger_row)

    await manager.broadcast({
        "type": "ledger_update",
        "order_id": order_id,
        "status": new_status,
        "amount": str(amount),
        "currency": currency,
        "event_id": event.event_id,
    })


@app.get("/ledger")
def list_ledger(db: Session = Depends(get_db)):
    rows = db.query(PaymentLedger).order_by(PaymentLedger.updated_at.desc()).all()
    return [
        {
            "order_id": r.order_id,
            "amount": str(r.amount),
            "currency": r.currency,
            "payment_status": r.payment_status,
            "last_event_id": r.last_event_id,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@app.get("/events")
def list_events(db: Session = Depends(get_db)):
    rows = db.query(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(50).all()
    return [
        {
            "event_id": r.event_id,
            "event_type": r.event_type,
            "order_id": r.order_id,
            "status": r.status,
            "received_at": r.received_at,
            "processed_at": r.processed_at,
        }
        for r in rows
    ]


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We don't expect incoming messages from the dashboard,
            # but we need to await something to keep the connection open
            # and detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
def dashboard_page():
    return DASHBOARD_HTML


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Payment Ledger Dashboard</title>
    <style>
        body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
        h1 { font-size: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 14px; }
        th { color: #666; font-weight: 600; }
        .status-paid { color: #16a34a; font-weight: 600; }
        .status-failed { color: #dc2626; font-weight: 600; }
        .status-refunded { color: #d97706; font-weight: 600; }
        .status-pending { color: #6b7280; }
        #status-indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #dc2626; margin-right: 6px; }
        #status-indicator.connected { background: #16a34a; }
    </style>
</head>
<body>
    <h1><span id="status-indicator"></span>Payment Ledger — Live Dashboard</h1>
    <table id="ledger-table">
        <thead><tr><th>Order ID</th><th>Amount</th><th>Status</th><th>Last Event</th></tr></thead>
        <tbody id="ledger-body"></tbody>
    </table>

    <script>
        const rows = new Map();

        function render() {
            const tbody = document.getElementById('ledger-body');
            tbody.innerHTML = '';
            [...rows.values()].reverse().forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${r.order_id}</td>
                    <td>${r.amount} ${r.currency.toUpperCase()}</td>
                    <td class="status-${r.status}">${r.status}</td>
                    <td>${r.event_id}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function loadInitial() {
            const res = await fetch('/ledger');
            const data = await res.json();
            data.forEach(r => rows.set(r.order_id, {
                order_id: r.order_id, amount: r.amount, currency: r.currency,
                status: r.payment_status, event_id: r.last_event_id
            }));
            render();
        }

        function connect() {
            const ws = new WebSocket(`ws://${location.host}/ws/dashboard`);
            ws.onopen = () => document.getElementById('status-indicator').classList.add('connected');
            ws.onclose = () => {
                document.getElementById('status-indicator').classList.remove('connected');
                setTimeout(connect, 2000);
            };
            ws.onmessage = (msg) => {
                const update = JSON.parse(msg.data);
                if (update.type === 'ledger_update') {
                    rows.set(update.order_id, {
                        order_id: update.order_id, amount: update.amount,
                        currency: update.currency, status: update.status,
                        event_id: update.event_id
                    });
                    render();
                }
            };
        }

        loadInitial();
        connect();
    </script>
</body>
</html>
"""
