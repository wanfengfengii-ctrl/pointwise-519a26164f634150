from dataclasses import dataclass

from review.handoff.envelope import DispatchEnvelope, envelope_from_row, envelope_values


CASE_PLACES = (1, 2)


@dataclass(frozen=True)
class DispatchAssignment:
    slot: int
    ordinal: int
    envelope: DispatchEnvelope


class DispatchLedger:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES review_cases(case_id),
                specimen_id TEXT NOT NULL,
                revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                micrometres TEXT NOT NULL,
                slot INTEGER NOT NULL CHECK (slot IN (1, 2)),
                ordinal INTEGER NOT NULL,
                UNIQUE (case_id, ordinal),
                UNIQUE (case_id, revision_id, slot)
            )
        ''')

    @staticmethod
    def _assignment(row):
        return DispatchAssignment(row['slot'], row['ordinal'], envelope_from_row(row))

    def stage(self, case_id, revision_id, choices):
        """Allocate immutable identities inside the caller's case transaction."""
        next_ordinal = self.db.execute('''
            SELECT COALESCE(MAX(ordinal), 0) + 1 FROM dispatches WHERE case_id = ?
        ''', (case_id,)).fetchone()[0]
        staged = []
        for ordinal, (slot, reading) in enumerate(choices, start=next_ordinal):
            envelope = DispatchEnvelope(f'{case_id}/{ordinal}', case_id, reading.specimen_id,
                                        revision_id, reading.micrometres)
            self.db.execute('''
                INSERT INTO dispatches(dispatch_id, case_id, specimen_id, revision_id,
                                       micrometres, slot, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (*envelope_values(envelope), slot, ordinal))
            staged.append(DispatchAssignment(slot, ordinal, envelope))
        return tuple(staged)

    def get(self, dispatch_id):
        row = self.db.execute('SELECT * FROM dispatches WHERE dispatch_id = ?', (dispatch_id,)).fetchone()
        if row is None:
            raise ValueError(f'unknown dispatch: {dispatch_id}')
        return self._assignment(row)

    def list_case_dispatches(self, case_id, *, revision_id=None):
        if revision_id is None:
            rows = self.db.execute('SELECT * FROM dispatches WHERE case_id = ? ORDER BY ordinal', (case_id,))
        else:
            rows = self.db.execute('''
                SELECT * FROM dispatches WHERE case_id = ? AND revision_id = ? ORDER BY ordinal
            ''', (case_id, revision_id))
        return tuple(self._assignment(row) for row in rows)
