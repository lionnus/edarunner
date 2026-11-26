"""The boundary of a private flow: what of a flow under a non-disclosure agreement may leave edr.

A private file, which a person writes and `private` in edr.toml names, holds the commands of the private stages, the
line each knob writes for the tool, and the rules of what leaves: the `keep` regexes of the output lines, the `redact`
regexes of the strings that never leave, the `cells` classes, `ge_um2` and the `[publish]` converters. This module
owns four things:

- the redaction: every string of every private file this process loaded, and every `redact` regex, is replaced in
  every text edr prints, writes as an event, posts to a channel or writes into a board (`redact`, `Stream`,
  `LogFilter`);
- the knobs: the value of each knob of a job, its build tag token, its environment variable and the lines of a
  stage's `{knob_file}` (`knob_values`, `knob_tokens`, `knob_env`, `knob_lines`);
- the publish: the conversion of the raw files of `data/private/<run_id>/` into the public files of
  `data/results/<run_id>/public/` (`publish_run`), with the area in gate equivalents and a standard form of the
  timing paths (`parse_timing`);
- the public view of a spec (`public_spec`).

The driver keeps the raw output of a private command on the host; `edr_driver.Mirror` writes the public log.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from . import config, metrics
from .model import Knob, Metric, Private, Project, Publish

NDA = "‹nda›"  # a match of a `redact` regex
PRIVATE = "‹private›"  # a string of the private file, or a field that comes from it
CELL = "‹cell›"  # a cell that no `cells` class matches
TREE_DIR = ".edr/private"  # the private directory of a run tree; nothing under it is collected into data/results
MIN_LITERAL = 8  # a shorter piece of a private string is too common to redact by itself
EXT = {"area": "csv", "timing": "json", "values": "json", "text": "txt", "python": "json"}
_PH = re.compile(r"(?<!\$)\{[\w.]+\}")


# --- redaction

def scoped(pattern: str) -> str:
    """A regex as one alternative of a larger one: its leading inline flags, such as `(?i)`, apply to it alone."""
    m = re.match(r"\(\?([aiLmsux]+)\)", pattern)
    return f"(?{m.group(1)}:{pattern[m.end():]})" if m else f"(?:{pattern})"


def combine(patterns: list[str]) -> list[re.Pattern[str]]:
    """The `redact` regexes as one regex, so that a single pass replaces the leftmost match of any of them; one regex
    per pattern when they do not combine, such as two that name the same group. A pass per regex would let one
    replacement cut the text another one needs."""
    if not patterns:
        return []
    try:
        return [re.compile("|".join(map(scoped, patterns)))]
    except re.error:
        return [re.compile(p) for p in patterns]


class Redactor:
    """The redaction rules of every private file this process loaded, by path; a reload replaces a file's rules."""

    def __init__(self) -> None:
        self._by_file: dict[str, tuple[list[str], list[str]]] = {}
        self._rx: list[re.Pattern[str]] = []

    def add(self, private: Private) -> None:
        """Take the rules of one private file."""
        self._by_file[str(private.path)] = (list(_literals(private)), list(private.redact))
        lits = sorted({lit for lits, _ in self._by_file.values() for lit in lits}, key=len, reverse=True)
        pats = [p for _, ps in self._by_file.values() for p in ps]
        # The private strings come first, so their pieces win over a regex that matches a part of them.
        lit = f"(?P<_edr_private>{'|'.join(map(re.escape, lits))})" if lits else ""
        self._rx = combine([lit, *map(scoped, pats)] if lit else pats)

    @property
    def active(self) -> bool:
        return bool(self._by_file)

    def text(self, s: str) -> str:
        """`s` with each private string as `‹private›` and each match of a `redact` regex as `‹nda›`."""
        if not self._by_file or not s:
            return s
        for rx in self._rx:
            s = rx.sub(_mark, s)
        return s


def _mark(m: re.Match[str]) -> str:
    return PRIVATE if m.groupdict().get("_edr_private") is not None else NDA


_ACTIVE = Redactor()


def register(private: Private) -> None:
    """Redact the strings of `private` from every output of this process from now on."""
    _ACTIVE.add(private)


def active() -> bool:
    """True once this process loaded a private file."""
    return _ACTIVE.active


def redact(text: str) -> str:
    """`text` without any string of a private file this process loaded; unchanged before the first one."""
    return _ACTIVE.text(text)


