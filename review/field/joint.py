"""Field-side ownership manifest and handoff gate for joint review tasks."""
from review.field.handover import read_dispatch_outcome
from review.field.inbox import FieldInbox
from review.handoff.envelope import DispatchEnvelope, envelope_values, same_dispatch
from review.storage import atomic


def joint_permit(task_id, dispatch_id):
    return f'{task_id}:{dispatch_id}'


class JointManifest:
    """The fixed, workbench-produced ownership that the field trusts."""

    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS joint_manifests (
                task_id TEXT PRIMARY KEY,
                revision_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS joint_manifest_entries (
                task_id TEXT NOT NULL REFERENCES joint_manifests(task_id),
                position INTEGER NOT NULL,
                dispatch_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                specimen_id TEXT NOT NULL,
                revision_id INTEGER NOT NULL,
                micrometres TEXT NOT NULL,
                permit TEXT NOT NULL,
                PRIMARY KEY (task_id, dispatch_id),
                UNIQUE (task_id, case_id, specimen_id)
            );
        ''')

    def publish(self, task_id, revision_id, assignments):
        with atomic(self.db):
            existing = self.db.execute('SELECT revision_id FROM joint_manifests WHERE task_id = ?',
                                       (task_id,)).fetchone()
            if existing is None:
                self.db.execute('INSERT INTO joint_manifests VALUES (?, ?)', (task_id, revision_id))
            elif existing['revision_id'] != revision_id:
                raise ValueError('joint task manifest is already bound to a different revision')
            for position, assignment in enumerate(assignments):
                envelope = assignment.envelope
                values = (position, *envelope_values(envelope), joint_permit(task_id, envelope.dispatch_id))
                row = self.db.execute('''
                    SELECT position, dispatch_id, case_id, specimen_id, revision_id, micrometres, permit
                    FROM joint_manifest_entries WHERE task_id = ? AND dispatch_id = ?
                ''', (task_id, envelope.dispatch_id)).fetchone()
                if row is None:
                    self.db.execute('''
                        INSERT INTO joint_manifest_entries
                            (task_id, position, dispatch_id, case_id, specimen_id,
                             revision_id, micrometres, permit)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (task_id, *values))
                elif tuple(row) != values:
                    raise ValueError('joint manifest entry is already bound to different inputs')

    def lookup_dispatch(self, dispatch_id):
        row = self.db.execute('''
            SELECT task_id, dispatch_id, case_id, specimen_id, revision_id, micrometres, permit
            FROM joint_manifest_entries WHERE dispatch_id = ?
        ''', (dispatch_id,)).fetchone()
        if row is None:
            return None
        envelope = DispatchEnvelope(row['dispatch_id'], row['case_id'], row['specimen_id'],
                                    row['revision_id'], row['micrometres'])
        return {'task_id': row['task_id'], 'envelope': envelope, 'permit': row['permit']}

    def lookup_task(self, task_id):
        rows = self.db.execute('''
            SELECT dispatch_id, case_id, specimen_id, revision_id, micrometres, permit
            FROM joint_manifest_entries WHERE task_id = ? ORDER BY position, dispatch_id
        ''', (task_id,))
        return tuple(dict(row) for row in rows)


def send_joint(field, task_id, envelope, permit=None):
    """Admit one joint dispatch only when it matches the fixed ownership manifest."""
    manifest = JointManifest(field)
    entry = manifest.lookup_dispatch(envelope.dispatch_id)
    if entry is None:
        # The full ownership manifest has not reached this field workstation yet:
        # nothing may be admitted, and the original send can be retried later.
        return None
    if entry['task_id'] != task_id:
        raise ValueError('joint dispatch belongs to a different task')
    if permit is not None and permit != entry['permit']:
        raise ValueError('joint handover permit does not match the fixed dispatch')
    if not same_dispatch(entry['envelope'], envelope):
        # Explicit misroute: reject without occupying the real dispatch's binding.
        raise ValueError('joint dispatch content does not match the fixed ownership')
    with atomic(field):
        FieldInbox(field).register(envelope)
        return read_dispatch_outcome(field, envelope.dispatch_id)


def gate_legacy_send(field, envelope):
    """Legacy delivery path must obey the same joint ownership checks."""
    entry = JointManifest(field).lookup_dispatch(envelope.dispatch_id)
    if entry is None:
        return False
    if not same_dispatch(entry['envelope'], envelope):
        raise ValueError('joint dispatch content does not match the fixed ownership')
    with atomic(field):
        FieldInbox(field).register(envelope)
    return True
