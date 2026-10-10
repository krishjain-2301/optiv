"""Last line of defence: nothing in the token vault may survive in an output.

Detection decides *what* is personal data; this module checks that every output actually lost it.
After a masked file is written, every member of the package is read (XML, relationships, images,
binaries, and packages nested inside it, recursively) and searched for every original value in the
vault, in the forms it can take there: as written, XML-escaped, URL-encoded (``mailto:`` links),
split across text runs, UTF-16 inside binary objects, and for long numbers digits-only
("tel:+12125550193"). If anything is found the file is not written.

``scrub_*`` replace surviving values with their tokens first, in places where that is safe to do
without understanding the format (element text, alt text, author attributes, external link targets
and plain text). ``check_*`` then report whatever is left.
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import quote, unquote

from lxml import etree

from ..redact.tokens import TokenVault

# Values this short (or numbers this short) match too much unrelated content to be checked
# reliably; they are still redacted by detection, they are just not used as needles.
MIN_LEN = 4
MIN_DIGITS_ONLY = 6
MIN_DIGITS_NORMALISED = 9
SCRUB_ATTRS = {"descr", "title", "author", "initials", "name"}


class LeakError(RuntimeError):
    """An output still contains an original value from the token vault."""

    def __init__(self, what: str, hits: list["Hit"]):
        self.hits = hits
        where = sorted({h.where for h in hits})
        super().__init__(f"{what}: {len(hits)} original value(s) survived masking in {', '.join(where[:6])}"
                         + (" ..." if len(where) > 6 else ""))


@dataclass
class Hit:
    where: str  # member path inside the package (nested packages joined with "!")
    token: str
    value: str


# ------------------------------------------------------------------------------- needles
@dataclass
class Needles:
    regex: re.Pattern | None  # any value, any written form, case-insensitive
    digits: re.Pattern | None  # long numbers, digits-only form
    by_form: dict[str, str]  # lowercased form -> token
    by_digits: dict[str, str]
    words: re.Pattern | None = None  # single-word names, case-sensitive ("Cloud" must not match "cloud")

    def token_for(self, matched: str) -> str:
        key = re.sub(r"\s+", " ", matched.lower())
        return self.by_form.get(key) or self.by_digits.get(re.sub(r"\D", "", matched)) or "[REDACTED]"


def _forms(value: str) -> set[str]:
    v = re.sub(r"\s+", " ", value.strip())
    forms = {v, html.escape(v, quote=True), html.escape(v, quote=False).replace("'", "&apos;"),
             quote(v, safe="@.-_ "), quote(v, safe="")}
    return {f for f in forms if f}


def _values(vault: TokenVault, findings: dict | None) -> list[tuple[str, str]]:
    """(value, token) pairs to search for. Single-word person hits in the review band (a lone
    capitalised word NER was unsure about: "Cloud", "Training") are redacted where they were found
    but are not evidence that the word is personal data everywhere else, so they are not needles.
    Neither is a word masked only because a stamp runs across it: it is unreadable there, not a value."""
    if findings is None:
        return [(v, t) for t, vs in vault.values.items() for v in vs]
    out = []
    for fs in findings.values():
        for f in fs:
            if f.decision not in ("redact", "review") or not f.token:
                continue
            if f.entity_type == "PERSON" and len(f.text.split()) == 1 and f.decision == "review":
                continue
            if f.recognizer == "failclosed:overprint":
                continue
            out.append((f.text, f.token))
    return out


def build_needles(vault: TokenVault, findings: dict | None = None) -> Needles:
    from ..detect.names import is_given_name

    by_form: dict[str, str] = {}
    by_digits: dict[str, str] = {}
    words: set[str] = set()
    for value, token in _values(vault, findings):
        v = value.strip()
        d = re.sub(r"\D", "", v)
        if len(v) < MIN_LEN or (d == re.sub(r"[\s().+-]", "", v) and len(d) < MIN_DIGITS_ONLY):
            continue
        if token.startswith("[PERSON_") and len(v.split()) == 1:
            # One-word names follow the propagation rule: as written or in capitals, lowercase
            # only for a listed given name. "Page" must not match every "page".
            forms = {v, v.upper()} | ({v.lower()} if is_given_name(v) else set())
            words |= forms
            for f in forms:
                by_form.setdefault(f.lower(), token)
            continue
        for f in _forms(v):
            by_form.setdefault(f.lower(), token)
        if len(d) >= MIN_DIGITS_NORMALISED and len(d) >= 0.6 * len(re.sub(r"\s", "", v)):
            by_digits.setdefault(d, token)
    multi = {k for k in by_form if k not in {w.lower() for w in words} or " " in k}
    regex = None
    if multi:
        alts = sorted(multi, key=len, reverse=True)
        body = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in alts)
        regex = re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])", re.IGNORECASE)
    word_re = None
    if words:
        body = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        word_re = re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])")
    digits = None
    if by_digits:
        alts = sorted(by_digits, key=len, reverse=True)
        digits = re.compile(r"(?<!\d)(?:" + "|".join(r"[\s().+-]{0,2}".join(a) for a in alts) + r")(?!\d)")
    return Needles(regex, digits, by_form, by_digits, word_re)


def find(text: str, n: Needles) -> list[tuple[int, int, str]]:
    out = []
    if n.regex is not None:
        out += [(m.start(), m.end(), m.group()) for m in n.regex.finditer(text)]
    if n.words is not None:
        out += [(m.start(), m.end(), m.group()) for m in n.words.finditer(text)]
    if n.digits is not None:
        out += [(m.start(), m.end(), m.group()) for m in n.digits.finditer(text)]
    return out


# ------------------------------------------------------------------------------- scanning
TAG = re.compile(r"<[^>]+>")


def _text_views(raw: str) -> list[str]:
    """The ways a value can sit in a text-like part: as written, with markup removed (values split
    across runs: "Grace Wil</w:t></w:r><w:r><w:t>son"), unescaped and URL-decoded."""
    views = [raw]
    if "<" in raw:
        views += [TAG.sub("", raw), TAG.sub(" ", raw)]
    more = []
    for v in views:
        u = html.unescape(v)
        more += [u, unquote(u)]
    return list(dict.fromkeys(views + more))


FONT_EXT = {"fntdata", "odttf", "ttf", "otf", "woff", "woff2"}


def image_metadata(data: bytes) -> str | None:
    """Text metadata of a raster image, or None when the bytes are not a raster image."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return None
    parts = []
    for v in list(img.info.values()) + [str(v) for v in img.getexif().values()]:
        if isinstance(v, bytes):
            parts += [v.decode("latin-1"), v.decode("utf-16-le", errors="ignore")]
        else:
            parts.append(str(v))
    return " \n ".join(parts)


