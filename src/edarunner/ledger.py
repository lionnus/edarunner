"""The SQLite ledger: schema, upserts, queries, board.json."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS batches(batch TEXT PRIMARY KEY, project TEXT, source TEXT, created INTEGER, retired INTEGER, run_date TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, batch TEXT, label TEXT, config TEXT, build_tag TEXT, src TEXT, dirty INTEGER,
  host TEXT, root TEXT, created INTEGER, phase TEXT, state TEXT, stage TEXT, step INTEGER, exit INTEGER, killed_by TEXT,
  started INTEGER, updated INTEGER, disk_free_gb REAL, tree_gb REAL, counts TEXT, tree_id TEXT);
CREATE TABLE IF NOT EXISTS stage_runs(run_id TEXT, stage TEXT, task TEXT, attempt INTEGER, started INTEGER, ended INTEGER,
  status TEXT, exit INTEGER, signature TEXT, log TEXT, PRIMARY KEY(run_id, stage, task, attempt));
CREATE TABLE IF NOT EXISTS params(run_id TEXT, key TEXT, value TEXT, source TEXT, PRIMARY KEY(run_id, key));
CREATE TABLE IF NOT EXISTS metrics(run_id TEXT, stage TEXT, step INTEGER, task TEXT, name TEXT, canonical TEXT, value REAL, unit TEXT,
  source_file TEXT, extracted_at INTEGER, PRIMARY KEY(run_id, stage, step, task, name));
CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT, path TEXT, bytes INTEGER, collected_at INTEGER, class TEXT, PRIMARY KEY(run_id, path));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, actor TEXT, run_id TEXT, kind TEXT, text TEXT);
"""

_PK = {
    "batches": ("batch",),
    "runs": ("run_id",),
    "stage_runs": ("run_id", "stage", "task", "attempt"),
    "artifacts": ("run_id", "path"),
}

Row = dict[str, Any]


def _now() -> int:
    return int(time.time())


def _text(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (dict, list)) else value


