from dataclasses import dataclass
import json

from review.handoff.ack import AckLedger, apply_field_outcome
from review.handoff.delivery import exchange
from review.measure.catalog import MeasurementCatalog
from review.measure.correction import check_correction_lineage
from review.measure.reading import require_name, require_revision_number
from review.pick.case import CaseStore
from review.pick.dispatch import DispatchAssignment, DispatchLedger
from review.pick.joint import JointTaskStore
from review.pick.progress import ProgressLedger
from review.pick.ranking import select_candidates
from review.storage import atomic


@dataclass(frozen=True)
class CorrectionResult:
    request_id: str
    case_id: str
    base_revision: int
    revision_id: int
    status: str
    dispatches: tuple[DispatchAssignment, ...]


class CorrectionStore:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS corrections (
                request_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES review_cases(case_id),
                base_revision INTEGER NOT NULL REFERENCES revisions(revision_id),
                revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                status TEXT NOT NULL CHECK (status IN ('pending', 'effective')),
                dispatch_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_correction
                ON corrections(case_id) WHERE status = 'pending';
        ''')

    def find(self, request_id):
        row = self.db.execute('SELECT * FROM corrections WHERE request_id = ?', (request_id,)).fetchone()
        if row is None:
            return None
        dispatches = DispatchLedger(self.db)
        assignments = tuple(dispatches.get(identity) for identity in json.loads(row['dispatch_ids']))
        return CorrectionResult(row['request_id'], row['case_id'], row['base_revision'],
                                row['revision_id'], row['status'], assignments)

    def save_pending(self, request_id, case_id, base_revision, revision_id):
        other = self.db.execute('''
            SELECT request_id FROM corrections WHERE case_id = ? AND status = 'pending'
        ''', (case_id,)).fetchone()
        if other is not None:
            raise ValueError('case already has a pending correction')
        self.db.execute('''
            INSERT INTO corrections(request_id, case_id, base_revision, revision_id, status)
            VALUES (?, ?, ?, ?, 'pending')
        ''', (request_id, case_id, base_revision, revision_id))
        return self.find(request_id)

    def save_effective(self, request_id, assignments):
        identities = [item.envelope.dispatch_id for item in assignments]
        changed = self.db.execute('''
            UPDATE corrections SET status = 'effective', dispatch_ids = ?
            WHERE request_id = ? AND status = 'pending'
        ''', (json.dumps(identities), request_id)).rowcount
        if changed != 1:
            raise ValueError('correction is no longer pending')
        return self.find(request_id)


def correct(db, channel, case_id, request_id, revision_id):
    require_name(case_id, 'case')
    require_name(request_id, 'correction request')
    require_revision_number(revision_id)
    catalog = MeasurementCatalog(db)
    cases = CaseStore(db)
    dispatches = DispatchLedger(db)
    corrections = CorrectionStore(db)
    acknowledgments = AckLedger(db)
    with atomic(db):
        case = cases.require(case_id)
        if JointTaskStore(db).task_of_case(case_id) is not None:
            raise ValueError('joint review cases cannot be replanned by correction')
        request = corrections.find(request_id)
        if request is None:
            check_correction_lineage(catalog, case.effective_revision, revision_id)
            request = corrections.save_pending(request_id, case_id, case.effective_revision, revision_id)
        elif (request.case_id, request.revision_id) != (case_id, revision_id):
            raise ValueError('correction identifier is already bound to different inputs')
        if request.status == 'effective':
            return request
        old_assignments = dispatches.list_case_dispatches(case_id, revision_id=request.base_revision)

    # Each terminal answer survives in the field even if the caller loses it.
    for assignment in old_assignments:
        envelope = assignment.envelope
        if acknowledgments.get_outcome(envelope.dispatch_id) is not None:
            continue
        outcome = exchange(db, envelope, 'retire', channel.retire)
        apply_field_outcome(db, envelope, outcome)

    with atomic(db):
        request = corrections.find(request_id)
        if request.status == 'effective':
            return request
        if any(acknowledgments.get_outcome(item.envelope.dispatch_id) is None for item in old_assignments):
            return request
        progress = ProgressLedger(db).snapshot(case_id)
        picks = select_candidates(catalog.read_revision(revision_id), len(progress.vacant_slots),
                                  progress.claimed_specimens)
        replacements = dispatches.stage(case_id, revision_id, zip(progress.vacant_slots, picks))
        cases.set_effective_revision(case_id, request.base_revision, revision_id)
        return corrections.save_effective(request_id, replacements)
