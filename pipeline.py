"""The invoice-to-decision pipeline.

Design principle: AI/OCR is only used to READ the document. The DECISION is made by deterministic,
auditable rules, and every rule that fires leaves a reason code. Same invoice + same state = same decision.
"""
import os, re, json, glob, time, uuid, hashlib, tempfile, subprocess, datetime as dt
from difflib import SequenceMatcher
import pdfplumber, pytesseract
from PIL import Image
import store

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CFG = json.load(open(os.path.join(DATA, "config.json")))
VENDORS = json.load(open(os.path.join(DATA, "vendors.json")))
POS = json.load(open(os.path.join(DATA, "pos.json")))

SEV = {"INFO": 0, "NOTE": 1, "HOLD": 2, "REJECT": 3, "ESCALATE": 4}
DECISION = {0: "APPROVE", 1: "APPROVE_WITH_NOTE", 2: "HOLD", 3: "REJECT", 4: "ESCALATE"}
RUNS = {}  # in-memory state of runs in progress (polled by the UI)


class Skip(Exception):
    pass


# ------------------------------------------------------------------ helpers
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for ch in iter(lambda: f.read(65536), b""):
            h.update(ch)
    return h.hexdigest()


def norm_name(s):
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    stop = {"ltd", "limited", "pvt", "inc", "llc", "co", "corp", "corporation", "the", "and"}
    return " ".join(t for t in s.split() if t not in stop)


def norm_inv(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def digits(s):
    return re.sub(r"\D", "", s or "")


def ratio(a, b):
    return SequenceMatcher(None, a, b).ratio()


MONEY = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?!\d)")


def last_money(s):
    m = list(MONEY.finditer(s))
    return float(m[-1].group(1).replace(",", "") + "." + m[-1].group(2)) if m else None


def parse_date(s):
    s = s.strip().rstrip(".,")
    for f in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def within_tol(diff, base):
    return abs(diff) <= CFG["tol_abs"] and abs(diff) <= base * CFG["tol_pct"] / 100.0


def po_state():
    b = store.billed_map()
    out = []
    for p in POS:
        billed = round(b.get(p["po_number"], 0.0), 2)
        out.append({**p, "billed": billed, "remaining": round(p["total"] - billed, 2)})
    return out


def add(ctx, ck, label, state, text, code=None, sev=None):
    """Record a check. If it carries a reason code it becomes a finding that feeds the rules engine."""
    ck.append({"label": label, "state": state, "text": text, "code": code, "sev": sev})
    if code:
        ctx["findings"].append({"code": code, "sev": sev, "msg": text, "stage": ctx["stage"]})


# ------------------------------------------------------------------ reading the document
def rasterize(path):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", path, os.path.join(td, "p")], check=True)
        return [Image.open(f).copy() for f in sorted(glob.glob(td + "/p*.png"))]


def ocr_lines(path):
    ext = os.path.splitext(path)[1].lower()
    imgs = rasterize(path) if ext == ".pdf" else [Image.open(path)]
    lines, confs = [], []
    for img in imgs:
        d = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
        groups, order = {}, []
        for i, w in enumerate(d["text"]):
            if not w.strip() or float(d["conf"][i]) < 0:
                continue
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append((w, float(d["conf"][i])))
            confs.append(float(d["conf"][i]))
        for k in order:
            ws = groups[k]
            lines.append((" ".join(w for w, _ in ws), sum(c for _, c in ws) / len(ws) / 100.0))
    return lines, {"mean_conf": (sum(confs) / len(confs) / 100.0 if confs else 0), "words": len(confs)}


ITEM = re.compile(r"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+[^\d\s]?(?P<unit>[\d,]+\.\d{2})\s+[^\d\s]?(?P<amt>[\d,]+\.\d{2})$")


