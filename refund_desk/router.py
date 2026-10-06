"""Route easy requests to small models, hard ones to reasoning models.

Practice: "Route easy requests to small models, hard ones to reasoning models."

Most tickets are easy ("where is my order?"). A small model answers them
well, fast and cheaply. Some tickets are hard: money, several orders, an
angry customer, an attack. They need a larger model that reasons first.

This router uses plain rules, so it is free, fast and easy to test. Each
decision has reasons, and the reasons go to the trace. When you have enough
labelled tickets, you can replace the rules with a small classifier, and
compare both on the same eval set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .injection import scan

ORDER_ID = re.compile(r"\bA-\d{4}\b")
MONEY = re.compile(r"\$\s?(\d[\d,]*)(?:\.\d\d)?")

STATUS_WORDS = re.compile(r"(?i)\b(where is|where's|tracking|track|status|when will|arrive|delivery date|shipped yet)\b")
FAQ_WORDS = re.compile(r"(?i)\b(how long|how many days|return window|policy|do you ship|can i return)\b")
MONEY_WORDS = re.compile(r"(?i)\b(refund|money back|charged|charge|return (it|this|the|my)|send it back|broken|"
                         r"damaged|dented|missing|cancel|compensation|discount|chargeback|reimburse)\b")
ANGRY_WORDS = re.compile(r"(?i)(!!|\b(unacceptable|ridiculous|lawyer|scam|furious|worst|chargeback)\b)")


@dataclass
class Route:
    tier: str                      # "small" or "large"
    reasons: list[str] = field(default_factory=list)


def route(subject: str, body: str) -> Route:
    text = f"{subject}\n{body}"
    reasons: list[str] = []
    order_ids = set(ORDER_ID.findall(text))
    amounts = [int(a.replace(",", "")) for a in MONEY.findall(text)]

    if scan(text):
        reasons.append("possible prompt injection")
    if MONEY_WORDS.search(text):
        reasons.append("money is involved")
    if len(order_ids) > 1:
        reasons.append("more than one order")
    if not order_ids and MONEY_WORDS.search(text):
        reasons.append("money request without an order number")
    if any(a >= 200 for a in amounts):
        reasons.append("amount of $200 or more")
    if ANGRY_WORDS.search(text):
        reasons.append("angry tone")
    if len(text) > 1500:
        reasons.append("long message")

    if reasons:
        return Route("large", reasons)
    if STATUS_WORDS.search(text):
        return Route("small", ["order status question"])
    if FAQ_WORDS.search(text):
        return Route("small", ["policy question"])
    return Route("small", ["simple message"])
