from dataclasses import dataclass

from review.field.inbox import FieldInbox
from review.field.receipt import Receipt, ReceiptLedger
from review.handoff.envelope import DispatchEnvelope


@dataclass(frozen=True)
class FieldOutcome:
    envelope: DispatchEnvelope
    status: str
    receipt: Receipt | None = None
    reason: str | None = None


def read_dispatch_outcome(db, dispatch_id):
    entry = FieldInbox(db).lookup(dispatch_id)
    if entry is None:
        return None
    if entry.refusal is not None:
        return FieldOutcome(entry.envelope, 'refused', reason=entry.refusal)
    receipt = ReceiptLedger(db).get_claim(dispatch_id)
    if receipt is not None:
        return FieldOutcome(entry.envelope, 'claimed', receipt=receipt)
    return FieldOutcome(entry.envelope, 'waiting')


def list_case_receipts(db, case_id):
    return ReceiptLedger(db).list_case_claims(case_id)