class Ledger:
    """One SQLite database, WAL mode, head node only. Use as a context manager."""

    def __init__(self, path: str | os.PathLike, threads: bool = False) -> None:
        """`threads=True` lets another thread use the connection; the caller serialises the calls."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=not threads)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self._columns = {t: self._table_columns(t) for t in ("batches", "runs", "stage_runs", "artifacts")}

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Commit and close the connection."""
        self.db.commit()
        self.db.close()

    def init_schema(self) -> None:
        """Create every table that does not exist yet."""
        self.db.executescript(DDL)
        # A ledger made before a column existed gets it here; SQLite adds a NULL column in place.
        if "tree_id" not in self._table_columns("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN tree_id TEXT")
        self.db.commit()

    def _table_columns(self, table: str) -> tuple[str, ...]:
        return tuple(r["name"] for r in self.db.execute(f"PRAGMA table_info({table})"))

    # writes

    def _upsert(self, table: str, row: Row) -> None:
        cols = self._columns[table]
        bad = set(row) - set(cols)
        if bad:
            raise KeyError(f"{table}: unknown columns {sorted(bad)}")
        pk = _PK[table]
        missing = [k for k in pk if row.get(k) is None]
        if missing:
            raise KeyError(f"{table}: key columns {missing} are missing")
        keys = [k for k in cols if k in row]
        update = [k for k in keys if k not in pk]
        sql = (
            f"INSERT INTO {table}({', '.join(keys)}) VALUES({', '.join('?' * len(keys))}) "
            f"ON CONFLICT({', '.join(pk)}) DO "
            + (f"UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in update)}" if update else "NOTHING")
        )
        self.db.execute(sql, [_text(row[k]) for k in keys])
        self.db.commit()

    def upsert_batch(self, row: Row) -> None:
        """Insert a batch, or update the columns given."""
        self._upsert("batches", {"created": _now(), **row})

    def upsert_run(self, row: Row) -> None:
        """Insert a run, or update the columns given. `counts` may be a dict."""
        self._upsert("runs", row)

    def upsert_stage_run(self, row: Row) -> None:
        """Insert a stage or task row, or update it. `task` defaults to '' and `attempt` to 1."""
        self._upsert("stage_runs", {"task": "", "attempt": 1, **row})

    def set_params(self, run_id: str, params: dict[str, Any], source: str) -> None:
        """Write the resolved configuration of a run. A key already present is replaced."""
        self.db.executemany(
            "INSERT OR REPLACE INTO params(run_id, key, value, source) VALUES(?, ?, ?, ?)",
            [(run_id, k, v if isinstance(v, str) else json.dumps(v), source) for k, v in params.items()],
        )
        self.db.commit()

    def add_metric(self, row: Row) -> bool:
        """Insert one metric. Returns False when the key is already present."""
        r = {"task": "", "step": None, "canonical": "", "unit": "", "source_file": "", "extracted_at": _now(), **row}
        # An `IS` match sees a NULL step; the primary key does not.
        cur = self.db.execute(
            "INSERT INTO metrics(run_id, stage, step, task, name, canonical, value, unit, source_file, extracted_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM metrics WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?)",
            (r["run_id"], r["stage"], r["step"], r["task"], r["name"], r["canonical"], r["value"], r["unit"],
             r["source_file"], r["extracted_at"], r["run_id"], r["stage"], r["step"], r["task"], r["name"]),
        )
        self.db.commit()
        return cur.rowcount == 1

    def add_artifact(self, row: Row) -> None:
        """Insert a collected file, or update its size and time."""
        self._upsert("artifacts", {"collected_at": _now(), **row})

    def add_event(self, actor: str, run_id: str | None, kind: str, text: str) -> int:
        """Append an event and return its id."""
        cur = self.db.execute(
            "INSERT INTO events(ts, actor, run_id, kind, text) VALUES(?, ?, ?, ?, ?)", (_now(), actor, run_id, kind, text)
        )
        self.db.commit()
        return int(cur.lastrowid)

    def mark_batch_retired(self, batch: str) -> None:
        """Set `retired` on a batch; the row is created when it is missing."""
        self.db.execute("INSERT OR IGNORE INTO batches(batch, created) VALUES(?, ?)", (batch, _now()))
        self.db.execute("UPDATE batches SET retired=? WHERE batch=?", (_now(), batch))
        self.db.commit()

    # queries

    def _rows(self, sql: str, args: tuple | list = ()) -> list[Row]:
        return [dict(r) for r in self.db.execute(sql, args)]

    @staticmethod
    def _run_row(row: Row) -> Row:
        if isinstance(row.get("counts"), str):
            row["counts"] = json.loads(row["counts"])
        return row

    def runs(self, batch: str | None = None, state: str | None = None) -> list[Row]:
        """Runs in run id order. `counts` comes back as a dict."""
        where, args = ["1"], []
        for col, val in (("batch", batch), ("state", state)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        return [self._run_row(r) for r in self._rows(f"SELECT * FROM runs WHERE {' AND '.join(where)} ORDER BY run_id", args)]

    def run(self, run_id: str) -> Row | None:
        """One run by exact id."""
        rows = self._rows("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return self._run_row(rows[0]) if rows else None

    def resolve(self, handle: str, last_board: list[str] | None = None) -> str:
        """Turn `label@batch`, a unique run id prefix or `#n` (1-based, from the last board) into a run id."""
        if handle.startswith("#"):
            n = int(handle[1:]) if handle[1:].isdigit() else 0
            if not last_board or not 1 <= n <= len(last_board):
                raise KeyError(f"{handle}: the last board has {len(last_board or [])} rows")
            return last_board[n - 1]
        if "@" in handle:
            label, batch = handle.split("@", 1)
            rows = self._rows("SELECT run_id FROM runs WHERE label=? AND batch=? ORDER BY run_id DESC", (label, batch))
            if not rows:
                raise KeyError(f"{handle}: no run has label '{label}' in batch '{batch}'")
            return rows[0]["run_id"]
        ids = [r["run_id"] for r in self._rows("SELECT run_id FROM runs WHERE substr(run_id, 1, ?)=?", (len(handle), handle))]
        if handle in ids:
            return handle
        if not ids:
            raise KeyError(f"{handle}: no run id starts with it")
        if len(ids) > 1:
            raise KeyError(f"{handle}: ambiguous, matches {', '.join(sorted(ids))}")
        return ids[0]

    def events(self, since_s: int | None = None, run_id: str | None = None, n: int = 50) -> list[Row]:
        """The last `n` events in time order. `since_s` is a unix timestamp."""
        where, args = ["1"], []
        if since_s is not None:
            where.append("ts>=?")
            args.append(since_s)
        if run_id is not None:
            where.append("run_id=?")
            args.append(run_id)
        sql = f"SELECT * FROM (SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?) ORDER BY id"
        return self._rows(sql, args + [n])

    def metrics(
        self,
        design: str | None = None,
        stage: str | None = None,
        step: int | None = None,
        name: str | None = None,
        run_ids: list[str] | None = None,
    ) -> list[Row]:
        """Metric rows joined with the run's label, config and src. `name` matches name or canonical."""
        where, args = ["1"], []
        for cond, val in (("r.src=?", design), ("m.stage=?", stage), ("m.step=?", step)):
            if val is not None:
                where.append(cond)
                args.append(val)
        if name is not None:
            where.append("(m.name=? OR m.canonical=?)")
            args += [name, name]
        if run_ids is not None:
            where.append(f"m.run_id IN ({', '.join('?' * len(run_ids))})" if run_ids else "0")
            args += list(run_ids)
        return self._rows(
            "SELECT m.*, r.label, r.config, r.src FROM metrics m JOIN runs r ON r.run_id=m.run_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.run_id, m.stage, m.step, m.task, m.name",
            args,
        )

    def batches(self) -> list[Row]:
        """Every batch in creation order."""
        return self._rows("SELECT * FROM batches ORDER BY created, batch")

    def write_board_json(self, path: str | os.PathLike, hosts: dict[str, Any]) -> None:
        """Write runs of live batches, the last 50 events and `hosts` to `path` by a temporary file and rename."""
        runs = self._rows(
            "SELECT r.* FROM runs r LEFT JOIN batches b ON b.batch=r.batch WHERE b.retired IS NULL ORDER BY r.run_id"
        )
        board = {"schema": 1, "written": _now(), "runs": [self._run_row(r) for r in runs], "events": self.events(n=50), "hosts": hosts}
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        try:
            tmp.write_text(json.dumps(board, indent=1) + "\n")
            os.replace(tmp, out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
