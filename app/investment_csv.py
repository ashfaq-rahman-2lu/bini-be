"""Strict, Decimal-only reader for the institution's multiline BDT CSV export."""
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation


COLUMNS = ['Date', 'Transaction', 'Withdrawn(bdt)', 'Invested into Business(bdt)', 'Investment Refund(bdt)',
           'Repaid by Business(bdt)', 'Ready Balance(bdt)', 'Invested into Protection Pool(bdt)',
           'Protection Pool Refund(bdt)', 'Capital With Business(bdt)']
ZERO = Decimal('0.00')


class CSVError(ValueError):
    pass


def amount(value, *, signed=False):
    value = value.strip()
    if not value:
        return ZERO
    if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d{1,2})?', value):
        raise CSVError('Invalid monetary value; use at most two decimal places.')
    try:
        result = Decimal(value.replace(',', ''))
    except InvalidOperation:
        raise CSVError('Invalid monetary value.') from None
    if abs(result) >= Decimal('10000000000000000') or (result < 0 and not signed):
        raise CSVError('Monetary value is outside the supported range.')
    return result.quantize(Decimal('.01'))


def cell_amount(value, column, row, *, signed=False):
    try:
        return amount(value, signed=signed)
    except CSVError as exc:
        raise CSVError(f'{column}{row}: {exc}') from None


def ready_amount(value, *, first_row, last_row, current_row, computed, total=False):
    """Recognize only our G-column formulas; never execute spreadsheet code."""
    value = value.strip()
    if not value.startswith('='):
        return cell_amount(value, 'G', current_row, signed=True)
    expected = f'=SUM($F${first_row}:F{last_row})-SUM($C${first_row}:C{last_row})'
    if value.upper() != expected and not (total and value.upper() == f'=G{last_row}'):
        raise CSVError(f'G{current_row}: Unsupported ready-balance formula or incorrect row references. '
                       'Use the running SUM(F) minus SUM(C) formula for this row, '
                       'or reference the final transaction balance in TOTAL.')
    return cell_amount(str(computed), 'G', current_row, signed=True)


def repayment_control(value, *, expected, computed, column, row):
    value = value.strip()
    if not value.startswith('='):
        return cell_amount(value, column, row, signed=True)
    if value.upper() != expected:
        raise CSVError(f'{column}{row}: Unsupported repayment-total formula or incorrect row references. '
                       f'Use {expected}.')
    return cell_amount(str(computed), column, row, signed=True)


def capital_amount(value, *, first_row, last_row, current_row, computed, total=False):
    value = value.strip().upper()
    if not value.startswith('='):
        return cell_amount(value, 'J', current_row, signed=True)
    if total:
        if value != f'=J{last_row}':
            raise CSVError(f'J{current_row}: Unsupported capital-total formula or incorrect row reference.')
        return cell_amount(str(computed), 'J', current_row, signed=True)
    prefix = (f'=SUM($D${first_row}:D{last_row})-SUM($E${first_row}:E{last_row})'
              f'-SUM($F${first_row}:F{last_row})')
    suffix = value[len(prefix):]
    if not value.startswith(prefix) or suffix and not re.fullmatch(r'[+-]\d+(?:\.\d{1,2})?', suffix):
        raise CSVError(f'J{current_row}: Unsupported capital formula or incorrect row references.')
    opening = cell_amount(suffix.lstrip('+'), 'J', current_row, signed=True) if suffix else ZERO
    return cell_amount(str(computed + opening), 'J', current_row, signed=True)


