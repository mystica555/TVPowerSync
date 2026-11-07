#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Build the checked-in HTML documentation from README.md and ARCHITECTURE.md.

The generated pages are self-contained: the shared theme is inlined into every
output file, so GitHub Pages can serve them without extra assets.

Usage:
    python3 build_docs.py
    python3 build_docs.py --check

Dependencies are pinned in docs/requirements.txt.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit(
        "Error: Python-Markdown is not installed.\n"
        "Install the documentation dependencies with:\n"
        "  python3 -m pip install -r docs/requirements.txt"
    )


ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
REPO_URL = "https://github.com/mystica555/TVPowerSync"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

MARKDOWN_EXTENSIONS = [
    "extra",
    "toc",
    "sane_lists",
    "codehilite",
]
MARKDOWN_CONFIG = {
    "toc": {"permalink": False},
    "codehilite": {"guess_lang": False, "css_class": "highlight"},
}


@dataclass(frozen=True)
class Document:
    source: str
    outputs: tuple[str, ...]
    page_class: str
    nav_label: str
    eyebrow: str
    switch_title: str
    switch_meta: str
    title: str
    brand_subtitle: str
    next_href: str
    next_kicker: str
    next_title: str
    next_meta: str
    next_back: bool = False


DOCUMENTS = (
    Document(
        source="README.md",
        outputs=("README.html", "index.html"),
        page_class="page-readme",
        nav_label="README",
        eyebrow="Documentation &middot; README",
        switch_title="Overview &amp; deployment",
        switch_meta="Features &middot; configuration &middot; CLI",
        title="TVPowerSync",
        brand_subtitle="Multi-vendor TV control",
        next_href="ARCHITECTURE.html",
        next_kicker="Read next",
        next_title="Architecture &amp; technical reference",
        next_meta="Topology, protocols, state machine, edge cases",
    ),
    Document(
        source="ARCHITECTURE.md",
        outputs=("ARCHITECTURE.html",),
        page_class="page-architecture",
        nav_label="Architecture",
        eyebrow="Documentation &middot; Technical reference",
        switch_title="Internals &amp; event flow",
        switch_meta="Topology &middot; state machine &middot; edge cases",
        title="Detailed Architecture & Technical Reference — TVPowerSync",
        brand_subtitle="Multi-vendor TV control",
        next_href="README.html",
        next_kicker="Back to",
        next_title="Overview, configuration &amp; deployment",
        next_meta="Features, environment variables, CLI usage",
        next_back=True,
    ),
)


def convert_markdown(source_text: str) -> tuple[str, list[dict]]:
    """Return rendered HTML and the heading tree for the table of contents."""
    parser = markdown.Markdown(
        extensions=MARKDOWN_EXTENSIONS,
        extension_configs=MARKDOWN_CONFIG,
    )
    body = parser.convert(source_text)

    # Markdown links in the sources point at .md files; the site serves .html.
    body = re.sub(
        r'href="([^"#]+)\.md(#[^"]*)?"',
        lambda match: (
            match.group(0)
            if "://" in match.group(1) or match.group(1).startswith("mailto:")
            else f'href="{match.group(1)}.html{match.group(2) or ""}"'
        ),
        body,
    )

    # The theme gives tables their own scroll container.
    body = re.sub(
        r"<table>(.*?)</table>",
        r'<div class="table-wrap"><table>\1</table></div>',
        body,
        flags=re.S,
    )

    # Add the same hover/focus permalink shown by the existing site.
    def add_headerlink(match: re.Match[str]) -> str:
        level, heading_id, inner = match.groups()
        if 'class="headerlink"' in inner:
            return match.group(0)
        return (
            f'<h{level} id="{heading_id}">{inner}'
            f'<a class="headerlink" href="#{heading_id}" '
            f'title="Link to this section">#</a></h{level}>'
        )

    body = re.sub(
        r'<h([2-6]) id="([^"]+)">(.*?)</h\1>',
        add_headerlink,
        body,
        flags=re.S,
    )
    return body, parser.toc_tokens


def _toc_items(tokens: list[dict]) -> list[dict]:
    """Drop the page title and keep h2/h3 entries for the sidebar TOC."""
    items: list[dict] = []
    for token in tokens:
        level = token["level"]
        children = _toc_items(token.get("children", []))
        if level == 1:
            items.extend(children)
        elif level in (2, 3):
            items.append(
                {
                    "id": token["id"],
                    "label": token.get("html") or html.escape(token["name"]),
                    "children": children,
                }
            )
        else:
            items.extend(children)
    return items


