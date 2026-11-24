from dataclasses import dataclass

from review.field.receipt import Receipt, receipt_from_row
from review.handoff.envelope import same_dispatch
from review.pick.dispatch import CASE_PLACES, DispatchLedger


@dataclass(frozen=True)
class CaseProgress:
    case_id: str
    receipts: tuple[Receipt, ...]
    vacant_slots: tuple[int, ...]

    @property
    def claimed_specimens(self):
        return frozenset(receipt.envelope.specimen_id for receipt in self.receipts)


class ProgressLedger:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS case_claims (
                case_id TEXT NOT NULL REFERENCES review_cases(case_id),
                slot INTEGER NOT NULL CHECK (slot IN (1, 2)),
                dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatches(dispatch_id),
                specimen_id TEXT NOT NULL,
                sequence INTEGER NOT NULL UNIQUE,
                PRIMARY KEY (case_id, slot),
                UNIQUE (case_id, specimen_id)
            )
        ''')

    def snapshot(self, case_id):
        rows = tuple(self.db.execute('''
            SELECT dispatches.*, case_claims.sequence FROM case_claims
            JOIN dispatches USING (dispatch_id)
            WHERE case_claims.case_id = ? ORDER BY case_claims.sequence
        ''', (case_id,)))
        occupied = {row['slot'] for row in rows}
        return CaseProgress(case_id, tuple(receipt_from_row(row) for row in rows),
                            tuple(slot for slot in CASE_PLACES if slot not in occupied))

    def apply_receipt(self, receipt):
        envelope = receipt.envelope
        assignment = DispatchLedger(self.db).get(envelope.dispatch_id)
        if not same_dispatch(assignment.envelope, envelope):
            raise ValueError('receipt does not match the original case assignment')
        existing = self.db.execute('''
            SELECT dispatch_id, sequence FROM case_claims WHERE case_id = ? AND slot = ?
        ''', (envelope.case_id, assignment.slot)).fetchone()
        if existing is not None:
            if (existing['dispatch_id'], existing['sequence']) != (envelope.dispatch_id, receipt.sequence):
                raise ValueError('the case slot already has a different receipt')
            return
        self.db.execute('INSERT INTO case_claims VALUES (?, ?, ?, ?, ?)', (
            envelope.case_id, assignment.slot, envelope.dispatch_id, envelope.specimen_id, receipt.sequence
        ))
