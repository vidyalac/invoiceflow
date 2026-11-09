"""SQLite audit log + PO balance state. Every run is stored so the dashboard can show history."""
import os, json, sqlite3, datetime as dt
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("STATE_DIR", os.path.join(BASE, "state"))
DB = os.path.join(STATE, "app.db")
APPROVED = ("APPROVE", "APPROVE_WITH_NOTE")


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    os.makedirs(STATE, exist_ok=True)
    with conn() as c:
        c.execute("""create table if not exists runs(
            id text primary key, created_at text, filename text, sha text, mode text, decision text,
            vendor_id text, vendor_name text, invoice_no text, invoice_date text, total real,
            po_number text, duration_ms int, payload text)""")
        c.execute("create table if not exists billed(po_number text primary key, amount real)")


def reset():
    with conn() as c:
        c.execute("delete from runs")
        c.execute("delete from billed")


def insert_run(run):
    v = run.get("view", {}).get("fields", {})
    po = run.get("view", {}).get("po") or {}
    with conn() as c:
        c.execute("insert or replace into runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run["id"], run["created_at"], run["filename"], run.get("sha"), run.get("mode"),
            run["result"]["decision"], run.get("vendor_id"), (v.get("vendor_name") or {}).get("value"),
            (v.get("invoice_no") or {}).get("value"), (v.get("invoice_date") or {}).get("value"),
            (v.get("total") or {}).get("value"), po.get("po_number"), run.get("duration_ms"), json.dumps(run)))


def get_run(run_id):
    with conn() as c:
        r = c.execute("select payload from runs where id=?", (run_id,)).fetchone()
    return json.loads(r["payload"]) if r else None


def list_runs(limit=200):
    with conn() as c:
        rows = c.execute("select id,created_at,filename,decision,vendor_name,invoice_no,invoice_date,total,po_number,duration_ms,mode "
                         "from runs order by created_at desc limit ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def prior_runs(vendor_id):
    """Earlier runs that still count for duplicate detection (approved or pending; not rejected/escalated)."""
    with conn() as c:
        rows = c.execute("select id,sha,invoice_no,invoice_date,total,po_number,decision,filename from runs "
                         "where vendor_id=? and decision in ('APPROVE','APPROVE_WITH_NOTE','HOLD')", (vendor_id,)).fetchall()
    return [dict(r) for r in rows]


def billed_map():
    with conn() as c:
        return {r["po_number"]: r["amount"] for r in c.execute("select * from billed")}


def add_billed(po_number, amount):
    with conn() as c:
        c.execute("insert into billed values(?,?) on conflict(po_number) do update set amount=amount+excluded.amount",
                  (po_number, amount))


def stats():
    with conn() as c:
        rows = c.execute("select decision,total,duration_ms,payload from runs").fetchall()
    by, reasons = Counter(), Counter()
    approved_amt = blocked_amt = 0.0
    dur = []
    for r in rows:
        by[r["decision"]] += 1
        amt = r["total"] or 0
        if r["decision"] in APPROVED:
            approved_amt += amt
        else:
            blocked_amt += amt
        if r["duration_ms"]:
            dur.append(r["duration_ms"])
        for f in json.loads(r["payload"]).get("result", {}).get("findings", []):
            if f["sev"] != "INFO":
                reasons[f["code"]] += 1
    n = len(rows)
    auto = by["APPROVE"] + by["APPROVE_WITH_NOTE"]
    return {"total": n, "by": dict(by), "auto_rate": (auto / n if n else 0), "approved_amount": approved_amt,
            "blocked_amount": blocked_amt, "avg_ms": (sum(dur) / len(dur) if dur else 0),
            "reasons": reasons.most_common(8)}
