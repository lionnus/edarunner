"""Load and validate the four TOML files, and read and write the JSON state files."""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from .model import (
    Batch, BotCommand, Budget, Host, Job, Licence, Limits, Metric, Needs,
    Placement, Project, Retry, Safety, Site, Source, Stage, Sync, Task, Telegram,
)

T = TypeVar("T")
PathLike = str | os.PathLike[str]

# `${VAR}` belongs to the shell, so a `$` before the brace is not a placeholder.
_PH = re.compile(r"(?<!\$)\{([\w.]+)\}")
_PROJECT_KEYS = {
    "schema", "project", "site", "state", "data", "run_prefix", "telegram_poll", "telegram",
    "source", "sync", "safety", "limits", "placement", "stages", "metrics", "env",
}
_SITE_KEYS = {"schema", "scratch", "env", "ssh", "tool_procs", "hosts", "licences", "nfs_export", "telegram"}
_SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
_EXTRACTORS = ("regex", "csv", "json", "python", "expr")


class ConfigError(Exception):
    """A config file is wrong. The message names the file and the key."""


# --- placeholders

def render(template: str, values: Mapping[str, object]) -> str:
    """Fill every {name} and {a.b} from `values`; a missing name is a ConfigError."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise ConfigError(f"missing placeholder {{{key}}} in '{template}'")
        return str(values[key])

    return _PH.sub(sub, template)


def _flag(raw: dict, key: str, file: Path) -> bool:
    value = raw.get(key, True)
    if not isinstance(value, bool):
        raise ConfigError(f"{file}: {key} must be true or false")
    return value


def placeholders(project: Project, **extra: object) -> dict[str, object]:
    """The placeholder dict of a project plus `extra`; a dict value flattens to dotted keys."""
    out: dict[str, object] = {
        "project": project.project,
        "project_root": str(project.root),
        "site_dir": str(project.site.path.parent),
        "user": getpass.getuser(),
    }
    for key, value in extra.items():
        if isinstance(value, dict) and key == "overrides":
            out[key] = " ".join(f"{k}={v}" for k, v in value.items())
        elif isinstance(value, dict):
            out.update({f"{key}.{k}": v for k, v in value.items()})
        else:
            out[key] = value
    return out


def load_hook(root: Path, spec: str) -> tuple[Callable[..., Any], str]:
    """Import `python:<file>:<function>` (or `<file>:<function>`) relative to `root`."""
    spec = spec.removeprefix("python:")
    file, _, func = spec.rpartition(":")
    if not file or not func:
        raise ConfigError(f"'{spec}' is not <file>:<function>")
    path = Path(os.path.abspath(root / Path(file).expanduser()))
    mod_spec = importlib.util.spec_from_file_location(path.stem, path)
    if mod_spec is None or mod_spec.loader is None:
        raise ConfigError(f"{path}: cannot import")
    module = importlib.util.module_from_spec(mod_spec)
    try:
        mod_spec.loader.exec_module(module)
    except Exception as e:
        raise ConfigError(f"{path}: {e}") from None
    fn = getattr(module, func, None)
    if not callable(fn):
        raise ConfigError(f"{path} has no function '{func}'")
    return fn, f"{file}:{func}"


# --- JSON state files

def load_json(path: PathLike) -> Any:
    """The parsed JSON of `path`; {} when the file is absent, or unreadable after three tries."""
    for _ in range(3):
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def save_text(path: PathLike, text: str) -> None:
    """Write `text` by a temporary file and a rename, so a reader never sees a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_json(path: PathLike, obj: object) -> None:
    """Write `obj` as indented JSON by a temporary file and a rename."""
    save_text(path, json.dumps(obj, indent=1) + "\n")


# --- table helpers

