"""Deterministic EBL adapter. PDF content is data, never executable instructions."""
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
from pypdf import PdfReader


class ParseError(ValueError):
    pass


DATE = re.compile(r"^\s*(\d{2}-[A-Za-z]{3}-\d{4})\s+")
MONEY = re.compile(r"^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?$")


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def money(text: str) -> Decimal:
    text = text.strip()
    if not MONEY.fullmatch(text):
        raise ParseError("An amount could not be read. Review the statement layout.")
    try:
        value = Decimal(text.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ParseError("An amount could not be read.") from None
    if abs(value) >= Decimal("10000000000000000"):
        raise ParseError("An amount exceeds the supported precision.")
    return value


def parse_date(text: str):
    try:
        return datetime.strptime(text, "%d-%b-%Y").date()
    except ValueError:
        raise ParseError("A statement date could not be read.") from None


def parse_pdf(data: bytes, password: str = "") -> dict:
    if len(data) > 10 * 1024 * 1024:
        raise ParseError("PDF must be at most 10 MB.")
    if not data.startswith(b"%PDF-"):
        raise ParseError("Upload a valid PDF file.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password):
            raise ParseError("Incorrect PDF password.")
        if len(reader.pages) > 50:
            raise ParseError("PDF must contain at most 50 pages.")
        page_texts = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
        # Coordinates provide source locations; no decrypted file is written.
        with pdfplumber.open(io.BytesIO(data), password=password) as document:
            positions = []
            for page in document.pages:
                words = page.extract_words()
                header_y = next((w["top"] for w in words if w["text"] == "TRN."), 0)
                positions.append({
                    "width": page.width, "height": page.height,
                    "dates": [w.copy() for w in words if re.fullmatch(r"\d{2}-[A-Za-z]{3}-\d{4}", w["text"]) and w["top"] >= header_y],
                })
    except ParseError:
        raise
    except Exception:
        raise ParseError("Unable to read this PDF. Check its password and format.") from None

    text = "\n".join(page_texts)
    if not re.search(r"\bEBL\b|Eastern Bank", text, re.I):
        raise ParseError("Only the supplied EBL statement format is supported.")
    account = re.search(r"Account No\s*:\s*([\d -]+)", text)
    period = re.search(r"Period From\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*-\s*(\d{2}-[A-Za-z]{3}-\d{4})", text)
    if not account or not period or "BANGLADESH TAKA" not in text.upper():
        raise ParseError("Account, period, or BDT currency could not be identified.")
    identifier = re.sub(r"\D", "", account[1])
    if not identifier:
        raise ParseError("Account identifier is missing.")

    rows, current = [], None
    opening = closing = None
    controls = {}
    table_finished = False

    def finish():
        nonlocal current
        if current is None:
            return
        description = " ".join(current.pop("description_parts")).strip()
        references = current.pop("references")
        embedded = re.findall(r"//\s*([A-Za-z0-9]+)", description)
        current["reference"] = " ".join(references).strip() or " ".join(embedded)
        current["description"] = description
        current["normalized_description"] = normalized(description)
        current["raw_text"] = "\n".join(current.pop("raw_lines"))
        if not description:
            raise ParseError("A transaction description is missing.")
        current["ordinal"] = len(rows) + 1
        rows.append(current)
        current = None

    for page_index, page_text in enumerate(page_texts):
        columns = None
        position = positions[page_index]
        for raw in page_text.splitlines():
            line = re.sub(r"\be-statement\b", lambda m: " " * len(m[0]), raw, flags=re.I)
            if re.match(r"^\s*Page\s+\d+\s*(?:of|/)\s*\d+\s*$", line, re.I):
                continue
            if "TRN. DATE" in line and all(label in line for label in ["DESCRIPTION", "REFERENCE", "DEBITS", "CREDITS", "BALANCE"]):
                columns = {label: line.index(label) for label in ["DESCRIPTION", "REFERENCE", "DEBITS", "CREDITS", "BALANCE"]}
                continue
            if "STATEMENT CLOSING BALANCE" in line:
                finish()
                closing = money(line.split("STATEMENT CLOSING BALANCE", 1)[1])
                table_finished = True
            for label, key, count_key, marker in [("DEBITS", "debit_total", "debit_count", "DR"), ("CREDITS", "credit_total", "credit_count", "CR")]:
                match = re.match(rf"^\s*{label}\s+([\d,.-]+)\s+#(?:{marker})COUNT:\s*(\d+)\s*$", line)
                if match:
                    controls[key] = str(money(match[1]))
                    controls[count_key] = int(match[2])
            if "UNSETTLED DEBIT CARD TRANSACTION" in line:
                finish()
                table_finished = True
            if columns is None or table_finished:
                continue
            match = DATE.match(line)
            if match:
                top = next((w["top"] for w in position["dates"] if w["text"].lower() == match[1].lower()), 0)
                for index, word in enumerate(position["dates"]):
                    if word["text"].lower() == match[1].lower():
                        position["dates"].pop(index)
                        break
                if current and current["page"] == page_index + 1:
                    current["location"]["bottom"] = top
                finish()
                if "Opening Balance" in line:
                    if opening is None:
                        opening = money(line[columns["BALANCE"]:])
                    continue
                debit_text = line[columns["DEBITS"]:columns["CREDITS"]].strip()
                credit_text = line[columns["CREDITS"]:columns["BALANCE"]].strip()
                debit = money(debit_text) if debit_text else Decimal("0")
                credit = money(credit_text) if credit_text else Decimal("0")
                if (debit > 0) == (credit > 0) or debit < 0 or credit < 0:
                    raise ParseError("A row must have exactly one positive debit or credit.")
                current = {
                    "page": page_index + 1,
                    "location": {"x0": 0, "top": top, "x1": position["width"], "bottom": position["height"]},
                    "posting_date": parse_date(match[1]).isoformat(),
                    "direction": "debit" if debit > 0 else "credit",
                    "amount": str(debit or credit),
                    "balance": str(money(line[columns["BALANCE"]:])),
                    "description_parts": [], "references": [], "raw_lines": [],
                }
            if current and line.strip():
                # Continuation text belongs to this row until the next date.
                current["description_parts"].append(" ".join(line[columns["DESCRIPTION"]:columns["REFERENCE"]].split()))
                reference = line[columns["REFERENCE"]:columns["DEBITS"]].strip()
                if reference:
                    current["references"].append(reference)
                current["raw_lines"].append(raw)
    finish()
    if opening is None or closing is None or not rows:
        raise ParseError("A complete posted-transaction table and balances are required.")
    period_from, period_to = parse_date(period[1]), parse_date(period[2])
    if period_from > period_to:
        raise ParseError("Statement period is invalid.")
    checks = []

    def check(code, label, passed, detail):
        checks.append({"code": code, "label": label, "passed": bool(passed), "detail": detail})

    debits = sum((Decimal(r["amount"]) for r in rows if r["direction"] == "debit"), Decimal("0"))
    credits = sum((Decimal(r["amount"]) for r in rows if r["direction"] == "credit"), Decimal("0"))
    check("closing", "Opening + credits − debits = closing", opening + credits - debits == closing,
          f"{opening} + {credits} − {debits} = {closing}")
    previous, bad_rows = opening, []
    for row in rows:
        expected = previous + Decimal(row["amount"]) * (1 if row["direction"] == "credit" else -1)
        if expected != Decimal(row["balance"]):
            bad_rows.append(row["ordinal"])
        previous = Decimal(row["balance"])
    check("running", "Running balances agree", not bad_rows, f"Rows with mismatches: {bad_rows}" if bad_rows else "Every posted movement agrees with its running balance.")
    check("last_balance", "Last transaction agrees with closing balance", previous == closing, f"Last balance {previous}; closing {closing}")
    check("dates", "Posting dates are within the statement period", all(period_from <= date.fromisoformat(r["posting_date"]) <= period_to for r in rows), "Posting dates use the bank's calendar dates.")
    for direction, total in [("debit", debits), ("credit", credits)]:
        count = sum(r["direction"] == direction for r in rows)
        check(direction + "_total", f"Printed {direction} total agrees", direction + "_total" in controls and Decimal(controls[direction + "_total"]) == total,
              f"Extracted {total}; printed {controls.get(direction + '_total', 'missing')}")
        check(direction + "_count", f"Printed {direction} count agrees", controls.get(direction + "_count") == count,
              f"Extracted {count}; printed {controls.get(direction + '_count', 'missing')}")
    return {"identifier": identifier, "period_from": period_from.isoformat(), "period_to": period_to.isoformat(),
            "opening_balance": str(opening), "closing_balance": str(closing), "controls": controls,
            "checks": checks, "rows": rows}
