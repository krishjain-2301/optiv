"""Layout analysis for scanned pages.

Plain page-level OCR merges multi-line table cells across columns and garbles the tiny text
inside screenshots. So before OCR we find:

* **ruled tables** (horizontal + vertical line grids) -> each cell is OCR'd on its own, which
  keeps rows and columns intact and gives every cell its column header;
* **image regions** (bordered or filled rectangles such as screenshots, badges, charts) -> the
  crop is upscaled and OCR'd again, and the result replaces the page-level text for that area.

Paragraph text outside those areas is grouped from OCR lines into blocks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from .ocr import OcrLine

Box = tuple[int, int, int, int]


@dataclass
class TableGrid:
    bbox: Box
    rows: list[int]  # y positions of horizontal rules
    cols: list[int]  # x positions of vertical rules
    cells: list[tuple[int, int, Box]] = field(default_factory=list)  # (row, col, box)

    @property
    def n_rows(self) -> int:
        return len(self.rows) - 1

    @property
    def n_cols(self) -> int:
        return len(self.cols) - 1


def to_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img


def find_tables(img: np.ndarray) -> list[TableGrid]:
    gray = to_gray(img)
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 25, 20), 1)))
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 60, 15))))
    grid = cv2.dilate(horiz | vert, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    tables: list[TableGrid] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.2 or ch < 25:
            continue
        rows = _rule_positions(horiz[y : y + ch, x : x + cw], axis=1, min_cover=0.5)
        cols = _rule_positions(vert[y : y + ch, x : x + cw], axis=0, min_cover=0.5)
        # At least two columns: a single bordered box (screenshot frame) is an image region.
        # One-row grids are kept here and sorted out in page_layout (screenshot vs header bar).
        if len(rows) < 2 or len(cols) < 3:
            continue
        rows = [y + r for r in rows]
        cols = [x + c_ for c_ in cols]
        grid_t = TableGrid((x, y, x + cw, y + ch), rows, cols)
        for ri in range(len(rows) - 1):
            for ci in range(len(cols) - 1):
                grid_t.cells.append((ri, ci, (cols[ci], rows[ri], cols[ci + 1], rows[ri + 1])))
        tables.append(grid_t)
    tables.sort(key=lambda t: t.bbox[1])
    return tables


def _runs(mask_1d: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of True values."""
    if not mask_1d.any():
        return []
    d = np.diff(np.concatenate([[0], mask_1d.astype(np.int8), [0]]))
    return list(zip(np.where(d == 1)[0].tolist(), np.where(d == -1)[0].tolist()))


