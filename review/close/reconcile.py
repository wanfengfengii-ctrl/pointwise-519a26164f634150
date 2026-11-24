from review.field.receipt import Receipt
from review.handoff.ack import AckLedger


def reconcile_receipts(db, case_id, evidence):
    try:
        supplied = tuple(evidence)
    except TypeError:
        return None
    if len(supplied) != 2:
        return None
    ledger = AckLedger(db)
    verified = []
    for entry in supplied:
        if isinstance(entry, Receipt):
            actual = ledger.find_receipt(entry.receipt_id)
            if actual != entry:
                return None
        elif isinstance(entry, str):
            actual = ledger.find_receipt(entry)
        else:
            return None
        if actual is None or actual.envelope.case_id != case_id:
            return None
        verified.append(actual)
    left, right = verified
    if left.receipt_id == right.receipt_id or left.envelope.specimen_id == right.envelope.specimen_id:
        return None
    return tuple(sorted(verified, key=lambda receipt: receipt.sequence))
