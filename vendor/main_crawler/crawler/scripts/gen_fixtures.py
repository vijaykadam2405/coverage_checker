"""Generate the BINARY test fixtures (PDF + images).

Run once: ``python scripts/gen_fixtures.py``. Output lands in tests/fixtures/.
These are committed assets; reportlab is only needed to (re)generate them, not
at crawler runtime.
"""
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)


def gen_rfp_pdf() -> None:
    """A realistic MoD India RFP with a deadline, value, qty, and requirements —
    text must be extractable by pypdf (so it appears in extracted_text)."""
    path = FIX / "mod_rfp.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    lines = [
        "MINISTRY OF DEFENCE (MoD), GOVERNMENT OF INDIA",
        "REQUEST FOR PROPOSAL (RFP)",
        "RFP No: MoD/2026/ART/0441",
        "",
        "Subject: Procurement of 155mm / 52-calibre Mounted Gun System (MGS)",
        "Issuer: Ministry of Defence, Department of Defence Production",
        "Country: India",
        "",
        "1. The Ministry of Defence invites proposals for the supply of 100 units",
        "   of a 155mm 52-calibre Mounted Gun System for the Indian Army.",
        "2. Estimated acquisition value: approximately Rs 6,500 cr.",
        "3. Last date for submission of bids: 08 July 2026 by 1500 hours.",
        "",
        "Key Requirements:",
        "   System: 155mm / 52-calibre mounted gun system",
        "   Range: not less than 45 km with ERFB-BT ammunition",
        "   Rate of fire: minimum 6 rounds per minute",
        "   Indigenous Content: minimum 50 percent",
        "",
        "Bidders must demonstrate prior experience in artillery gun systems.",
        "This RFP is issued under the Defence Acquisition Procedure (DAP).",
    ]
    y = h - 60
    for ln in lines:
        c.setFont("Helvetica-Bold" if ln.isupper() and ln else "Helvetica", 11)
        c.drawString(50, y, ln)
        y -= 20
    c.showPage()
    c.save()
    print("wrote", path)


def gen_caesar_jpg() -> None:
    """A 1200x800 'product' image (passes the meaningful-image filter)."""
    path = FIX / "caesar.jpg"
    img = Image.new("RGB", (1200, 800), (60, 70, 55))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 300, 1100, 520], fill=(90, 100, 80))   # crude vehicle body
    d.rectangle([300, 180, 760, 340], fill=(80, 90, 70))
    d.line([760, 240, 1140, 210], fill=(40, 45, 35), width=18)  # the barrel
    d.text((120, 60), "CAESAR 6x6 155mm/52 self-propelled howitzer (KNDS)", fill=(230, 230, 220))
    img.save(path, "JPEG", quality=85)
    print("wrote", path)


def gen_logo_png() -> None:
    """A tiny logo — must be DROPPED by the meaningful-image filter."""
    path = FIX / "logo.png"
    img = Image.new("RGB", (80, 40), (10, 30, 80))
    ImageDraw.Draw(img).text((6, 12), "KNDS", fill=(255, 255, 255))
    img.save(path, "PNG")
    print("wrote", path)


if __name__ == "__main__":
    gen_rfp_pdf()
    gen_caesar_jpg()
    gen_logo_png()
