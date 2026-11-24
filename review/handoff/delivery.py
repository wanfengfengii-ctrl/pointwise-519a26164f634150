from review.field.acceptance import receive, retire_dispatch
from review.field.handover import read_dispatch_outcome
from review.field.inbox import FieldInbox
from review.field.joint import JointManifest, gate_legacy_send, joint_permit, send_joint
from review.handoff.envelope import same_dispatch
from review.storage import atomic


class FieldUnavailable(ConnectionError):
    pass


class DeliveryChannel:
    def __init__(self, field_db):
        self.field_db = field_db

    def _connection(self):
        if self.field_db is None:
            raise FieldUnavailable('field workstation is offline')
        return self.field_db

    def send(self, envelope):
        field = self._connection()
        if gate_legacy_send(field, envelope):
            return read_dispatch_outcome(field, envelope.dispatch_id)
        with atomic(field):
            FieldInbox(field).register(envelope)
            return read_dispatch_outcome(field, envelope.dispatch_id)

    def send_joint(self, task_id, envelope, permit=None):
        return send_joint(self._connection(), task_id, envelope, permit)

    def publish_joint(self, task_id, revision_id, assignments):
        field = self._connection()
        JointManifest(field).publish(task_id, revision_id, assignments)
        return joint_permit(task_id, assignments[0].envelope.dispatch_id)

    def claim(self, envelope):
        return receive(self._connection(), envelope)

    def fetch_outcome(self, envelope):
        outcome = read_dispatch_outcome(self._connection(), envelope.dispatch_id)
        if outcome is not None and not same_dispatch(envelope, outcome.envelope):
            raise ValueError('field dispatch has different inputs')
        return outcome

    def retire(self, envelope):
        return retire_dispatch(self._connection(), envelope)


class DeliveryJournal:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                attempt_id INTEGER PRIMARY KEY,
                dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
                operation TEXT NOT NULL,
                status TEXT NOT NULL
            )
        ''')

    def record_attempt(self, dispatch_id, operation):
        cursor = self.db.execute('''
            INSERT INTO delivery_attempts(dispatch_id, operation, status) VALUES (?, ?, 'unconfirmed')
        ''', (dispatch_id, operation))
        return cursor.lastrowid

    def finish(self, attempt_id, status):
        self.db.execute('UPDATE delivery_attempts SET status = ? WHERE attempt_id = ?', (status, attempt_id))

    def for_dispatch(self, dispatch_id):
        rows = self.db.execute('''
            SELECT attempt_id, dispatch_id, operation, status FROM delivery_attempts
            WHERE dispatch_id = ? ORDER BY attempt_id
        ''', (dispatch_id,))
        return tuple(dict(row) for row in rows)


def exchange(db, envelope, operation, perform):
    journal = DeliveryJournal(db)
    with atomic(db):
        attempt = journal.record_attempt(envelope.dispatch_id, operation)
    try:
        outcome = perform(envelope)
    except FieldUnavailable:
        outcome = None
    except ValueError:
        with atomic(db):
            journal.finish(attempt, 'binding_rejected')
        raise
    status = 'unknown' if outcome is None else outcome.status
    if operation == 'send' and status == 'waiting':
        status = 'delivered'
    with atomic(db):
        journal.finish(attempt, status)
    return outcome
