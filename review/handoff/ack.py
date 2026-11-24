from review.field.handover import FieldOutcome
from review.field.receipt import Receipt, receipt_from_row
from review.handoff.envelope import envelope_from_row, same_dispatch
from review.pick.dispatch import DispatchLedger
from review.pick.progress import ProgressLedger
from review.storage import atomic


def verify_receipt(envelope, receipt):
    return (isinstance(receipt, Receipt) and type(receipt.sequence) is int and receipt.sequence > 0
            and same_dispatch(envelope, receipt.envelope))


class AckLedger:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS verified_outcomes (
                dispatch_id TEXT PRIMARY KEY REFERENCES dispatches(dispatch_id),
                status TEXT NOT NULL,
                reason TEXT,
                sequence INTEGER UNIQUE,
                receipt_id TEXT UNIQUE,
                CHECK ((status = 'claimed' AND sequence IS NOT NULL AND receipt_id IS NOT NULL AND reason IS NULL)
                    OR (status = 'refused' AND sequence IS NULL AND receipt_id IS NULL AND reason IS NOT NULL))
            )
        ''')

    def record_outcome(self, envelope, outcome):
        assignment = DispatchLedger(self.db).get(envelope.dispatch_id)
        if not same_dispatch(assignment.envelope, envelope) or not same_dispatch(envelope, outcome.envelope):
            raise ValueError('field outcome does not match the named dispatch')
        sequence = receipt_id = None
        if outcome.status == 'claimed':
            if not verify_receipt(envelope, outcome.receipt) or outcome.reason is not None:
                raise ValueError('field receipt does not match the named dispatch')
            sequence = outcome.receipt.sequence
            receipt_id = outcome.receipt.receipt_id
        elif outcome.status == 'refused':
            if not outcome.reason or outcome.receipt is not None:
                raise ValueError('field refusal lacks a valid terminal result')
        elif outcome.status == 'waiting' and outcome.receipt is None and outcome.reason is None:
            return
        else:
            raise ValueError('invalid field outcome')
        existing = self.get_outcome(envelope.dispatch_id)
        if existing is not None:
            if existing != outcome:
                raise ValueError('field outcome conflicts with the previously verified result')
            return
        self.db.execute('INSERT INTO verified_outcomes VALUES (?, ?, ?, ?, ?)',
                        (envelope.dispatch_id, outcome.status, outcome.reason, sequence, receipt_id))

    def get_outcome(self, dispatch_id):
        row = self.db.execute('''
            SELECT dispatches.*, verified_outcomes.status, verified_outcomes.reason, verified_outcomes.sequence
            FROM verified_outcomes JOIN dispatches USING (dispatch_id)
            WHERE dispatch_id = ?
        ''', (dispatch_id,)).fetchone()
        if row is None:
            return None
        receipt = receipt_from_row(row) if row['status'] == 'claimed' else None
        return FieldOutcome(envelope_from_row(row), row['status'], receipt, row['reason'])

    def get_verified_receipt(self, dispatch_id):
        outcome = self.get_outcome(dispatch_id)
        return outcome.receipt if outcome is not None else None

    def find_receipt(self, receipt_id):
        row = self.db.execute('''
            SELECT dispatches.*, verified_outcomes.sequence FROM verified_outcomes
            JOIN dispatches USING (dispatch_id) WHERE verified_outcomes.receipt_id = ?
        ''', (receipt_id,)).fetchone()
        return receipt_from_row(row) if row is not None else None


def apply_field_outcome(db, envelope, outcome):
    if outcome is None:
        return
    with atomic(db):
        AckLedger(db).record_outcome(envelope, outcome)
        if outcome.status == 'claimed':
            ProgressLedger(db).apply_receipt(outcome.receipt)
