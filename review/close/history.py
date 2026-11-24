from fractions import Fraction

from review.close.report import Report, ReportLine


class ReportArchive:
    def __init__(self, db):
        self.db = db

    def install(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS reports (
                case_id TEXT PRIMARY KEY REFERENCES review_cases(case_id),
                mean_micrometres TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_lines (
                case_id TEXT NOT NULL REFERENCES reports(case_id),
                sequence INTEGER NOT NULL,
                specimen_id TEXT NOT NULL,
                revision_id INTEGER NOT NULL,
                micrometres TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                PRIMARY KEY (case_id, sequence),
                UNIQUE (case_id, receipt_id),
                UNIQUE (case_id, specimen_id)
            );
        ''')

    def store(self, report):
        """Persist a complete report in the caller's publication transaction."""
        self.db.execute('INSERT INTO reports VALUES (?, ?)', (report.case_id, str(report.mean_micrometres)))
        self.db.executemany('INSERT INTO report_lines VALUES (?, ?, ?, ?, ?, ?, ?)', (
            (report.case_id, line.sequence, line.specimen_id, line.revision_id, str(line.micrometres),
             line.dispatch_id, line.receipt_id) for line in report.lines
        ))
        return report

    def get(self, case_id):
        head = self.db.execute('SELECT mean_micrometres FROM reports WHERE case_id = ?', (case_id,)).fetchone()
        if head is None:
            return None
        rows = self.db.execute('SELECT * FROM report_lines WHERE case_id = ? ORDER BY sequence', (case_id,))
        lines = tuple(ReportLine(row['specimen_id'], row['revision_id'], Fraction(row['micrometres']),
                                 row['dispatch_id'], row['receipt_id'], row['sequence']) for row in rows)
        return Report(case_id, lines, Fraction(head['mean_micrometres']))


def read(db, case_id):
    return ReportArchive(db).get(case_id)
