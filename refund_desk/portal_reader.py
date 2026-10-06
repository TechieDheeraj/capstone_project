"""The browser fallback: read a carrier's tracking web page.

Practice: "Build a browser fallback for apps with no API."

Parcelo has no API, and the SwiftShip API can be down. A person would open
the tracking page, type the number and read the result. This module does
the same. It runs in a SANDBOX (see sandbox.py): a separate process with no
secrets, a time limit and a memory limit, because it reads untrusted HTML.

Two engines:
  * "playwright": a real headless browser. Use it for pages that need
    JavaScript. Install with: pip install playwright && playwright install chromium
  * "http": submit the form with a plain GET request and parse the HTML.
    Enough for simple server-rendered pages, and it needs nothing extra.

Both engines return the same thing: the VISIBLE text and a few fields.
Hidden elements (display:none), comments and scripts are dropped, because a
person cannot see them and an attacker likes to hide instructions there.

Protocol: JSON on stdin -> JSON on stdout.
"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

SKIP_TAGS = {"script", "style", "noscript", "template", "head"}
VOID_TAGS = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "area", "col", "embed", "track"}


class VisibleText(HTMLParser):
    """Collect visible text and the fields of a tracking result."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, str]] = []   # (tag, hidden, class)
        self.lines: list[str] = []
        self.fields: dict = {"status": None, "eta": None, "events": [], "note": None}
        self._time: str | None = None
        self._buf: list[str] = []

    def _hidden(self) -> bool:
        return any(h for _, h, _ in self.stack)

    def _cls(self) -> str:
        return self.stack[-1][2] if self.stack else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        style = a.get("style", "").replace(" ", "").lower()
        hidden = (tag in SKIP_TAGS or "display:none" in style or "visibility:hidden" in style
                  or "hidden" in a or a.get("aria-hidden") == "true")
        if tag in VOID_TAGS:
            return
        self.stack.append((tag, hidden, a.get("class", "")))
        if self._hidden():
            return
        if tag == "p" and a.get("class") == "status":
            self.fields["status"] = a.get("data-status")
        if tag == "time":
            self._time = a.get("datetime")
        if tag == "li":
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        # pop to the matching tag (tolerate broken HTML)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                was_hidden = self._hidden()
                cls = self.stack[i][2]
                del self.stack[i:]
                if not was_hidden and tag == "li" and self._time:
                    text = " ".join("".join(self._buf).split())
                    self.fields["events"].append({"on": self._time, "text": text.replace(self._time, "").strip()})
                    self._time = None
                if not was_hidden and tag == "p" and cls == "eta" and self._time:
                    self.fields["eta"] = self._time
                    self._time = None
                break

    def handle_data(self, data: str) -> None:
        if self._hidden():
            return
        text = " ".join(data.split())
        if not text:
            return
        self.lines.append(text)
        self._buf.append(data)
        if self._cls() == "note":
            prev = self.fields["note"] or ""
            self.fields["note"] = (prev + " " + text).strip().removeprefix("Delivery note:").strip()

    # HTML comments are never visible: handle_comment is not overridden, so they are dropped.


def parse_page(html_text: str) -> dict:
    p = VisibleText()
    p.feed(html_text)
    p.close()
    return {"visible_text": "\n".join(p.lines)[:4000], "fields": p.fields}


def _check_host(url: str, allowed: list[str]) -> None:
    host = urlparse(url).hostname or ""
    if host not in allowed:
        raise PermissionError(f"host {host!r} is not in the allowlist")


def read_with_http(portal_url: str, tracking_no: str, allowed: list[str]) -> str:
    import urllib.request
    url = portal_url.rstrip("/") + "/track?" + urlencode({"no": tracking_no})
    _check_host(url, allowed)
    with urllib.request.urlopen(url, timeout=10) as resp:   # noqa: S310 (host checked above)
        return resp.read(500_000).decode("utf-8", "replace")


def read_with_playwright(portal_url: str, tracking_no: str, allowed: list[str]) -> str:
    from playwright.sync_api import sync_playwright  # optional dependency
    _check_host(portal_url, allowed)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # Block every request to a host that is not allowed (images, trackers, redirects).
            page.route("**/*", lambda route: route.continue_()
                       if (urlparse(route.request.url).hostname or "") in allowed else route.abort())
            page.goto(portal_url, timeout=10_000)
            page.fill("#tracking", tracking_no)
            page.click("#go")
            page.wait_for_selector("#result", timeout=10_000)
            return page.content()
        finally:
            browser.close()


def main() -> None:
    req = json.loads(sys.stdin.read())
    engine = req.get("engine", "auto")
    if engine == "auto":
        try:
            import playwright  # noqa: F401
            engine = "playwright"
        except ImportError:
            engine = "http"
    try:
        reader = read_with_playwright if engine == "playwright" else read_with_http
        page = reader(req["portal_url"], req["tracking_no"], req["allowed_hosts"])
        out = {"ok": True, "engine": engine, **parse_page(page)}
    except Exception as exc:  # report, do not crash: the caller decides what to do
        out = {"ok": False, "engine": engine, "error": f"{type(exc).__name__}: {exc}"[:500]}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