def redact_obj(obj: Any) -> Any:
    """`obj` with `redact` applied to every string, key or value, of every dict and list in it."""
    if not _ACTIVE.active:
        return obj
    if isinstance(obj, str):
        return _ACTIVE.text(obj)
    if isinstance(obj, dict):
        return {redact_obj(k): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def literals(private: Private) -> list[str]:
    """The pieces between the placeholders of every string a private file gives that are long enough to redact,
    longest first; the driver redacts them from the public log too."""
    return sorted(set(_literals(private)), key=len, reverse=True)


def _literals(private: Private) -> Iterable[str]:
    texts: list[str] = [*private.env.values()]
    for st in private.stages.values():
        texts += [st.cmd, st.resume, st.progress, st.prepare, st.after_each, *st.collect]
    for k in private.knobs.values():
        texts += [k.line] if isinstance(k.line, str) else list(k.line.values())
    for p in private.publish.values():
        texts += [p.file, p.python]
    for text in texts:
        for piece in _PH.split(text or ""):
            piece = piece.strip()
            if len(piece) >= MIN_LITERAL:
                yield piece


class Stream(io.TextIOBase):
    """A text stream that redacts each line before it reaches `raw`; it passes text straight through while no private
    file is loaded, so a command of a project without one prints as before."""

    def __init__(self, raw: TextIO) -> None:
        self.raw = raw
        self._buf = ""

    def write(self, s: str) -> int:
        if not _ACTIVE.active:
            if self._buf:
                self.flush()
            return self.raw.write(s)
        self._buf += s
        if "\n" in self._buf:
            head, _, self._buf = self._buf.rpartition("\n")
            self.raw.write(redact(head + "\n"))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self.raw.write(redact(self._buf))
            self._buf = ""
        self.raw.flush()

    def isatty(self) -> bool:
        return self.raw.isatty()

    def fileno(self) -> int:
        return self.raw.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self.raw, "encoding", "utf-8")

    def writable(self) -> bool:
        return True


class LogFilter(logging.Filter):
    """Redact the message and the traceback of every log record, once a private file is loaded."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _ACTIVE.active:
            record.msg, record.args = redact(record.getMessage()), None
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


# --- knobs

def knob_type(knob: Knob) -> str:
    """The type of a knob: its `type`, else `choice` with choices, else the type of its default."""
    if knob.type:
        return knob.type
    if knob.choices:
        return "choice"
    return {bool: "bool", int: "int", float: "float"}.get(type(knob.default), "str")


def check_knob(knob: Knob, value: object) -> bool | int | float | str:
    """`value` as a value of `knob`; ValueError with the reason when it does not fit."""
    kind = knob_type(knob)
    ok = {"bool": isinstance(value, bool), "int": type(value) is int,
          "float": type(value) in (int, float), "str": isinstance(value, str),
          "choice": isinstance(value, str) and value in knob.choices}[kind]
    if not ok:
        want = f"one of {', '.join(knob.choices)}" if kind == "choice" else f"a {kind}"
        raise ValueError(f"knob {knob.name} is {value!r}, not {want}")
    if kind in ("int", "float"):
        num = float(value)  # type: ignore[arg-type]
        if knob.min is not None and num < knob.min:
            raise ValueError(f"knob {knob.name} is {value!r}, below its min {knob.min:g}")
        if knob.max is not None and num > knob.max:
            raise ValueError(f"knob {knob.name} is {value!r}, above its max {knob.max:g}")
        return float(num) if kind == "float" else int(value)  # type: ignore[call-overload]
    return value  # type: ignore[return-value]


def knob_values(project: Project, given: dict[str, object]) -> dict[str, bool | int | float | str]:
    """Every knob of the project with the value of `given`, else its default; ValueError for a name or a value
    outside the knobs."""
    unknown = sorted(set(given) - set(project.knobs))
    if unknown:
        known = ", ".join(project.knobs) or "none"
        raise ValueError(f"unknown knob {', '.join(unknown)} (edr.toml has: {known})")
    return {n: check_knob(k, given[n]) if n in given else k.default for n, k in project.knobs.items()}


def knob_text(value: object) -> str:
    """A knob value as the text of its variable and its line: `true`, `false`, `2`, `1.6`, `high`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)  # the shortest text that reads back as the same float
    return str(value)


