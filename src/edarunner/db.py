"""The run database: schema, upserts, queries, board.json."""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
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
CREATE TABLE IF NOT EXISTS parameters(run_id TEXT, key TEXT, value TEXT, source TEXT, PRIMARY KEY(run_id, key));
CREATE TABLE IF NOT EXISTS metrics(run_id TEXT, stage TEXT, step INTEGER, task TEXT, name TEXT, canonical TEXT, value REAL, unit TEXT,
  source_file TEXT, extracted_at INTEGER, PRIMARY KEY(run_id, stage, step, task, name));
CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT, path TEXT, bytes INTEGER, collected_at INTEGER, class TEXT, PRIMARY KEY(run_id, path));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, actor TEXT, run_id TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS store(key TEXT PRIMARY KEY, value TEXT);
"""

# A table of an older schema, with its current name.
_RENAMED = {"kv": "store", "params": "parameters"}

_PK = {
    "batches": ("batch",),
    "runs": ("run_id",),
    "stage_runs": ("run_id", "stage", "task", "attempt"),
    "artifacts": ("run_id", "path"),
}

Row = dict[str, Any]

# The statfs f_type of a network filesystem, where SQLite WAL does not work.
NETWORK_FS = {0x6969: "nfs", 0xFF534D42: "cifs", 0xFE534D42: "smb2", 0x01021997: "9p", 0x65735546: "fuse"}


def fs_magic(path: str | os.PathLike) -> int | None:
    """The statfs f_type of the filesystem that holds `path`, or None off Linux or on an error."""
    if not sys.platform.startswith("linux"):
        return None
    buf = ctypes.create_string_buffer(256)  # struct statfs, f_type first
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.statfs(os.fsencode(path), buf) != 0:
        return None
    return ctypes.c_ulong.from_buffer(buf).value & 0xFFFFFFFF


def network_fs(path: str | os.PathLike) -> str | None:
    """The network filesystem that holds `path` or its nearest existing parent, such as "nfs"; None for a local or unknown one."""
    p = Path(path).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    magic = fs_magic(p)
    return None if magic is None else NETWORK_FS.get(magic)


def _now() -> int:
    return int(time.time())


def _text(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (dict, list)) else value


class Database:
    """One SQLite database, head node only. Use as a context manager.

    The journal is WAL on a local filesystem. On a network filesystem it is DELETE with
    `synchronous=FULL`, because WAL needs shared memory that such a filesystem does not give.
    """

    def __init__(self, path: str | os.PathLike, threads: bool = False) -> None:
        """`threads=True` lets another thread use the connection; the caller serialises the calls."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=not threads)
        self.conn.row_factory = sqlite3.Row
        self.network_fs = None if str(path) == ":memory:" else network_fs(self.path.parent)
        mode = "DELETE" if self.network_fs else "WAL"
        self.journal_mode: str = self.conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
        if self.network_fs:
            self.conn.execute("PRAGMA synchronous=FULL")
        self.init_schema()
        self._columns = {t: self._table_columns(t) for t in ("batches", "runs", "stage_runs", "artifacts")}

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Commit and close the connection."""
        self.conn.commit()
        self.conn.close()

    def init_schema(self) -> None:
        """Rename every table of an older schema and create every table that does not exist yet, in one transaction."""
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r["name"] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for old, new in _RENAMED.items():
                if old in tables and new not in tables:
                    self.conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            for stmt in DDL.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
        except BaseException:
            self.conn.rollback()
            raise
        # A database made before a column existed gets it here; SQLite adds a NULL column in place.
        if "tree_id" not in self._table_columns("runs"):
            self.conn.execute("ALTER TABLE runs ADD COLUMN tree_id TEXT")
        self.conn.commit()

    def _table_columns(self, table: str) -> tuple[str, ...]:
        return tuple(r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})"))

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
        self.conn.execute(sql, [_text(row[k]) for k in keys])
        self.conn.commit()

    def upsert_batch(self, row: Row) -> None:
        """Insert a batch, or update the columns given."""
        self._upsert("batches", {"created": _now(), **row})

    def upsert_run(self, row: Row) -> None:
        """Insert a run, or update the columns given. `counts` may be a dict."""
        self._upsert("runs", row)

    def upsert_stage_run(self, row: Row) -> None:
        """Insert a stage or task row, or update it. `task` defaults to '' and `attempt` to 1."""
        self._upsert("stage_runs", {"task": "", "attempt": 1, **row})

    def set_parameters(self, run_id: str, parameters: dict[str, Any], source: str) -> None:
        """Write the resolved configuration of a run. A key already present is replaced."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO parameters(run_id, key, value, source) VALUES(?, ?, ?, ?)",
            [(run_id, k, v if isinstance(v, str) else json.dumps(v), source) for k, v in parameters.items()],
        )
        self.conn.commit()

    def add_metric(self, row: Row) -> bool:
        """Insert one metric. Returns False when the key is already present."""
        r = {"task": "", "step": None, "canonical": "", "unit": "", "source_file": "", "extracted_at": _now(), **row}
        # An `IS` match sees a NULL step; the primary key does not.
        cur = self.conn.execute(
            "INSERT INTO metrics(run_id, stage, step, task, name, canonical, value, unit, source_file, extracted_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM metrics WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?)",
            (r["run_id"], r["stage"], r["step"], r["task"], r["name"], r["canonical"], r["value"], r["unit"],
             r["source_file"], r["extracted_at"], r["run_id"], r["stage"], r["step"], r["task"], r["name"]),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def add_artifact(self, row: Row) -> None:
        """Insert a collected file, or update its size and time."""
        self._upsert("artifacts", {"collected_at": _now(), **row})

    def add_event(self, actor: str, run_id: str | None, kind: str, text: str) -> int:
        """Append an event and return its id."""
        cur = self.conn.execute(
            "INSERT INTO events(ts, actor, run_id, kind, text) VALUES(?, ?, ?, ?, ?)", (_now(), actor, run_id, kind, text)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_store(self, key: str, value: Any) -> None:
        """Store `value` as JSON under `key`; a key already present is replaced."""
        self.conn.execute("INSERT OR REPLACE INTO store(key, value) VALUES(?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    def get_store(self, key: str, default: Any = None) -> Any:
        """The value stored under `key`, or `default`."""
        row = self.conn.execute("SELECT value FROM store WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def mark_batch_retired(self, batch: str) -> None:
        """Set `retired` on a batch; the row is created when it is missing."""
        self.conn.execute("INSERT OR IGNORE INTO batches(batch, created) VALUES(?, ?)", (batch, _now()))
        self.conn.execute("UPDATE batches SET retired=? WHERE batch=?", (_now(), batch))
        self.conn.commit()

    # queries

    def _rows(self, sql: str, args: tuple | list = ()) -> list[Row]:
        return [dict(r) for r in self.conn.execute(sql, args)]

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
