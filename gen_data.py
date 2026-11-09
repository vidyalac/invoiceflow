"""Generates the synthetic dataset: vendor master, PO sheet, config, and sample invoice PDFs.
Run once:  python gen_data.py
Invoice dates are relative to today so the demo never looks stale.
"""
import os, json, glob, subprocess, tempfile, datetime as dt
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from PIL import Image, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SAMPLES = os.path.join(BASE, "samples")
os.makedirs(DATA, exist_ok=True)
os.makedirs(SAMPLES, exist_ok=True)

T = dt.date.today()
def d(n): return T - dt.timedelta(days=n)
def iso(x): return x.isoformat()
def dmy(x): return x.strftime("%d %b %Y")
def long(x): return x.strftime("%B %d, %Y").replace(" 0", " ")

# ---------------------------------------------------------------- master data
VENDORS = [
    {"id": "V001", "name": "Northwind Office Supplies Ltd", "email": "ar@northwind-supplies.example", "bank": "4022889103", "terms_days": 30, "approved": True,
     "address": ["12 Commerce Street", "Chicago, IL 60601"]},
    {"id": "V002", "name": "Apex Cloud Services Pvt Ltd", "email": "billing@apexcloud.example", "bank": "5530127788", "terms_days": 30, "approved": True,
     "address": ["88 Tech Park Road", "San Jose, CA 95112"]},
    {"id": "V003", "name": "BlueRiver Logistics LLC", "email": "accounts@blueriver-log.example", "bank": "7781203349", "terms_days": 45, "approved": True,
     "address": ["301 Harbor Drive", "Long Beach, CA 90802"]},
    {"id": "V004", "name": "Meridian Print & Packaging Inc", "email": "finance@meridianprint.example", "bank": "3109987215", "terms_days": 30, "approved": True,
     "address": ["77 Press Lane", "Dallas, TX 75201"]},
    {"id": "V005", "name": "Helios Facility Management", "email": "invoices@helios-fm.example", "bank": "6624410087", "terms_days": 15, "approved": True,
     "address": ["5 Sunrise Avenue", "Phoenix, AZ 85004"]},
]
POS = [
    {"po_number": "PO-1001", "vendor_id": "V001", "description": "Q4 office supplies", "total": 4320.00, "allows_partial": False, "currency": "USD"},
    {"po_number": "PO-1002", "vendor_id": "V002", "description": "Cloud platform subscription, 3 monthly instalments", "total": 12960.00, "allows_partial": True, "currency": "USD"},
    {"po_number": "PO-1003", "vendor_id": "V003", "description": "Q4 freight and handling", "total": 8300.00, "allows_partial": False, "currency": "USD"},
    {"po_number": "PO-1004", "vendor_id": "V004", "description": "Brochures and packaging inserts", "total": 2916.00, "allows_partial": False, "currency": "USD"},
    {"po_number": "PO-1005", "vendor_id": "V005", "description": "Facility maintenance, November", "total": 4860.00, "allows_partial": False, "currency": "USD"},
]
CONFIG = {
    "tol_pct": 1.0,          # invoice may exceed/undershoot PO balance by up to 1% ...
    "tol_abs": 50.0,         # ... AND by no more than $50 (both must hold)
    "line_sum_tol": 0.02,    # rounding tolerance for arithmetic checks ($)
    "dup_window_days": 7,    # same vendor + same amount within N days = near-duplicate
    "fuzzy_vendor_threshold": 0.85,  # name similarity that counts as a lookalike
    "ocr_min_conf": 0.80,    # below this on a key field, a human must verify
    "stale_days": 120,       # invoices older than this get a note
}
json.dump(VENDORS, open(os.path.join(DATA, "vendors.json"), "w"), indent=2)
json.dump(POS, open(os.path.join(DATA, "pos.json"), "w"), indent=2)
json.dump(CONFIG, open(os.path.join(DATA, "config.json"), "w"), indent=2)

# ---------------------------------------------------------------- invoice drawing
def money(x): return f"${x:,.2f}"

def make_invoice(path, vendor, address, meta, lines, totals, bank=None, terms=None, note=None, bold_total=True):
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 745, vendor)
    c.setFont("Helvetica", 9); y = 730
    for a in address:
        c.drawString(50, y, a); y -= 12
    c.setFont("Helvetica-Bold", 20); c.drawString(50, 672, "INVOICE")
    c.setFont("Helvetica", 10); y = 650
    for lab, val in meta:
        c.drawString(50, y, f"{lab} {val}"); y -= 15
    y -= 6
    c.drawString(50, y, "Bill To: Orbit Manufacturing Inc., 400 Industrial Way, Austin TX"); y -= 30
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Description"); c.drawRightString(360, y, "Qty"); c.drawRightString(450, y, "Unit Price"); c.drawRightString(560, y, "Amount")
    y -= 4; c.line(50, y, 560, y); y -= 14
    c.setFont("Helvetica", 9)
    for desc, q, u in lines:
        c.drawString(50, y, desc); c.drawRightString(360, y, f"{q}"); c.drawRightString(450, y, money(u)); c.drawRightString(560, y, money(q * u)); y -= 14
    y -= 10
    for i, (lab, val) in enumerate(totals):
        last = i == len(totals) - 1
        c.setFont("Helvetica-Bold" if (last and bold_total) else "Helvetica", 10)
        c.drawString(380, y, lab); c.drawRightString(560, y, money(val)); y -= 15
    y -= 20
    c.setFont("Helvetica", 9)
    if terms: c.drawString(50, y, f"Payment terms: {terms}"); y -= 13
    if bank: c.drawString(50, y, f"Bank Account: {bank}   Routing: 021000021"); y -= 13
    if note: c.setFont("Helvetica-Bold", 9); c.drawString(50, y, note)
    c.save()