def knob_tokens(project: Project, values: dict[str, object]) -> list[str]:
    """One build tag token per knob whose value differs from its default: `retime`, `noretime`, `clock_ns1p6`."""
    out = []
    for name, knob in project.knobs.items():
        v = values.get(name, knob.default)
        if v == knob.default:
            continue
        if isinstance(v, bool):
            out.append(name if v else f"no{name}")
            continue
        text = knob_text(v)
        token = name + re.sub(r"[^A-Za-z0-9]", "", text.replace(".", "p").replace("-", "m"))
        if re.search(r"[^A-Za-z0-9.-]", text):  # what the token dropped, a hash keeps apart
            token += "h" + hashlib.sha256(text.encode()).hexdigest()[:6]
        out.append(token)
    return out


def knob_lines(project: Project, stage: str, values: dict[str, object]) -> str:
    """The text of the `{knob_file}` of `stage`: a comment, then the line of each knob that acts on the stage and that
    the private file maps, in the order of edr.toml."""
    private = project.private
    out = [f"# edr: the knobs of stage {stage}"]
    for name, knob in project.knobs.items():
        kmap = private.knobs.get(name) if private else None
        if kmap is None or knob.stages and stage not in knob.stages:
            continue
        text = knob_text(values.get(name, knob.default))
        line = kmap.line.replace("{value}", text) if isinstance(kmap.line, str) else kmap.line.get(text, "")
        if line:
            out.append(line)
    return "\n".join(out) + "\n"


def withheld(project: Project) -> list[str]:
    """The paths of a run tree that no public collect copies: the private directory, the `paths` of the private
    file and the raw `collect` paths of its stages, with their placeholders."""
    p = project.private
    if p is None:
        return []
    return [TREE_DIR + "/", *p.paths, *(e for st in p.stages.values() for e in st.collect)]


def knob_file(root: str, stage: str) -> str:
    """The path of the `{knob_file}` of a stage in a run tree."""
    return os.path.join(root, TREE_DIR, "knobs", f"{stage}.knobs")


# --- the public view of a spec

def public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """A spec as an agent may read it: every field that comes from the private file is `‹private›`."""
    if not spec.get("private"):
        return spec
    out = dict(spec)
    hidden = set(spec["private"].get("env") or [])
    out["env"] = {k: PRIVATE if k in hidden else v for k, v in (spec.get("env") or {}).items()}
    out["private"] = {"dir": spec["private"].get("dir")}  # neither the redact regexes nor the literals
    stages = []
    for st in spec.get("stages") or []:
        if st.get("private"):
            st = {k: (PRIVATE if k in ("cmd", "resume", "progress", "prepare", "after_each", "retry") else v)
                  for k, v in st.items() if k not in ("files",)}
            st["private"] = True
            if "tasks" in st and isinstance(st["tasks"], list):
                st["tasks"] = [{**t, "cmd": PRIVATE} for t in st["tasks"]]
        stages.append(st)
    out["stages"] = stages
    return out


# --- publish

class PublishError(ValueError):
    """A raw file did not convert. The message never quotes the file."""


def cell_class(private: Private, name: str) -> str:
    """The class of a library cell: the first `cells` class whose regex matches, with `x<drive>` from a group
    `drive`; `‹cell›` when none matches."""
    for cls, rx in private.cells.items():
        m = re.search(rx, name)
        if m:
            drive = m.groupdict().get("drive")
            return f"{cls}x{drive}" if drive else cls
    return CELL


def publish_run(project: Project, run_id: str, dry_run: bool = False,
                force: bool = False) -> tuple[list[str], list[str]]:
    """Write the public file of every `[publish]` of the private file whose raw file is in `data/private/<run_id>/`
    and newer than its public file, or every one with `force`: (the public paths written, relative to the run's
    results, and the failures)."""
    private = project.private
    if private is None or not private.publish:
        return [], []
    src = project.data / "private" / run_id
    out = project.data / "results" / run_id
    written: list[str] = []
    failures: list[str] = []
    for p in private.publish.values():
        step = "*" if "{step}" in p.file else None
        for n, path in metrics.find_files(p.file, src, step):
            rel = f"public/{n}/{p.name}.{EXT[p.kind]}" if n is not None else f"public/{p.name}.{EXT[p.kind]}"
            dest = out / rel
            try:
                if not force and dest.is_file() and dest.stat().st_mtime >= path.stat().st_mtime:
                    continue
                text = _CONVERT[p.kind](private, p, path)
            except PublishError as e:
                failures.append(f"publish {p.name}: {redact(str(e))}")
                continue
            except Exception as e:  # a converter fault is a failure of that publish only
                failures.append(f"publish {p.name}: {type(e).__name__} in the converter")
                continue
            if not dry_run:
                config.save_text(dest, text)
            written.append(rel)
    return written, failures


