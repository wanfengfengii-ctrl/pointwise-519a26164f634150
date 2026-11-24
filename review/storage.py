from contextlib import contextmanager
from pathlib import Path
import sqlite3


def connect(path, *, create=False):
    """Open an independent workstation store with explicit transactions."""
    mode = 'rwc' if create else 'rw'
    uri = f'{Path(path).resolve().as_uri()}?mode={mode}'
    db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


@contextmanager
def atomic(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
