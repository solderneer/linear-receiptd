"""Durable assignment queue with an explicit boundary before the first BLE write."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def assignment(payload, user_id):
    if payload.get("type") != "Issue" or payload.get("action") not in ("create", "update"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("assigneeId") != user_id or not data.get("id"):
        return None
    old = payload.get("updatedFrom") or {}
    if not isinstance(old, dict):
        return None
    if payload["action"] == "update" and ("assigneeId" not in old or old["assigneeId"] == user_id):
        return None
    # Use the assignment's object timestamp, not its delivery timestamp or the
    # subscription ID (webhookId). Retries may change delivery metadata.
    changed_at = data.get("updatedAt") or payload.get("createdAt")
    if not isinstance(changed_at, str) or not changed_at:
        raise ValueError("Assignment has no event timestamp")
    identity = [payload.get("organizationId"), data["id"], payload["action"], changed_at, old.get("assigneeId"), user_id]
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return str(data["id"]), key


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY, received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, assignment_key TEXT UNIQUE NOT NULL,
                    issue_id TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    error TEXT, receipt_path TEXT, snapshot TEXT, sent_at REAL
                );
                CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO controls VALUES ('paused', '1');
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, delivery_id, payload, user_id):
        target = assignment(payload, user_id)
        with self.connect() as db:
            if not db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?)", (delivery_id, time.time())).rowcount:
                return "duplicate"
            if target is None:
                return "ignored"
            issue_id, key = target
            inserted = db.execute(
                "INSERT OR IGNORE INTO jobs (assignment_key, issue_id, source, created_at) VALUES (?, ?, 'webhook', ?)",
                (key, issue_id, time.time()),
            ).rowcount
            return "queued" if inserted else "duplicate"

    def enqueue(self, issue_id):
        with self.connect() as db:
            return db.execute(
                "INSERT INTO jobs (assignment_key, issue_id, source, created_at) VALUES (?, ?, 'manual', ?)",
                (f"manual:{uuid.uuid4()}", issue_id, time.time()),
            ).lastrowid

    def paused(self):
        with self.connect() as db:
            return db.execute("SELECT value FROM controls WHERE key='paused'").fetchone()[0] == "1"

    def pause(self, paused=True):
        with self.connect() as db:
            db.execute("UPDATE controls SET value=? WHERE key='paused'", ("1" if paused else "0",))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='preparing'")
            interrupted = db.execute("UPDATE jobs SET status='uncertain', error='Process stopped during transfer; inspect the paper before reprinting' WHERE status='sending'").rowcount
            if interrupted:
                db.execute("UPDATE controls SET value='1' WHERE key='paused'")
        return interrupted

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM controls WHERE key='paused'").fetchone()[0] == "1":
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND next_attempt<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='preparing', attempts=attempts+1 WHERE id=?", (row["id"],))
                return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def update(self, job_id, **values):
        allowed = {"status", "error", "next_attempt", "receipt_path", "snapshot", "sent_at"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job fields")
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ", ".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), job_id))

    def retry(self, job, message):
        delay = min(300, 5 * 2 ** min(job["attempts"], 6))
        self.update(job["id"], status="queued", error=message, next_attempt=time.time() + delay)

    def rows(self, limit=20):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError(f"No job {job_id}")
            return dict(row)