def _area(private: Private, p: Publish, path: Path) -> str:
    if private.ge_um2 <= 0:
        raise PublishError("ge_um2 of the private file is not set")
    try:
        rows = metrics.parse_area_hier(path.read_text(errors="replace"))
    except (ValueError, StopIteration):
        raise PublishError("not a hierarchical area report of Synopsys or OpenROAD") from None
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["instance", "depth", "ge", "local_ge", "cells"])
    for r in rows:
        if p.depth and r["depth"] > p.depth:
            continue
        w.writerow([redact(r["instance"]), r["depth"], round(r["value"] / private.ge_um2, 3),
                    round(r["local"] / private.ge_um2, 3), "" if r["cells"] is None else r["cells"]])
    return buf.getvalue()


def _timing(private: Private, p: Publish, path: Path) -> str:
    paths = parse_timing(path.read_text(errors="replace"))
    if not paths:
        raise PublishError("no timing path with a slack in the file")
    for t in paths:
        for pt in t["points"]:
            if "cell" in pt:
                pt["cell"] = "port" if pt["cell"] in ("in", "out", "inout") else cell_class(private, pt["cell"])
    paths.sort(key=lambda t: t["slack"])
    # Setup (max) and hold (min) slacks never share a sum; a report without a path type has setup paths.
    summary: dict[str, dict[str, Any]] = {}
    for t in paths:
        t["type"] = t["type"] or "max"
        s = summary.setdefault(t["type"], {"paths": 0, "worst_slack": t["slack"], "violated": 0, "negative_sum": 0.0,
                                           "groups": {}})
        g = s["groups"].setdefault(t["group"], {"paths": 0, "worst_slack": t["slack"], "violated": 0, "negative_sum": 0.0})
        for x in (s, g):
            x["paths"] += 1
            x["violated"] += t["slack"] < 0
            x["negative_sum"] = round(x["negative_sum"] + min(t["slack"], 0.0), 6)
    kept = paths[:p.paths] if p.paths else paths
    if not p.points:
        kept = [{k: v for k, v in t.items() if k != "points"} for t in kept]
    doc = {"kind": "timing", "unit": "ns", "paths_in_file": len(paths), "summary": summary, "paths": kept}
    return json.dumps(redact_obj(doc), indent=1) + "\n"


def _values(private: Private, p: Publish, path: Path) -> str:
    text = path.read_text(errors="replace")
    out: dict[str, float | None] = {}
    for name, spec in p.values.items():
        s = spec if isinstance(spec, dict) else {"regex": spec}
        m = Metric(name=name, regex=str(s["regex"]), reduce=str(s.get("reduce", "first")), optional=True)
        got = metrics._regex(m, text)
        if got is None:
            out[name] = None
            continue
        value = got[0] * float(s.get("scale", 1.0))  # type: ignore[arg-type]
        if s.get("ge"):
            if private.ge_um2 <= 0:
                raise PublishError(f"values.{name} has ge = true, but ge_um2 of the private file is not set")
            value /= private.ge_um2
        out[name] = round(value, 6)
    return json.dumps({"kind": "values", "values": out}, indent=1) + "\n"


def _text(private: Private, p: Publish, path: Path) -> str:
    keep = [re.compile(k) for k in p.keep]
    lines = [redact(ln) for ln in path.read_text(errors="replace").splitlines() if any(k.search(ln) for k in keep)]
    return "".join(f"{ln}\n" for ln in lines)


def _python(private: Private, p: Publish, path: Path) -> str:
    fn, _ = config.load_hook(private.path.parent, p.python)
    ctx = SimpleNamespace(ge_um2=private.ge_um2, cell=lambda name: cell_class(private, name), redact=redact)
    args: tuple[Any, ...] = (path, ctx)
    try:
        inspect.signature(fn).bind(*args)
    except TypeError:
        args = (path,)  # a hook of the form (path)
    got = fn(*args)
    if not isinstance(got, dict):
        raise PublishError(f"the hook returned a {type(got).__name__}, not a dict")
    return json.dumps(redact_obj(got), indent=1, default=str) + "\n"


_CONVERT = {"area": _area, "timing": _timing, "values": _values, "text": _text, "python": _python}


