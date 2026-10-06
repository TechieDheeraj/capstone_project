"""Strip secrets and personal data (PII) before anything reaches the model.

Practice: "Strip secrets and PII before anything reaches the model."

Two classes of sensitive data, two rules:
  * PII that a tool may need later (e-mail, phone, name) -> a placeholder such
    as <EMAIL_1>. The vault (kept by the harness, never sent to the model)
    maps it back. Tools and the final e-mail get the real value again.
  * Secrets that nobody needs (card numbers, IBANs, API keys, passwords) ->
    removed for ever: [CARD_REMOVED]. They are never put back.

Limits: regular expressions find well-formed patterns only. They do not find
a street address or a name that the system does not know. Production systems
add an NER-based detector (for example Microsoft Presidio) on top.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Secrets: removed, never restored.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API_KEY", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
                           r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,})")),
    ("PASSWORD", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")),
    ("CARD", re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")),
]

# PII: replaced by a placeholder that the harness can restore.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}(?![\w-])")),
]

PLACEHOLDER = re.compile(r"<(?:NAME|FIRST_NAME|EMAIL|PHONE)_\d+>")


def luhn_ok(number: str) -> bool:
    """Card numbers pass the Luhn check. Random digit strings usually do not."""
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class Redactor:
    """One redactor per run. Same value -> same placeholder, for the whole run."""

    def __init__(self, vault: dict[str, str] | None = None):
        self.vault: dict[str, str] = dict(vault or {})          # placeholder -> real value
        self._reverse = {v: k for k, v in self.vault.items()}   # real value -> placeholder
        self.counts: Counter[str] = Counter()

    # -- known PII from the ticket header (exact matches are the most reliable)
    def register(self, kind: str, value: str) -> str:
        value = value.strip()
        if value in self._reverse:
            return self._reverse[value]
        n = sum(1 for k in self.vault if k.startswith(f"<{kind}_")) + 1
        ph = f"<{kind}_{n}>"
        self.vault[ph] = value
        self._reverse[value] = ph
        return ph

    def register_person(self, name: str, email: str) -> None:
        self.register("NAME", name)
        first = name.split()[0] if name.split() else ""
        if len(first) >= 3:
            self.register("FIRST_NAME", first)
        self.register("EMAIL", email.lower())

    # -- text ----------------------------------------------------------------
    def redact(self, text: str) -> str:
        for kind, pat in SECRET_PATTERNS:
            def drop(m: re.Match[str], kind: str = kind) -> str:
                if kind == "CARD" and not luhn_ok(m.group(0)):
                    return m.group(0)
                self.counts[kind] += 1
                return f"[{kind}_REMOVED]"
            text = pat.sub(drop, text)

        for kind, pat in PII_PATTERNS:
            def swap(m: re.Match[str], kind: str = kind) -> str:
                value = m.group(0).lower() if kind == "EMAIL" else m.group(0)
                self.counts[kind] += 1
                return self.register(kind, value)
            text = pat.sub(swap, text)

        # Known values last: longest first, so "Maya Lindqvist" wins over "Maya".
        for value, ph in sorted(self._reverse.items(), key=lambda kv: -len(kv[0])):
            if ph.startswith(("<NAME_", "<FIRST_NAME_")) and value:
                # A first name must match its case: "Will" is a name, "will" is a verb.
                flags = re.IGNORECASE if ph.startswith("<NAME_") else 0
                new = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", ph, text, flags=flags)
                if new != text:
                    self.counts[ph.strip("<>").rsplit("_", 1)[0]] += 1
                    text = new
        return text

    def rehydrate(self, text: str) -> str:
        """Put the real values back (only for tools and the final e-mail)."""
        return PLACEHOLDER.sub(lambda m: self.vault.get(m.group(0), m.group(0)), text)

    # -- JSON-like objects (tool inputs and outputs) ---------------------------
    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self.redact_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        return obj

    def rehydrate_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.rehydrate(obj)
        if isinstance(obj, list):
            return [self.rehydrate_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.rehydrate_obj(v) for k, v in obj.items()}
        return obj


def find_raw_pii(text: str, known_values: list[str] | None = None) -> list[str]:
    """Return the kinds of raw PII or secrets in text. Used by checks and evals."""
    kinds = []
    for kind, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            if kind != "CARD" or luhn_ok(m.group(0)):
                kinds.append(kind)
                break
    for kind, pat in PII_PATTERNS:
        if pat.search(text):
            kinds.append(kind)
    for v in known_values or []:
        if v and re.search(rf"(?<!\w){re.escape(v)}(?!\w)", text, flags=re.IGNORECASE):
            kinds.append("KNOWN_VALUE")
            break
    return kinds