def extract_fields(lines):
    F, items = {}, []

    def put(k, v, conf, src):
        if k not in F and v not in (None, ""):
            F[k] = {"value": v, "conf": round(conf, 2), "source": src}

    if lines:
        put("vendor_name", re.sub(r"(?i)^invoice from[:\s]*", "", lines[0][0]).strip(), lines[0][1], lines[0][0])
    for t, conf in lines:
        m = re.match(r"(?i)^invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/\.]{2,})\s*$", t)
        if m: put("invoice_no", m.group(1), conf, t)
        m = re.match(r"(?i)^(?:invoice\s+)?date\s*[:\-]?\s*(.+)$", t)
        if m and not t.lower().startswith("due"):
            put("invoice_date", parse_date(m.group(1)), conf, t)
        m = re.search(r"(?i)\bPO[\s\-#:]*(\d{3,6})\b", t) or re.search(r"(?i)purchase\s*order\s*(?:no\.?|number|#|ref)?\s*[:\-]?\s*(?:PO[\s\-]?)?(\d{3,6})", t)
        if m: put("po_ref", f"PO-{m.group(1)}", conf, t)
        m = re.search(r"(?i)bank\s*(?:account|a/c|acct)\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d[\d\-\s]{5,}\d)", t)
        if m: put("bank", digits(m.group(1))[:12], conf, t)
        if re.match(r"(?i)^sub\s*-?\s*total\b", t): put("subtotal", last_money(t), conf, t)
        elif re.match(r"(?i)^(?:sales\s+tax|tax|vat|gst)\b", t): put("tax", last_money(t), conf, t)
        elif re.match(r"(?i)^(?:grand\s+)?total\b|^(?:amount|balance)\s+due\b", t): put("total", last_money(t), conf, t)
        m = ITEM.match(t)
        if m:
            items.append({"description": m.group("desc"), "qty": float(m.group("qty")),
                          "unit": float(m.group("unit").replace(",", "")), "amount": float(m.group("amt").replace(",", "")), "conf": round(conf, 2)})
    return F, items



# ------------------------------------------------------------------ optional LLM extraction
LLM_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
LLM_SYSTEM = (
    "You extract data from vendor invoices. Return ONLY a JSON object, no prose. "
    "Copy every text value EXACTLY as printed on the invoice: never correct spelling, never normalise a name, never infer a value that is not printed. "
    "If a field is not on the invoice, use null. Schema: "
    '{"vendor_name":str|null,"invoice_no":str|null,"invoice_date":"YYYY-MM-DD"|null,"po_ref":str|null,"subtotal":number|null,"tax":number|null,"total":number|null,'
    '"bank_account":str|null,"line_items":[{"description":str,"qty":number,"unit_price":number,"amount":number}],'
    '"confidence":{"vendor_name":0-1,"invoice_no":0-1,"invoice_date":0-1,"po_ref":0-1,"total":0-1,"bank_account":0-1}}. '
    "vendor_name is the company issuing the invoice (the seller), not the bill-to party. total is the final amount due."
)


def grounded(key, val, raw):
    """Check an LLM-returned value actually appears in the source text. Guards against hallucination and silent 'autocorrect'."""
    low = raw.lower()
    if key in ("subtotal", "tax", "total"):
        return f"{val:,.2f}" in raw or f"{val:.2f}" in raw
    if key == "invoice_date":
        return str(val)[:4] in raw
    if key in ("bank", "po_ref"):
        return digits(val) != "" and digits(val) in digits(raw)
    return str(val).lower() in low


def llm_extract(raw_text):
    import requests
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": LLM_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": LLM_MODEL, "max_tokens": 1500, "system": LLM_SYSTEM,
                            "messages": [{"role": "user", "content": "Invoice text:\n\n" + raw_text[:12000]}]}, timeout=45)
    r.raise_for_status()
    txt = r.json()["content"][0]["text"]
    d = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    conf = d.get("confidence") or {}
    F = {}

    def put(k, v, mk):
        if v in (None, ""):
            return
        try:
            if k in ("subtotal", "tax", "total"): v = float(v)
            elif k == "invoice_date": v = parse_date(str(v)) or str(v)
            elif k == "po_ref":
                m = re.search(r"(\d{3,6})", str(v)); v = f"PO-{m.group(1)}" if m else None
            elif k == "bank": v = digits(v)[:12]
            else: v = str(v).strip()
        except Exception:
            return
        if v in (None, ""):
            return
        c = min(float(conf.get(mk, 0.9) or 0.9), 0.99)
        F[k] = {"value": v, "conf": round(c if grounded(k, v, raw_text) else 0.4, 2), "source": "LLM" + ("" if grounded(k, v, raw_text) else " (NOT found verbatim in source)")}

    for k, mk in [("vendor_name", "vendor_name"), ("invoice_no", "invoice_no"), ("invoice_date", "invoice_date"), ("po_ref", "po_ref"),
                  ("subtotal", "subtotal"), ("tax", "tax"), ("total", "total"), ("bank", "bank_account")]:
        put(k, d.get("bank_account" if k == "bank" else k), mk)
    items = []
    for it in d.get("line_items") or []:
        try:
            items.append({"description": str(it["description"]), "qty": float(it.get("qty", 1)), "unit": float(it.get("unit_price", it["amount"])),
                          "amount": float(it["amount"]), "conf": 0.95})
        except Exception:
            pass
    return F, items