def find_header_bar_tables(img: np.ndarray, body_h: float, lines: list[OcrLine],
                           exclude: list[Box] | None = None) -> list[TableGrid]:
    """Tables drawn Word-style with a dark header row and horizontal rules only (no vertical lines).

    * header bar: a dark band about one text line tall spanning much of the page width;
    * rows: light full-width rules below the bar (or the edges of shaded row bands), followed while
      the gap between them stays under a few text lines;
    * columns: where the white header labels start, merged with where body text lines start.
    """
    gray = to_gray(img)
    H, W = gray.shape
    exclude = exclude or []
    dark = gray < 110
    frac = dark.mean(axis=1)
    tables: list[TableGrid] = []
    for y0, y1 in _runs(frac > 0.3):
        bh = y1 - y0
        if not (0.6 * body_h <= bh <= 3.5 * body_h):
            continue  # a dark screenshot or a thin rule, not a header row
        if frac[max(0, y0 - 6):max(0, y0 - 1)].max(initial=0) > 0.3 or frac[y1 + 1:y1 + 6].max(initial=0) > 0.3:
            continue  # stacked dark rows (terminal/log screenshot), not an isolated header bar
        cols_dark = dark[y0:y1].mean(axis=0) > 0.6
        xr = _runs(cols_dark)
        if not xr:
            continue
        x0, x1 = xr[0][0], xr[-1][1]
        if x1 - x0 < 0.3 * W:
            continue
        box = (x0, y0, x1, y1)
        if any(_overlap(box, e) > 0.3 for e in exclude):
            continue

        # Column starts from the header labels (white text on the dark bar) ...
        label = (gray[y0:y1, x0:x1] > 170).any(axis=0)
        starts = []
        gap_min = 0.55 * bh
        prev_end = None
        for s, e in _runs(label):
            if prev_end is None or s - prev_end >= gap_min:
                starts.append(x0 + s)
            prev_end = e
        inner = starts[1:]
        margin = 0.8 * body_h

        def paragraph_line(l) -> bool:
            # A paragraph/heading line crosses a column boundary, is long, and has only normal
            # word spacing. OCR sometimes joins a whole table row into one line, but that line
            # keeps wide gaps where the columns change.
            crosses = any(l.bbox[0] < c - margin and l.bbox[2] > c + margin for c in inner)
            if not crosses or not (len(l.text) >= 60 or l.bbox[2] - l.bbox[0] > 0.75 * (x1 - x0)):
                return False
            gaps = [b.bbox[0] - a.bbox[2] for a, b in zip(l.words, l.words[1:])]
            return max(gaps, default=0) < body_h

        # Row boundaries from the median grey of each pixel row across the table: light rules
        # measure ~220-240, alternate-row shading ~245-250, white rows and text rows ~255.
        # Rules are used when the table has them; shading edges only when it has none.
        rowmed = np.median(gray[:, x0:x1], axis=1)
        text_rows = [(l.bbox[1] + 0.2 * l.height, l.bbox[3] - 0.2 * l.height) for l in lines
                     if l.bbox[2] > x0 and l.bbox[0] < x1]
        max_gap = 7 * body_h

        def walk(candidates: list[int]) -> list[int]:
            out = [y1]
            for b in candidates:
                if b - out[-1] > max_gap:
                    break
                if any(a < b < c for a, c in text_rows):
                    continue  # never cut through a line of text
                if b - out[-1] <= 0.6 * body_h:
                    continue
                if any(out[-1] < (l.bbox[1] + l.bbox[3]) / 2 < b and paragraph_line(l) for l in lines):
                    break
                out.append(b)
            return out

        runs = [(r0 + y1, r1 + y1) for r0, r1 in _runs(rowmed[y1:] < 251)]
        stop = next((r0 for r0, r1 in runs if frac[r0:r1].mean() > 0.3), H)  # next table's header bar
        runs = [(r0, r1) for r0, r1 in runs if r1 <= stop]
        # Rules: thin runs darker than shading. Searched on their own (< 243) so a rule directly
        # under a shaded row does not melt into that row's band.
        rules = [(r0 + y1 + r1 + y1) // 2 for r0, r1 in _runs(rowmed[y1:stop] < 243)
                 if r1 - r0 <= max(4, 0.25 * body_h)]
        boundaries = walk(rules)
        if len(boundaries) == 1:
            bands = [e for r0, r1 in runs if r1 - r0 > 0.25 * body_h for e in (r0, r1)]
            boundaries = walk(sorted(bands))
        # The last row may have no bottom rule: close it after the last text line that starts
        # inside the table's width before the next large gap.
        body_lines = sorted((l for l in lines if l.bbox[1] >= boundaries[-1] - 2 and l.bbox[0] >= x0 - 5
                             and l.bbox[2] <= x1 + 5), key=lambda l: l.bbox[1])
        if len(boundaries) == 1 and body_lines:
            last = boundaries[-1]
            for l in body_lines:
                if l.bbox[1] - last > 1.2 * body_h:
                    break
                last = l.bbox[3]
            if last > boundaries[-1] + 0.5 * body_h:
                boundaries.append(int(last + 0.3 * body_h))
        if len(boundaries) < 2:
            continue

        # ... plus left edges shared by body lines in at least two rows.
        inside = [l for l in lines if y1 <= l.bbox[1] < boundaries[-1] and x0 <= l.bbox[0] < x1]
        lefts = sorted(int(l.bbox[0]) for l in inside)
        tol = 0.6 * body_h
        clusters: list[list[int]] = []
        for x in lefts:
            if clusters and x - clusters[-1][-1] <= tol:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        for c in clusters:
            if len(c) >= 2 and all(abs(c[0] - s) > 1.5 * body_h for s in starts):
                starts.append(c[0])
        starts = sorted(set(starts))
        if len(starts) < 2:
            continue
        pad = int(0.3 * body_h)
        cols = [x0] + [max(x0, s - pad) for s in starts[1:]] + [x1]
        rows = [y0] + boundaries
        grid = TableGrid((x0, y0, x1, boundaries[-1]), rows, cols)
        for ri in range(len(rows) - 1):
            for ci in range(len(cols) - 1):
                grid.cells.append((ri, ci, (cols[ci], rows[ri], cols[ci + 1], rows[ri + 1])))
        tables.append(grid)
        exclude = exclude + [grid.bbox]
    return tables


