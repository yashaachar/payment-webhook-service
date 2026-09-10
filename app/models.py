from sqlalchemy import Column, String, Numeric, DateTime, Text, Enum
from sqlalchemy.sql import func
from .database import Base
import enum


class EventStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class WebhookEvent(Base):
    """
    Append-only ledger of every webhook event we've ever received.

    event_id is UNIQUE — this is what makes the whole service idempotent.
    If the same event_id arrives twice (Stripe/webhook senders retry on
    timeout, network blips, etc.), the DB unique constraint stops us
    from double-processing it, no matter how the request arrives.
    """

    __tablename__ = "webhook_events"

    event_id = Column(String, primary_key=True)  # e.g. "evt_1NxxxAbc..."
    event_type = Column(String, nullable=False)  # e.g. "payment_intent.succeeded"
    order_id = Column(String, nullable=True, index=True)
    amount = Column(Numeric(10, 2), nullable=True)
    currency = Column(String, nullable=True)
    status = Column(Enum(EventStatus), default=EventStatus.RECEIVED, nullable=False)
    raw_payload = Column(Text, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())
    processed_at = Column(DateTime(timezone=True), nullable=True)


class PaymentLedger(Base):
    """
    The actual business record: one row per order, updated as payment
    events arrive. This is separate from WebhookEvent so that the
    "did we already see this raw event" question and the "what's the
    current state of this order" question don't get tangled together.
    """

    __tablename__ = "payment_ledger"

    order_id = Column(String, primary_key=True)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String, nullable=False)
    payment_status = Column(String, nullable=False, default="pending")
    last_event_id = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