def _read(file: Path) -> dict[str, Any]:
    try:
        with open(file, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{file}: {e}") from None


def _dot(keypath: str, key: str) -> str:
    return f"{keypath}.{key}" if keypath else key


def _table(raw: object, allowed: set[str] | None, file: Path, keypath: str) -> dict[str, Any]:
    """Refuse a value that is not a table and every key outside `allowed` (None: any key)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{file}: '{keypath or 'top level'}' must be a table")
    if allowed is not None:
        for key in raw:
            if key not in allowed:
                raise ConfigError(f"{file}: unknown key '{_dot(keypath, key)}'")
    return raw


def _need(raw: Mapping[str, Any], key: str, file: Path, keypath: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{file}: missing key '{_dot(keypath, key)}'")
    return raw[key]


def _schema(raw: Mapping[str, Any], file: Path) -> None:
    if raw.get("schema", 1) != 1:
        raise ConfigError(f"{file}: schema {raw['schema']!r} is not 1")


def _typed(value: object, hint: Any) -> bool:
    """True when `value` fits `hint`: a scalar by its class, a container by its outer type."""
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        return any(_typed(value, a) for a in get_args(hint))
    cls = origin or hint
    if isinstance(value, bool):
        return cls is bool
    if cls is float and isinstance(value, int):
        return True
    return isinstance(value, cls)


def _type_name(hint: Any) -> str:
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        return " or ".join(_type_name(a) for a in get_args(hint) if a is not NoneType)
    return (origin or hint).__name__


def _build(cls: type[T], raw: object, file: Path, keypath: str, **fixed: Any) -> T:
    """Fill a dataclass from a table; the fields of the model are the allowed keys and types."""
    allowed = {f.name for f in fields(cls)} - set(fixed)  # type: ignore[arg-type]
    raw = _table(raw, allowed, file, keypath)
    hints = get_type_hints(cls)
    for key, value in raw.items():
        if not _typed(value, hints[key]):
            raise ConfigError(f"{file}: {_dot(keypath, key)} must be {_type_name(hints[key])}, not {type(value).__name__}")
    try:
        return cls(**fixed, **raw)
    except TypeError as e:
        raise ConfigError(f"{file}: {keypath}: {e}") from None


def _path(value: str, base: Path, values: Mapping[str, object] | None = None) -> Path:
    """Expand ~ and placeholders; a relative path is relative to the directory of `base`."""
    text = render(value, values) if values is not None else value
    return Path(os.path.abspath(base.parent / Path(text).expanduser()))


# --- site.toml

def load_site(path: PathLike) -> Site:
    """Load site.toml; `path` may start with ~."""
    file = Path(os.path.abspath(Path(path).expanduser()))
    raw = _table(_read(file), _SITE_KEYS, file, "")
    _schema(raw, file)
    ssh = _table(raw.get("ssh", {}), {"options", "timeout_s"}, file, "ssh")
    hosts = {n: _build(Host, t, file, f"hosts.{n}", name=n)
             for n, t in _table(raw.get("hosts", {}), None, file, "hosts").items()}
    licences = {n: _build(Licence, t, file, f"licences.{n}", name=n)
                for n, t in _table(raw.get("licences", {}), None, file, "licences").items()}
    telegram = _telegram(raw["telegram"], file, None, {f.name for f in fields(Telegram)}) if "telegram" in raw else None
    return Site(
        path=file,
        scratch=_need(raw, "scratch", file, ""),
        env=raw.get("env", {}),
        ssh_options=ssh.get("options", _SSH_OPTIONS),
        ssh_timeout_s=ssh.get("timeout_s", 45),
        tool_procs=raw.get("tool_procs", ""),
        hosts=hosts,
        licences=licences,
        nfs_export=raw.get("nfs_export", ""),
        telegram=telegram,
    )


def _telegram(raw: object, file: Path, base: Telegram | None, allowed: set[str]) -> Telegram:
    """The [telegram] table of `file`; a key it leaves out keeps its value from `base`."""
    tg = dict(_table(raw, allowed, file, "telegram"))
    token = tg.get("token_file", "~/.config/edarunner/telegram.token" if base is None else None)
    if token is not None:
        if not isinstance(token, str):
            raise ConfigError(f"{file}: telegram.token_file must be str, not {type(token).__name__}")
        tg["token_file"] = _path(token, file)
    if "commands" in tg:
        tg["commands"] = {n: _build(BotCommand, t, file, f"telegram.commands.{n}", name=n)
                          for n, t in _table(tg["commands"], None, file, "telegram.commands").items()}
    if base is None:
        _need(tg, "chat_id", file, "telegram")
    return _build(Telegram, {**({f.name: getattr(base, f.name) for f in fields(Telegram)} if base else {}), **tg},
                  file, "telegram")


# --- edr.toml and tasks.toml

def load_project(project_dir: PathLike, site_path: PathLike | None = None) -> Project:
    """Load edr.toml, the site it names (or `site_path`, relative to the project), and tasks.toml."""
    root = Path(os.path.abspath(Path(project_dir).expanduser()))
    file = root / "edr.toml"
    raw = _table(_read(file), _PROJECT_KEYS, file, "")
    _schema(raw, file)
    name = _need(raw, "project", file, "")
    values: dict[str, object] = {"project": name, "project_root": str(root), "user": getpass.getuser()}
    if site_path is None:
        site = load_site(_path(_need(raw, "site", file, ""), file, values))
    else:
        site = load_site(root / Path(site_path).expanduser())
    values["site_dir"] = str(site.path.parent)
    if "telegram" in raw:
        site.telegram = _telegram(raw["telegram"], file, site.telegram, {"token_file", "chat_id", "user_id", "topic_id"})

    src = _table(raw.get("source", {}), {f.name for f in fields(Source)}, file, "source")
    source = Source(
        repo=_path(_need(src, "repo", file, "source"), file, values),
        worktrees=_path(_need(src, "worktrees", file, "source"), file, values),
        ref=src.get("ref", "HEAD"),
        nested=src.get("nested", []),
        run_id=src.get("run_id", "{date}_{label}_{build_tag}_g{src}"),
        build_tag=src.get("build_tag", ""),
    )
    stages = {n: _stage(n, t, file) for n, t in _table(raw.get("stages", {}), None, file, "stages").items()}
    for stage in stages.values():
        _check_stage(stage, stages, site, file)
    metrics = {n: _metric(n, t, stages, file)
               for n, t in _table(raw.get("metrics", {}), None, file, "metrics").items()}
    tasks, resolver = _load_tasks(root / "tasks.toml", site)
    return Project(
        root=root,
        project=name,
        site=site,
        state=_path(raw.get("state", "~/.edr/{project}"), file, values),
        data=_path(raw.get("data", "data"), file, values),
        run_prefix=raw.get("run_prefix", "{user}/edr/{project}"),
        telegram_poll=_flag(raw, "telegram_poll", file),
        source=source,
        sync=_build(Sync, {"exclude": [], **raw.get("sync", {})}, file, "sync"),
        safety=_build(Safety, {"marker": "/edr/", **raw.get("safety", {})}, file, "safety"),
        limits=_build(Limits, raw.get("limits", {}), file, "limits"),
        placement=_build(Placement, raw.get("placement", {}), file, "placement"),
        stages=stages,
        metrics=metrics,
        env={str(k): str(val) for k, val in _table(raw.get("env", {}), None, file, "env").items()},
        tasks=tasks,
        task_resolver=resolver,
    )


def _stage(name: str, raw: object, file: Path) -> Stage:
    at = f"stages.{name}"
    raw = dict(_table(raw, None, file, at))
    for key, cls in (("needs", Needs), ("budget", Budget), ("retry", Retry)):
        if key in raw:
            raw[key] = _build(cls, raw[key], file, f"{at}.{key}")
    return _build(Stage, raw, file, at, name=name)


def _check_stage(stage: Stage, stages: dict[str, Stage], site: Site, file: Path) -> None:
    at = f"stages.{stage.name}"
    if not stage.cmd:
        raise ConfigError(f"{file}: {at} needs cmd")
    if stage.is_group and not stage.task_dir:
        raise ConfigError(f"{file}: {at} is a task group and needs task_dir")
    _check_licence(stage.needs, site, file, f"{at}.needs.licence")


def _check_licence(needs: Needs | None, site: Site, file: Path, at: str) -> None:
    lic = needs.licence if needs else None
    for name in [lic] if isinstance(lic, str) else list(lic or {}):
        if name not in site.licences:
            known = ", ".join(sorted(site.licences)) or "none"
            raise ConfigError(f"{file}: {at} names unknown licence '{name}' (site has: {known})")


def _metric(name: str, raw: object, stages: dict[str, Stage], file: Path) -> Metric:
    at = f"metrics.{name}"
    raw = dict(_table(raw, None, file, at))
    stage = raw.get("stage", [])
    raw["stage"] = [stage] if isinstance(stage, str) else stage
    for s in raw["stage"]:
        if s not in stages:
            raise ConfigError(f"{file}: {at}.stage names unknown stage '{s}'")
    if "step" in raw:
        raw["step"] = str(raw["step"])
    if "csv" in raw:
        _table(raw["csv"], {"where", "column"}, file, f"{at}.csv")
    if sum(k in raw for k in _EXTRACTORS) != 1:
        raise ConfigError(f"{file}: {at} needs exactly one of {', '.join(_EXTRACTORS)}")
    if "expr" not in raw and not (raw.get("file") and raw["stage"]):
        raise ConfigError(f"{file}: {at} needs file and stage")
    return _build(Metric, raw, file, at, name=name)


def _load_tasks(file: Path, site: Site) -> tuple[dict[str, Task], str]:
    if not file.exists():
        return {}, ""
    raw = _table(_read(file), {"tasks", "pattern"}, file, "")
    tasks = {i: _task(i, t, site, file, f"tasks.{i}")
             for i, t in _table(raw.get("tasks", {}), None, file, "tasks").items()}
    pattern = _table(raw.get("pattern", {}), {"resolver"}, file, "pattern")
    return tasks, pattern.get("resolver", "")


def _task(task_id: str, raw: object, site: Site, file: Path, at: str) -> Task:
    raw = _table(raw, None, file, at)
    needs = _build(Needs, raw["needs"], file, f"{at}.needs") if "needs" in raw else None
    budget = _build(Budget, raw["budget"], file, f"{at}.budget") if "budget" in raw else None
    _check_licence(needs, site, file, f"{at}.needs.licence")
    values = {k: str(v) for k, v in raw.items() if k not in ("needs", "budget")}
    return Task(id=task_id, fields=values, needs=needs, budget=budget)


def resolve_task(project: Project, task_id: str) -> Task:
    """Find a task in the table, else through the [pattern] resolver of tasks.toml."""
    if task_id in project.tasks:
        return project.tasks[task_id]
    if not project.task_resolver:
        raise ConfigError(f"unknown task '{task_id}' and tasks.toml has no [pattern] resolver")
    fn, where = load_hook(project.root, project.task_resolver)
    table = fn(task_id)
    if not table:
        raise ConfigError(f"unknown task '{task_id}': {where} returned nothing")
    return _task(task_id, table, project.site, project.root / "tasks.toml", f"{where}({task_id!r})")


# --- jobs/<batch>.toml

def load_batch(project: Project, path: PathLike) -> Batch:
    """Load jobs/<batch>.toml; `path` is a batch name or a .toml path relative to the project."""
    p = Path(path).expanduser()
    if p.suffix != ".toml":
        p = project.root / "jobs" / f"{p}.toml"
    file = Path(os.path.abspath(project.root / p))
    raw = _table(_read(file), {"batch", "source", "job"}, file, "")
    entries = raw.get("job", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{file}: 'job' must be an array of tables ([[job]])")
    jobs = [_job(t, i, project, file) for i, t in enumerate(entries)]
    seen: set[str] = set()
    for job in jobs:
        if job.label in seen:
            raise ConfigError(f"{file}: two jobs share the label '{job.label}'")
        seen.add(job.label)
    return Batch(batch=raw.get("batch", file.stem), source=_need(raw, "source", file, ""), jobs=jobs, path=file)


def _job(raw: object, index: int, project: Project, file: Path) -> Job:
    at = f"job[{index}]"
    raw = dict(_table(raw, None, file, at))
    raw["overrides"] = {k: str(v) for k, v in raw.get("overrides", {}).items()}
    for s in raw.get("stages", []):
        if s not in project.stages:
            raise ConfigError(f"{file}: {at}.stages names unknown stage '{s}'")
    reuse = raw.get("reuse")
    if reuse is not None:
        _table(reuse, {"run_id", "label", "latest", "restore"}, file, f"{at}.reuse")
        label = reuse.get("label", "")
        if "run_id" not in reuse and not (label and reuse.get("latest")):
            raise ConfigError(f"{file}: {at}.reuse needs run_id, or label with latest = true")
        if any(c in label for c in "*?["):
            raise ConfigError(f"{file}: {at}.reuse.label '{label}' is a glob")
    return _build(Job, raw, file, at)