# ------------------------------------------------------------------ stages
def stage_ingest(ctx):
    p, ck = ctx["path"], []
    ext = os.path.splitext(p)[1].lower()
    ctx["sha"] = sha256(p)
    if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
        add(ctx, ck, "File type", "fail", f"Unsupported file type '{ext}'. Send a PDF or image.", "UNSUPPORTED_FILE", "HOLD")
        ctx["halt"] = True
        return ck, "Unsupported file type"
    pages, mode = 1, "scanned"
    if ext == ".pdf":
        try:
            with pdfplumber.open(p) as pdf:
                pages = len(pdf.pages)
                ctx["text_pages"] = [(pg.extract_text() or "") for pg in pdf.pages]
        except Exception as e:
            add(ctx, ck, "Open file", "fail", f"File could not be opened as a PDF ({e}).", "UNREADABLE_FILE", "HOLD")
            ctx["halt"] = True
            return ck, "Unreadable file"
        mode = "text" if sum(len(t.strip()) for t in ctx["text_pages"]) >= 40 else "scanned"
    ctx["mode"], ctx["pages"] = mode, pages
    add(ctx, ck, "File", "pass", f"{ext[1:].upper()} · {pages} page(s) · {os.path.getsize(p)/1024:.0f} KB · fingerprint {ctx['sha'][:10]}")
    add(ctx, ck, "Document type", "info", "Machine-readable text layer found. Reading it directly." if mode == "text"
        else "No text layer: this is an image-only scan. Using OCR, and tracking confidence per field.")
    return ck, ("Text PDF" if mode == "text" else "Scanned image (OCR needed)") + f", {pages} page(s)"


FIELD_LABELS = [("vendor_name", "Vendor", True), ("invoice_no", "Invoice number", True), ("invoice_date", "Invoice date", True),
                ("po_ref", "PO reference", False), ("subtotal", "Subtotal", False), ("tax", "Tax", False),
                ("total", "Total", True), ("bank", "Bank account", False)]


def stage_extract(ctx):
    ck = []
    if ctx["mode"] == "text":
        lines = [(l.strip(), 0.99) for t in ctx["text_pages"] for l in t.splitlines() if l.strip()]
        ocr = None
    else:
        lines, ocr = ocr_lines(ctx["path"])
    raw_text = "\n".join(t for t, _ in lines)
    F, items = extract_fields(lines)
    ctx["extractor"] = "rules"
    if LLM_KEY:
        try:
            F, items = llm_extract(raw_text)
            ctx["extractor"] = "llm"
            add(ctx, ck, "Extractor", "info", f"Claude ({LLM_MODEL}) read the invoice. Every value is verified against the source text; anything not found verbatim gets low confidence and is held.")
        except Exception as e:
            add(ctx, ck, "Extractor", "info", f"LLM extraction unavailable ({str(e)[:80]}). Fell back to the rule-based extractor.")
    else:
        add(ctx, ck, "Extractor", "info", "Rule-based extractor (no LLM key configured).")
    ctx["F"], ctx["items"] = F, items
    ctx["view"]["extractor"] = ctx["extractor"]
    ctx["view"]["fields"], ctx["view"]["items"] = F, items
    ctx["view"]["ocr"] = ocr
    for k, lab, req in FIELD_LABELS:
        if k in F:
            v = F[k]["value"]
            txt = money(v) if k in ("subtotal", "tax", "total") else str(v)
            add(ctx, ck, lab, "pass", f"{txt}  (confidence {F[k]['conf']:.0%})")
        else:
            add(ctx, ck, lab, "warn" if req else "info", "Not found on the invoice")
    add(ctx, ck, "Line items", "pass" if items else "warn", f"{len(items)} line item(s) parsed")
    s = f"Pulled {len(F)} fields and {len(items)} line item(s)"
    if ocr:
        s += f" · OCR mean confidence {ocr['mean_conf']:.0%}"
    return ck, s


def money(x):
    return f"${x:,.2f}"


