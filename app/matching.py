from collections import Counter, defaultdict

from sqlalchemy import select

from .models import Account, StatementRow, Transaction


FIELDS = ("posting_date", "direction", "amount", "balance", "normalized_description", "reference")


def coarse(row):
    return row.posting_date, row.direction, row.amount


def exact(row):
    return tuple(getattr(row, field) for field in FIELDS)


def transaction_view(row):
    return {key: getattr(row, key, None) for key in ["id", "account_id", "posting_date", "direction", "amount", "balance", "description", "reference"]}


def review(session, statement, *, rows=None):
    account = session.get(Account, statement.account_id)
    if rows is None:
        rows = list(session.scalars(select(StatementRow).where(StatementRow.statement_id == statement.id).order_by(StatementRow.ordinal)))
    # Scope the lookup to the imported period, not the entire ledger.
    transactions = list(session.scalars(select(Transaction).where(
        Transaction.account_id == account.id,
        Transaction.posting_date >= statement.period_from,
        Transaction.posting_date <= statement.period_to,
    ).order_by(Transaction.id)))
    groups = defaultdict(list)
    for transaction in transactions:
        groups[coarse(transaction)].append(transaction)
    multiplicities = Counter(exact(row) for row in rows)
    output = []
    for row in rows:
        candidates = groups[coarse(row)]
        matches = [candidate for candidate in candidates if exact(candidate) == exact(row)]
        if not candidates:
            proposal, effective, target, explanation = "new", "new", None, "No existing transaction with this date, direction and amount."
        elif len(matches) == 1 and multiplicities[exact(row)] == 1:
            proposal, effective, target, explanation = "duplicate", "match", matches[0].id, "Exact one-to-one match: date, direction, amount, balance, full description and reference."
        else:
            proposal, effective, target, explanation = "ambiguous", "unresolved", None, "Similar transaction found; choose a source match or keep this occurrence separately."
        if row.decision == "new":
            effective, target = "new", None
        elif row.decision == "match":
            if row.match_id in {candidate.id for candidate in candidates}:
                effective, target = "match", row.match_id
            else:
                effective, target = "unresolved", None
        if statement.status == "confirmed":
            effective, target = row.decision, row.match_id
        item = transaction_view(row)
        item["account_id"] = account.id
        item.update(ordinal=row.ordinal, page=row.page, raw_text=row.raw_text, location=row.location,
                    proposal=proposal, effective=effective, decision=row.decision, target_id=target,
                    explanation=explanation, candidates=[transaction_view(c) for c in candidates])
        output.append(item)
    # Two source occurrences cannot silently collapse onto one canonical row.
    targets = Counter(row["target_id"] for row in output if row["effective"] == "match")
    if statement.status != "confirmed":
        for row in output:
            if row["effective"] == "match" and targets[row["target_id"]] > 1:
                row.update(effective="unresolved", explanation="Another row is linked to the same transaction. Keep distinct occurrences separate.")
    summary = {"new": 0, "duplicate": 0, "unresolved": 0}
    for row in output:
        summary[{"new": "new", "match": "duplicate", "unresolved": "unresolved"}[row["effective"]]] += 1
    return {
        "id": statement.id, "filename": statement.filename, "status": statement.status,
        "account": {"id": account.id, "label": account.label, "currency": account.currency},
        "period_from": statement.period_from, "period_to": statement.period_to,
        "opening_balance": statement.opening_balance, "closing_balance": statement.closing_balance,
        "controls": statement.controls, "checks": statement.checks, "rows": output,
        "revision": f"{account.revision}:{statement.review_revision}", "summary": summary,
        "can_confirm": statement.status == "draft" and all(c["passed"] for c in statement.checks) and not summary["unresolved"],
    }
