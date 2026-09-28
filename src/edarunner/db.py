"""The project database: schema, upserts, queries, board.json."""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS batches(batch TEXT PRIMARY KEY, project TEXT, source TEXT, created INTEGER, retired INTEGER, run_date TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, batch TEXT, label TEXT, config TEXT, build_tag TEXT, source TEXT, dirty INTEGER,
  host TEXT, root TEXT, created INTEGER, phase TEXT, state TEXT, stage TEXT, step INTEGER, exit INTEGER, killed_by TEXT,
  started INTEGER, updated INTEGER, disk_free_gb REAL, tree_gb REAL, counts TEXT, tree_id TEXT, handle TEXT, cores INTEGER);
CREATE TABLE IF NOT EXISTS stage_runs(run_id TEXT, stage TEXT, task TEXT, attempt INTEGER, started INTEGER, ended INTEGER,
  status TEXT, exit INTEGER, signature TEXT, log TEXT, PRIMARY KEY(run_id, stage, task, attempt));
CREATE TABLE IF NOT EXISTS parameters(run_id TEXT, key TEXT, value TEXT, origin TEXT, PRIMARY KEY(run_id, key, origin));
CREATE TABLE IF NOT EXISTS task_fields(run_id TEXT, task TEXT, key TEXT, value TEXT, origin TEXT, PRIMARY KEY(run_id, task, key));
CREATE TABLE IF NOT EXISTS flags(run_id TEXT, task TEXT, "check" TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS metrics(run_id TEXT, stage TEXT, step INTEGER, task TEXT, name TEXT, canonical TEXT, value REAL, unit TEXT,
  source_file TEXT, extracted_at INTEGER, PRIMARY KEY(run_id, stage, step, task, name));
CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT, path TEXT, bytes INTEGER, collected_at INTEGER, class TEXT, PRIMARY KEY(run_id, path));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, actor TEXT, run_id TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS store(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS step_runs(run_id TEXT, stage TEXT, step INTEGER, started INTEGER, PRIMARY KEY(run_id, stage, step));
CREATE TABLE IF NOT EXISTS host_samples(host TEXT, ts INTEGER, cores INTEGER, load REAL, ram_gb REAL, ram_used_gb REAL,
  scratch_gb REAL, scratch_used_gb REAL, gpus INTEGER, gpus_busy INTEGER, PRIMARY KEY(host, ts));
CREATE TABLE IF NOT EXISTS run_samples(run_id TEXT, ts INTEGER, cpu_pct REAL, rss_gb REAL, tree_gb REAL, disk_free_gb REAL,
  PRIMARY KEY(run_id, ts));
CREATE TABLE IF NOT EXISTS instances(run_id TEXT, stage TEXT, step INTEGER, task TEXT, name TEXT, part TEXT, instance TEXT,
  depth INTEGER, value REAL, local REAL, cells INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS instances_key ON instances(run_id, stage, ifnull(step, -1), task, name, part, instance);
"""

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


class NotFound(KeyError):
    """A handle or a reuse that names no run. Its text is the message, without the quotes of a KeyError."""

    def __str__(self) -> str:
        return str(self.args[0])


def _within(where: list[str], args: list, *columns: tuple[str, list[str] | None]) -> None:
    """Add `column IN (...)` for each (column, values) whose values are not None; an empty list matches no row."""
    for col, values in columns:
        if values is not None:
            where.append(f"{col} IN ({', '.join('?' * len(values))})" if values else "0")
            args += list(values)


def pick(runs: list[Row]) -> Row | None:
    """The run of record among `runs`, the runs of one label at one source: the newest run by start time that ended
    done, else the newest run; the run id breaks a tie. Every command that takes one run of a label uses it."""
    done = [r for r in runs if r.get("phase") == "done"]
    return max(done or runs, key=lambda r: (r.get("started") or 0, r["run_id"]), default=None)


class Database:
    """One SQLite database, head node only. Use as a context manager.

    The journal is WAL on a local filesystem. On a network filesystem it is DELETE with
    `synchronous=FULL`, because WAL needs shared memory that such a filesystem does not give.
    A writer waits up to 30 s for the lock of another.
    """

    def __init__(self, path: str | os.PathLike, threads: bool = False, readonly: bool = False) -> None:
        """`threads=True` lets another thread use the connection; the caller serialises the calls. `readonly` opens an
        existing database for reading only, without the journal mode and the schema script, and creates no file."""
        self.path = Path(path)
        self.network_fs, self.journal_mode = None, ""
        if readonly:
            self.conn = sqlite3.connect(self._read_uri(), uri=True, timeout=30, check_same_thread=not threads)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=not threads)
            self.network_fs = None if str(path) == ":memory:" else network_fs(self.path.parent)
            mode = "DELETE" if self.network_fs else "WAL"
            self.journal_mode = self.conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
            if self.network_fs:
                self.conn.execute("PRAGMA synchronous=FULL")
            self.init_schema()
        self.conn.row_factory = sqlite3.Row
        self._columns = {t: self._table_columns(t) for t in ("batches", "runs", "stage_runs", "artifacts")}

    def _read_uri(self) -> str:
        with self.path.open("rb") as fh:
            wal = fh.read(20)[18:19] == b"\x02"  # the header's write version: 2 is WAL
        # mode=ro makes the -wal and -shm files of a WAL database. Without a -wal file no connection has the
        # database open, so the file is whole, and immutable=1 reads it without making any file.
        flag = "immutable=1" if wal and not Path(f"{self.path}-wal").exists() else "mode=ro"
        return f"{self.path.absolute().as_uri()}?{flag}"

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Commit and close the connection."""
        self.conn.commit()
        self.conn.close()

    def init_schema(self) -> None:
        """Create every table that does not exist yet."""
        self.conn.executescript(DDL)

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

    def set_parameters(self, run_id: str, parameters: dict[str, Any], origin: str, replace: bool = False) -> None:
        """Write parameters of a run as text under one origin, such as `spec`, `checkout`, `import` or `extract`; the
        caller commits. A key already present under that origin is replaced, and with `replace` every earlier row of
        that origin goes."""
        if replace:
            self.conn.execute("DELETE FROM parameters WHERE run_id=? AND origin=?", (run_id, origin))
        self.conn.executemany(
            "INSERT OR REPLACE INTO parameters(run_id, key, value, origin) VALUES(?, ?, ?, ?)",
            [(run_id, k, v if isinstance(v, str) else json.dumps(v), origin) for k, v in parameters.items()],
        )

    def set_flags(self, run_id: str, flags: list[tuple[str, str, str]], check: str | None = None) -> None:
        """Replace the flags of a run, or only those of one check, with `flags` of (task, check, text); the caller
        commits."""
        self.conn.execute('DELETE FROM flags WHERE run_id=?' + (' AND "check"=?' if check else ""),
                          (run_id, check) if check else (run_id,))
        self.conn.executemany("INSERT INTO flags VALUES(?, ?, ?, ?)", [(run_id, *f) for f in dict.fromkeys(flags)])

    def set_task_fields(self, run_id: str, rows: list[tuple[str, str, str, str]]) -> None:
        """Replace the task fields of a run with `rows` of (task, key, value, origin); the origin is `spec`, `resolver`
        or `import`."""
        self.conn.execute("DELETE FROM task_fields WHERE run_id=?", (run_id,))
        self.conn.executemany("INSERT INTO task_fields(run_id, task, key, value, origin) VALUES(?, ?, ?, ?, ?)",
                              [(run_id, *r) for r in rows])
        self.conn.commit()

    def add_metric(self, row: Row, replace: bool = False) -> bool:
        """Insert one metric, and its `instances` into the instances table; the caller commits. Returns True when the
        row went in or took the new value.

        A present row keeps its value, but a failed row, whose value is empty, takes a new value. The instance rows go
        in even when the metric was present, so a new instance metric fills an old run. `replace` overwrites a present
        metric and replaces its instance rows.
        """
        r = {"task": "", "step": None, "canonical": "", "unit": "", "source_file": "", "extracted_at": _now(), **row}
        key = (r["run_id"], r["stage"], r["step"], r["task"], r["name"])
        # An `IS` match sees a NULL step; the primary key does not.
        filled = 0
        if replace or r["value"] is not None:
            filled = self.conn.execute(
                "UPDATE metrics SET canonical=?, value=?, unit=?, source_file=?, extracted_at=? "
                "WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?" + ("" if replace else " AND value IS NULL"),
                (r["canonical"], r["value"], r["unit"], r["source_file"], r["extracted_at"], *key)).rowcount
        if replace:
            self.conn.execute("DELETE FROM instances WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?", key)
        cur = self.conn.execute(
            "INSERT INTO metrics(run_id, stage, step, task, name, canonical, value, unit, source_file, extracted_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM metrics WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?)",
            (*key, r["canonical"], r["value"], r["unit"], r["source_file"], r["extracted_at"], *key),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO instances(run_id, stage, step, task, name, part, instance, depth, value, local, cells) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, i["part"], i["instance"], i["depth"], i["value"], i["local"], i["cells"]) for i in r.get("instances") or []])
        return cur.rowcount == 1 or filled == 1

    def remove_metrics(self, rows: list[Row]) -> None:
        """Delete metric rows and their instance rows; the caller commits."""
        keys = [(r["run_id"], r["stage"], r["step"], r["task"], r["name"]) for r in rows]
        for table in ("metrics", "instances"):
            self.conn.executemany(f"DELETE FROM {table} WHERE run_id=? AND stage=? AND step IS ? AND task=? AND name=?", keys)

    def set_step_times(self, run_id: str, times: dict[str, dict[str, int]]) -> None:
        """Write the start time of each step, {stage: {step: unix time}}; a resumed step replaces its time."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO step_runs(run_id, stage, step, started) VALUES(?, ?, ?, ?)",
            [(run_id, stage, int(step), int(ts)) for stage, steps in times.items() for step, ts in steps.items()])
        self.conn.commit()

    def add_host_samples(self, ts: int, probes: dict[str, dict], keep_days: int = 30) -> None:
        """One row per host that answered, from the probe dicts of `hosts.HostProbe`; rows older than `keep_days` go."""
        rows = [(h, ts, p.get("cores"), p.get("load"), p.get("total_ram_gb"),
                 round(p.get("total_ram_gb", 0) - p.get("free_ram_gb", 0), 2), p.get("total_gb"),
                 round(p.get("total_gb", 0) - p.get("free_gb", 0), 2), p.get("gpus"),
                 p.get("gpus", 0) - p.get("gpus_idle", 0)) for h, p in probes.items() if "error" not in p]
        self.conn.executemany("INSERT OR REPLACE INTO host_samples VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.execute("DELETE FROM host_samples WHERE ts<?", (ts - keep_days * 86400,))
        self.conn.commit()

    def host_samples(self, since_s: int, host: str | None = None) -> list[Row]:
        """Host samples from `since_s` on, by host and time."""
        where, args = "ts>=?", [since_s]
        if host is not None:
            where, args = where + " AND host=?", [since_s, host]
        return self._rows(f"SELECT * FROM host_samples WHERE {where} ORDER BY host, ts", args)

    def add_run_sample(self, hb: dict) -> None:
        """Keep the CPU, RSS, tree size and free disk of one heartbeat, once per heartbeat time."""
        if hb.get("updated") is None:
            return
        self.conn.execute("INSERT OR IGNORE INTO run_samples VALUES(?, ?, ?, ?, ?, ?)",
                          (hb["run_id"], int(hb["updated"]), hb.get("cpu_pct"), hb.get("rss_gb"), hb.get("tree_gb"),
                           hb.get("disk_free_gb")))
        self.conn.commit()

    def run_samples(self, run_id: str) -> list[Row]:
        """The samples of one run in time order."""
        return self._rows("SELECT * FROM run_samples WHERE run_id=? ORDER BY ts", (run_id,))

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

    def stage_runs(self, run_id: str) -> list[Row]:
        """The stage and task rows of one run, oldest first; a stage row has task ''."""
        return self._rows("SELECT * FROM stage_runs WHERE run_id=? ORDER BY started, stage, task, attempt", (run_id,))

    def step_runs(self, run_id: str) -> list[Row]:
        """The step start times of one run, by stage and step."""
        return self._rows("SELECT stage, step, started FROM step_runs WHERE run_id=? ORDER BY stage, step", (run_id,))

    def parameters(self, run_id: str | None = None) -> list[Row]:
        """The parameter rows of one run, or of every run, by run, key and origin, with the `extract` row of a key last:
        a reader that keeps one value per key keeps the value read from the run's files."""
        where, args = (" WHERE run_id=?", (run_id,)) if run_id else ("", ())
        return self._rows("SELECT run_id, key, value, origin FROM parameters" + where
                          + " ORDER BY run_id, key, origin = 'extract', origin", args)

    def flags(self, run_ids: list[str] | None = None) -> list[Row]:
        """The flags of the runs, or of every run, by run, task and check."""
        where, args = ["1"], []
        _within(where, args, ("run_id", run_ids))
        return self._rows(f"SELECT * FROM flags WHERE {' AND '.join(where)} ORDER BY run_id, task, \"check\", text", args)

    def task_fields(self, run_ids: list[str] | None = None) -> list[Row]:
        """The task field rows of the runs, or of every run, by run, task and key."""
        where, args = ["1"], []
        _within(where, args, ("run_id", run_ids))
        return self._rows(f"SELECT * FROM task_fields WHERE {' AND '.join(where)} ORDER BY run_id, task, key", args)

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
        """Turn `label@batch`, `label@source`, a unique run id prefix or `#n` (1-based, from the last board) into a
        run id.

        `label@batch` is the one run of that label in that batch; a batch with several is refused, and the error
        lists them. `label@source` is the `pick` of that label at that source tag. A name after the `@` that is both
        a batch and a source is refused."""
        if handle.startswith("#"):
            n = int(handle[1:]) if handle[1:].isdigit() else 0
            if not last_board or not 1 <= n <= len(last_board):
                raise NotFound(f"{handle}: the last board has {len(last_board or [])} rows")
            return last_board[n - 1]
        if "@" in handle:
            label, at = handle.split("@", 1)
            rows = self._rows("SELECT * FROM runs WHERE batch=? OR source=? ORDER BY run_id", (at, at))
            is_batch, is_source = any(r["batch"] == at for r in rows), any(r["source"] == at for r in rows)
            if is_batch and is_source:
                raise NotFound(f"{handle}: '{at}' is both a batch and a source; name the run by its run id")
            mine = [r for r in rows if r["label"] == label]
            if not mine:
                raise NotFound(f"{handle}: no run has label '{label}' in a batch or at a source '{at}'")
            if len(mine) > 1 and is_batch:
                raise NotFound(f"{handle}: {len(mine)} runs have label '{label}' in batch '{at}'; name one by its run id "
                               "or as label@source: " + ", ".join(f"{r['run_id']} ({r['source']}, {r['phase']})"
                                                                  for r in mine))
            return pick(mine)["run_id"]
        ids = [r["run_id"] for r in self._rows("SELECT run_id FROM runs WHERE substr(run_id, 1, ?)=?", (len(handle), handle))]
        if handle in ids:
            return handle
        if not ids:
            raise NotFound(f"{handle}: no run id starts with it")
        if len(ids) > 1:
            raise NotFound(f"{handle}: ambiguous, matches {', '.join(sorted(ids))}")
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
        sources: list[str] | None = None,
        stage: str | None = None,
        step: int | None = None,
        name: str | None = None,
        run_ids: list[str] | None = None,
    ) -> list[Row]:
        """Metric rows joined with the run's label, config, build tag, source and host. `name` matches name or canonical."""
        where, args = ["1"], []
        for cond, val in (("m.stage=?", stage), ("m.step=?", step)):
            if val is not None:
                where.append(cond)
                args.append(val)
        if name is not None:
            where.append("(m.name=? OR m.canonical=?)")
            args += [name, name]
        _within(where, args, ("r.source", sources), ("m.run_id", run_ids))
        return self._rows(
            "SELECT m.*, r.label, r.config, r.build_tag, r.source, r.host FROM metrics m JOIN runs r ON r.run_id=m.run_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.run_id, m.stage, m.step, m.task, m.name",
            args,
        )

    def instances(self, run_ids: list[str] | None = None, sources: list[str] | None = None, stage: str | None = None,
                  step: int | None = None, task: str | None = None, name: str | None = None, depth: int | None = None,
                  instance: str | None = None) -> list[Row]:
        """Instance rows with the run's label and source and the metric's source_file and unit. `instance` is a glob
        over the path, in which `*` also matches `/`."""
        where, args = ["1"], []
        for cond, val in (("i.stage=?", stage), ("i.step=?", step), ("i.task=?", task), ("i.name=?", name),
                          ("i.depth=?", depth)):
            if val is not None:
                where.append(cond)
                args.append(val)
        _within(where, args, ("r.source", sources), ("i.run_id", run_ids))
        rows = self._rows(
            "SELECT i.*, r.label, r.source, m.source_file, m.unit FROM instances i JOIN runs r ON r.run_id=i.run_id "
            "LEFT JOIN metrics m ON m.run_id=i.run_id AND m.stage=i.stage AND m.step IS i.step AND m.task=i.task "
            "AND m.name=i.name "
            f"WHERE {' AND '.join(where)} ORDER BY i.run_id, i.stage, i.step, i.task, i.name, i.part, i.value DESC",
            args,
        )
        return [r for r in rows if instance is None or fnmatchcase(r["instance"], instance)]

    def instance_names(self, run_ids: list[str]) -> list[str]:
        """The metrics with instance rows in the runs."""
        where, args = [], []
        _within(where, args, ("run_id", run_ids))
        return [r["name"] for r in self._rows(f"SELECT DISTINCT name FROM instances WHERE {where[0]} ORDER BY name", args)]

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
