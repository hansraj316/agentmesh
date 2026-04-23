#!/usr/bin/env python3
"""One-command growth agent MVP.

Generates a markdown report with:
1) Landing-page audit
2) Copy rewrite with a narrow paid-test offer
3) Publish checklist
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import textwrap
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def fetch_html(url: str, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": "AgentMesh-GrowthAgent/0.1"})
    with urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="ignore")


def html_to_text(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def grab_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else "Untitled page"


def has_any(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)


def audit(text: str) -> list[tuple[str, bool, str]]:
    checks = [
        (
            "Clear value proposition",
            has_any(text, ["helps", "save", "faster", "increase", "reduce", "get"]),
            "Headline should describe who it is for + measurable outcome.",
        ),
        (
            "Single focused CTA",
            has_any(text, ["book", "start", "get started", "request", "demo", "apply"]),
            "Use one primary CTA above the fold.",
        ),
        (
            "Social proof",
            has_any(text, ["testimonial", "trusted", "customers", "case study", "review"]),
            "Add logos, quotes, or before/after proof.",
        ),
        (
            "Offer specificity",
            has_any(text, ["days", "week", "$", "usd", "price", "guarantee"]),
            "Make scope, timeline, and price explicit.",
        ),
        (
            "Risk reversal",
            has_any(text, ["guarantee", "refund", "cancel", "no lock-in", "risk-free"]),
            "Reduce fear with a clear guarantee/cancel policy.",
        ),
    ]
    return checks


def make_rewrite(domain: str, offer_name: str, offer_price: str, offer_timeline: str) -> str:
    return textwrap.dedent(
        f"""
        ## Copy rewrite draft (paid-test offer)

        **Hero headline**
        Turn your landing page into a qualified-demo engine in {offer_timeline}

        **Subheadline**
        We run a focused conversion sprint for {domain}: message clarity, proof placement, and CTA optimization. Fixed scope, fixed price, shipped fast.

        **Primary CTA**
        Book the {offer_name}

        **Offer block**
        - Offer: **{offer_name}**
        - Price: **{offer_price}**
        - Timeline: **{offer_timeline}**
        - Includes:
          - Landing-page teardown + friction map
          - Rewritten hero + proof + CTA section
          - Final publish-ready copy doc
        - Fit: Best for teams with 1 core landing page and active paid or outbound traffic

        **Risk reversal**
        If we miss the agreed deliverables in the sprint window, you do not pay the final 50%.
        """
    ).strip()


def make_checklist() -> str:
    items = [
        "Replace hero headline/subheadline and keep one primary CTA",
        "Place proof directly under hero (logos, testimonial, or quantified result)",
        "Add paid-test offer card with scope, timeline, and fixed price",
        "Add risk reversal line near CTA",
        "Ensure form asks only for name, work email, and URL",
        "Set conversion event tracking for CTA click + form submit",
        "Run mobile QA (iOS Safari + Android Chrome)",
        "Run page-speed check and keep LCP under 2.5s where possible",
        "Publish and monitor first 100 visitors for CTR and submit rate",
    ]
    lines = ["## Publish checklist"] + [f"- [ ] {i}" for i in items]
    return "\n".join(lines)


def build_report(url: str, offer_name: str, offer_price: str, offer_timeline: str) -> str:
    html = fetch_html(url)
    text = html_to_text(html)
    page_title = grab_title(html)
    parsed = urlparse(url)
    domain = parsed.netloc or "your market"

    checks = audit(text)
    score = sum(1 for _, ok, _ in checks if ok)

    lines = [
        "# Growth Agent MVP report",
        "",
        f"- URL: {url}",
        f"- Page title: {page_title}",
        f"- Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- Audit score: {score}/{len(checks)}",
        "",
        "## Landing-page audit",
    ]

    for name, ok, note in checks:
        badge = "PASS" if ok else "GAP"
        lines.append(f"- **{name}: {badge}**")
        if not ok:
            lines.append(f"  - Action: {note}")

    lines += ["", make_rewrite(domain, offer_name, offer_price, offer_timeline), "", make_checklist(), ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate landing-page audit + rewrite + publish checklist")
    parser.add_argument("url", help="Landing page URL to audit")
    parser.add_argument("--offer-name", default="Landing Page Conversion Sprint (Paid Test)")
    parser.add_argument("--offer-price", default="$1,500 fixed")
    parser.add_argument("--offer-timeline", default="5 business days")
    parser.add_argument("--output", default="reports/growth-agent-report.md")
    args = parser.parse_args()

    report = build_report(args.url, args.offer_name, args.offer_price, args.offer_timeline)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"Wrote report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
