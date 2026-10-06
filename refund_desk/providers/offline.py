"""The offline model: a scripted stand-in for an LLM.

It is NOT intelligent. It is a few hundred lines of rules that know the shop
policy and the demo tickets. It speaks the same protocol as a real model:
it reads the conversation and returns text and tool calls.

Why it exists:
  * Tests and CI run the WHOLE harness (tools, guards, approvals, traces,
    budgets, evals) with no API key, no GPU, no cost and no randomness.
  * A public demo can run without spending money (DEMO_MODE=1).
  * It is a baseline. A real model must beat the rules on the eval set, or
    it is not worth its price.

Tokens and cost are ESTIMATES (about 4 characters per token), priced as the
Claude model that the route would use. The trace marks them "simulated".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .. import injection
from ..costs import Usage
from .base import DeltaFn, ModelRequest, ModelResponse

ORDER_ID = re.compile(r"\bA-\d{4}\b")
WANTS_MONEY = re.compile(r"(?i)\b(refund|money back|reimburse|charged|twice|broken|damaged|dented|cracked|"
                         r"missing|discount|compensation|return (it|this|the|my)|send it back|changed my mind|"
                         r"don't like|do not like)\b")
FAQ = re.compile(r"(?i)\b(return window|how long|how many days|policy|can i return)\b")
REASONS = [
    ("duplicate", re.compile(r"(?i)charged (me )?twice|double charge|paid twice|twice for")),
    ("damaged", re.compile(r"(?i)\b(broken|damaged|dented|cracked|shattered|smashed)\b")),
    ("missing", re.compile(r"(?i)\bmissing\b|only (got|received)|not in the box")),
    ("late", re.compile(r"(?i)not arrived|hasn't arrived|has not arrived|never arrived|still waiting|still not here")),
    ("goodwill", re.compile(r"(?i)discount|compensation|slightly different|colou?r is (different|off)")),
    ("change_mind", re.compile(r"(?i)changed my mind|don't like|do not like|don't want|no longer|send it back")),
]
POLICY_QUERY = {
    "damaged": "damaged or broken item refund", "missing": "missing item partial refund",
    "duplicate": "duplicate charge paid twice", "late": "late parcel refund or reship",
    "goodwill": "goodwill refund discount", "change_mind": "return window final sale",
}
SIGN = "\n\nBest regards,\nThe Copper Kettle team"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class Call:
    name: str
    input: dict
    data: dict | None
    error: str | None


def _history(messages: list[dict]) -> list[Call]:
    pending: dict[str, tuple[str, dict]] = {}
    calls: list[Call] = []
    for m in messages:
        if not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b["type"] == "tool_use":
                pending[b["id"]] = (b["name"], b["input"])
            elif b["type"] == "tool_result" and b["tool_use_id"] in pending:
                name, inp = pending[b["tool_use_id"]]
                content = b["content"] if isinstance(b["content"], str) else json.dumps(b["content"])
                if b.get("is_error") or content.startswith("error"):
                    calls.append(Call(name, inp, None, content))
                else:
                    try:
                        calls.append(Call(name, inp, json.loads(content), None))
                    except json.JSONDecodeError:
                        calls.append(Call(name, inp, {"text": content}, None))
    return calls


def _ticket(messages: list[dict]) -> tuple[str, str]:
    first = messages[0]["content"]
    text = first if isinstance(first, str) else "".join(b.get("text", "") for b in first if b["type"] == "text")
    m = re.search(r'<untrusted_data source="ticket">\n(.*?)\n</untrusted_data>', text, re.S)
    name = re.search(r"<FIRST_NAME_\d+>", text)
    return (m.group(1) if m else text), (name.group(0) if name else "there")


class OfflineModel:
    name = "offline"

    def __init__(self, stream_delay_s: float = 0.0):
        self.stream_delay_s = stream_delay_s

    # ------------------------------------------------------------------ protocol
    def complete(self, req: ModelRequest, on_delta: DeltaFn | None = None) -> ModelResponse:
        step = sum(1 for m in req.messages if m["role"] == "assistant")
        say, tool, args = self.decide(req.messages)
        content = []
        if say:
            content.append({"type": "text", "text": say})
            self._stream(on_delta, "text", say)
        if tool == "finish":
            self._stream(on_delta, "reply", args.get("reply", ""))
        content.append({"type": "tool_use", "id": f"toolu_off_{step:02d}", "name": tool, "input": args})
        prompt_chars = len(req.system) + len(json.dumps(req.messages)) + len(json.dumps(req.tools))
        usage = Usage(input_tokens=prompt_chars // 4, output_tokens=max(1, len(json.dumps(content)) // 4))
        return ModelResponse(content, "tool_use", usage, req.model, self.name, simulated=True)

    def _stream(self, on_delta: DeltaFn | None, kind: str, text: str) -> None:
        if not on_delta or not text:
            return
        for i in range(0, len(text), 12):
            on_delta(kind, text[i:i + 12])
            if self.stream_delay_s:
                time.sleep(self.stream_delay_s)

    # ------------------------------------------------------------------ the rules
    def decide(self, messages: list[dict]) -> tuple[str, str, dict]:
        text, first = _ticket(messages)
        calls = _history(messages)

        def last(name: str) -> Call | None:
            for c in reversed(calls):
                if c.name == name:
                    return c
            return None

        def finish(action: str, body: str, summary: str, refs: list[str], order_id: str | None = None,
                   refund: int = 0) -> tuple[str, str, dict]:
            reply = f"Hi {first},\n\n{body}{SIGN}"
            args = {"action": action, "order_id": order_id, "refund_cents": refund, "reply": reply,
                    "policy_refs": refs, "summary": summary}
            return "I have what I need. I will write the reply.", "finish", args

        order_ids = list(dict.fromkeys(ORDER_ID.findall(text)))
        reason = next((r for r, pat in REASONS if pat.search(text)), None)
        wants_money = bool(reason) or bool(WANTS_MONEY.search(text))

        # 1. An attack in the ticket: do nothing with money. A human decides.
        if injection.is_high_risk(injection.scan(text)):
            for oid in order_ids:
                if not any(c.name == "lookup_order" and c.input.get("order_id") == oid for c in calls):
                    return f"I will look up order {oid}.", "lookup_order", {"order_id": oid}
            return finish("escalate", "Thank you for your message. A member of our team will review your "
                          "request and reply within one business day.",
                          "The ticket contains instructions for an AI system. No action taken; a human must review.",
                          ["refunds#escalate"], order_ids[0] if order_ids else None)

        order: dict | None = None
        # 2. No order number.
        if not order_ids:
            if wants_money:
                found = last("find_sender_orders")
                if not found:
                    return "The ticket has no order number. I will list the sender's orders.", "find_sender_orders", {}
                orders = (found.data or {}).get("orders", []) if not found.error else []
                if reason == "duplicate":
                    order = _duplicate(orders)
                elif len(orders) == 1:
                    order = orders[0]
                if not order:
                    return finish("need_info", "Thank you for your message. Please send us the order number "
                                  "(for example A-1234), so that we can help you quickly.",
                                  "No order number, and the sender's orders do not show which one.", [])
            elif FAQ.search(text):
                hits = last("search_policy")
                if not hits:
                    return "This is a policy question. I will search the policy.", "search_policy", \
                        {"query": "return window refund days"}
                top = (hits.data or {}).get("hits", [{}])[0]
                return finish("reply_only", "Thank you for your question. You can return an item within 30 days "
                              "after delivery for a full refund of the item price. Items marked \"final sale\" "
                              "cannot be returned, unless they arrive damaged.",
                              "Answered a policy question about the return window.", [top.get("id", "refunds#window")])
            else:
                return finish("need_info", "Thank you for your message. Please send us your order number "
                              "(for example A-1234) and tell us how we can help.", "No order number.", [])

        # 3. Look up the order.
        if order is None:
            oid = order_ids[0]
            looked = next((c for c in reversed(calls) if c.name == "lookup_order" and c.input.get("order_id") == oid), None)
            if not looked:
                return f"I will look up order {oid}.", "lookup_order", {"order_id": oid}
            if looked.error:
                return finish("need_info", f"We could not find order {oid}. Please check the order number.",
                              f"Order {oid} not found.", [])
            order = looked.data or {}
        oid = order.get("order_id")
        if not order.get("belongs_to_sender", False):
            return finish("escalate", f"We could not match order {oid} to the e-mail address that you wrote from. "
                          "For your security, a member of our team will check this and reply to you.",
                          f"Order {oid} does not belong to the sender. No details shared.", ["refunds#escalate"], oid)

        tracking = next((c for c in reversed(calls) if c.name == "track_shipment"), None)

        # 4. Status questions.
        if not wants_money and reason is None:
            if order["status"] == "shipped" and order.get("tracking_no"):
                if not tracking:
                    return "I will check the tracking status.", "track_shipment", {"tracking_no": order["tracking_no"]}
                return finish("reply_only", _status_text(oid, tracking), f"Status question for {oid}.",
                              ["shipping#times"], oid)
            if order["status"] == "delivered":
                return finish("reply_only", f"Our records show that order {oid} was delivered on "
                              f"{order['delivered_on']}. If you cannot find it, please reply and we will help.",
                              f"Status question; {oid} delivered.", ["shipping#status"], oid)
            return finish("reply_only", f"Order {oid} is paid and waits to ship. Orders ship within 2 business days.",
                          f"Status question; {oid} not shipped yet.", ["shipping#times"], oid)

        # 5. Money requests: memory, policy, tracking, then the decision.
        reason = reason or "change_mind"
        if not last("recall_customer"):
            return "I will check the customer's history.", "recall_customer", {}
        if not last("search_policy"):
            return "I will read the policy for this case.", "search_policy", {"query": POLICY_QUERY[reason]}
        if reason == "late" and order["status"] == "shipped" and order.get("tracking_no") and not tracking:
            return "I will check the tracking status.", "track_shipment", {"tracking_no": order["tracking_no"]}

        memory = (last("recall_customer").data or {})  # type: ignore[union-attr]
        if order.get("refundable_cents", 0) == 0 and order.get("refunded_cents", 0) > 0 \
                and not last("issue_refund"):
            return finish("reply_only", f"We already refunded {money(order['refunded_cents'])} for order {oid}. "
                          "The refund shows on your statement in 5 to 10 business days.",
                          f"{oid} is already fully refunded. No new refund.", ["refunds#payment"], oid)
        action, amount, kind, refs, body, summary = _rules(reason, order, text, tracking, memory)
        if "[CARD_REMOVED]" in text:
            body += (" Note: for your security, we removed the card number from your message. We refund only to "
                     "the original payment method, never to a different card.")
            refs = refs + ["refunds#payment"]

        if action == "refund":
            refund = last("issue_refund")
            if not refund:
                return f"The policy allows a refund of {money(amount)}. I will request it.", "issue_refund", \
                    {"order_id": oid, "amount_cents": amount, "kind": kind, "reason": summary[:200]}
            if refund.error:
                return finish("escalate", "Thank you for your patience. A member of our team will review your "
                              "request and reply within one business day.",
                              f"Refund was not completed ({refund.error[:120]}). A human must decide.", refs, oid)
            if not last("save_customer_note"):
                return "I will save a short note for the next ticket.", "save_customer_note", \
                    {"note": f"{kind.title()} refund of {money(amount)} on {oid} ({reason.replace('_', ' ')})."}
            return finish("refund", body, summary, refs, oid, amount)
        return finish(action, body, summary, refs, oid)


def _duplicate(orders: list[dict]) -> dict | None:
    for a in orders:
        for b in orders:
            if a is not b and a["ordered_on"] == b["ordered_on"] and \
                    [i["sku"] for i in a["items"]] == [i["sku"] for i in b["items"]] and b["status"] == "paid":
                return b   # the copy that was never shipped
    return None


def _status_text(oid: str, tracking: Call) -> str:
    if tracking.error or not tracking.data:
        return f"We could not get the tracking status for order {oid} right now. We will check and reply soon."
    t = tracking.data
    last_event = t["events"][-1]["text"] if t.get("events") else "no events yet"
    eta = f" The estimated delivery date is {t['eta']}." if t.get("eta") else ""
    return f"Your order {oid} is on its way. The newest update from the carrier is: \"{last_event}\".{eta}"


def _rules(reason: str, order: dict, text: str, tracking: Call | None, memory: dict):
    oid, days = order["order_id"], order.get("days_since_delivery")
    total, refundable = order["total_cents"], order["refundable_cents"]
    final_sale = any(i["final_sale"] for i in order["items"])

    if reason == "damaged":
        if days is not None and days > 30:
            return ("deny", 0, "", ["refunds#damaged"], "We are sorry that the item is damaged. Damage must be "
                    "reported within 30 days after delivery, and this order was delivered more than 30 days ago, "
                    "so we cannot refund it.", f"Damage reported after 30 days on {oid}. Denied.")
        if total >= 20000 and "photo" not in text.lower():
            return ("need_info", 0, "", ["refunds#damaged"], "We are sorry that the item arrived damaged. For "
                    "items of $200 or more, please send us a photo of the damage. Then we will refund you.",
                    f"Damaged item on {oid} ({money(total)}); a photo is needed first.")
        return ("refund", refundable, "full", ["refunds#damaged"], f"We are sorry that your item arrived damaged. "
                f"We have refunded {money(refundable)} to your original payment method. You do not need to "
                "send the item back. The refund shows on your statement in 5 to 10 business days.",
                f"Damaged item on {oid}: full refund of {money(refundable)}.")
    if reason == "missing":
        for item in order["items"]:
            words = [w for w in re.findall(r"[a-z]+", item["name"].lower()) if len(w) > 3]
            if any(re.search(rf"\b{w}", text.lower()) for w in words):
                amount = item["unit_price_cents"]
                return ("refund", amount, "partial", ["refunds#missing"], f"We are sorry that one {item['name'].lower()} "
                        f"was missing from your order. We have refunded {money(amount)} for the missing item to your "
                        "original payment method. The refund shows in 5 to 10 business days.",
                        f"Missing item on {oid}: partial refund of {money(amount)}.")
        return ("need_info", 0, "", ["refunds#missing"], "We are sorry that something is missing. Please tell us "
                "which item was not in the parcel.", f"Missing item on {oid}, item not clear.")
    if reason == "duplicate":
        return ("refund", refundable, "full", ["refunds#duplicate"], f"You are right: you paid twice for the same item, "
                f"and we sent only one parcel. We have refunded the second payment ({oid}, {money(refundable)}) to "
                "your original payment method. The refund shows in 5 to 10 business days.",
                f"Duplicate payment: full refund of the unshipped order {oid}.")
    if reason == "late":
        late = (tracking.data or {}).get("days_late", 0) if tracking and tracking.data else 0
        if late > 7:
            return ("refund", refundable, "full", ["refunds#late"], f"We are sorry that your parcel is late. It is "
                    f"{late} days after the estimated delivery date, so we have refunded {money(refundable)} to your "
                    "original payment method, as you asked. The refund shows in 5 to 10 business days.",
                    f"Parcel {late} days late on {oid}: full refund.")
        eta = (tracking.data or {}).get("eta") if tracking and tracking.data else None
        return ("reply_only", 0, "", ["refunds#late"], f"Your parcel is on its way.{' The estimated delivery date is ' + eta + '.' if eta else ''} "
                "If it is more than 7 days late, write to us again and we will refund you or send a new one.",
                f"Parcel on {oid} is not late enough for a refund.")
    if reason == "goodwill":
        if memory.get("goodwill_refunds_last_12_months", 0) >= 1:
            if days is not None and days <= 30:
                return ("reply_only", 0, "", ["refunds#goodwill", "refunds#window"], "We are sorry that the item is "
                        "not what you expected. We cannot give a discount this time, because our policy allows one "
                        "goodwill refund in 12 months. You can still return the item within 30 days after delivery "
                        "for a full refund of the item price.",
                        f"Goodwill refund already used in the last 12 months. Offered a normal return for {oid}.")
            return ("deny", 0, "", ["refunds#goodwill"], "We are sorry, but we cannot give a discount on this order.",
                    f"Goodwill already used; {oid} outside the return window.")
        amount = int(total * 0.10)
        return ("refund", amount, "goodwill", ["refunds#goodwill"], f"We are sorry that the item is not exactly what "
                f"you expected. As a goodwill gesture, we have refunded {money(amount)} to your original payment method.",
                f"Goodwill refund of {money(amount)} on {oid}.")
    # change_mind
    if final_sale:
        return ("deny", 0, "", ["refunds#final-sale"], "We are sorry, but this item was sold as \"final sale\", so we "
                "cannot accept a return or give a refund.", f"Final-sale item on {oid}. Denied.")
    if days is not None and days > 30:
        return ("deny", 0, "", ["refunds#window"], f"We are sorry, but we can accept a return only within 30 days "
                f"after delivery, and order {oid} was delivered {days} days ago. Thus we cannot refund it.",
                f"Return request after {days} days on {oid}. Denied (30-day window).")
    return ("reply_only", 0, "", ["refunds#window"], "You can return the item within 30 days after delivery. Send it "
            "back in its original box. When we receive it, we refund the item price to your original payment method.",
            f"Change of mind on {oid}, inside the window. Sent return instructions.")
