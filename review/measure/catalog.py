from dataclasses import replace

from review.measure.batch import BatchLedger
from review.measure.reading import Reading, require_name, require_revision_number
from review.measure.revision import Revision, build_revision
from review.storage import atomic


class MeasurementCatalog:
    def __init__(self, db):
        self.db = db
        self.batches = BatchLedger(db)

    def install(self):
        self.batches.install()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS revisions (
                revision_id INTEGER PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                parent_id INTEGER REFERENCES revisions(revision_id)
            );
            CREATE TABLE IF NOT EXISTS readings (
                revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                ordinal INTEGER NOT NULL,
                specimen_id TEXT NOT NULL,
                micrometres TEXT NOT NULL,
                PRIMARY KEY (revision_id, specimen_id),
                UNIQUE (revision_id, ordinal)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_root_per_batch
                ON revisions(batch_id) WHERE parent_id IS NULL;
        ''')

    def _append(self, draft):
        self.batches.ensure_batch(draft.batch_id)
        if self.batches.head(draft.batch_id) != draft.parent_id:
            raise ValueError('parent revision is not the current batch revision')
        cursor = self.db.execute('INSERT INTO revisions(batch_id, parent_id) VALUES (?, ?)',
                                 (draft.batch_id, draft.parent_id))
        revision_id = cursor.lastrowid
        self.db.executemany('INSERT INTO readings VALUES (?, ?, ?, ?)', (
            (revision_id, ordinal, reading.specimen_id, str(reading.micrometres))
            for ordinal, reading in enumerate(draft.readings)
        ))
        self.batches.advance(draft.batch_id, draft.parent_id, revision_id)
        return replace(draft, revision_id=revision_id)

    def record(self, batch_id, parent_id, readings):
        draft = build_revision(batch_id, parent_id, readings)
        with atomic(self.db):
            return self._append(draft)

    def read_revision(self, revision_id):
        require_revision_number(revision_id)
        row = self.db.execute('SELECT * FROM revisions WHERE revision_id = ?', (revision_id,)).fetchone()
        if row is None:
            raise ValueError(f'unknown revision: {revision_id}')
        readings = tuple(Reading(item['specimen_id'], item['micrometres']) for item in self.db.execute('''
            SELECT specimen_id, micrometres FROM readings WHERE revision_id = ? ORDER BY ordinal
        ''', (revision_id,)))
        return Revision(row['batch_id'], row['parent_id'], readings, revision_id)

    def parent_of(self, revision_id):
        row = self.db.execute('SELECT parent_id FROM revisions WHERE revision_id = ?', (revision_id,)).fetchone()
        if row is None:
            raise ValueError(f'unknown revision: {revision_id}')
        return row['parent_id']

    def current_revision(self, batch_id):
        require_name(batch_id, 'batch')
        head = self.batches.head(batch_id)
        return self.read_revision(head) if head is not None else None

    def ensure_sample(self, batch_id, readings):
        draft = build_revision(batch_id, None, readings)
        with atomic(self.db):
            root = self.db.execute('''
                SELECT revision_id FROM revisions WHERE batch_id = ? AND parent_id IS NULL
            ''', (batch_id,)).fetchone()
            if root is not None:
                return self.read_revision(root['revision_id'])
            return self._append(draft)
