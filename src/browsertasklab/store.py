import json
import sqlite3
from pathlib import Path

from .security import redact


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS demos (
                  id TEXT PRIMARY KEY, workflow TEXT NOT NULL, variant TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS searches (
                  run_id TEXT PRIMARY KEY, parameters TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (
                  run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bookings (
                  run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def create_demo(self, run_id, workflow, variant="normal"):
        with self.connect() as db:
            db.execute("INSERT INTO demos VALUES (?,?,?)", (run_id, workflow, variant))

    def demo(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM demos WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def save_run(self, state):
        snapshot = json.dumps(redact(state.model_dump(mode="json")))
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (state.id, snapshot))

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list_runs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM runs ORDER BY rowid DESC LIMIT 30")]

    def write_search(self, run_id, parameters):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO searches VALUES (?,?)", (run_id, json.dumps(parameters)))

    def record(self, table, run_id):
        if table not in {"searches", "drafts", "bookings"}:
            raise ValueError("Unknown demo record")
        column = "parameters" if table == "searches" else "payload"
        with self.connect() as db:
            row = db.execute(f"SELECT {column} FROM {table} WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def commit_once(self, table, run_id, payload):
        """SQLite uniqueness is the second defense against double submissions."""
        if table not in {"drafts", "bookings"}:
            raise ValueError("Unknown demo record")
        with self.connect() as db:
            existing = db.execute(f"SELECT payload FROM {table} WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if json.loads(existing[0]) != payload:
                    raise ValueError("A different record already exists for this run")
                return json.loads(existing[0])
            db.execute(f"INSERT INTO {table} VALUES (?,?)", (run_id, json.dumps(payload)))
            return payload

    def count(self, table, run_id):
        if table not in {"drafts", "bookings"}:
            raise ValueError("Unknown demo record")
        with self.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