REQUIRED = [("vendor_name", "vendor name"), ("invoice_no", "invoice number"), ("invoice_date", "invoice date"), ("total", "total amount")]
KEY_CONF = ["vendor_name", "invoice_no", "invoice_date", "total", "po_ref", "bank"]


def stage_validate(ctx):
    F = {k: v["value"] for k, v in ctx["F"].items()}
    ctx["Fv"] = F
    items, ck, tol = ctx["items"], [], CFG["line_sum_tol"]
    missing = [lab for k, lab in REQUIRED if k not in F]
    ctx["missing"] = missing
    if missing:
        add(ctx, ck, "Required fields", "warn", "Missing: " + ", ".join(missing) + ". The process will not guess these.", "MISSING_FIELDS", "HOLD")
    else:
        add(ctx, ck, "Required fields", "pass", "Vendor, invoice number, date and total are all present")
    s, sub, tax, tot = sum(i["amount"] for i in items), F.get("subtotal"), F.get("tax"), F.get("total")
    if items:
        target, lab = (sub, "subtotal") if sub is not None else (tot, "total (tax embedded in lines)")
        if target is None:
            add(ctx, ck, "Line items vs total", "info", "No total to compare the line items against")
        elif abs(s - target) <= tol:
            add(ctx, ck, "Line items vs total", "pass", f"Line items add up to {money(s)}, which matches the {lab}")
        else:
            add(ctx, ck, "Line items vs total", "warn", f"Line items add up to {money(s)} but the {lab} says {money(target)}", "ARITHMETIC_MISMATCH", "HOLD")
    else:
        add(ctx, ck, "Line items vs total", "info", "No line items could be read, so the line-level arithmetic check was skipped")
    if sub is not None and tot is not None:
        exp = sub + (tax or 0)
        if abs(exp - tot) <= tol:
            add(ctx, ck, "Subtotal + tax = total", "pass", f"{money(sub)} + {money(tax or 0)} = {money(tot)}" + (f" (tax rate {tax/sub:.1%})" if tax and sub else ""))
        else:
            add(ctx, ck, "Subtotal + tax = total", "warn", f"{money(sub)} + {money(tax or 0)} = {money(exp)}, but the invoice total says {money(tot)}", "ARITHMETIC_MISMATCH", "HOLD")
    elif sub is None and tax is None and tot is not None:
        add(ctx, ck, "Tax treatment", "info", "No separate tax line: tax is treated as embedded in the line amounts")
    d = F.get("invoice_date")
    if d:
        age = (dt.date.today() - dt.date.fromisoformat(d)).days
        if age < 0:
            add(ctx, ck, "Invoice date", "warn", f"Invoice is dated {-age} day(s) in the future", "FUTURE_DATE", "HOLD")
        elif age > CFG["stale_days"]:
            add(ctx, ck, "Invoice date", "warn", f"Invoice is {age} days old (policy: {CFG['stale_days']})", "STALE_INVOICE", "NOTE")
        else:
            add(ctx, ck, "Invoice date", "pass", f"Dated {d}, {age} day(s) ago")
    if ctx["mode"] == "scanned" or ctx.get("extractor") == "llm":
        low = [(k, ctx["F"][k]["conf"]) for k in KEY_CONF if k in ctx["F"] and ctx["F"][k]["conf"] < CFG["ocr_min_conf"]]
        if low:
            add(ctx, ck, "Extraction confidence", "warn", "Low extraction confidence on: " + ", ".join(f"{k} ({c:.0%})" for k, c in low) + ". A person must verify.", "LOW_CONFIDENCE", "HOLD")
        else:
            add(ctx, ck, "Extraction confidence", "pass", f"Every key field read with ≥{CFG['ocr_min_conf']:.0%} confidence")
    n = sum(1 for c in ck if c["state"] == "warn")
    return ck, "All checks passed" if n == 0 else f"{n} issue(s) found"


