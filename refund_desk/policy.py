"""Policy search: the agent's context (a small RAG without a vector database).

Practice: "Give it tools, context, memory and MCP" -> this is the context.

The policy files have short sections with an id: "## Damaged items {#damaged}".
search() scores each section against the question with BM25 (the classic
keyword ranking of search engines) and returns the best ones. The agent
cites the ids ("refunds#damaged"), so a human can check each decision.

Why not embeddings: the corpus is 20 sections. Keyword search is exact,
cheap, explainable and easy to test. Add embeddings when the corpus is big
and people use other words than the documents do. Measure both with evals.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*\{#(?P<id>[a-z0-9-]+)\}\s*$")
WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the and or of to in on for is are be it at by we you with this that if not do does can "
           "my me i our your was were has have from as after before than".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in WORD.findall(text.lower()):
        if w in STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):   # a tiny stemmer: "refunds" -> "refund"
            if len(w) > 4 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


@dataclass
class Section:
    id: str
    title: str
    text: str


def load_sections(policy_dir: Path) -> list[Section]:
    sections: list[Section] = []
    for path in sorted(policy_dir.glob("*.md")):
        current: Section | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            m = HEADING.match(line)
            if m:
                current = Section(f"{path.stem}#{m['id']}", m["title"], "")
                sections.append(current)
            elif current and line.strip():
                current.text += line.strip() + " "
    for s in sections:
        s.text = s.text.strip()
    return sections


class PolicyIndex:
    def __init__(self, sections: list[Section], k1: float = 1.4, b: float = 0.75):
        self.sections, self.k1, self.b = sections, k1, b
        self.docs = [tokens(s.title + " " + s.title + " " + s.text) for s in sections]  # title counts twice
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 3) -> list[tuple[Section, float]]:
        q = tokens(query)
        scored = []
        for s, d in zip(self.sections, self.docs, strict=True):
            tf = Counter(d)
            score = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            if score > 0:
                scored.append((s, round(score, 3)))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
