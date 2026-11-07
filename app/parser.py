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


def positioned_lines(page):
    """Keep physical word positions; proportional-font text has no stable character grid."""
    groups = pdfplumber.utils.cluster_objects(page.extract_words(), "top", 3)
    lines = [sorted(group, key=lambda word: word["x0"]) for group in groups]
    # Clip extraction at the detected bank footer, without modifying the PDF.
    # This also removes wrapped footer fragments and either Web:/Website: variant.
    footer_top = page.bbox[3]
    for words in lines:
        text = normalized(" ".join(word["text"] for word in words))
        top = min(word["top"] for word in words)
        notice = "this document is verifiable by scanning" in text
        contact = "contact center:" in text and ("web:" in text or "website:" in text)
        if top >= page.bbox[1] + page.height * 0.75 and (notice or contact):
            footer_top = min(footer_top, top)
    return [words for words in lines if min(word["top"] for word in words) < footer_top]


def table_columns(words):
    headers = {word["text"]: word for word in words}
    labels = ["DESCRIPTION", "REFERENCE", "DEBITS", "CREDITS", "BALANCE"]
    if not all(label in headers for label in labels):
        raise ParseError("The transaction table columns could not be identified.")
    # Amounts are right-aligned and can start to the left of their header text.
    # The whitespace between adjacent column headings defines their separator.
    # Continuation text can begin slightly left of the DESCRIPTION heading.
    # Use the gap after the date column, rather than the heading's exact x0.
    date_header = headers.get("DATE")
    description_left = ((date_header["x1"] + headers["DESCRIPTION"]["x0"]) / 2
                        if date_header else headers["DESCRIPTION"]["x0"] - 3)
    columns = {"DESCRIPTION": description_left, "REFERENCE": headers["REFERENCE"]["x0"] - 1}
    for previous, label in zip(labels[1:], labels[2:]):
        columns[label] = (headers[previous]["x1"] + headers[label]["x0"]) / 2
    if list(columns.values()) != sorted(columns.values()):
        raise ParseError("The transaction table column order is unsupported.")
    return columns


def table_cell(words, columns, label):
    labels = list(columns)
    index = labels.index(label)
    left = columns[label]
    right = columns[labels[index + 1]] if index + 1 < len(labels) else float("inf")
    return " ".join(word["text"] for word in words
                    if left <= word["x0"] < right and word["text"].casefold() != "e-statement")


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
        # Physical coordinates determine cells as well as source locations.
        with pdfplumber.open(io.BytesIO(data), password=password) as document:
            pages = []
            for page in document.pages:
                pages.append({
                    "width": page.width, "height": page.height,
                    "lines": positioned_lines(page),
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

    for page_index, position in enumerate(pages):
        columns = None
        for words in position["lines"]:
            raw = " ".join(word["text"] for word in words)
            line = re.sub(r"\be-statement\b", lambda m: " " * len(m[0]), raw, flags=re.I)
            if re.match(r"^\s*Page\s+\d+\s*(?:of|/)\s*\d+\s*$", line, re.I):
                continue
            if "This document is verifiable by scanning" in line or ("Contact Center:" in line and ("Website:" in line or "Web:" in line)):
                continue
            if "TRN. DATE" in line and all(label in line for label in ["DESCRIPTION", "REFERENCE", "DEBITS", "CREDITS", "BALANCE"]):
                columns = table_columns(words)
                continue
            closing_label = re.search(r"STATEMENT\s*CLOSING\s*BALANCE", line)
            if closing_label:
                finish()
                closing = money(line[closing_label.end():])
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
                top = min(word["top"] for word in words)
                if current and current["page"] == page_index + 1:
                    current["location"]["bottom"] = top
                finish()
                if "Opening Balance" in line:
                    if opening is None:
                        opening = money(table_cell(words, columns, "BALANCE"))
                    continue
                debit_text = table_cell(words, columns, "DEBITS")
                credit_text = table_cell(words, columns, "CREDITS")
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
                    "balance": str(money(table_cell(words, columns, "BALANCE"))),
                    "description_parts": [], "references": [], "raw_lines": [],
                }
            if current and line.strip():
                # Continuation text belongs to this row until the next date.
                current["description_parts"].append(table_cell(words, columns, "DESCRIPTION"))
                reference = table_cell(words, columns, "REFERENCE")
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