def stage_vendor(ctx):
    F, ck = ctx["Fv"], []
    ctx["vendor"] = None
    name = F.get("vendor_name")
    if not name:
        add(ctx, ck, "Vendor name", "warn", "No vendor name could be read from the invoice.", "VENDOR_MISSING", "HOLD")
        return ck, "No vendor name"
    n = norm_name(name)
    exact = [v for v in VENDORS if norm_name(v["name"]) == n]
    if not exact:
        best = max(VENDORS, key=lambda v: ratio(n, norm_name(v["name"])))
        r = ratio(n, norm_name(best["name"]))
        if r >= CFG["fuzzy_vendor_threshold"]:
            extra = ""
            if F.get("bank") and digits(F["bank"]) != best["bank"]:
                extra = " The bank account on the invoice also differs from the one on file."
            add(ctx, ck, "Approved vendor list", "fail",
                f"'{name}' is not an approved vendor but is {r:.0%} similar to '{best['name']}'. Possible impersonation.{extra}", "LOOKALIKE_VENDOR", "ESCALATE")
            ctx["halt"] = True
            return ck, f"Lookalike of {best['name']}: halted"
        add(ctx, ck, "Approved vendor list", "fail", f"'{name}' is not on the approved vendor list.", "UNKNOWN_VENDOR", "REJECT")
        ctx["halt"] = True
        return ck, "Unknown vendor: halted"
    v = exact[0]
    ctx["vendor"] = v
    ctx["view"]["vendor"] = {"id": v["id"], "name": v["name"]}
    if not v["approved"]:
        add(ctx, ck, "Approved vendor list", "fail", f"{v['name']} is on file but not approved.", "VENDOR_NOT_APPROVED", "REJECT")
        ctx["halt"] = True
        return ck, "Vendor not approved: halted"
    add(ctx, ck, "Approved vendor list", "pass", f"Matched {v['name']} ({v['id']}), approved, payment terms Net {v['terms_days']}")
    inv_bank = digits(F.get("bank"))
    if inv_bank and inv_bank != v["bank"]:
        add(ctx, ck, "Bank details", "fail", f"Bank account on the invoice (…{inv_bank[-4:]}) does not match the account on file (…{v['bank'][-4:]}). Changed bank details are the #1 payment-fraud signal.", "BANK_CHANGED", "ESCALATE")
        ctx["halt"] = True
        return ck, "Bank details changed: halted"
    if inv_bank:
        add(ctx, ck, "Bank details", "pass", f"Account …{inv_bank[-4:]} matches the vendor master")
    else:
        add(ctx, ck, "Bank details", "info", "No bank details on the invoice; payment will use the account on file")
    return ck, f"Verified: {v['name']}"


def stage_po(ctx):
    v, F, ck = ctx["vendor"], ctx["Fv"], []
    if v is None:
        raise Skip("Vendor not verified, so PO matching was skipped")
    total = F.get("total")
    if total is None:
        raise Skip("No invoice total, so it cannot be compared to a PO")
    pos = {p["po_number"]: p for p in po_state()}
    ref, po, inferred = F.get("po_ref"), None, False
    if ref:
        po = pos.get(ref)
        if not po:
            add(ctx, ck, "PO lookup", "warn", f"{ref} is referenced on the invoice but does not exist in the PO register.", "PO_NOT_FOUND", "HOLD")
            return ck, f"{ref} not found"
        if po["vendor_id"] != v["id"]:
            add(ctx, ck, "PO lookup", "warn", f"{ref} belongs to a different vendor than the one on the invoice.", "PO_VENDOR_MISMATCH", "HOLD")
            return ck, "PO belongs to another vendor"
        add(ctx, ck, "PO lookup", "pass", f"Explicit reference {ref} found: {po['description']}")
    else:
        cands = [p for p in pos.values() if p["vendor_id"] == v["id"] and p["remaining"] > 0.01]
        scored = []
        for p in cands:
            if within_tol(total - p["remaining"], p["remaining"]):
                scored.append((1.0, p))
            elif total < p["remaining"] and p["allows_partial"]:
                scored.append((0.6, p))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            add(ctx, ck, "PO lookup", "warn", "No PO number on the invoice and no open PO for this vendor fits the amount.", "PO_NOT_IDENTIFIED", "HOLD")
            return ck, "Could not identify a PO"
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            add(ctx, ck, "PO lookup", "warn", "No PO number on the invoice and more than one open PO fits. Not guessing.", "PO_AMBIGUOUS", "HOLD")
            return ck, "Ambiguous PO"
        po, inferred = scored[0][1], True
        add(ctx, ck, "PO lookup", "warn", f"No PO number on the invoice. Inferred {po['po_number']} because it is the only open PO for {v['name']} that fits the amount.", "PO_INFERRED", "NOTE")
    ctx["po"] = po
    rem, over = po["remaining"], round(total - po["remaining"], 2)
    add(ctx, ck, "PO balance", "info", f"PO total {money(po['total'])}, already billed {money(po['billed'])}, remaining {money(rem)}")
    after = rem - total
    if abs(over) <= 0.01:
        add(ctx, ck, "Amount vs PO", "pass", f"Invoice total {money(total)} equals the remaining PO balance")
    elif over > 0:
        if within_tol(over, rem):
            add(ctx, ck, "Amount vs PO", "warn", f"Invoice is {money(over)} ({over/rem:.1%}) over the remaining PO balance, inside tolerance (≤{CFG['tol_pct']}% and ≤{money(CFG['tol_abs'])}).", "AMOUNT_WITHIN_TOLERANCE", "NOTE")
        else:
            add(ctx, ck, "Amount vs PO", "warn", f"Invoice total {money(total)} exceeds the remaining PO balance of {money(rem)} by {money(over)}.", "OVER_PO_BALANCE", "HOLD")
    else:
        if po["allows_partial"]:
            add(ctx, ck, "Amount vs PO", "pass", f"Partial invoice: {money(total)} of the remaining {money(rem)}. {money(after)} will remain on the PO.")
        elif within_tol(over, rem):
            add(ctx, ck, "Amount vs PO", "warn", f"Invoice is {money(-over)} under the PO balance, inside tolerance.", "AMOUNT_WITHIN_TOLERANCE", "NOTE")
        else:
            add(ctx, ck, "Amount vs PO", "warn", f"Invoice is {money(-over)} below the PO balance and this PO does not allow partial billing. Confirm partial delivery or a credit note.", "UNDER_PO_AMOUNT", "HOLD")
    add(ctx, ck, "Match type", "info", "2-way match only (PO vs invoice). There is no goods-receipt data in this dataset, so a 3-way match is not possible.")
    ctx["view"]["po"] = {"po_number": po["po_number"], "description": po["description"], "total": po["total"], "billed": po["billed"],
                         "remaining": rem, "this_invoice": total, "remaining_after": round(after, 2), "inferred": inferred}
    return ck, f"{po['po_number']}{' (inferred)' if inferred else ''} · remaining {money(rem)}"