def scanify(src, dst):
    """Turn a text PDF into an image-only PDF that looks like a scan (slight skew, blur, noise)."""
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "150", "-gray", "-png", src, os.path.join(td, "p")], check=True)
        im = Image.open(sorted(glob.glob(td + "/p*.png"))[0]).convert("L")
        im = im.rotate(0.5, resample=Image.BICUBIC, fillcolor=255)
        im = im.filter(ImageFilter.GaussianBlur(0.5))
        noise = Image.effect_noise(im.size, 12)
        im = Image.blend(im, noise, 0.06)
        im.save(dst, "PDF", resolution=150)

V = {v["id"]: v for v in VENDORS}
NW_LINES = [("Copier paper A4 (box)", 50, 40.00), ("Toner cartridge - black", 10, 120.00), ("Desk organizer", 20, 40.00)]
manifest = []

def sample(fname, title, tag, group, desc, expect):
    manifest.append({"file": fname, "title": title, "tag": tag, "group": group, "description": desc, "expected": expect})

# 01 happy path
make_invoice(os.path.join(SAMPLES, "01_happy_northwind.pdf"), V["V001"]["name"], V["V001"]["address"],
             [("Invoice No:", "INV-NW-2041"), ("Invoice Date:", iso(d(6))), ("PO Reference:", "PO-1001")],
             NW_LINES, [("Subtotal", 4000.00), ("Tax (8%)", 320.00), ("Total", 4320.00)], bank="4022889103", terms="Net 30")
sample("01_happy_northwind.pdf", "Clean invoice, exact PO match", "Happy path", "Happy path",
       "Text PDF, tax shown separately, explicit PO reference, amount matches the PO exactly.", "APPROVE")

# 02 tolerance + tax embedded + bundled line
make_invoice(os.path.join(SAMPLES, "02_tolerance_blueriver.pdf"), V["V003"]["name"], V["V003"]["address"],
             [("Invoice Number:", "INV-BR-7782"), ("Date:", long(d(4))), ("Your PO:", "PO-1003")],
             [("Q4 freight & handling services (tax included)", 1, 8326.00)], [("Total (incl. tax)", 8326.00)],
             bank="7781203349", terms="Net 45")
sample("02_tolerance_blueriver.pdf", "Bundled line, tax embedded, $26 over PO", "Happy path", "Happy path",
       "Different layout and date format. One bundled line with tax embedded. Amount is $26 (0.3%) over the PO, inside tolerance.", "APPROVE_WITH_NOTE")

# 03 implied PO
make_invoice(os.path.join(SAMPLES, "03_implied_po_helios.pdf"), V["V005"]["name"], V["V005"]["address"],
             [("Invoice #:", "INV-HF-0310"), ("Invoice Date:", dmy(d(1))), ("Reference:", "November maintenance contract")],
             [("Facility maintenance - November", 1, 4500.00)], [("Subtotal", 4500.00), ("Tax (8%)", 360.00), ("Amount Due", 4860.00)],
             bank="6624410087", terms="Net 15")
sample("03_implied_po_helios.pdf", "No PO number on the invoice", "Happy path", "Happy path",
       "The invoice never mentions a PO. The process infers it from vendor and amount and flags that it did so.", "APPROVE_WITH_NOTE")

# 04 split PO
def apex(fname, no, days_ago, lines, sub, tax, total):
    make_invoice(os.path.join(SAMPLES, fname), V["V002"]["name"], V["V002"]["address"],
                 [("Invoice No:", no), ("Invoice Date:", dmy(d(days_ago))), ("PO Reference:", "PO-1002")],
                 lines, [("Subtotal", sub), ("Tax (8%)", tax), ("Total", total)], bank="5530127788", terms="Net 30")
apex("04a_split_po_apex_month1.pdf", "INV-AC-1001", 40, [("Platform subscription - Month 1 of 3", 1, 4000.00)], 4000.00, 320.00, 4320.00)
apex("04b_split_po_apex_month2.pdf", "INV-AC-1002", 9, [("Platform subscription - Month 2 of 3", 1, 4000.00)], 4000.00, 320.00, 4320.00)
apex("04c_split_po_apex_overbill.pdf", "INV-AC-1003", 3, [("Platform subscription - Month 3 of 3", 1, 4000.00), ("Additional compute seats (unplanned)", 1, 8000.00)], 12000.00, 960.00, 12960.00)
sample("04a_split_po_apex_month1.pdf", "Split PO: month 1 of 3", "Edge case 1", "Edge 1: Split PO",
       "One PO, three monthly invoices. Approved as a partial invoice; the PO balance drops to $8,640.", "APPROVE")