def parse_investment_csv(data: bytes):
    if not data or len(data) > 2 * 1024 * 1024:
        raise CSVError('Upload a nonempty UTF-8 CSV of at most 2 MB.')
    try:
        records = list(csv.reader(io.StringIO(data.decode('utf-8-sig')), strict=True))
    except (UnicodeDecodeError, csv.Error):
        raise CSVError('The file must be a valid UTF-8 CSV export.') from None
    try:
        start = records.index(COLUMNS)
    except ValueError:
        raise CSVError('The investment transaction headers do not match the supported BDT export.') from None
    summary, repayment_summary_formulas, balance_summary_formulas = {}, [], []
    for index, row in enumerate(records[:start], 1):
        # Spreadsheet exports may pad two-cell summary rows to ten columns.
        if len(row) >= 2 and not any(cell.strip() for cell in row[2:]) and row[0].endswith(':'):
            label = row[0].rstrip(':')
            if label == 'Total Repaid (from campaigns)' and row[1].strip().startswith('='):
                repayment_summary_formulas.append((row[1], index))
            elif label in ('Ready Balance', 'Total Currently Invested') and row[1].strip().startswith('='):
                balance_summary_formulas.append((label, row[1], index))
            else:
                summary[label] = str(cell_amount(row[1], 'B', index, signed=True))
    rows, warnings, totals = [], [], None
    occurrences, reinvest_by_day, settlement_by_day = Counter(), defaultdict(Decimal), defaultdict(Decimal)
    previous, last_row = None, None
    formula_balance, repayment_sum, capital_sum = ZERO, ZERO, ZERO
    total_row = None
    first_row = start + 2
    for csv_row, raw in enumerate(records[start + 1:], first_row):
        if not raw or not any(raw):
            continue
        if len(raw) != len(COLUMNS):
            raise CSVError('A transaction has the wrong number of columns.')
        if raw[1] == 'TOTAL':
            if totals is not None:
                raise CSVError('Multiple TOTAL rows were found.')
            if last_row is None:
                raise CSVError('The export must contain transactions before TOTAL.')
            totals = {}
            for col, (key, value) in enumerate(zip(COLUMNS[2:], raw[2:]), 2):
                if col == 6:
                    parsed = ready_amount(value, first_row=first_row, last_row=last_row,
                                          current_row=csv_row, computed=formula_balance, total=True)
                elif col == 5:
                    parsed = repayment_control(value, expected=f'=SUM(F{first_row}:F{last_row})',
                                               computed=repayment_sum, column='F', row=csv_row)
                elif col == 9:
                    parsed = capital_amount(value, first_row=first_row, last_row=last_row,
                                            current_row=csv_row, computed=Decimal(rows[-1]['reported_capital']), total=True)
                else:
                    parsed = cell_amount(value, chr(65 + col), csv_row, signed=True)
                totals[key] = str(parsed)
            total_row = csv_row
            continue
        if totals is not None:
            raise CSVError('Transactions cannot follow the TOTAL row.')
        if len(rows) >= 10000:
            raise CSVError('CSV exports are limited to 10,000 transactions.')
        try:
            posting = datetime.strptime(raw[0], '%d-%b-%Y').date()
        except ValueError:
            raise CSVError('Transaction dates must use DD-Mon-YYYY.') from None
        if previous and posting < previous:
            raise CSVError('Transactions must be in chronological export order.')
        previous = posting
        values = [cell_amount(v, chr(65 + col), csv_row) if col not in (6, 9) else ZERO
                  for col, v in enumerate(raw[2:], 2)]
        formula_balance += values[3] - values[0]
        repayment_sum += values[3]
        capital_sum += values[1] - values[2] - values[3]
        values[7] = capital_amount(raw[9], first_row=first_row, last_row=csv_row,
                                   current_row=csv_row, computed=capital_sum)
        values[4] = ready_amount(raw[6], first_row=first_row, last_row=csv_row,
                                 current_row=csv_row, computed=formula_balance)
        last_row = csv_row
        withdrawal, investment, refund, repayment, ready, pool, pool_refund, capital = values
        if pool or pool_refund:
            raise CSVError('Protection-pool activity is not supported yet; no investment records were saved.')
        text = raw[1].strip()
        lines = text.splitlines()
        title = lines[0].casefold() if lines else ''
        project = re.search(r'(?:for |made by )(.+? \[\d+\])(?: to biniyog)?(?:$|\n)', text, re.I)
        if title in ('investment', 'reinvestment'):
            kind, value = title, investment
        elif title == 'repayment received':
            kind, value = 'repayment', repayment
        elif title in ('investment refund', 'refund received'):
            kind, value = 'refund', refund
        elif title == 'disbursed' and len(lines) == 2 and lines[1].strip().casefold() in ('cashout', 'reinvestment'):
            kind, value = ('cashout' if lines[1].strip().casefold() == 'cashout' else 'reinvestment_settlement'), withdrawal
        else:
            raise CSVError(f'Unrecognized transaction type at row {len(rows) + 1}.')
        if value <= 0 or sum(v != ZERO for v in (withdrawal, investment, refund, repayment)) != 1:
            raise CSVError(f'Row {len(rows) + 1} must contain exactly one positive movement in its expected column.')
        if kind in ('investment', 'reinvestment', 'repayment', 'refund') and not project:
            raise CSVError(f'Project identity is missing at row {len(rows) + 1}.')
        name = project.group(1).strip() if project else None
        if name and len(name) > 255:
            raise CSVError('Project name is too long.')
        profit_match = re.search(r'with ([\d,]+(?:\.\d{1,2})?) profit', text, re.I)
        profit = amount(profit_match.group(1)) if profit_match else None
        if profit is not None and (kind != 'repayment' or profit > value):
            raise CSVError('Reported profit is invalid for its repayment.')
        # Full record identity includes wallet/capital balances and repeated-occurrence number.
        canonical = [posting.isoformat(), text] + [str(v) for v in values]
        signature = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()
        occurrences[signature] += 1
        rows.append(dict(ordinal=len(rows) + 1, posting_date=posting.isoformat(), kind=kind, amount=str(value),
                         description=text, project=name, reported_profit=str(profit) if profit is not None else None,
                         ready_balance=str(ready), reported_capital=str(capital), signature=signature,
                         occurrence=occurrences[signature], raw=dict(zip(COLUMNS, raw))))
        if kind == 'reinvestment': reinvest_by_day[posting.isoformat()] += value
        if kind == 'reinvestment_settlement': settlement_by_day[posting.isoformat()] += value
    if not rows or totals is None:
        raise CSVError('The export must contain transactions and one TOTAL row.')
    for value, index in repayment_summary_formulas:
        summary['Total Repaid (from campaigns)'] = str(repayment_control(
            value, expected=f'=F{total_row}', computed=Decimal(totals[COLUMNS[5]]), column='B', row=index))
    for label, value, index in balance_summary_formulas:
        expected = f'=G{total_row}' if label == 'Ready Balance' else f'=J{total_row}+G{total_row}'
        if value.strip().upper() != expected:
            raise CSVError(f'B{index}: Unsupported summary formula or incorrect row references for {label}. '
                           f'Use {expected}.')
        computed = Decimal(totals[COLUMNS[6]])
        if label == 'Total Currently Invested':
            computed += Decimal(totals[COLUMNS[9]])
        summary[label] = str(cell_amount(str(computed), 'B', index, signed=True))
    if reinvest_by_day != settlement_by_day:
        raise CSVError('Reinvestment funding and wallet settlement totals must agree on each date.')
    for column in (2, 3, 4, 5, 7, 8):
        actual = sum((amount(r['raw'][COLUMNS[column]]) for r in rows), ZERO)
        if actual != Decimal(totals[COLUMNS[column]]):
            raise CSVError(f'TOTAL does not reconcile for {COLUMNS[column]}: '
                           f'transaction rows sum to {actual:.2f}, but TOTAL says {totals[COLUMNS[column]]}.')
    for column in ('Ready Balance(bdt)', 'Capital With Business(bdt)'):
        field = 'ready_balance' if column.startswith('Ready') else 'reported_capital'
        if Decimal(totals[column]) != Decimal(rows[-1][field]):
            raise CSVError(f'TOTAL does not match the final {column}.')
    for label, column in [('Total Deposited', COLUMNS[3]), ('Total Repaid (from campaigns)', COLUMNS[5]),
                          ('Total Withdrawn (to bank)', COLUMNS[2]), ('Total Investment Refund', COLUMNS[4]),
                          ('Ready Balance', COLUMNS[6])]:
        if label in summary and Decimal(summary[label]) != Decimal(totals[column]):
            raise CSVError(f'The provider summary does not reconcile for {label}.')
    # Infer an opening wallet for partial exports. Do not invent missing history.
    def wallet_delta(r):
        return Decimal(r['amount']) * (1 if r['kind'] in ('repayment', 'refund') else -1 if r['kind'] in ('cashout', 'reinvestment_settlement') else 0)
    opening = Decimal(rows[0]['ready_balance']) - wallet_delta(rows[0])
    balance = opening
    for r in rows:
        balance += wallet_delta(r)
        if balance != Decimal(r['ready_balance']):
            raise CSVError(f'Wallet running balance does not reconcile at row {r["ordinal"]}.')
    def capital_delta(r):
        return Decimal(r['amount']) * (1 if r['kind'] in ('investment', 'reinvestment') else -1 if r['kind'] in ('repayment', 'refund') else 0)
    capital = Decimal(rows[0]['reported_capital']) - capital_delta(rows[0])
    if capital:
        warnings.append('This export starts with existing business capital. Import earlier history before measuring project returns or final gains.')
    for r in rows:
        capital += capital_delta(r)
        if capital != Decimal(r['reported_capital']):
            raise CSVError(f'Reported business-capital running balance does not reconcile at transaction '
                           f'{r["ordinal"]}: expected {capital:.2f}, found {r["reported_capital"]}. '
                           'After correcting repayments, recalculate column J and its TOTAL as well.')
    if opening:
        warnings.append('This export starts with a nonzero wallet balance. Earlier funding history is not included.')
    if any(Decimal(r['ready_balance']) < 0 for r in rows):
        warnings.append('The provider reports negative wallet balances on some dates. These source values are retained.')
    if 'Total Currently Invested' in summary and Decimal(summary['Total Currently Invested']) != Decimal(rows[-1]['reported_capital']) + Decimal(rows[-1]['ready_balance']):
        warnings.append('The summary “Total Currently Invested” differs from final capital with business plus ready balance. This combined balance is not treated as principal outstanding.')
    if any(n > 1 for n in occurrences.values()):
        warnings.append('Identical full CSV rows repeat. Multiplicity is preserved; review overlap carefully for partial exports.')
    warnings.append('Withdrawn totals include reinvestment settlements. Only cashout rows are bank payouts. Repayments contain capital and profit; unclassified amounts are not treated as profit.')
    return dict(rows=rows, summary=summary, totals=totals, warnings=warnings, opening_wallet=str(opening),
                closing_wallet=rows[-1]['ready_balance'], parser_version='biniyog-csv-v5')