# --- timing reports

_NUM = re.compile(r"^-?\d+(\.\d*)?([eE][-+]?\d+)?$")
_PIN = re.compile(r"^(\S+) \(([^()]*)\)$")
_EDGE = {"r": "r", "f": "f", "^": "r", "v": "f"}


def _num(word: str) -> float | None:
    word = word.rstrip("*&#H")
    return float(word) if _NUM.match(word) else None


def parse_timing(text: str) -> list[dict[str, Any]]:
    """The paths of a Synopsys `report_timing` or an OpenROAD `report_checks`: per path the group, the type, the start
    and end points, the slack, the arrival and required times, the count of cells on the data path and its points.

    A point with a cell is `{pin, cell, incr, time, edge}` with the library cell name as the report gives it; any
    other point, such as the clock network delay or the setup time, is `{what, incr, time}`. A net line is left out,
    and so is any table above the points, such as the wire load table that names the library.
    """
    paths: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    layout = ""  # "synopsys": the description first; "openroad": the numbers first
    data = True  # in the data path, before the first `data arrival time`
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Startpoint:"):
            cur = {"group": "", "type": "", "scenario": "", "startpoint": s.split(None, 2)[1] if len(s.split()) > 1 else "",
                   "endpoint": "", "slack": None, "arrival": None, "required": None, "cells": 0, "points": []}
            layout, data = "", True
            continue
        if cur is None:
            continue
        if s.startswith("Endpoint:"):
            cur["endpoint"] = s.split(None, 2)[1] if len(s.split()) > 1 else ""
        elif s.startswith("Path Group:"):
            cur["group"] = s.partition(":")[2].strip()
        elif s.startswith("Path Type:"):
            cur["type"] = s.partition(":")[2].split()[0] if s.partition(":")[2].split() else ""
        elif s.startswith(("Scenario:", "Corner:")):
            cur["scenario"] = s.partition(":")[2].strip()
        elif not layout and s.startswith("Point") and s.split()[-2:] == ["Incr", "Path"]:
            layout = "synopsys"
        elif not layout and s.split()[-3:] == ["Delay", "Time", "Description"]:
            layout = "openroad"
        elif layout and s and s.strip("- "):  # a rule is dashes only; a row may start with a negative number
            point = _point(s, layout)
            if point is None:
                continue
            what = point.get("what", "")
            if what.startswith("slack"):
                cur["slack"] = point["time"]
                cur["cells"] = len({pt["pin"].rpartition("/")[0] for pt in cur["points"]
                                    if pt.get("cell") not in (None, "in", "out", "inout")})
                if cur["slack"] is not None:
                    paths.append(cur)
                cur, layout = None, ""
            elif what == "data arrival time":
                cur["arrival"] = point["time"] if cur["arrival"] is None else cur["arrival"]
                data = False
            elif what == "data required time":
                cur["required"] = point["time"] if cur["required"] is None else cur["required"]
            elif data:
                cur["points"].append(point)
    return paths


def _point(s: str, layout: str) -> dict[str, Any] | None:
    """One point line: its description and its last two numbers, the increment and the time."""
    words = s.split()
    edge = ""
    if layout == "openroad":
        nums: list[float] = []
        while words and _num(words[0]) is not None:
            nums.append(_num(words.pop(0)))  # type: ignore[arg-type]
        if words and words[0] in ("^", "v"):
            edge = _EDGE[words.pop(0)]
        desc = " ".join(words)
    else:
        if words and words[-1] in ("r", "f"):
            edge = words.pop()
        nums = []
        while words and _num(words[-1]) is not None:
            nums.insert(0, _num(words.pop()))  # type: ignore[arg-type]
        while words and words[-1] in ("*", "&", "#", "H"):
            words.pop()
            while words and _num(words[-1]) is not None:
                nums.insert(0, _num(words.pop()))  # type: ignore[arg-type]
        desc = " ".join(words)
    if not desc:
        return None
    incr, time = (nums[-2], nums[-1]) if len(nums) >= 2 else (None, nums[-1] if nums else None)
    m = None if desc.startswith("slack") else _PIN.match(desc)
    if m and m.group(2) != "net" and " " not in m.group(2):
        return {"pin": m.group(1), "cell": m.group(2), "incr": incr, "time": time, "edge": edge}
    if m and m.group(2) == "net":
        return None
    return {"what": desc, "incr": incr, "time": time}