sample("04b_split_po_apex_month2.pdf", "Split PO: month 2 of 3", "Edge case 1", "Edge 1: Split PO",
       "Same vendor, same amount, a month later. NOT a duplicate. Approved; PO balance drops to $4,320. Run after 04a.", "APPROVE")
sample("04c_split_po_apex_overbill.pdf", "Split PO: invoice exceeds remaining balance", "Edge case 1", "Edge 1: Split PO",
       "Third invoice includes $8,000 of unplanned extras and would push total billing past the PO. Held. Run after 04a and 04b.", "HOLD")

# 05 near duplicate
make_invoice(os.path.join(SAMPLES, "05_near_duplicate_northwind.pdf"), V["V001"]["name"], V["V001"]["address"],
             [("Invoice No:", "INV-NW-2041A"), ("Invoice Date:", iso(d(5))), ("PO Reference:", "PO-1001")],
             NW_LINES, [("Subtotal", 4000.00), ("Tax (8%)", 320.00), ("Total", 4320.00)], bank="4022889103", terms="Net 30")
sample("05_near_duplicate_northwind.pdf", "Resubmitted with a changed invoice number", "Edge case 2", "Edge 2: Near-duplicate",
       "Vendor resends sample 01 as 'INV-NW-2041A' with a date one day later. The invoice number differs, so an exact check misses it. Run after 01.", "HOLD")

# 06 scanned, missing fields
tmp = os.path.join(SAMPLES, "_tmp_scan.pdf")
make_invoice(tmp, V["V004"]["name"], V["V004"]["address"],
             [("PO Reference:", "PO-1004")],
             [("Brochure printing - 5000 copies", 1, 1800.00), ("Packaging inserts", 1, 900.00)],
             [("Subtotal", 2700.00), ("Tax (8%)", 216.00), ("Total", 2916.00)], bank="3109987215", terms="Net 30")
scanify(tmp, os.path.join(SAMPLES, "06_scanned_missing_fields_meridian.pdf")); os.remove(tmp)
sample("06_scanned_missing_fields_meridian.pdf", "Scanned image, no invoice number or date", "Edge case 3", "Edge 3: Scanned + missing data",
       "Image-only scan that needs OCR. The vendor left off the invoice number and date. The process holds it and drafts a specific question to the vendor instead of guessing.", "HOLD")

# 07 lookalike vendor
make_invoice(os.path.join(SAMPLES, "07_lookalike_vendor.pdf"), "Northwlnd Office Supplies Ltd", V["V001"]["address"],
             [("Invoice No:", "INV-NW-2090"), ("Invoice Date:", iso(d(2))), ("PO Reference:", "PO-1001")],
             NW_LINES, [("Subtotal", 4000.00), ("Tax (8%)", 320.00), ("Total", 4320.00)], bank="9981204477", terms="Net 30",
             note="NOTE: Our banking details have changed. Please remit to the account above.")
sample("07_lookalike_vendor.pdf", "Lookalike vendor name + new bank account", "Edge case 4", "Edge 4: Possible fraud",
       "'Northwlnd' (l instead of i), a different bank account, and a 'we changed banks' note. Classic business-email-compromise pattern. Escalated, payment blocked.", "ESCALATE")

# 08 bank change, exact vendor
make_invoice(os.path.join(SAMPLES, "08_bank_details_changed_meridian.pdf"), V["V004"]["name"], V["V004"]["address"],
             [("Invoice No:", "INV-MP-5512"), ("Invoice Date:", iso(d(2))), ("PO Reference:", "PO-1004")],
             [("Brochure printing - 5000 copies", 1, 1800.00), ("Packaging inserts", 1, 900.00)],
             [("Subtotal", 2700.00), ("Tax (8%)", 216.00), ("Total", 2916.00)], bank="5500123987", terms="Net 30",
             note="NOTE: We have moved banks. Please use the account above for all future payments.")
sample("08_bank_details_changed_meridian.pdf", "Correct vendor, bank account changed", "Edge case 4", "Edge 4: Possible fraud",
       "The vendor name is exactly right but the bank account differs from the vendor master. Escalated for a call-back verification.", "ESCALATE")

# 09 unknown vendor
make_invoice(os.path.join(SAMPLES, "09_unknown_vendor.pdf"), "Zenith Marketing Group LLC", ["900 Ad Avenue", "New York, NY 10001"],
             [("Invoice No:", "ZMG-2207"), ("Invoice Date:", iso(d(3))), ("PO Reference:", "PO-1004")],
             [("Brand campaign retainer", 1, 2500.00)], [("Total", 2500.00)], bank="1100223344", terms="Net 30")
sample("09_unknown_vendor.pdf", "Vendor not in the approved list", "Edge case", "Other",
       "An invoice from a company that is not an approved vendor. Rejected outright.", "REJECT")

json.dump(manifest, open(os.path.join(SAMPLES, "manifest.json"), "w"), indent=2)
print("generated", len(manifest), "samples")
