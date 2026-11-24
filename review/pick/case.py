from dataclasses import dataclass

from review.measure.catalog import MeasurementCatalog
from review.measure.reading import require_name
from review.pick.dispatch import CASE_PLACES, DispatchLedger
from review.pick.ranking import select_candidates
from review.storage import atomic


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    initial_revision: int
    effective_revision: int


class CaseStore:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS review_cases (
                case_id TEXT PRIMARY KEY,
                initial_revision INTEGER NOT NULL REFERENCES revisions(revision_id),
                effective_revision INTEGER NOT NULL REFERENCES revisions(revision_id)
            )
        ''')

    def get(self, case_id):
        row = self.db.execute('SELECT * FROM review_cases WHERE case_id = ?', (case_id,)).fetchone()
        return ReviewCase(**dict(row)) if row is not None else None

    def require(self, case_id):
        case = self.get(case_id)
        if case is None:
            raise ValueError(f'unknown case: {case_id}')
        return case

    def create(self, case_id, revision_id):
        if self.get(case_id) is not None:
            raise ValueError(f'case already exists: {case_id}')
        self.db.execute('INSERT INTO review_cases VALUES (?, ?, ?)', (case_id, revision_id, revision_id))

    def set_effective_revision(self, case_id, expected_revision, target_revision):
        updated = self.db.execute('''
            UPDATE review_cases SET effective_revision = ? WHERE case_id = ? AND effective_revision = ?
        ''', (target_revision, case_id, expected_revision)).rowcount
        if updated != 1:
            raise ValueError('case revision changed before correction publication')


def open(db, case_id, revision_id):
    require_name(case_id, 'case')
    revision = MeasurementCatalog(db).read_revision(revision_id)
    selection = select_candidates(revision, len(CASE_PLACES))
    if len(selection) != len(CASE_PLACES):
        raise ValueError('opening a case requires two eligible specimens')
    with atomic(db):
        CaseStore(db).create(case_id, revision_id)
        return DispatchLedger(db).stage(case_id, revision_id, zip(CASE_PLACES, selection))
