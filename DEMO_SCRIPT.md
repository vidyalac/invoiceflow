# 5-minute demo script

**Before recording:** open the app, go to **Vendors, POs & rules**, click **Reset demo data**, then go back to **Process an invoice**. Have the Dashboard tab ready. Speak slowly; the pipeline is the star.

## 0:00 – 0:30 · The problem and the idea
"A mid-size company gets hundreds of invoices a month. Someone opens each PDF, finds the PO, checks the numbers and decides. This tool does that end to end. One principle: **AI reads the invoice, rules make the decision**, because finance needs decisions that are repeatable and explainable."

## 0:30 – 1:45 · Happy path (sample 01)
Click **Run** on *Clean invoice, exact PO match*. Narrate each stage as it lights up:
- Ingest: "It's a text PDF, so it reads it directly."
- Extract: "Pulls vendor, number, date, PO, line items, tax, total, with a confidence per field."
- Validate: "Line items add up, subtotal plus tax equals total."
- Vendor: "Approved vendor, bank account matches what we have on file."
- PO match: "PO-1001, amount equals the remaining balance."
- Duplicate: "Nothing similar before."
- Result: "Approved, with the payment date from the vendor's terms."

## 1:45 – 3:00 · Edge case 1: split PO (04a, then 04b, then 04c)
"One PO of $12,960 billed as three monthly invoices." Run **04a**: approved, balance drops to $8,640. Run **04b**: "Same vendor, same amount. A naive duplicate check would reject this. Mine looks at timing, so it's approved." Run **04c**: "The third invoice sneaks in $8,000 of extras. It would push billing past the PO, so it's held with a drafted note to procurement."

## 3:00 – 4:00 · Edge case 3: scanned, missing data (06)
"This is an image-only scan, no text layer, so it falls back to OCR and tracks confidence." Point at the confidence bars. "The vendor left off the invoice number and date. The process won't guess. It holds the invoice and drafts a specific email asking for those two fields."

## 4:00 – 4:40 · Edge case 4: fraud signal (07)
"Looks like Northwind, but it's spelled 'Northwlnd' and the bank account is new. The run halts at the vendor stage, before any PO logic, and escalates: do not pay, verify by phone."

## 4:40 – 5:00 · Dashboard and close
Open **Dashboard**. "Every run is logged: auto-approval rate, money held back, top exception reasons, and a full audit trail on click." Close: "Limits I'd call out: 2-way match only, since there's no goods-receipt data, and extraction is pattern-based. In production I'd add an LLM extractor for messy layouts and keep the rules engine deterministic."

## Interview: be ready for these
- *Why rules, not an LLM, for the decision?* Repeatability and auditability.
- *Why a near-duplicate window?* Vendors resubmit with a changed number; recurring invoices a month apart are legitimate.
- *Why do tolerances use "1% AND $50"?* Percentage alone lets large invoices drift by a lot; a flat amount alone is too loose for small ones.
- *What breaks it?* Layouts the patterns don't know. Low OCR confidence is caught and held rather than guessed.
