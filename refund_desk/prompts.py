"""Version your prompts like code.

Practice: "Version your prompts like code."

Each prompt is a file: prompts/system/v1.md, v2.md, v3.md. A new version is a
new file plus a line in prompts/CHANGELOG.md, reviewed in a pull request
like any code change. The run records the version and a short hash of the
text, so every trace and every eval score points to the exact prompt.

Why a hash as well as a name: somebody can edit v3.md in place. The hash
shows that the text changed, even when the name did not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    version: str
    text: str
    sha: str


def load_prompt(prompts_dir: Path, version: str, shop_name: str = "Copper Kettle Co.") -> Prompt:
    path = prompts_dir / "system" / f"{version}.md"
    if not path.exists():
        known = sorted(p.stem for p in (prompts_dir / "system").glob("v*.md"))
        raise FileNotFoundError(f"prompt version {version!r} not found; known versions: {known}")
    raw = path.read_text(encoding="utf-8")
    # The file may start with a comment block for humans. Do not send it.
    body = raw.split("-->", 1)[1].lstrip() if raw.lstrip().startswith("<!--") else raw
    text = body.replace("{{shop_name}}", shop_name)
    return Prompt(version, text, hashlib.sha256(text.encode()).hexdigest()[:12])