def _render_toc_items(items: list[dict], nested: bool) -> str:
    if not items:
        return ""
    class_attr = ' class="toc-sub"' if nested else ""
    parts = [f"<ul{class_attr}>"]
    for item in items:
        children = _render_toc_items(item["children"], nested=True)
        parts.append(
            f'<li><a href="#{item["id"]}">{item["label"]}</a>{children}</li>'
        )
    parts.append("</ul>")
    return "".join(parts)


def render_toc(tokens: list[dict]) -> str:
    return _render_toc_items(_toc_items(tokens), nested=False)


def meta_description(body: str) -> str:
    match = re.search(r"<p>(.*?)</p>", body, re.S)
    if not match:
        return "TVPowerSync documentation."
    text = re.sub(r"<[^>]+>", "", match.group(1))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 158:
        text = text[:155].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text


def topbar_nav(active: Document) -> str:
    links = []
    for doc in DOCUMENTS:
        current = doc.source == active.source
        classes = "pill is-active" if current else "pill"
        aria = ' aria-current="page"' if current else ""
        links.append(
            f'<a class="{classes}" href="{doc.outputs[0]}"{aria}>'
            f"{doc.nav_label}</a>"
        )
    return "".join(links)


def sidebar_switch(active: Document) -> str:
    links = []
    for doc in DOCUMENTS:
        current = doc.source == active.source
        classes = "switch is-active" if current else "switch"
        aria = ' aria-current="page"' if current else ""
        links.append(
            f'<a class="{classes}" href="{doc.outputs[0]}"{aria}>'
            f'<span class="switch-kicker">{doc.nav_label}</span>'
            f'<span class="switch-title">{doc.switch_title}</span>'
            f'<span class="switch-meta">{doc.switch_meta}</span></a>'
        )
    return "".join(links)


def next_card(doc: Document) -> str:
    if not doc.next_href:
        return ""
    classes = "next-card back" if doc.next_back else "next-card"
    return (
        f'<a class="{classes}" href="{doc.next_href}">'
        f'<span class="next-kicker">{doc.next_kicker}</span>'
        f'<span class="next-title">{doc.next_title}</span>'
        f'<span class="next-meta">{doc.next_meta}</span></a>'
    )


def build_page(doc: Document, template: str, style: str) -> tuple[str, str]:
    source_text = (ROOT / doc.source).read_text(encoding="utf-8")
    body, toc_tokens = convert_markdown(source_text)
    title = html.escape(doc.title, quote=True)
    description = html.escape(meta_description(body), quote=True)
    replacements = {
        "TITLE": title,
        "DESCRIPTION": description,
        "PAGE_CLASS": doc.page_class,
        "BRAND_SUBTITLE": doc.brand_subtitle,
        "STYLE": style,
        "TOPBAR_NAV": topbar_nav(doc),
        "SIDEBAR_SWITCH": sidebar_switch(doc),
        "TOC": render_toc(toc_tokens),
        "EYEBROW": doc.eyebrow,
        "CONTENT": body,
        "SOURCE": doc.source,
        "VERSION": VERSION,
        "REPO_URL": REPO_URL,
        "NEXT_CARD": next_card(doc),
    }
    page = template
    for key, value in replacements.items():
        page = page.replace("{{" + key + "}}", value)

    leftover = re.search(r"\{\{[A-Z_]+\}\}", page)
    if leftover:
        raise RuntimeError(f"unreplaced template placeholder: {leftover.group(0)}")
    return page.rstrip() + "\n", doc.source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in HTML is not up to date",
    )
    args = parser.parse_args()

    template = (DOCS_DIR / "template.html").read_text(encoding="utf-8")
    style = (DOCS_DIR / "style.css").read_text(encoding="utf-8").strip()

    stale: list[str] = []
    for doc in DOCUMENTS:
        page, source = build_page(doc, template, style)
        for output in doc.outputs:
            output_path = ROOT / output
            if args.check:
                current = (
                    output_path.read_text(encoding="utf-8")
                    if output_path.exists()
                    else None
                )
                if current != page:
                    stale.append(output)
            else:
                output_path.write_text(page, encoding="utf-8")
                print(f"wrote {output} from {source}")

    if args.check:
        if stale:
            print("Documentation is out of date: " + ", ".join(stale))
            return 1
        print("Documentation is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
