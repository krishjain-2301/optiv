"""Generate synthetic test artifacts that reproduce the traps found in the Cadence samples.

All people, numbers and addresses are invented. Every planted PII value is written to
``gold_labels.csv`` so recall and precision can be measured, not claimed.

    python scripts/make_samples.py [out_dir]
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pymupdf as fitz
from docx import Document as DocxDocument
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches as PInches, Pt as PPt

GOLD: list[dict] = []


def gold(file, page, text, category, context, note=""):
    GOLD.append({"file": file, "page": page if page is not None else "", "text": text, "category": category,
                 "context_type": context, "note": note})


def font(size: int, bold: bool = False):
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------------------------ PDF
PDF = "risk_policy_scanned.pdf"
W, H = 2480, 3508  # A4 at 300 dpi


def page_canvas():
    img = Image.new("RGB", (W, H), "white")
    return img, ImageDraw.Draw(img)


def text_block(d, x, y, lines, size=40, gap=1.45, bold=False):
    f = font(size, bold)
    for line in lines:
        d.text((x, y), line, fill="black", font=f)
        y += int(size * gap)
    return y


def ruled_table(d, x, y, col_w, rows, size=34, row_h=None):
    f = font(size)
    fb = font(size, bold=True)
    heights = []
    for r in rows:
        n = max(len(c.split("\n")) for c in r)
        heights.append(row_h or int(size * 1.5 * n + size))
    total_w = sum(col_w)
    yy = y
    d.line([(x, yy), (x + total_w, yy)], fill="black", width=3)
    for ri, r in enumerate(rows):
        xx = x
        for ci, cell in enumerate(r):
            ty = yy + size // 2
            for line in cell.split("\n"):
                d.text((xx + 14, ty), line, fill="black", font=fb if ri == 0 else f)
                ty += int(size * 1.5)
            xx += col_w[ci]
        yy += heights[ri]
        d.line([(x, yy), (x + total_w, yy)], fill="black", width=3)
    xx = x
    for cw in [0] + col_w:
        xx += cw
        d.line([(xx, y), (xx, yy)], fill="black", width=3)
    return yy


def screenshot(d, x, y, w, h, lines, size=22):
    """A mock admin-console screenshot: grey chrome, small text."""
    d.rectangle([x, y, x + w, y + h], fill=(236, 238, 242), outline=(90, 90, 90), width=4)
    d.rectangle([x, y, x + w, y + 60], fill=(40, 70, 120))
    d.text((x + 20, y + 14), "User Group Administration", fill="white", font=font(28, True))
    ty = y + 90
    for line in lines:
        d.text((x + 24, ty), line, fill=(20, 20, 20), font=font(size))
        ty += int(size * 1.9)


def make_pdf(out: Path):
    pages = []
    # Page 1: labelled identity record (body text with field labels)
    img, d = page_canvas()
    y = text_block(d, 180, 180, ["Appendix C.2 - Personnel Identity Record"], size=64, bold=True) + 40
    y = text_block(d, 180, y, ["This record is maintained by Human Resources for the Risk Owner of Tier 1 vendors."], size=38) + 30
    fields = [
        ("Full name: ", "Rafael Mendoza-Kowalski", "PERSON"),
        ("Employee ID: ", "EMP-40718", "EMPLOYEE_ID"),
        ("Date of birth: ", "12/04/1985", "DATE_OF_BIRTH"),
        ("SSN: ", "900-12-3456", "US_SSN"),
        ("Passport No: ", "K4829175", "PASSPORT"),
        ("PAN: ", "ABCPM1234F", "IN_PAN"),
        ("PESEL: ", "890412xxxxx", "PL_PESEL"),
        ("TIN: ", "12-3456789", "TAX_ID"),
        ("Home address: ", "14 Harrow Lane, Leeds LS6 2QT", "ADDRESS"),
        ("Mobile: ", "+44 7700 900418", "PHONE_NUMBER"),
    ]
    for label, value, cat in fields:
        y = text_block(d, 180, y, [label + value], size=40)
        gold(PDF, 1, value, cat, "labelled")
    y += 40
    y = text_block(d, 180, y, ["Corporate card ending 2218 is assigned for travel.", "Policy approved on 14/03/2024 by the Risk Committee."], size=38)
    gold(PDF, 1, "2218", "CREDIT_CARD", "narrative", "card last-4")
    pages.append(img)

    # Page 2: ruled contact table with multi-line cells and six phone formats
    img, d = page_canvas()
    y = text_block(d, 180, 180, ["Appendix D - Escalation Contacts"], size=64, bold=True) + 40
    rows = [
        ["Name", "Role", "Email", "Phone"],
        ["Grace Wilson", "Risk Manager", "grace.wilson@cadence-demo.example", "+1 (212) 555-0193"],
        ["Tomasz Zielinski", "Director, EMEA\nDIR-2291", "t.zielinski@cadence-demo.example", "+48 22 555 01 87"],
        ["Anjali Deshpande", "Vendor Lead\nEMP-51120", "anjali.d@cadence-demo.example", "+91 98200 55512"],
        ["Kenji Watanabe", "Control Owner", "kwatanabe@cadence-demo.example", "+81 3-5550-1234"],
        ["Lena Hoffmann", "Process Owner", "lena.hoffmann@cadence-demo.example", "+49 30 5550 1733"],
    ]
    ruled_table(d, 150, y, [500, 480, 760, 460], rows, size=30)  # 2200 px wide, fits A4
    for r in rows[1:]:
        gold(PDF, 2, r[0], "PERSON", "table")
        gold(PDF, 2, r[2], "EMAIL_ADDRESS", "table")
        gold(PDF, 2, r[3], "PHONE_NUMBER", "table")
    gold(PDF, 2, "DIR-2291", "EMPLOYEE_ID", "table")
    gold(PDF, 2, "EMP-51120", "EMPLOYEE_ID", "table")
    pages.append(img)

    # Page 3: narrative prose without labels + screenshot with small text
    img, d = page_canvas()
    y = text_block(d, 180, 180, ["16.5 Case Narrative"], size=64, bold=True) + 40
    narrative = [
        "During the quarterly review, Priya Raman noticed that access for the payroll",
        "vendor had not been revoked. Rafael's own record showed the change was approved",
        "while he was on leave, so Raman escalated the exception to Mendoza-Kowalski's",
        "manager. She can be reached at priya.raman@cadence-demo.example or on",
        "(415) 555-0142. The vendor contact, Samuel Okafor, lives at 221 Birch Street,",
        "Austin, TX 78701 and was born on 3 March 1979.",
    ]
    y = text_block(d, 180, y, narrative, size=38) + 60
    for text, cat in [("Priya Raman", "PERSON"), ("Rafael", "PERSON"), ("Raman", "PERSON"),
                      ("Mendoza-Kowalski", "PERSON"), ("priya.raman@cadence-demo.example", "EMAIL_ADDRESS"),
                      ("(415) 555-0142", "PHONE_NUMBER"), ("Samuel Okafor", "PERSON"),
                      ("221 Birch Street", "ADDRESS"), ("3 March 1979", "DATE_OF_BIRTH")]:
        gold(PDF, 3, text, cat, "narrative")
    shot = ["Member            Email                          Status",
            "Oliver Grant      ogrant@acmeco.example          Active",
            "Maria Santos      msantos@acmeco.example         Active",
            "Wei Chen          wchen@acmeco.example           Disabled"]
    screenshot(d, 180, y, 1900, 420, shot, size=24)
    for name, mail in [("Oliver Grant", "ogrant@acmeco.example"), ("Maria Santos", "msantos@acmeco.example"),
                       ("Wei Chen", "wchen@acmeco.example")]:
        gold(PDF, 3, name, "PERSON", "image")
        gold(PDF, 3, mail, "EMAIL_ADDRESS", "image")
    pages.append(img)

    pdf = fitz.open()
    for im in pages:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)  # scans are JPEG-compressed in practice
        page = pdf.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
    pdf.set_metadata({"author": "Helena Brandt", "creator": "Microsoft Word", "title": "Risk Management Policy"})
    gold(PDF, None, "Helena Brandt", "PERSON", "metadata")
    pdf.save(out / PDF)


# ----------------------------------------------------------------------------------- DOCX
DOCX = "tprm_training.docx"


def tool_screenshot_png() -> bytes:
    img = Image.new("RGB", (900, 260), (245, 246, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 899, 40], fill=(30, 60, 110))
    d.text((12, 8), "TPRM Tool - Assessment Owners", fill="white", font=font(20, True))
    rows = ["Owner: Daniel Brooks    daniel.brooks@vendorco.example",
            "Reviewer: Aisha Karim   aisha.karim@vendorco.example"]
    y = 70
    for r in rows:
        d.text((16, y), r, fill="black", font=font(20))
        y += 50
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_docx(out: Path):
    doc = DocxDocument()
    doc.core_properties.author = "Marcus Feld"
    doc.core_properties.last_modified_by = "Ines Duarte"
    gold(DOCX, None, "Marcus Feld", "PERSON", "metadata")
    gold(DOCX, None, "Ines Duarte", "PERSON", "metadata")

    sec = doc.sections[0]
    sec.header.paragraphs[0].text = "TPRM Training - contact tprm-office@cadence-demo.example"
    sec.footer.paragraphs[0].text = "Prepared by Jonathan Reyes"
    gold(DOCX, None, "Jonathan Reyes", "PERSON", "native")

    doc.add_heading("Third-Party Risk Management Training", level=1)
    doc.add_paragraph("This module explains how Vendor Tier and QRC_BYOD fields are used in OneTrust assessments.")
    doc.add_heading("Assessment roles", level=2)
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    hdr = ["Engagement", "Assigned reviewers", "Vendor Tier"]
    for i, h in enumerate(hdr):
        table.rows[0].cells[i].text = h
    data = [("Payroll outsourcing", ["Tamika Oliver", "Shimane Smith"], "Tier 1"),
            ("Cloud hosting", ["Rob Tilney", "Jyoti Karwal", "Jonathan Pierce"], "Tier 2")]
    for ri, (eng, people, tier) in enumerate(data, start=1):
        row = table.rows[ri].cells
        row[0].text = eng
        row[1].text = people[0]
        for p in people[1:]:
            row[1].add_paragraph(p)
        row[2].text = tier
        for p in people:
            gold(DOCX, None, p, "PERSON", "table")

    doc.add_paragraph(
        "If an assessment stalls, email the analyst directly: kofi.mensah@cadence-demo.example "
        "or call +1 408 555 0177. Kofi will re-assign it within two days."
    )
    gold(DOCX, None, "kofi.mensah@cadence-demo.example", "EMAIL_ADDRESS", "narrative")
    gold(DOCX, None, "+1 408 555 0177", "PHONE_NUMBER", "narrative")
    gold(DOCX, None, "Kofi", "PERSON", "narrative")

    doc.add_paragraph("The screenshot below shows the owner view:")
    doc.add_picture(io.BytesIO(tool_screenshot_png()), width=Inches(6))
    for t, c in [("Daniel Brooks", "PERSON"), ("daniel.brooks@vendorco.example", "EMAIL_ADDRESS"),
                 ("Aisha Karim", "PERSON"), ("aisha.karim@vendorco.example", "EMAIL_ADDRESS")]:
        gold(DOCX, None, t, c, "image")
    doc.save(out / DOCX)


# ----------------------------------------------------------------------------------- PPTX
PPTX = "org_pack.pptx"


def make_pptx(out: Path):
    prs = Presentation()
    prs.core_properties.author = "Sofia Lindqvist"
    prs.core_properties.last_modified_by = "Sofia Lindqvist"
    gold(PPTX, None, "Sofia Lindqvist", "PERSON", "metadata")

    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Risk Organisation - Contacts"
    people = [
        ("Martin Vossberg", "Chief Executive Officer", "m.vossberg@cadence.example", "+1 (555) 0114"),
        ("Clara Nguyen", "Chief Risk Officer", "c.nguyen@cadence.example", "+1 (555) 0127"),
        ("Farid Haddad", "Head of Internal Audit", "f.haddad@cadence.example", "+1 (555) 0139"),
        ("Beatriz Lopes", "TPRM Lead", "b.lopes@cadence.example", "+1 (555) 0145"),
    ]
    rows, cols = len(people) + 1, 4
    tbl = s.shapes.add_table(rows, cols, PInches(0.4), PInches(1.5), PInches(9.2), PInches(3)).table
    for i, h in enumerate(["Name", "Title", "E-mail", "Mobile"]):
        tbl.cell(0, i).text = h
    for r, p in enumerate(people, start=1):
        for c, v in enumerate(p):
            tbl.cell(r, c).text = v
            tbl.cell(r, c).text_frame.paragraphs[0].runs[0].font.size = PPt(12)
        gold(PPTX, 1, p[0], "PERSON", "table")
        gold(PPTX, 1, p[2], "EMAIL_ADDRESS", "table")
        gold(PPTX, 1, p[3], "PHONE_NUMBER", "table")
    s.notes_slide.notes_text_frame.text = "Confirm Clara's mobile before the board pack goes out."
    gold(PPTX, 1, "Clara", "PERSON", "narrative")

    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "RACI - Vendor Onboarding"
    grp = s2.shapes.add_group_shape()
    box = grp.shapes.add_textbox(PInches(0.5), PInches(1.6), PInches(3), PInches(0.6))
    box.text_frame.text = "M. Vossberg CEO"
    box2 = grp.shapes.add_textbox(PInches(4), PInches(1.6), PInches(3), PInches(0.6))
    box2.text_frame.text = "F. Haddad Internal Audit"
    gold(PPTX, 2, "M. Vossberg", "PERSON", "native")
    gold(PPTX, 2, "F. Haddad", "PERSON", "native")
    body = s2.shapes.add_textbox(PInches(0.5), PInches(2.6), PInches(9), PInches(1))
    body.text_frame.text = "Onboarding approvals route through Beatriz for all Tier 1 vendors."
    gold(PPTX, 2, "Beatriz", "PERSON", "narrative")
    prs.save(out / PPTX)


def main(out_dir: str = "samples/synthetic"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    GOLD.clear()
    make_pdf(out)
    make_docx(out)
    make_pptx(out)
    with open(out / "gold_labels.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "page", "text", "category", "context_type", "note"])
        w.writeheader()
        w.writerows(GOLD)
    print(f"wrote 3 artifacts and {len(GOLD)} gold labels to {out}")
    return out


if __name__ == "__main__":
    main(*sys.argv[1:])
