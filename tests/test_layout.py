"""Layout detection on synthetic scans (no OCR needed: text lines are given directly)."""
import numpy as np

from pii_shield.extract.layout import find_header_bar_tables, find_tables, is_screenshot_grid
from pii_shield.extract.ocr import OcrLine, OcrWord

BODY_H = 30


def line(x0, y0, x1, text, gap_every=None):
    """An OCR line with evenly spread words (optionally a wide gap, like a joined table row)."""
    words, toks = [], text.split()
    step = (x1 - x0) / max(len(toks), 1)
    for i, t in enumerate(toks):
        words.append(OcrWord(t, (x0 + i * step, y0, x0 + (i + 0.8) * step, y0 + BODY_H), 0.99))
    return OcrLine(words)


def bar_table_page(shaded=True):
    """Word-style table: dark header bar, faint rules (230), alternate shading (247), no verticals,
    followed by a paragraph and a framed figure whose top edge looks like one more rule."""
    img = np.full((1400, 1200, 3), 255, np.uint8)
    x0, x1 = 100, 1100
    img[200:245, x0:x1] = 35  # header bar
    img[205:235, 110:220] = 250  # white header labels at the column starts
    img[205:235, 420:560] = 250
    img[205:235, 760:900] = 250
    rows = [245, 320, 395, 470]
    for i, (a, b) in enumerate(zip(rows, rows[1:])):
        if shaded and i % 2 == 0:
            img[a:b, x0:x1] = 247
        img[b - 1:b + 1, x0:x1] = 230  # faint rule
    img[700:702, x0:x1] = 225  # top border of a figure frame below the paragraph
    img[700:1000, x0:x0 + 2] = 225
    lines = []
    for a in rows[:-1]:
        lines += [line(110, a + 20, 300, "Alice Smith"), line(420, a + 20, 700, "Risk Manager"),
                  line(760, a + 20, 1080, "alice@example.com")]
    lines.append(line(100, 520, 1100, "This paragraph follows the table and spans the full text width of the page body."))
    lines.append(line(100, 560, 1100, "It has normal word spacing everywhere and must never become a table row at all."))
    return img, lines


def test_header_bar_table_rows_and_columns():
    img, lines = bar_table_page()
    tables = find_header_bar_tables(img, BODY_H, lines)
    assert len(tables) == 1
    t = tables[0]
    assert t.n_rows == 4  # header + 3 body rows: stops before the paragraph and the figure frame
    assert t.n_cols == 3
    assert t.bbox[3] < 520


def test_header_bar_table_without_shading():
    img, lines = bar_table_page(shaded=False)
    t = find_header_bar_tables(img, BODY_H, lines)[0]
    assert (t.n_rows, t.n_cols) == (4, 3)


def test_stacked_dark_rows_are_not_header_bars():
    img = np.full((1000, 1200, 3), 255, np.uint8)
    for y in range(100, 600, 32):  # terminal/log screenshot: dark rows separated by 2 px
        img[y:y + 30, 100:1100] = 30
    assert find_header_bar_tables(img, BODY_H, []) == []


def test_framed_card_is_screenshot_not_table():
    img = np.full((1200, 1200, 3), 255, np.uint8)
    for (a, b, c, d) in [(100, 100, 1000, 900), (120, 120, 980, 880)]:  # double border, like the ID badge
        img[b:b + 3, a:c] = 60
        img[d - 3:d, a:c] = 60
        img[b:d, a:a + 3] = 60
        img[b:d, c - 3:c] = 60
    grids = find_tables(img)
    assert grids and all(is_screenshot_grid(g, BODY_H) for g in grids)


def test_word_glued_across_columns_is_split():
    from pii_shield.extract.layout import TableGrid, assign_words_to_cells

    text = "EMP-41077IAM"
    chars = [(100 + 20 * i, 118 + 20 * i) for i in range(len(text))]  # "IAM" starts at x=280
    glued = OcrWord(text, (100, 10, chars[-1][1], 40), 0.99, chars)
    grid = TableGrid((90, 0, 500, 50), rows=[0, 50], cols=[90, 270, 500])
    grid.cells = [(0, 0, (90, 0, 270, 50)), (0, 1, (270, 0, 500, 50))]
    cells = assign_words_to_cells(grid, [OcrLine([glued, OcrWord("Engineer", (345, 10, 440, 40), 0.99)])])
    assert cells[(0, 0)][0].text == "EMP-41077"
    assert cells[(0, 1)][0].text == "IAM Engineer"
