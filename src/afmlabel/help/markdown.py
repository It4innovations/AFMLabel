# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Minimal, dependency-free Markdown to HTML converter for the local Help
pages.

It covers what the project documentation uses: ATX headings (with
GitHub-style anchor ids), paragraphs, fenced code blocks, inline code,
emphasis, links, images, ordered/unordered lists (one nesting level), pipe
tables, block quotes and horizontal rules. Everything is HTML-escaped
first, so documentation content can never inject markup. Links to other
``*.md`` documents are rewritten to the local ``/help/<name>`` routes and
raster images in ``docs/images/`` to ``/help/images/<file>``, so the Help
stays self-contained; any other image (remote, SVG, outside ``images/``)
is replaced by its alt text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^```")
_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)\)")
#: File names the Help serves from ``docs/images`` (``/help/images/<file>``).
IMAGE_FILE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.(?:png|jpe?g|gif|webp)$", re.IGNORECASE
)
_LOCAL_IMAGE = re.compile(r"^(?:\./)?images/([^/]+)$")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(
    r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"
)
_MD_LINK = re.compile(r"^(?:\./)?([A-Za-z0-9_\-./]+?)\.md(#[A-Za-z0-9_\-]+)?$")


def slugify(text: str) -> str:
    """GitHub-style heading id: lowercase, punctuation removed, spaces -> ``-``."""
    plain = re.sub(r"`|\*|_", "", text)
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", plain)
    slug = re.sub(r"[^\w\s-]", "", plain.strip().lower())
    # GitHub keeps one hyphen per whitespace character (no collapsing).
    slug = re.sub(r"\s", "-", slug)
    return slug or "section"


def rewrite_link(target: str) -> str:
    """Local ``docs/x.md#anchor`` links become ``/help/x#anchor``."""
    match = _MD_LINK.match(target)
    if match:
        name = match.group(1).split("/")[-1]
        return f"/help/{name}{match.group(2) or ''}"
    return target


def image_src(target: str) -> str | None:
    """``/help/images/<file>`` for a raster image in ``docs/images``, else None."""
    match = _LOCAL_IMAGE.match(target)
    if match and IMAGE_FILE.match(match.group(1)):
        return f"/help/images/{match.group(1)}"
    return None


def render_inline(text: str) -> str:
    # Images become placeholders first, so neither the alt text nor the file
    # name is touched by the code/link/emphasis rules below.
    images: list[str] = []

    def image(match: re.Match[str]) -> str:
        alt = html.unescape(match.group(1))
        src = image_src(html.unescape(match.group(2)))
        if src is None:
            images.append(html.escape(alt, quote=False))
        else:
            images.append(
                f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}"'
                ' loading="lazy" />'
            )
        return f"\x00{len(images) - 1}\x00"

    escaped = html.escape(text.replace("\x00", ""), quote=False)
    escaped = _IMAGE.sub(image, escaped)
    escaped = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)

    def link(match: re.Match[str]) -> str:
        label, target = match.group(1), html.unescape(match.group(2))
        href = rewrite_link(target)
        safe = href if href.startswith(("/", "#", "http://", "https://", "mailto:")) else "#"
        external = ' rel="noopener" target="_blank"' if safe.startswith("http") else ""
        return f'<a href="{html.escape(safe, quote=True)}"{external}>{label}</a>'

    escaped = _LINK.sub(link, escaped)
    escaped = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
    return _PLACEHOLDER.sub(lambda m: images[int(m.group(1))], escaped)


@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Rendered:
    html: str
    title: str
    headings: list[Heading] = field(default_factory=list)


def render_markdown(source: str) -> Rendered:
    lines = source.splitlines()
    out: list[str] = []
    headings: list[Heading] = []
    used_anchors: dict[str, int] = {}
    title = ""
    paragraph: list[str] = []
    list_stack: list[tuple[str, int]] = []  # (tag, indent)
    in_code = False
    code: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            text = " ".join(paragraph)
            # A paragraph holding only images is a figure; its caption follows.
            figure = _IMAGE.search(text) is not None and not _IMAGE.sub("", text).strip()
            css = ' class="lv-help-figure"' if figure else ""
            out.append(f"<p{css}>{render_inline(text)}</p>")
            paragraph = []

    def close_lists(to_indent: int = -1) -> None:
        while list_stack and list_stack[-1][1] > to_indent:
            tag, _ = list_stack.pop()
            out[-1] += f"</li></{tag}>"

    def unique_anchor(text: str) -> str:
        base = slugify(text)
        count = used_anchors.get(base, 0)
        used_anchors[base] = count + 1
        return base if count == 0 else f"{base}-{count}"

    while index < len(lines):
        line = lines[index]
        if in_code:
            if _FENCE.match(line):
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = []
                in_code = False
            else:
                code.append(line)
            index += 1
            continue
        if _FENCE.match(line):
            flush_paragraph()
            close_lists()
            in_code = True
            index += 1
            continue
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_lists()
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            close_lists()
            level = len(heading.group(1))
            text = heading.group(2)
            anchor = unique_anchor(text)
            headings.append(Heading(level=level, text=re.sub(r"`", "", text), anchor=anchor))
            if not title and level == 1:
                title = re.sub(r"`", "", text)
            out.append(f'<h{level} id="{anchor}">{render_inline(text)}</h{level}>')
            index += 1
            continue
        if stripped in ("---", "***", "___"):
            flush_paragraph()
            close_lists()
            out.append("<hr />")
            index += 1
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip()[1:].strip())
                index += 1
            out.append(f"<blockquote><p>{render_inline(' '.join(quote))}</p></blockquote>")
            continue
        if "|" in stripped and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
            flush_paragraph()
            close_lists()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index]:
                rows.append([c.strip() for c in lines[index].strip().strip("|").split("|")])
                index += 1
            out.append('<div class="lv-help-table"><table>')
            out.append("<thead><tr>" + "".join(f"<th>{render_inline(c)}</th>" for c in header))
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table></div>")
            continue
        item = _UL.match(line) or _OL.match(line)
        if item:
            flush_paragraph()
            indent = len(item.group(1).replace("	", "    "))
            tag = "ul" if _UL.match(line) else "ol"
            content = render_inline(item.group(2))
            if list_stack and list_stack[-1][1] == indent:
                out[-1] += f"</li><li>{content}"
            elif not list_stack or indent > list_stack[-1][1]:
                # A deeper indent opens a nested list inside the open <li>.
                list_stack.append((tag, indent))
                if len(list_stack) > 1:
                    out[-1] += f"<{tag}><li>{content}"
                else:
                    out.append(f"<{tag}><li>{content}")
            else:
                close_lists(indent)
                if list_stack and list_stack[-1][1] == indent:
                    out[-1] += f"</li><li>{content}"
                else:
                    list_stack.append((tag, indent))
                    out.append(f"<{tag}><li>{content}")
            index += 1
            # continuation lines of the same item (indented, not a new item)
            while (
                index < len(lines)
                and lines[index].strip()
                and not _UL.match(lines[index])
                and not _OL.match(lines[index])
                and not _HEADING.match(lines[index])
                and not _FENCE.match(lines[index])
                and lines[index].startswith(" ")
            ):
                out[-1] += " " + render_inline(lines[index].strip())
                index += 1
            continue
        if list_stack:
            close_lists()
        paragraph.append(stripped)
        index += 1
    if in_code:
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush_paragraph()
    close_lists()
    return Rendered(html="\n".join(out), title=title, headings=headings)
