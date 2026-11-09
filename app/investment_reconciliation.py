"""Pure reconciliation rules. No database writes or HTTP dependencies.

All identities are strings and all arithmetic is Decimal. Proposals are separate
from persisted decisions; an unresolved consumption stops dependent proposals.
"""
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

ZERO = Decimal('0.00')
SEARCH_LIMIT = 20000
DIFFERENCE_LIMIT = Decimal('5.00')


def cash(value):
    return f'{value:.2f}'


def accepted_differences(events, allocations, cases):
    """Bind consent to the exact saved source amounts and destination amount.

    Amounts on allocations remain gross source contributions. The difference
    belongs to the settlement pool, never to an arbitrarily chosen business.
    """
    by_id = {str(e.id): e for e in events}
    latest = {}
    for case in cases:
        for review in case.resolutions:
            key = str(case.event_id)
            if review.get('accept_difference') and (key not in latest or review['reviewed_at'] > latest[key]['reviewed_at']):
                latest[key] = dict(review, case_id=str(case.id), event_id=key)
    result = {}
    for key, review in latest.items():
        if key not in by_id: continue
        selected = {a['source_id']: Decimal(a['amount']) for a in review['allocations']}
        current = {str(a.source_id): a.amount for a in allocations if str(a.target_id) == key}
        difference = sum(selected.values(), ZERO) - by_id[key].amount
        if current == selected and ZERO < abs(difference) <= DIFFERENCE_LIMIT and cash(difference) == review.get('difference'):
            result[key] = dict(event_id=key, case_id=review['case_id'], amount=cash(difference),
                               source_ids=list(selected), explanation=review['explanation'], reviewed_at=review['reviewed_at'])
    return result


def delta(event):
    return event.amount if event.kind in ('repayment', 'refund') else -event.amount if event.kind in ('cashout', 'reinvestment_settlement') else ZERO


def ordered_events(events, sequences):
    """Merge source-order constraints, requiring a unique order within each date."""
    by_id = {e.id: e for e in events}
    edges, incoming = defaultdict(set), {e.id: 0 for e in events}
    seen = set()
    for sequence in sequences:
        seen.update(sequence)
        for left, right in zip(sequence, sequence[1:]):
            if left == right or left not in by_id or right not in by_id:
                return [], 'A source repeats or refers to an unknown occurrence.'
            if by_id[left].posting_date > by_id[right].posting_date:
                return [], 'Source ordering conflicts with posting dates.'
            if right not in edges[left]:
                edges[left].add(right); incoming[right] += 1
    if seen != set(by_id):
        return [], 'Some movements have no confirmed CSV source order.'
    result = []
    while incoming:
        earliest = min(by_id[k].posting_date for k in incoming)
        ready = [k for k, n in incoming.items() if n == 0 and by_id[k].posting_date == earliest]
        if len(ready) != 1:
            return [], 'Conflicting or indeterminate same-day CSV order. Import a complete ordered report or correct the conflicting source.'
        key = ready[0]; result.append(by_id[key]); del incoming[key]
        for child in edges[key]: incoming[child] -= 1
    return result, None


def combinations(lots, amount, limit=SEARCH_LIMIT):
    """Find at most two whole-credit subsets. Exhaustion is never uniqueness."""
    items = [(key, value) for key, value in lots.items() if ZERO < value <= amount]
    if len(items) > 200:
        return [], False
    suffix = [ZERO] * (len(items) + 1)
    for i in range(len(items) - 1, -1, -1): suffix[i] = suffix[i + 1] + items[i][1]
    answers, stack, steps = [], [(0, amount, [])], 0
    while stack:
        steps += 1
        if steps > limit: return answers, False
        i, remaining, selected = stack.pop()
        if remaining == ZERO:
            answers.append(selected)
            if len(answers) == 2: return answers, True
            continue
        if i == len(items) or remaining < ZERO or suffix[i] < remaining: continue
        key, value = items[i]
        stack.append((i + 1, remaining, selected))
        stack.append((i + 1, remaining - value, selected + [key]))
    return answers, True