def stage_dup(ctx):
    v, F, ck = ctx["vendor"], ctx["Fv"], []
    if v is None:
        raise Skip("Vendor not verified, so duplicate check was skipped")
    prior = store.prior_runs(v["id"])
    same_file = [r for r in prior if r["sha"] == ctx["sha"]]
    inv = norm_inv(F.get("invoice_no"))
    same_no = [r for r in prior if inv and norm_inv(r["invoice_no"]) == inv]
    near = []
    tot = F.get("total")
    for r in prior:
        if tot is None or r["total"] is None or abs(r["total"] - tot) > 0.01:
            continue
        if F.get("invoice_date") and r["invoice_date"]:
            gap = abs((dt.date.fromisoformat(F["invoice_date"]) - dt.date.fromisoformat(r["invoice_date"])).days)
            if gap <= CFG["dup_window_days"]:
                near.append((r, f"same amount, dated {gap} day(s) apart"))
        elif F.get("po_ref") and F["po_ref"] == r["po_number"]:
            near.append((r, "same amount and same PO, and a date is missing so timing can't be compared"))
    if same_file:
        r = same_file[0]
        add(ctx, ck, "Exact duplicate", "fail", f"This exact file was already processed ({r['filename']}).", "DUPLICATE_FILE", "REJECT")
    elif same_no:
        r = same_no[0]
        add(ctx, ck, "Exact duplicate", "fail", f"Invoice number {F['invoice_no']} from {v['name']} was already processed.", "DUPLICATE_INVOICE", "REJECT")
    else:
        add(ctx, ck, "Exact duplicate", "pass", "No earlier invoice with the same number or file for this vendor")
        if near:
            r, why = near[0]
            add(ctx, ck, "Near-duplicate", "warn", f"Looks like a resubmission of {r['invoice_no'] or r['filename']}: {why}, but the invoice number is different.", "NEAR_DUPLICATE", "HOLD")
        else:
            add(ctx, ck, "Near-duplicate", "pass", f"No same-amount invoice from this vendor within {CFG['dup_window_days']} days. Recurring invoices a month apart are not treated as duplicates.")
    n = sum(1 for c in ck if c["state"] in ("warn", "fail"))
    return ck, "No duplicates found" if n == 0 else "Possible duplicate"


