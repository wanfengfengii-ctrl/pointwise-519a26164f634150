from dataclasses import dataclass

from review.handoff.envelope import DispatchEnvelope, envelope_from_row, envelope_values


@dataclass(frozen=True)
class Receipt:
    envelope: DispatchEnvelope
    sequence: int

    @property
    def receipt_id(self):
        return f'receipt-{self.sequence}'


def receipt_from_row(row):
    return Receipt(envelope_from_row(row), row['sequence'])


class ReceiptLedger:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS field_claims (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE REFERENCES field_inbox(dispatch_id),
                case_id TEXT NOT NULL,
                specimen_id TEXT NOT NULL,
                revision_id INTEGER NOT NULL,
                micrometres TEXT NOT NULL,
                UNIQUE (case_id, specimen_id)
            )
        ''')

    def record_claim(self, envelope):
        cursor = self.db.execute('''
            INSERT INTO field_claims(dispatch_id, case_id, specimen_id, revision_id, micrometres)
            SELECT dispatch_id, case_id, specimen_id, revision_id, micrometres FROM field_inbox
            WHERE dispatch_id = ? AND case_id = ? AND specimen_id = ? AND revision_id = ?
              AND micrometres = ? AND refusal IS NULL
        ''', envelope_values(envelope))
        if cursor.rowcount != 1:
            raise ValueError('dispatch has no matching first-claim eligibility')
        return Receipt(envelope, cursor.lastrowid)

    def get_claim(self, dispatch_id):
        row = self.db.execute('SELECT * FROM field_claims WHERE dispatch_id = ?', (dispatch_id,)).fetchone()
        return receipt_from_row(row) if row is not None else None

    def list_case_claims(self, case_id):
        rows = self.db.execute('SELECT * FROM field_claims WHERE case_id = ? ORDER BY sequence', (case_id,))
        return tuple(receipt_from_row(row) for row in rows)
