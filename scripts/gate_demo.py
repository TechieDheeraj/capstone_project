"""Show the eval gate catching a bug.

A "small refactor" drops phone numbers from the PII patterns. The eval gate must fail:
the missing-item-pii case then sends a raw phone number to the model.

Run from the repository root:  python scripts/gate_demo.py   (exit code 1 = the gate caught it)
Nothing is changed on disk: the patch lives only in this process.
"""

import sys

import refund_desk.redact as redact
from refund_desk.cli import main

redact.PII_PATTERNS[:] = [p for p in redact.PII_PATTERNS if p[0] != "PHONE"]
sys.exit(main(["eval", "--gate"]))
