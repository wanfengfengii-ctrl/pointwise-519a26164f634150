from review.field.handover import FieldOutcome, read_dispatch_outcome
from review.field.inbox import FieldInbox
from review.field.receipt import ReceiptLedger
from review.storage import atomic


def receive(db, envelope):
    """Claim a received dispatch or repeat its original terminal answer."""
    inbox = FieldInbox(db)
    ledger = ReceiptLedger(db)
    with atomic(db):
        inbox.require_binding(envelope)
        outcome = read_dispatch_outcome(db, envelope.dispatch_id)
        if outcome.status != 'waiting':
            return outcome
        case_claims = ledger.list_case_claims(envelope.case_id)
        if any(item.envelope.specimen_id == envelope.specimen_id for item in case_claims):
            reason = 'specimen_already_claimed'
        elif len(case_claims) >= 2:
            reason = 'case_full'
        else:
            return FieldOutcome(envelope, 'claimed', receipt=ledger.record_claim(envelope))
        inbox.store_refusal(envelope, reason)
        return FieldOutcome(envelope, 'refused', reason=reason)


def retire_dispatch(db, envelope):
    """Close an old first-claim right, including for a dispatch still in transit."""
    inbox = FieldInbox(db)
    with atomic(db):
        inbox.register(envelope)
        outcome = read_dispatch_outcome(db, envelope.dispatch_id)
        if outcome.status != 'waiting':
            return outcome
        inbox.store_refusal(envelope, 'superseded_dispatch')
        return FieldOutcome(envelope, 'refused', reason='superseded_dispatch')
