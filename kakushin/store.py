"""SQLite run journal and private original-file archive, single-machine initial deployment."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .master import RULE_VERSION


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sources = self.root / 'sources'
        self.sources.mkdir(exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, input_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, source_sha TEXT NOT NULL,
                result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, reference TEXT NOT NULL, details TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING_REVIEW');
              CREATE TABLE IF NOT EXISTS notifications (
                key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT');
            ''')
        os.chmod(self.root / 'operations.sqlite3', 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / 'operations.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def execute(self, data, month, completeness, business_date, compute):
        sha = hashlib.sha256(data).hexdigest()
        key = hashlib.sha256(json.dumps([sha, month, completeness, business_date, RULE_VERSION]).encode()).hexdigest()
        # Archive is content addressed; original bytes never enter normal audit logs.
        path = self.sources / (sha + '.csv')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RuntimeError('原本アーカイブの整合性エラー')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT result FROM runs WHERE input_key=?', (key,)).fetchone()
            if old:
                return json.loads(old['result']), True
            result = compute()
            result['run_id'] = uuid.uuid4().hex
            encoded = json.dumps(result, ensure_ascii=False)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', (result['run_id'], key, self.now(), sha, encoded))
            db.execute('INSERT INTO audit(at,actor,action,reference,details) VALUES (?,?,?,?,?)',
                       (self.now(), 'local-operator', 'SHADOW_RUN', result['run_id'],
                        json.dumps(dict(source_sha=sha, rule_version=RULE_VERSION))))
        return result, False

    def list_runs(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT id,created_at,source_sha FROM runs ORDER BY created_at DESC LIMIT 50')]

    def get_run(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
            return json.loads(row['result']) if row else None

    def accept_event(self, event_id, payload):
        with self.connection() as db:
            result = db.execute('INSERT OR IGNORE INTO inbox(event_id,received_at,payload) VALUES (?,?,?)',
                                (event_id, self.now(), json.dumps(payload, ensure_ascii=False)))
            return result.rowcount == 1

    def backup(self, destination):
        """Consistent SQLite backup; source CSV archive must be copied separately."""
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        os.chmod(destination, 0o600)