def assign_words_to_cells(grid: TableGrid, lines: list[OcrLine]) -> dict[tuple[int, int], list[OcrLine]]:
    """Split OCR lines into per-cell lines by each word's centre. Words that belong to the same
    source line and cell stay one line; a cell's lines are ordered top to bottom."""
    import bisect

    out: dict[tuple[int, int], list[OcrLine]] = {}
    for line in sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0])):
        parts: dict[tuple[int, int], OcrLine] = {}
        words = []
        for w in line.words:  # split words OCR glued across a column boundary
            pieces = [w]
            for c in grid.cols[1:-1]:
                pieces = [q for p in pieces for q in (p.split_at(c) if p.bbox[0] < c < p.bbox[2] else [p])]
            words.extend(pieces)
        for w in words:
            cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
            ri = min(max(bisect.bisect_right(grid.rows, cy) - 1, 0), grid.n_rows - 1)
            ci = min(max(bisect.bisect_right(grid.cols, cx) - 1, 0), grid.n_cols - 1)
            parts.setdefault((ri, ci), OcrLine()).words.append(w)
        for key, part in parts.items():
            out.setdefault(key, []).append(part)
    return out


def is_screenshot_grid(t: TableGrid, body_h: float) -> bool:
    """Window chrome and sidebars form line grids too. Real table rows are a few text lines tall;
    a grid whose rows average many text lines is a screenshot and belongs to the image path."""
    height = t.bbox[3] - t.bbox[1]
    tallest = max((b - a for a, b in zip(t.rows, t.rows[1:])), default=height)
    # Mean catches window grids; the tallest row catches framed cards (a badge's double border
    # makes thin strips that pull the mean down).
    return height / max(t.n_rows, 1) > 5 * body_h or tallest > 6 * body_h


