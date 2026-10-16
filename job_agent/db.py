"""SQLite tracker: every role the agent has seen, its status, and an event log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from job_agent.sources import Job

# Lifecycle: new -> skipped | pending_review | auto_approved
#            pending_review -> approved | rejected
#            approved/auto_approved -> submitted | needs_manual | failed
#            submitted -> interviewing | offer | rejected_by_company (set by you)
STATUSES = (
    "new", "skipped", "pending_review", "approved", "auto_approved", "rejected",
    "submitted", "needs_manual", "failed", "interviewing", "offer", "rejected_by_company",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    source TEXT, company TEXT, job_id TEXT, title TEXT, location TEXT,
    url TEXT, apply_url TEXT, description TEXT, posted_at TEXT, compensation TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    score INTEGER,
    assessment TEXT,
    materials_dir TEXT,
    answers TEXT,
    note TEXT,
    created_at TEXT, updated_at TEXT, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT, at TEXT, event TEXT, detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add_job(self, job: Job) -> bool:
        """Insert a newly discovered job. Returns False if already known (dedup)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO jobs (key, source, company, job_id, title, location, url,
               apply_url, description, posted_at, compensation, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)""",
            (job.key, job.source, job.company, job.job_id, job.title, job.location, job.url,
             job.apply_url, job.description, job.posted_at, job.compensation, now(), now()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY score DESC, created_at", statuses
        ).fetchall()

    def update(self, key: str, event: str | None = None, **fields) -> None:
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}")
        for k in ("assessment", "answers"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = now()
        if fields.get("status") == "submitted":
            fields["submitted_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE key = ?", (*fields.values(), key))
        if event:
            self.log(key, event, fields.get("note") or fields.get("status") or "")
        self.conn.commit()

    def log(self, key: str, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (job_key, at, event, detail) VALUES (?,?,?,?)", (key, now(), event, detail)
        )
        self.conn.commit()

    def submitted_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE submitted_at >= ?", (today,)
        ).fetchone()[0]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    def find(self, prefix: str) -> sqlite3.Row:
        """Look up a job by full key or unique suffix of its key (as shown in listings)."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE key = ? OR key LIKE ?", (prefix, f"%{prefix}%")
        ).fetchall()
        if len(rows) != 1:
            raise SystemExit(f"{len(rows)} jobs match {prefix!r}; be more specific.")
        return rows[0]