def stage_rules(ctx):
    ck = []
    fs = ctx["findings"]
    mx = max([SEV[f["sev"]] for f in fs], default=0)
    ctx["decision"] = DECISION[mx]
    if not fs:
        add(ctx, ck, "Rules fired", "pass", "No rule fired. Every check passed.")
    for f in fs:
        st = "fail" if SEV[f["sev"]] >= 3 else "warn"
        add(ctx, ck, f["code"], st, f"[{f['sev']}] {f['msg']}")
    add(ctx, ck, "Decision logic", "info", "Highest severity wins: ESCALATE > REJECT > HOLD > NOTE > none. The decision is rule-based and repeatable; no AI is involved here.")
    return ck, f"{len(fs)} rule(s) fired → {ctx['decision']}"


HEAD = {"APPROVE": "Approved for payment", "APPROVE_WITH_NOTE": "Approved, with notes for the AP team",
        "HOLD": "Held for human review", "REJECT": "Rejected", "ESCALATE": "Escalated: do not pay"}


def build_draft(ctx):
    F, v, codes = ctx["Fv"], ctx["vendor"], {f["code"] for f in ctx["findings"]}
    ref = F.get("invoice_no") or "(no invoice number)"
    vname = F.get("vendor_name") or "vendor"
    if "MISSING_FIELDS" in codes:
        miss = ctx["missing"]
        return {"to": (v["email"] if v else "vendor"), "subject": f"Action needed: your invoice to Orbit Manufacturing is missing {', '.join(miss)}",
                "body": f"Hello {vname} team,\n\nWe received your invoice (PO {F.get('po_ref', 'not stated')}, total {money(F['total']) if 'total' in F else 'unclear'}) but could not process it because it is missing: {', '.join(miss)}.\n\nPlease reply with a corrected invoice or confirm those details so we can release payment.\n\nThank you,\nAccounts Payable, Orbit Manufacturing"}
    if codes & {"LOOKALIKE_VENDOR", "BANK_CHANGED"}:
        return {"to": "Treasury / Security", "subject": f"Possible payment fraud: invoice {ref} from '{vname}'",
                "body": f"Do not pay invoice {ref}.\n\nThe automated check flagged it for {', '.join(sorted(codes & {'LOOKALIKE_VENDOR','BANK_CHANGED'}))}. Please verify by calling the vendor on the phone number held in the vendor master (not any number on the invoice) before any change of bank details is accepted."}
    if codes & {"NEAR_DUPLICATE", "DUPLICATE_INVOICE", "DUPLICATE_FILE"}:
        return {"to": "AP team", "subject": f"Possible duplicate invoice {ref} from {vname}",
                "body": f"Invoice {ref} for {money(F['total']) if 'total' in F else 'unknown amount'} resembles an invoice already processed. Please confirm with the vendor whether this is a resubmission before paying."}
    if codes & {"OVER_PO_BALANCE", "UNDER_PO_AMOUNT", "PO_NOT_FOUND", "PO_VENDOR_MISMATCH", "PO_NOT_IDENTIFIED", "PO_AMBIGUOUS"}:
        return {"to": "Procurement / PO owner", "subject": f"PO query on invoice {ref} from {vname}",
                "body": "The invoice does not reconcile with its purchase order:\n\n" + "\n".join("- " + f["msg"] for f in ctx["findings"] if f["sev"] in ("HOLD", "REJECT")) + "\n\nPlease confirm whether to amend the PO, request a credit note, or dispute the invoice."}
    return None


