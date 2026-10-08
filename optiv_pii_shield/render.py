"""Render a Document's spans as Markdown. Used for the extracted view and, with redacted span
texts substituted, for the LLM-safe output, so both always have identical structure."""
from __future__ import annotations

from typing import Optional

from .models import Document, Span

QUOTED = {"header", "footer", "comment", "footnote", "endnote", "notes", "text_box", "alt_text", "chart", "diagram", "link"}
# Layout / master placeholders and Word building blocks are redacted in the masked file but are
# boilerplate ("Click to edit Master title style"), so they are left out of the text for the LLM.
NOT_RENDERED = {"template", "field", "bookmark"}  # bookmarks repeat the headings


def _esc(t: str) -> str:
    return t.replace("|", "\\|").replace("\n", "<br>")


def render_markdown(doc: Document, texts: Optional[dict[str, str]] = None) -> str:
    texts = texts or {}
    txt = lambda s: texts.get(s.id, s.text)  # noqa: E731
    positional = doc.file_type in ("pdf", "image")
    img_boxes = {i.id: i.bbox for i in doc.images}

    items: list[tuple[tuple, str, object]] = []  # (sort key, type, payload)
    tables: dict[tuple, list[Span]] = {}
    images: dict[str, list[Span]] = {}
    meta: list[Span] = []
    for idx, s in enumerate(doc.spans):
        y = 0.0
        if positional:
            box = s.bbox or img_boxes.get(s.image_ref)
            y = box[1] if box else 0.0
        key = (s.page or 0, y, idx)
        if s.kind in NOT_RENDERED:
            continue
        if s.kind == "metadata":
            meta.append(s)
        elif s.kind == "table_cell" and s.table is not None:
            tkey = (s.page, s.anchor.split("/r[")[0] if s.anchor else s.table[0])
            if tkey not in tables:
                tables[tkey] = []
                items.append((key, "table", tkey))
            tables[tkey].append(s)
        elif s.kind == "image_ocr" and s.image_ref:
            if s.image_ref not in images:
                images[s.image_ref] = []
                items.append((key, "image", s.image_ref))
            images[s.image_ref].append(s)
        else:
            items.append((key, "span", s))
    items.sort(key=lambda t: t[0])

    out: list[str] = []
    page = None
    speaker = ""
    for key, typ, payload in items:
        if key[0] and key[0] != page:
            page = key[0]
            out.append(f"<!-- {'slide' if doc.file_type == 'pptx' else 'page'} {page} -->")
        if typ == "table":
            cells = tables[payload]
            n_rows = max(c.table[1] for c in cells) + 1
            n_cols = max(c.table[2] for c in cells) + 1
            grid = [[""] * n_cols for _ in range(n_rows)]
            for c in cells:
                grid[c.table[1]][c.table[2]] = _esc(txt(c).strip())
            lines = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * n_cols]
            lines += ["| " + " | ".join(r) + " |" for r in grid[1:]]
            out.append("\n".join(lines))
        elif typ == "image":
            spans = images[payload]
            body = " / ".join(txt(s).replace("\n", " / ") for s in spans)
            out.append(f"> [text in image: {spans[0].location.rsplit(',', 1)[0]}] {body}")
        else:
            s: Span = payload
            t = txt(s).strip()
            if s.kind == "speaker":  # a transcript cue's speaker: joined to what they said
                speaker = t
                continue
            if speaker:
                t, speaker = f"**{speaker}:** {t}", ""
            if s.kind == "heading":
                out.append("#" * min(s.level or 2, 6) + " " + " ".join(t.split()))
            elif s.kind in QUOTED:
                out.append(f"> [{s.kind.replace('_', ' ')}] " + t.replace("\n", " / "))
            else:
                out.append(t)
    for i in doc.images:
        if i.ocr_status == "unreadable":
            out.append(f"> [{i.location}: image could not be read; withheld]")
    if meta:
        out.append("<!-- document properties -->\n" + "\n".join(f"- {s.location}: {' '.join(txt(s).split())}" for s in meta))
    return "\n\n".join(out).strip()
