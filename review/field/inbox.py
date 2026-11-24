from dataclasses import dataclass

from review.handoff.envelope import DispatchEnvelope, envelope_from_row, envelope_values, same_dispatch


@dataclass(frozen=True)
class InboxEntry:
    envelope: DispatchEnvelope
    refusal: str | None


class FieldInbox:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS field_inbox (
                dispatch_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                specimen_id TEXT NOT NULL,
                revision_id INTEGER NOT NULL,
                micrometres TEXT NOT NULL,
                refusal TEXT
            )
        ''')

    def register(self, envelope):
        self.db.execute('''
            INSERT INTO field_inbox(dispatch_id, case_id, specimen_id, revision_id, micrometres)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(dispatch_id) DO NOTHING
        ''', envelope_values(envelope))
        return self.require_binding(envelope)

    def lookup(self, dispatch_id):
        row = self.db.execute('SELECT * FROM field_inbox WHERE dispatch_id = ?', (dispatch_id,)).fetchone()
        if row is None:
            return None
        return InboxEntry(envelope_from_row(row), row['refusal'])

    def require_binding(self, envelope):
        entry = self.lookup(envelope.dispatch_id)
        if entry is None:
            raise ValueError('dispatch has not arrived at the field workstation')
        if not same_dispatch(entry.envelope, envelope):
            raise ValueError('dispatch identifier is already bound to different inputs')
        return entry

    def store_refusal(self, envelope, reason):
        entry = self.require_binding(envelope)
        if entry.refusal is not None and entry.refusal != reason:
            raise ValueError('dispatch already has a different terminal refusal')
        changed = self.db.execute('''
            UPDATE field_inbox SET refusal = ? WHERE dispatch_id = ? AND NOT EXISTS (
                SELECT 1 FROM field_claims WHERE dispatch_id = ?
            )
        ''', (reason, envelope.dispatch_id, envelope.dispatch_id)).rowcount
        if changed != 1:
            raise ValueError('a claimed dispatch cannot be refused')