def stage_explain(ctx):
    ck, F, dec = [], ctx["Fv"], ctx["decision"]
    fs = ctx["findings"]
    reasons = [f for f in fs if f["sev"] != "INFO"]
    inv = F.get("invoice_no") or "(number missing)"
    lead = f"Invoice {inv} from {F.get('vendor_name', 'unknown vendor')}" + (f" for {money(F['total'])}" if "total" in F else "")
    po = ctx.get("view", {}).get("po")
    if dec in ("APPROVE", "APPROVE_WITH_NOTE"):
        due = ""
        if F.get("invoice_date") and ctx.get("vendor"):
            due = (dt.date.fromisoformat(F["invoice_date"]) + dt.timedelta(days=ctx["vendor"]["terms_days"])).isoformat()
        nxt = f"Release for payment on {due} (Net {ctx['vendor']['terms_days']})." if due else "Release for payment per vendor terms."
        base = f"{lead} matches {po['po_number']}. {money(po['remaining_after'])} will remain on the PO." if po else lead
    elif dec == "HOLD":
        base, nxt = f"{lead} was not approved automatically because something needs a person to look at it.", "Resolve the points below, then resubmit. Nothing is paid until then."
    elif dec == "REJECT":
        base, nxt = f"{lead} was rejected.", "Notify the sender and do not pay."
    else:
        base, nxt = f"{lead} shows signs of payment fraud.", "Do not pay. Contact the vendor using the phone number on file, not the one on the invoice."
    ctx["result"] = {"decision": dec, "headline": HEAD[dec], "summary": base, "reasons": [{"code": f["code"], "sev": f["sev"], "msg": f["msg"]} for f in reasons],
                     "next_action": nxt, "draft": build_draft(ctx), "findings": fs}
    add(ctx, ck, "Explanation", "pass", "Plain-English rationale written from the reason codes")
    d = ctx["result"]["draft"]
    if d:
        add(ctx, ck, "Draft message", "pass", f"Drafted a message to {d['to']}")
    add(ctx, ck, "Audit log", "pass", "Decision, evidence and reason codes saved for the dashboard")
    return ck, f"{HEAD[dec]}"


STAGES = [("ingest", "Ingest", stage_ingest), ("extract", "Extract", stage_extract), ("validate", "Validate", stage_validate),
          ("vendor", "Vendor check", stage_vendor), ("po", "PO match", stage_po), ("dup", "Duplicate check", stage_dup),
          ("rules", "Rules engine", stage_rules), ("explain", "Explain & log", stage_explain)]
ALWAYS = {"rules", "explain"}


def new_run(filename, path):
    rid = uuid.uuid4().hex[:10]
    RUNS[rid] = {"id": rid, "filename": filename, "file_path": path, "status": "running",
                 "created_at": dt.datetime.utcnow().isoformat() + "Z",
                 "stages": [{"key": k, "name": n, "status": "pending", "summary": "", "checks": [], "ms": None} for k, n, _ in STAGES],
                 "view": {}, "result": None}
    return rid


def execute(rid, delay=0.7):
    run = RUNS[rid]
    ctx = {"path": run["file_path"], "findings": [], "halt": False, "view": run["view"], "stage": "",
           "Fv": {}, "vendor": None, "missing": [], "po": None, "decision": "HOLD"}
    t_all = time.time()
    for i, (key, name, fn) in enumerate(STAGES):
        st = run["stages"][i]
        if ctx["halt"] and key not in ALWAYS:
            st.update(status="skipped", summary="Skipped: an earlier stage halted processing")
            continue
        st["status"] = "running"
        time.sleep(delay)
        t0, ctx["stage"] = time.time(), key
        try:
            ck, summary = fn(ctx)
            st["status"] = "fail" if any(c["state"] == "fail" for c in ck) else ("warn" if any(c["state"] == "warn" for c in ck) else "ok")
            st["checks"], st["summary"] = ck, summary
        except Skip as s:
            st.update(status="skipped", summary=str(s))
        except Exception as e:  # never crash silently: turn it into a hold with a reason
            ctx["findings"].append({"code": "PROCESSING_ERROR", "sev": "HOLD", "msg": f"Unexpected error in {name}: {e}", "stage": key})
            ctx["halt"] = True
            st.update(status="fail", summary=f"Error: {e}", checks=[])
        st["ms"] = int((time.time() - t0) * 1000)
    if "result" not in ctx:  # explain stage failed before producing a result
        ctx["decision"] = "HOLD"
        ctx["result"] = {"decision": "HOLD", "headline": HEAD["HOLD"], "summary": "Processing error", "reasons": [], "next_action": "Retry or review manually.", "draft": None, "findings": ctx["findings"]}
    run["result"] = ctx["result"]
    run["sha"], run["mode"] = ctx.get("sha"), ctx.get("mode")
    run["vendor_id"] = ctx["vendor"]["id"] if ctx.get("vendor") else None
    run["duration_ms"] = int((time.time() - t_all) * 1000)
    if ctx["decision"] in store.APPROVED and ctx.get("po") and ctx["Fv"].get("total") is not None:
        store.add_billed(ctx["po"]["po_number"], ctx["Fv"]["total"])
    run["status"] = "done"
    store.insert_run(run)
