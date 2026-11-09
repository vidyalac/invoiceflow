# InvoiceFlow: invoice PDF to reasoned decision (PS-1)

Upload a vendor invoice (text PDF, scanned PDF or image). The process extracts the data, validates it, checks the vendor, matches it to a purchase order, screens for duplicates and fraud, then returns a decision with reason codes and a plain-English explanation. Every stage is visible live, and every run is logged to a dashboard.

## Design principle
**AI/OCR reads the document. Rules make the decision.** Finance needs decisions that are repeatable and defensible, so the decision engine is deterministic: the same invoice in the same state always gets the same answer, and every rule that fires leaves a reason code.

## Pipeline (8 stages, shown live in the UI)
1. **Ingest**: detect text PDF vs scanned image, fingerprint the file.
2. **Extract**: vendor, invoice no., date, PO ref, line items, tax, total, bank account. Each field carries a confidence score (OCR confidence for scans).
3. **Validate**: required fields, line items vs total, subtotal + tax = total, date sanity, OCR confidence floor.
4. **Vendor check**: approved-vendor list with fuzzy matching for lookalikes; bank details compared with the vendor master.
5. **PO match**: explicit PO reference, or inferred from vendor + amount (and flagged as inferred). Compared against the **remaining** balance so split POs work.
6. **Duplicate check**: exact (same file / same invoice number) and near-duplicate (same vendor + amount within 7 days).
7. **Rules engine**: highest severity wins: `ESCALATE > REJECT > HOLD > NOTE > none`.
8. **Explain & log**: plain-English rationale, next action, a drafted message when one is needed, audit record.

## Decisions
`APPROVE` · `APPROVE_WITH_NOTE` · `HOLD` (human review, with a drafted question) · `REJECT` · `ESCALATE` (suspected fraud, do not pay)

## Edge cases built in
1. **Split PO**: three monthly invoices against one PO. Balance is tracked, and a same-amount invoice a month later is *not* a duplicate. A third invoice that overshoots the remaining balance is held.
2. **Near-duplicate**: a resubmission with a changed invoice number and date is caught on amount + vendor + timing.
3. **Scanned invoice with missing invoice number and date**: OCR, confidence tracking, hold, and a specific drafted question to the vendor instead of a guess.
4. **Possible fraud**: lookalike vendor name (`Northwlnd`) with a new bank account, and a correct vendor with changed bank details. Both halt before any PO logic.

## Assumptions and limitations (state these out loud)
- **2-way match only**: there is no goods-receipt data, so no 3-way match.
- Tolerance (1% **and** $50) and the 7-day duplicate window are configurable in `data/config.json` and shown in the UI.
- **Extraction has two modes.** Default: pattern-based (tuned to the sample layouts). If the `ANTHROPIC_API_KEY` environment variable is set, Claude reads the invoice instead, which handles messy layouts. Each LLM value is checked against the source text, so a value that is not found verbatim (a hallucination, or the model silently 'correcting' a misspelled vendor name) gets low confidence and the invoice is held. If the API call fails, it falls back to the pattern extractor. **The decision engine stays rule-based in both modes.**
- Vendors, POs and invoices are synthetic (`gen_data.py`).
- Single currency (USD).

## Turning on LLM extraction
Set the environment variable `ANTHROPIC_API_KEY` (on Render: Environment tab). Optional: `LLM_MODEL` (default `claude-sonnet-5-5`). Never commit the key to GitHub. The Extract stage shows which extractor ran.

## Run locally
Needs Python 3.10+, `tesseract` and `poppler` installed.
```
pip install -r requirements.txt
python gen_data.py        # optional: regenerate the dataset
python app.py             # http://localhost:5000
python test_all.py -v     # runs all 11 samples and checks the decisions
```

## Deploy (Render, free)
1. Push this folder to a new GitHub repo.
2. On render.com: **New → Web Service → connect the repo → Runtime: Docker**. Choose the free instance.
3. Deploy, then open the URL. The free tier sleeps after inactivity (first load can take ~1 minute), so open it a few minutes before a demo.
4. Run state lives in the container, so a restart clears history. Use **Vendors, POs & rules → Reset demo data** to get a clean slate before a demo.
