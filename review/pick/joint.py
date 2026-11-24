"""Joint review tasks: one task fixes the complete specimen ownership for several cases."""
from dataclasses import dataclass
import json
from itertools import combinations

from review.handoff.delivery import FieldUnavailable
from review.measure.catalog import MeasurementCatalog
from review.measure.reading import require_name, require_revision_number
from review.pick.case import CaseStore
from review.pick.dispatch import CASE_PLACES, DispatchLedger
from review.pick.ranking import rank_candidates
from review.storage import atomic


PLACES_PER_CASE = len(CASE_PLACES)


@dataclass(frozen=True)
class JointTask:
    task_id: str
    revision_id: int
    status: str


class JointTaskStore:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS joint_tasks (
                task_id TEXT PRIMARY KEY,
                revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'shortage'))
            );
            CREATE TABLE IF NOT EXISTS joint_task_cases (
                task_id TEXT NOT NULL REFERENCES joint_tasks(task_id),
                position INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                PRIMARY KEY (task_id, position),
                UNIQUE (task_id, case_id)
            );
            CREATE TABLE IF NOT EXISTS joint_candidates (
                task_id TEXT NOT NULL REFERENCES joint_tasks(task_id),
                case_id TEXT NOT NULL,
                specimen_id TEXT NOT NULL,
                PRIMARY KEY (task_id, case_id, specimen_id)
            );
            CREATE TABLE IF NOT EXISTS joint_case_bindings (
                task_id TEXT NOT NULL REFERENCES joint_tasks(task_id),
                case_id TEXT PRIMARY KEY REFERENCES review_cases(case_id)
            );
            CREATE TABLE IF NOT EXISTS joint_shortages (
                task_id TEXT PRIMARY KEY REFERENCES joint_tasks(task_id),
                case_ids TEXT NOT NULL,
                candidates TEXT NOT NULL
            );
        ''')

    def get_task(self, task_id):
        row = self.db.execute('SELECT * FROM joint_tasks WHERE task_id = ?', (task_id,)).fetchone()
        return JointTask(row['task_id'], row['revision_id'], row['status']) if row is not None else None

    def require_task(self, task_id):
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f'unknown joint task: {task_id}')
        return task

    def list_cases(self, task_id):
        rows = self.db.execute('''
            SELECT case_id FROM joint_task_cases WHERE task_id = ? ORDER BY position
        ''', (task_id,))
        return tuple(row['case_id'] for row in rows)

    def candidates_for(self, task_id, case_id):
        rows = self.db.execute('''
            SELECT specimen_id FROM joint_candidates WHERE task_id = ? AND case_id = ?
        ''', (task_id, case_id))
        return frozenset(row['specimen_id'] for row in rows)

    def task_of_case(self, case_id):
        row = self.db.execute('SELECT task_id FROM joint_case_bindings WHERE case_id = ?', (case_id,)).fetchone()
        return row['task_id'] if row is not None else None

    def list_task_dispatches(self, task_id):
        rows = self.db.execute('''
            SELECT dispatches.* FROM dispatches
            JOIN joint_case_bindings USING (case_id)
            JOIN joint_task_cases ON joint_task_cases.task_id = joint_case_bindings.task_id
                AND joint_task_cases.case_id = dispatches.case_id
            WHERE joint_case_bindings.task_id = ?
            ORDER BY joint_task_cases.position, dispatches.ordinal
        ''', (task_id,))
        ledger = DispatchLedger(self.db)
        return tuple(ledger._assignment(row) for row in rows)

    def save_shortage(self, task_id, revision_id, cases, candidate_sets, witness_cases, witness_candidates):
        with atomic(self.db):
            self.db.execute('INSERT INTO joint_tasks VALUES (?, ?, ?)', (task_id, revision_id, 'shortage'))
            self._save_request(task_id, tuple(case_id for case_id, _ in cases), candidate_sets)
            self.db.execute('INSERT INTO joint_shortages VALUES (?, ?, ?)',
                            (task_id, json.dumps(list(witness_cases)),
                             json.dumps(list(witness_candidates))))

    def get_shortage(self, task_id):
        row = self.db.execute('SELECT * FROM joint_shortages WHERE task_id = ?', (task_id,)).fetchone()
        if row is None:
            return None
        return {'status': 'shortage',
                'cases': tuple(json.loads(row['case_ids'])),
                'candidates': tuple(json.loads(row['candidates']))}

    def save_pending_allocation(self, task_id, revision_id, cases, candidate_sets, allocation, revision):
        with atomic(self.db):
            self.db.execute('INSERT INTO joint_tasks VALUES (?, ?, ?)', (task_id, revision_id, 'pending'))
            self._save_request(task_id, tuple(case_id for case_id, _ in cases), candidate_sets)
            case_store = CaseStore(self.db)
            ledger = DispatchLedger(self.db)
            readings = revision.by_specimen
            for position, (case_id, _) in enumerate(cases):
                case_store.create(case_id, revision_id)
                self.db.execute('INSERT INTO joint_case_bindings VALUES (?, ?)', (task_id, case_id))
                choices = []
                for slot, specimen_id in zip(CASE_PLACES, allocation[position]):
                    reading = readings[specimen_id]
                    choices.append((slot, reading))
                ledger.stage(case_id, revision_id, choices)

    def mark_ready(self, task_id):
        with atomic(self.db):
            changed = self.db.execute('''
                UPDATE joint_tasks SET status = 'ready' WHERE task_id = ? AND status = 'pending'
            ''', (task_id,)).rowcount
            if changed != 1:
                raise ValueError(f'joint task is not awaiting confirmation: {task_id}')

    def _save_request(self, task_id, case_ids, candidate_sets):
        for position, case_id in enumerate(case_ids):
            self.db.execute('INSERT INTO joint_task_cases(task_id, position, case_id) VALUES (?, ?, ?)',
                            (task_id, position, case_id))
            self.db.executemany('''
                INSERT INTO joint_candidates(task_id, case_id, specimen_id) VALUES (?, ?, ?)
            ''', ((task_id, case_id, specimen_id) for specimen_id in sorted(candidate_sets[case_id])))


def _normalize_request(cases):
    if not isinstance(cases, (tuple, list)) or not cases:
        raise ValueError('joint review requires a nonempty ordered sequence of cases')
    normalized = []
    seen_cases = set()
    for item in cases:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('each joint case must contain a case id and its candidate set')
        case_id, allowed = item
        require_name(case_id, 'case')
        if case_id in seen_cases:
            raise ValueError(f'duplicate joint case: {case_id}')
        seen_cases.add(case_id)
        if not isinstance(allowed, (tuple, list, frozenset, set)):
            raise ValueError('joint case candidates must be a sequence of specimen ids')
        candidate_ids = []
        seen_specimens = set()
        for specimen_id in allowed:
            require_name(specimen_id, 'specimen')
            if specimen_id in seen_specimens:
                raise ValueError(f'duplicate joint candidate for {case_id}: {specimen_id}')
            seen_specimens.add(specimen_id)
            candidate_ids.append(specimen_id)
        normalized.append((case_id, frozenset(candidate_ids)))
    return tuple(normalized)


def solve_allocation(candidate_sets, ranked_specimens):
    """Pick two specimens per case with task-wide exclusivity.

    The output pairs follow specimen priority and cases keep request order, so the
    first complete depth-first result is the lexicographically smallest ranking
    sequence among every feasible arrangement.
    """
    order = tuple(candidate_sets)
    chosen = [[] for _ in order]
    used = set()

    def remaining_feasible(case_index, open_slots):
        if open_slots and len(order[case_index] - used) < open_slots:
            return False
        for later in range(case_index + 1, len(order)):
            if len(order[later] - used) < PLACES_PER_CASE:
                return False
        return True

    def fill(case_index, slot):
        if case_index == len(order):
            return True
        open_slots = PLACES_PER_CASE - slot
        if not remaining_feasible(case_index, open_slots):
            return False
        for specimen_id in ranked_specimens:
            if specimen_id in used or specimen_id not in order[case_index]:
                continue
            used.add(specimen_id)
            chosen[case_index].append(specimen_id)
            next_case, next_slot = (case_index + 1, 0) if slot == PLACES_PER_CASE - 1 else (case_index, slot + 1)
            if fill(next_case, next_slot):
                return True
            chosen[case_index].pop()
            used.discard(specimen_id)
        return False

    if fill(0, 0):
        return tuple(tuple(pair) for pair in chosen)
    return None


def find_shortage_group(candidate_sets):
    """Return a case subset whose candidate union cannot fill two pieces per case."""
    for size in range(1, len(candidate_sets) + 1):
        for indexes in combinations(range(len(candidate_sets)), size):
            union = frozenset().union(*(candidate_sets[index] for index in indexes))
            if len(union) < PLACES_PER_CASE * size:
                return indexes, union
    return None


def open_joint(analysis, channel, task_id, revision_id, cases):
    require_name(task_id, 'task')
    require_revision_number(revision_id)
    normalized = _normalize_request(cases)
    store = JointTaskStore(analysis)
    existing = store.get_task(task_id)
    if existing is not None:
        if existing.revision_id != revision_id:
            raise ValueError('joint task identifier is already bound to a different revision')
        stored_signature = tuple((case_id, store.candidates_for(task_id, case_id))
                                 for case_id in store.list_cases(task_id))
        if stored_signature != normalized:
            raise ValueError('joint task identifier is already bound to different cases or candidates')
        if existing.status == 'shortage':
            return store.get_shortage(task_id)
        if existing.status == 'ready':
            return {'status': 'ready', 'assignments': store.list_task_dispatches(task_id)}
    else:
        revision = MeasurementCatalog(analysis).read_revision(revision_id)
        known = frozenset(revision.by_specimen)
        candidate_sets = tuple(allowed for _, allowed in normalized)
        for case_id, allowed in normalized:
            unknown = allowed - known
            if unknown:
                raise ValueError(f'unknown joint candidate for {case_id}: {sorted(unknown)[0]}')
        case_store = CaseStore(analysis)
        for case_id, _ in normalized:
            if case_store.get(case_id) is not None:
                raise ValueError(f'case already exists: {case_id}')
        ranked = tuple(reading.specimen_id for reading in rank_candidates(revision))
        allocation = solve_allocation(candidate_sets, ranked)
        candidate_map = {case_id: allowed for case_id, allowed in normalized}
        if allocation is None:
            indexes, union = find_shortage_group(candidate_sets)
            witness_cases = tuple(normalized[index][0] for index in indexes)
            witness_candidates = tuple(specimen_id for specimen_id in ranked if specimen_id in union)
            store.save_shortage(task_id, revision_id, normalized, candidate_map,
                                witness_cases, witness_candidates)
            return store.get_shortage(task_id)
        store.save_pending_allocation(task_id, revision_id, normalized, candidate_map, allocation, revision)

    task = store.require_task(task_id)
    if task.status != 'pending':
        raise ValueError(f'joint task cannot be confirmed: {task_id}')
    assignments = store.list_task_dispatches(task_id)
    try:
        channel.publish_joint(task_id, revision_id, assignments)
    except FieldUnavailable:
        return {'status': 'pending'}
    store.mark_ready(task_id)
    return {'status': 'ready', 'assignments': store.list_task_dispatches(task_id)}