def strip_image_metadata(data: bytes) -> bytes:
    """Re-save a raster image without EXIF / XMP / text chunks when it carries any."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return data
    keep = {"dpi", "gamma", "transparency", "aspect", "duration", "loop", "background", "jfif", "jfif_version",
            "jfif_unit", "jfif_density", "progressive", "progression"}
    if not set(img.info) - keep and not len(img.getexif()):
        return data
    fmt = img.format or "PNG"
    buf = io.BytesIO()
    params = {"quality": 95} if fmt.upper() in ("JPEG", "JPG") else {}
    if "transparency" in img.info:
        params["transparency"] = img.info["transparency"]
    if "dpi" in img.info:
        params["dpi"] = img.info["dpi"]
    img.save(buf, format=fmt, **params)
    return buf.getvalue()


def scan_bytes(name: str, data: bytes, n: Needles, depth: int = 0) -> list[Hit]:
    if data[:4] == b"PK\x03\x04" and depth < 4:
        try:
            return scan_package(data, n, prefix=f"{name}!", depth=depth + 1)
        except zipfile.BadZipFile:
            pass
    if name.rsplit(".", 1)[-1].lower() in FONT_EXT:
        return []
    hits: list[Hit] = []
    texts = []
    try:
        texts += _text_views(data.decode("utf-8"))
    except UnicodeDecodeError:
        meta = image_metadata(data)
        if meta is not None:
            # Compressed pixels look like random letters to a text search; only the metadata
            # (EXIF artist, XMP creator, PNG text chunks, JPEG comments) can carry a readable value.
            texts.append(meta)
        else:
            # EMF/WMF records and OLE streams store text as uncompressed UTF-16.
            texts.append(data.decode("utf-16-le", errors="ignore"))
            texts.append(data[1:].decode("utf-16-le", errors="ignore"))
    seen = set()
    for t in texts:
        for _, _, matched in find(t, n):
            key = matched.lower()
            if key not in seen:
                seen.add(key)
                hits.append(Hit(name, n.token_for(matched), matched))
    return hits


def scan_package(data: bytes, n: Needles, prefix: str = "", depth: int = 0) -> list[Hit]:
    hits: list[Hit] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            hits += scan_bytes(prefix + info.filename, zf.read(info), n, depth)
    return hits


def check_package(path_or_bytes, n: Needles, what: str) -> None:
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, "rb").read()
    hits = scan_package(data, n)
    if hits:
        raise LeakError(what, hits)


def check_pdf(path, n: Needles, what: str) -> None:
    import pymupdf as fitz

    hits: list[Hit] = []
    with fitz.open(path) as pdf:
        meta = " ".join(v for v in (pdf.metadata or {}).values() if v) + " " + (pdf.get_xml_metadata() or "")
        toc = " | ".join(entry[1] for entry in pdf.get_toc(simple=True))
        for name, text in [("metadata", meta), ("bookmarks", toc)] + [(f"page {p.number + 1}", p.get_text()) for p in pdf]:
            for _, _, m in find(text, n):
                hits.append(Hit(name, n.token_for(m), m))
        for i, link in ((p.number, l) for p in pdf for l in p.get_links()):
            for _, _, m in find(unquote(link.get("uri") or ""), n):
                hits.append(Hit(f"page {i + 1} link", n.token_for(m), m))
    if hits:
        raise LeakError(what, hits)


# ------------------------------------------------------------------------------- scrubbing
def scrub_text(text: str, n: Needles) -> tuple[str, int]:
    """Replace every remaining vault value in plain text with its token."""
    hits = sorted(find(text, n), key=lambda h: (h[0], -h[1]))
    out, pos, count = [], 0, 0
    for s, e, m in hits:
        if s < pos:
            continue
        out.append(text[pos:s] + n.token_for(m))
        pos, count = e, count + 1
    out.append(text[pos:])
    return "".join(out), count


def scrub_xml(data: bytes, n: Needles) -> tuple[bytes, int]:
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        return data, 0
    count = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("text", "tail"):
            v = getattr(el, attr)
            if v:
                new, c = scrub_text(v, n)
                if c:
                    setattr(el, attr, new)
                    count += c
        local = etree.QName(el).localname
        for k, v in el.attrib.items():
            name = etree.QName(k).localname
            external = local == "Relationship" and el.get("TargetMode") == "External" and name == "Target"
            if name in SCRUB_ATTRS or external:
                new, c = scrub_text(unquote(v) if external else v, n)
                if c:
                    el.set(k, new if not external else quote(new, safe=":/@.-_[]?=&#%+"))
                    count += c
    if not count:
        return data, 0
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), count


def scrub_package(data: bytes, n: Needles) -> tuple[bytes, int]:
    """Rewrite every XML part of a saved OOXML package with surviving values replaced."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info)
            if info.filename.endswith((".xml", ".rels")):
                body, c = scrub_xml(body, n)
                total += c
            dst.writestr(info, body)
    return buf.getvalue(), total
