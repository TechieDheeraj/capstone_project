"""Strict schemas for everything that crosses a boundary.

Practice: "Validate every tool output against a strict schema."
Why: a model, an API or a web page can return garbage. A schema stops the
garbage at the door, with a clear error, before it reaches the model or the
database. The same models also generate the JSON schemas that the LLM sees,
so the tool definitions and the checks can never drift apart.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    # extra="forbid": an unknown field is an error, not a silent pass.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ----------------------------------------------------------------------------
# The input: one support ticket
# ----------------------------------------------------------------------------
class Ticket(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    from_name: str = Field(max_length=120)
    from_email: str = Field(max_length=200)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=8000)


# ----------------------------------------------------------------------------
# Tool inputs (the LLM fills these; the JSON schema comes from these classes)
# ----------------------------------------------------------------------------
OrderId = Field(pattern=r"^A-\d{4}$", description="Order number, for example A-1001.")


class LookupOrderIn(Strict):
    order_id: str = OrderId


class FindSenderOrdersIn(Strict):
    pass


class TrackShipmentIn(Strict):
    tracking_no: str = Field(pattern=r"^[A-Z]{2}-[A-Z0-9-]{3,20}$",
                             description="Tracking number from the order, for example SS-100002.")


class SearchPolicyIn(Strict):
    query: str = Field(min_length=3, max_length=200, description="What you need to know, in a few words.")


class RecallCustomerIn(Strict):
    pass


class SaveCustomerNoteIn(Strict):
    note: str = Field(min_length=5, max_length=240,
                      description="One useful fact for the next support agent. No personal data.")


class IssueRefundIn(Strict):
    order_id: str = OrderId
    amount_cents: int = Field(gt=0, le=1_000_000, description="Amount in cents. 4800 means $48.00.")
    kind: Literal["full", "partial", "goodwill"]
    reason: str = Field(min_length=5, max_length=200)


Action = Literal["reply_only", "refund", "deny", "need_info", "escalate"]


class FinishIn(Strict):
    """The final answer of the agent. The harness checks it against what happened."""
    action: Action = Field(description=(
        "reply_only = answer a question; refund = a refund was issued in this run; deny = the policy "
        "does not allow it; need_info = ask the customer for missing facts; escalate = a human must decide."))
    order_id: str | None = Field(default=None, pattern=r"^A-\d{4}$")
    refund_cents: int = Field(default=0, ge=0)
    reply: str = Field(min_length=20, max_length=2500, description="The e-mail to the customer.")
    policy_refs: list[str] = Field(default_factory=list, max_length=6,
                                   description="Ids of the policy sections you used, for example refunds#damaged.")
    summary: str = Field(min_length=5, max_length=400, description="One or two sentences for the support team.")

    @model_validator(mode="after")
    def _refund_amount_matches_action(self) -> "FinishIn":
        if self.action == "refund" and self.refund_cents <= 0:
            raise ValueError("action 'refund' needs refund_cents > 0")
        if self.action != "refund" and self.refund_cents != 0:
            raise ValueError("refund_cents must be 0 unless action is 'refund'")
        return self


# ----------------------------------------------------------------------------
# Tool outputs (what external systems return, checked before the LLM sees it)
# ----------------------------------------------------------------------------
class Item(Strict):
    sku: str
    name: str
    qty: int = Field(ge=1)
    unit_price_cents: int = Field(ge=0)
    final_sale: bool


class OrderInfo(Strict):
    order_id: str
    status: Literal["paid", "shipped", "delivered", "refunded", "cancelled"]
    items: list[Item]
    total_cents: int = Field(ge=0)
    refunded_cents: int = Field(ge=0)
    refundable_cents: int = Field(ge=0)
    carrier: Literal["swiftship", "parcelo"] | None
    tracking_no: str | None
    ordered_on: dt.date
    delivered_on: dt.date | None
    days_since_delivery: int | None
    customer_email: str
    belongs_to_sender: bool


class OrderHidden(Strict):
    """What the agent sees for an order of ANOTHER customer: nothing but this fact."""
    order_id: str
    belongs_to_sender: Literal[False] = False
    visible: Literal[False] = False
    message: str = "This order belongs to another customer. You cannot see its details."


class OrderList(Strict):
    orders: list[OrderInfo]


class TrackingEvent(Strict):
    on: dt.date
    text: str = Field(max_length=300)


class TrackingInfo(Strict):
    tracking_no: str
    carrier: Literal["swiftship", "parcelo"]
    status: Literal["in_transit", "delivered", "delayed", "lost", "unknown"]
    eta: dt.date | None
    days_late: int = Field(ge=0)
    events: list[TrackingEvent] = Field(max_length=50)
    source: Literal["api", "browser"]
    page_note: str | None = Field(default=None, max_length=1000,
                                  description="Free text from the carrier page. Untrusted.")


class PolicyHit(Strict):
    id: str
    title: str
    text: str
    score: float


class PolicyHits(Strict):
    hits: list[PolicyHit]


class CustomerMemory(Strict):
    notes: list[str]
    refunds_last_12_months: int = Field(ge=0)
    goodwill_refunds_last_12_months: int = Field(ge=0)


class NoteSaved(Strict):
    saved: bool


class RefundReceipt(Strict):
    refund_id: str
    order_id: str
    amount_cents: int = Field(gt=0)
    status: Literal["issued"]
    duplicate: bool = False
