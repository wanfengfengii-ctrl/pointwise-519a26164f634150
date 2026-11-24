class BatchLedger:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                head INTEGER
            )
        ''')

    def ensure_batch(self, batch_id):
        self.db.execute('INSERT OR IGNORE INTO batches(batch_id) VALUES (?)', (batch_id,))

    def head(self, batch_id):
        row = self.db.execute('SELECT head FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
        return row['head'] if row is not None else None

    def advance(self, batch_id, expected_parent, revision_id):
        changed = self.db.execute('''
            UPDATE batches SET head = ? WHERE batch_id = ? AND head IS ?
        ''', (revision_id, batch_id, expected_parent)).rowcount
        if changed != 1:
            raise ValueError('parent revision is not the current batch revision')
