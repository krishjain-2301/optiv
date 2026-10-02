"""Text inside vector images. SVG text is plain XML; EMF/WMF are reported as unreadable."""
from __future__ import annotations

from lxml import etree


def svg_text(blob: bytes) -> str:
    try:
        root = etree.fromstring(blob)
    except Exception:
        return ""
    parts = []
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname in ("text", "tspan", "title", "desc"):
            if (el.text or "").strip():
                parts.append(el.text.strip())
    return "\n".join(dict.fromkeys(parts))