def wallet_replay(events, sequences, allocations, associations, reviews):
    ordered, ordering_error = ordered_events(events, sequences)
    by_id = {e.id: e for e in events}
    issues, proposals, groups = [], [], []
    state = {e.id: dict(available=None, reserved='0.00', consumed='0.00') for e in events if e.kind in ('repayment', 'refund')}

    def issue(kind, event, message, **details):
        value = dict(key=f'{kind}:{event.id if event else "wallet"}', kind=kind, event_id=event.id if event else None,
                     message=message, **details)
        issues.append(value)
        return value

    if ordering_error:
        issue('source_order', None, ordering_error)
        return dict(issues=issues, proposals=[], groups=[], credits=state, ordered_ids=[], wallet_status='needs_review', opening='0.00', closing=None)
    if not ordered:
        return dict(issues=[], proposals=[], groups=[], credits={}, ordered_ids=[], wallet_status='reconciled', opening='0.00', closing='0.00')
    position = {e.id: i for i, e in enumerate(ordered)}
    incoming, associated = defaultdict(list), defaultdict(list)
    for a in allocations: incoming[a.target_id].append(a)
    for a in associations: associated[a.funding_id].append(a.event_id)
    opening = ordered[0].ready_balance - delta(ordered[0])
    blocked = opening != ZERO
    if blocked: issue('opening_funds', ordered[0], 'Opening wallet funds have unknown business ownership. Import earlier history before allocating them.', amount=cash(opening))
    physical, available, reserved, consumed = opening, {}, defaultdict(dict), defaultdict(lambda: ZERO)
    settlement_owner, group_unsettled = {}, {}
    discrepancy_balance = ZERO
    claimed = {a.event_id: a.funding_id for a in associations}
    for e in ordered:
        physical += delta(e)
        if physical != e.ready_balance or physical < ZERO:
            issue('source_balance', e, 'CSV movements do not reconcile to a nonnegative Ready Balance.', calculated=cash(physical), reported=cash(e.ready_balance))
            blocked = True
        if e.kind in ('repayment', 'refund'):
            if not e.project_id:
                issue('credit_owner', e, 'This credit has no originating business.'); blocked = True
            available[e.id] = e.amount
            continue
        if e.kind == 'investment': continue
        if e.kind == 'reinvestment_settlement':
            funding = settlement_owner.get(e.id)
            if not funding:
                issue('settlement_group', e, 'This settlement has no validated reinvestment group.'); blocked = True
            else:
                group_unsettled[funding] -= e.amount
                if group_unsettled[funding] == ZERO:
                    for source, amount in reserved.pop(funding, {}).items(): consumed[source] += amount
            continue
        if e.kind not in ('cashout', 'reinvestment'): continue
        if blocked:
            issue('dependent_wallet', e, 'Resolve the earlier wallet issue before attributing this movement.')
            continue
        live = {k: v for k, v in available.items() if v > ZERO}
        review = reviews.get(e.id, {})
        # A reinvestment owns whole settlement rows and, when present, one top-up.
        if e.kind == 'reinvestment':
            linked = associated[e.id]
            if not linked:
                same_day = [x for x in ordered if x.posting_date == e.posting_date]
                funding = [x for x in same_day if x.kind == 'reinvestment']
                settlements = [x for x in same_day if x.kind == 'reinvestment_settlement' and x.id not in claimed]
                topups = [x for x in same_day if x.kind == 'investment' and x.project_id == e.project_id and x.id not in claimed]
                if len(funding) == 1 and sum((x.amount for x in settlements), ZERO) == e.amount and len(topups) <= 1:
                    linked = [x.id for x in settlements + topups]
            linked_events = [by_id[k] for k in linked if k in by_id]
            settlements = [x for x in linked_events if x.kind == 'reinvestment_settlement']
            topups = [x for x in linked_events if x.kind == 'investment']
            valid_group = (len(linked) == len(linked_events) and len(set(linked)) == len(linked)
                           and sum((x.amount for x in settlements), ZERO) == e.amount and len(topups) <= 1
                           and all(position[x.id] > position[e.id] for x in settlements)
                           and all(x.project_id == e.project_id for x in topups)
                           and all(x.kind in ('investment', 'reinvestment_settlement') for x in linked_events)
                           and all(claimed.get(k, e.id) == e.id for k in linked))
            if not valid_group:
                issue('reinvestment_group', e, 'Choose the settlement rows and bank top-up belonging to this destination. Settlement amounts must equal the reinvestment.',
                      association_candidates=[x.id for x in ordered if x.kind in ('investment', 'reinvestment_settlement') and x.id not in claimed and (x.kind != 'investment' or x.project_id == e.project_id)])
                blocked = True; continue
        else:
            linked = []
        existing = incoming[e.id]
        selection = {a.source_id: a.amount for a in existing}
        difference = sum(selection.values(), ZERO) - e.amount
        accepted = (review.get('accept_difference') and ZERO < abs(difference) <= DIFFERENCE_LIMIT
                    and cash(difference) == review.get('difference')
                    and selection == {a['source_id']: Decimal(a['amount']) for a in review.get('allocations', [])})
        if any(k not in live or v != live[k] for k, v in selection.items()) or (difference > ZERO and not accepted):
            issue('allocation_conflict', e, 'Existing allocations conflict with source order, capacity, or whole outstanding credits. Remove or correct these links; they were preserved.')
            blocked = True; continue
        needed = ZERO if accepted else e.amount - sum(selection.values(), ZERO)
        remaining_lots = {k: v for k, v in live.items() if k not in selection}
        delayed = e.kind == 'reinvestment' and sum(live.values(), ZERO) != e.amount
        if delayed and not review.get('delayed_booking') and not (accepted and sum(selection.values(), ZERO) == sum(live.values(), ZERO)):
            issue('delayed_booking', e, 'Ready Balance at booking differs from the reinvestment. Review the credits available at the earlier instruction; later credits must remain in the wallet.',
                  available_before=cash(sum(live.values(), ZERO)), amount=cash(e.amount), credit_candidates=[dict(source_id=k, amount=cash(v)) for k, v in live.items()], association_ids=linked)
            blocked = True; continue
        if review.get('instruction_date'):
            instruction = datetime.strptime(review['instruction_date'], '%Y-%m-%d').date()
            if instruction > e.posting_date or any(by_id[k].posting_date > instruction for k in selection):
                issue('allocation_conflict', e, 'Reviewed source credits fall after the recorded instruction date.'); blocked = True; continue
            if review.get('delayed_booking') and any(by_id[k].posting_date < instruction and k not in selection for k in live):
                issue('allocation_conflict', e, 'Earlier available credits were excluded. The instruction must consume the full wallet available at that time.'); blocked = True; continue
        wallet_after = e.ready_balance - discrepancy_balance - (difference if accepted else ZERO)
        choices, complete = [], True
        if needed == ZERO: choices = [[]]
        elif e.kind == 'reinvestment' or wallet_after == ZERO:
            if sum(remaining_lots.values(), ZERO) == needed: choices = [list(remaining_lots)]
        else:
            project_lots = defaultdict(dict)
            for k, v in remaining_lots.items(): project_lots[by_id[k].project_id][k] = v
            pinned = {by_id[k].project_id for k in selection}
            for project, lots in project_lots.items():
                if pinned and pinned != {project}: continue
                found, finished = combinations(lots, needed)
                complete = complete and finished
                choices.extend(found)
                if len(choices) > 1: break
        if not complete or len(choices) != 1:
            issue('wallet_match', e, 'Whole-credit attribution is ambiguous or unavailable. Select the actual source credits.' if complete else 'Combination search limit reached. Review the actual credits; uniqueness is unproven.',
                  credit_candidates=[dict(source_id=k, amount=cash(v)) for k, v in live.items()],
                  candidates=[dict(allocations=[dict(source_id=k, amount=cash(live[k])) for k in list(selection) + choice]) for choice in choices[:2]])
            blocked = True; continue
        chosen = dict(selection, **{k: live[k] for k in choices[0]})
        if e.kind == 'cashout' and wallet_after > ZERO and len({by_id[k].project_id for k in chosen}) != 1:
            issue('allocation_conflict', e, 'An automatic payout with a remaining wallet balance must belong to one business.'); blocked = True; continue
        if e.kind == 'cashout' and wallet_after == ZERO and sum(live.values(), ZERO) != sum(chosen.values(), ZERO):
            issue('allocation_conflict', e, 'A full-wallet cashout must consume all available credits.'); blocked = True; continue
        if accepted:
            discrepancy_balance += difference
        for source, amount in chosen.items():
            available[source] -= amount
            if source not in selection: proposals.append(dict(source_id=source, target_id=e.id, amount=cash(amount), explanation='Unique whole-credit match under wallet and business constraints.'))
            if e.kind == 'reinvestment': reserved[e.id][source] = amount
            else: consumed[source] += amount
        if e.kind == 'reinvestment':
            groups.append(dict(funding_id=e.id, event_ids=linked))
            group_unsettled[e.id] = e.amount
            for k in linked:
                claimed[k] = e.id
                if by_id[k].kind == 'reinvestment_settlement': settlement_owner[k] = e.id
        held = sum(group_unsettled.values(), ZERO)
        if sum(available.values(), ZERO) + held + discrepancy_balance != physical:
            issue('conservation', e, 'Available plus reserved funds and reviewed differences do not equal the reported wallet.'); blocked = True
        if discrepancy_balance < ZERO and sum(available.values(), ZERO) > ZERO:
            issue('discrepancy_balance', e, 'The accepted shortfall leaves other wallet credits with uncertain backing. Review the source history before spending those credits.')
            blocked = True
    for k in state:
        held = sum((r.get(k, ZERO) for r in reserved.values()), ZERO)
        state[k] = dict(available=None if blocked else cash(available.get(k, ZERO)), reserved=cash(held), consumed=cash(consumed[k]))
    return dict(issues=issues, proposals=proposals, groups=groups, credits=state, ordered_ids=[e.id for e in ordered],
                wallet_status='needs_review' if blocked else 'reconciled', opening=cash(opening), closing=cash(physical), discrepancy_balance=cash(discrepancy_balance))


def normalized(text):
    return ' '.join(text.casefold().split())


def bank_evidence(event, transaction):
    funding = event.kind == 'investment'
    days = (event.posting_date - transaction.posting_date).days if funding else (transaction.posting_date - event.posting_date).days
    pattern = 'other bank transfer skb-beftn' if funding else 'eft inflow - the city bank ltd., cbl, payment,'
    matches = pattern in normalized(transaction.description)
    business_date = None
    found = re.search(r'BUSINESS\s+DATE\s*:\s*(\d{2}-\d{2}-\d{2,4})\b', transaction.description, re.I)
    if found:
        try: business_date = datetime.strptime(found[1], '%d-%m-%Y' if len(found[1]) == 10 else '%d-%m-%y').date().isoformat()
        except ValueError: pass
    return dict(pattern_matches=matches, business_date=business_date, eligible=matches and 0 <= days <= 3 and transaction.direction == ('debit' if funding else 'credit'))