def _rule_positions(mask: np.ndarray, axis: int, min_cover: float) -> list[int]:
    """Positions where a rule spans most of the table, clustered so thick lines count once."""
    length = mask.shape[axis]
    proj = (mask > 0).sum(axis=axis)
    idx = np.where(proj >= length * min_cover)[0]
    if idx.size == 0:
        return []
    groups, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > 4:
            groups.append((start + prev) // 2)
            start = i
        prev = i
    groups.append((start + prev) // 2)
    return [int(g) for g in groups]


def find_image_regions(img: np.ndarray, exclude: list[Box]) -> list[Box]:
    """Screenshots, photos and diagrams: large bordered or filled rectangles that are not tables."""
    gray = to_gray(img)
    h, w = gray.shape
    mask = (gray < 235).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions: list[Box] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.15 or ch < h * 0.04 or cw * ch < w * h * 0.015:
            continue
        box = (x, y, x + cw, y + ch)
        if any(_overlap(box, e) > 0.5 for e in exclude):
            continue
        # A region must be a real object: either a closed border or a filled background.
        fill = (mask[y : y + ch, x : x + cw] > 0).mean()
        border = _border_ratio(mask, box)
        if fill > 0.35 or border > 0.75:
            regions.append(box)
    return _dedupe(regions)


CAPTION = re.compile(r"^\s*(?:Figure|Fig\.|Exhibit|Chart|Diagram)\s*\d+", re.I)


def find_captioned_figures(img: np.ndarray, lines: list[OcrLine], body_h: float) -> list[Box]:
    """Figures announced by a caption ("Figure 1 - ..."). The caption is strong evidence, so the
    object directly above it is accepted with relaxed fill/border thresholds: org charts and
    diagrams on white backgrounds with faint frames fail the generic region test."""
    gray = to_gray(img)
    h, w = gray.shape
    mask = (gray < 235).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in contours]
    boxes = [(x, y, x + cw, y + ch) for x, y, cw, ch in boxes if cw * ch >= w * h * 0.015 and cw >= w * 0.15]
    out: list[Box] = []
    for cap in (l for l in lines if CAPTION.match(l.text)):
        cy = cap.bbox[1]
        above = [b for b in boxes if cy - 3 * body_h <= b[3] <= cy + 0.5 * body_h
                 and min(b[2], cap.bbox[2]) - max(b[0], cap.bbox[0]) > 0]
        if not above:
            continue
        b = max(above, key=lambda b: b[3])  # the object closest to its caption
        fill = (mask[b[1]:b[3], b[0]:b[2]] > 0).mean()
        if fill > 0.08 or _border_ratio(mask, b) > 0.4:
            out.append(b)
    return _dedupe(out)


def _border_ratio(mask: np.ndarray, box: Box) -> float:
    x0, y0, x1, y1 = box
    edges = [mask[y0, x0:x1], mask[y1 - 1, x0:x1], mask[y0:y1, x0], mask[y0:y1, x1 - 1]]
    return float(np.mean([(e > 0).mean() for e in edges]))


def _overlap(a: Box, b: Box) -> float:
    """Intersection over the area of ``a``."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1)
    return ix * iy / area


def _dedupe(boxes: list[Box]) -> list[Box]:
    boxes = sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    kept: list[Box] = []
    for b in boxes:
        if not any(_overlap(b, k) > 0.8 for k in kept):
            kept.append(b)
    return sorted(kept, key=lambda b: (b[1], b[0]))


def inside(line: OcrLine, box: Box, min_overlap: float = 0.5) -> bool:
    return _overlap(tuple(int(v) for v in line.bbox), box) >= min_overlap


def group_blocks(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Group OCR lines into paragraphs: close vertically and aligned or overlapping horizontally."""
    lines = sorted(lines, key=lambda l: (round(l.bbox[1] / 8), l.bbox[0]))
    lines = _merge_same_row(lines)
    blocks: list[list[OcrLine]] = []
    for line in lines:
        if blocks:
            prev = blocks[-1][-1]
            gap = line.bbox[1] - prev.bbox[3]
            lh = max(min(prev.height, line.height), 1)
            x_overlap = min(prev.bbox[2], line.bbox[2]) - max(prev.bbox[0], line.bbox[0])
            aligned = abs(line.bbox[0] - blocks[-1][0].bbox[0]) < 3 * lh or x_overlap > 0
            similar = 0.7 < line.height / max(prev.height, 1) < 1.4
            if -lh * 0.5 < gap < lh * 0.9 and aligned and similar:
                blocks[-1].append(line)
                continue
        blocks.append([line])
    return blocks


def _merge_same_row(lines: list[OcrLine]) -> list[OcrLine]:
    """OCR engines sometimes split one visual line in pieces; join pieces on the same baseline."""
    out: list[OcrLine] = []
    for line in lines:
        if out:
            p = out[-1]
            same_row = abs((p.bbox[1] + p.bbox[3]) / 2 - (line.bbox[1] + line.bbox[3]) / 2) < min(p.height, line.height) * 0.4
            near = 0 <= line.bbox[0] - p.bbox[2] < max(p.height, line.height) * 1.5
            if same_row and near:
                out[-1] = OcrLine(p.words + line.words)
                continue
        out.append(line)
    return out
