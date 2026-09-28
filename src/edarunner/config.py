"""Load and validate the five TOML files, and read and write the JSON state files.

`edr.toml` and `tasks.toml` live in the project directory, one `jobs/<batch>.toml` per batch next
to them, `site.toml`, wherever `site` points, with the hosts, the tools and the lab's bot
commands, and `user.toml` in `~/.config/edarunner/`, with your chat and the digest time. These
rules hold for every file:

- An unknown key is an error. A value of the wrong type is an error that names the file and the
  key path, so `cores = "16"` stops the load.
- A key without a default is required.
- A path is absolute or relative to the file that names it, and `~` expands.
- A string may hold `{placeholders}`; the last section lists them. `${VAR}` belongs to the shell
  and stays as it is.
- A hook is `<file>:<function>`, with `<file>` relative to the project directory; a leading
  `python:` is optional.

`edr check` loads all five, imports every hook, probes the hosts and plans every batch under
`jobs/`, so a wrong file stops there.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from .model import (
    BACKENDS,
    OPT_IN,
    SCHEDULERS,
    Batch,
    BotCommand,
    Budget,
    Host,
    Job,
    Limits,
    Mail,
    Metric,
    Needs,
    Ntfy,
    Placement,
    Project,
    Retry,
    Runtime,
    Safety,
    Scheduler,
    Site,
    Source,
    Stage,
    Sync,
    Task,
    Telegram,
    Tool,
    User,
    pass_rule,
)

T = TypeVar("T")
PathLike = str | os.PathLike[str]
DEFAULT_SITE = "~/.config/edarunner/site.toml"  # the site file of `edr hosts` outside a project
DEFAULT_USER = "~/.config/edarunner/user.toml"

# `${VAR}` belongs to the shell, so a `$` before the brace is not a placeholder.
_PH = re.compile(r"(?<!\$)\{([\w.]+)\}")
_PROJECT_KEYS = {
    "schema", "project", "site", "state_dir", "data", "run_prefix", "ge_um2",
    "source", "sync", "runtime", "safety", "limits", "placement", "stages", "metrics", "env",
}
_SITE_KEYS = {"schema", "scratch", "env", "ssh", "tool_procs", "host_free_min_gb", "hosts", "tools", "nfs_export",
              "telegram", "scheduler"}
_EXTRACTORS = ("regex", "csv", "json", "python", "area_hier", "table")
_REDUCE = ("first", "last", "min", "max", "sum")


class ConfigError(Exception):
    """A config file is wrong. The message names the file and the key."""


# --- placeholders

# Every placeholder edr fills: its value, and the strings that may use it.
PLACEHOLDERS = {
    "project": ("the project name", "everywhere"),
    "project_root": ("the project directory", "everywhere"),
    "site_dir": ("the directory of the site file", "everywhere"),
    "user": ("the login name", "everywhere"),
    "date": ("the pinned date of the batch, `YYYYMMDD_HHMM`", "the run id"),
    "batch": ("the batch name", "the run id, the stage strings"),
    "label": ("the label of the job", "the run id, the stage strings"),
    "config": ("the configuration name of the job; `\"\"` without one", "the run id, the stage strings"),
    "build_tag": ("the build tag of the job", "the run id, the stage strings"),
    "source": ("the source tag of the batch", "the run id, the stage strings"),
    "overrides": ("the overrides of the job as `KEY=VALUE` tokens separated by spaces", "the stage strings"),
    "vars.<name>": ("a key of the job's `vars` table", "the stage strings, `[env]`, `collect`"),
    "run_id": ("the run id", "the stage strings, `[env]`, `sync.after`"),
    "host": ("the host of the run", "the stage strings, `[env]`, `sync.after`"),
    "mount": ("the scratch mount of the host", "the stage strings, `[env]`, `sync.after`"),
    "root": ("the run tree", "the stage strings, `[env]`, `sync.after`"),
    "tree_id": ("the run id of the tree the flow writes in: the reused run's id under `reuse`, else `{run_id}`",
                "the stage strings"),
    "cores": ("`needs.cores` of the stage or task", "the stage strings"),
    "tool.<name>.version": ("the version the host lists for the tool; `\"\"` without one",
                            "the stage strings, a tool `probe`"),
    "checkpoint": ("the step name a resume starts from", "`resume`"),
    "task_dir": ("the task directory", "the strings of a task group, `collect`, metric files"),
    "task.<key>": ("a key of the task table", "the strings of a task group, `collect`, metric files"),
    "step": ("the step number", "a metric `file` with `step = \"*\"`"),
    "handle": ("the handle of the run, `label@batch`, or the shortest unique prefix of its run id when another run "
               "has the same label and batch", "a bot command sent as a reply to an alert"),
    "run_root": ("the run tree", "a bot command sent as a reply to an alert"),
}


def render(template: str, values: Mapping[str, object]) -> str:
    """Fill every `{name}` and `{a.b}` in `template` from `values`.

    A placeholder without a value is a load error that names it: `missing` for one of the
    table below that this string cannot use, `unknown` for a name edr never fills. A dict
    value flattens to dotted keys, so a task table gives `{task.kernel}`. `${VAR}` belongs
    to the shell and stays as it is.
    """

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            known = key in PLACEHOLDERS or key.startswith(("task.", "tool.", "vars."))
            raise ConfigError(f"{'missing' if known else 'unknown'} placeholder {{{key}}} in '{template}'")
        return str(values[key])

    return _PH.sub(sub, template)


def _default(cls: type, name: str) -> Any:
    """The default the model carries for a key; the loader applies it when the file leaves the key out."""
    f = next(f for f in fields(cls) if f.name == name)  # type: ignore[arg-type]
    return f.default if f.default is not MISSING else f.default_factory()  # type: ignore[misc]


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


def kept(project: Project, run: Mapping[str, Any], now: float) -> bool:
    """True while the keep file of a run holds off the watcher: for its hours from the time it was written."""
    path = project.state_dir / str(run.get("batch")) / f"{run['run_id']}.keep.json"
    try:
        return now < path.stat().st_mtime + float(load_json(path).get("hours") or 0) * 3600
    except (OSError, ValueError):
        return False


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
    raw = _read(file)
    raw = _table(raw, _SITE_KEYS, file, "")
    _schema(raw, file)
    ssh = _table(raw.get("ssh", {}), {"options", "timeout_s"}, file, "ssh")
    tools = {n: _build(Tool, t, file, f"tools.{n}", name=n)
             for n, t in _table(raw.get("tools", {}), None, file, "tools").items()}
    hosts = {n: _host(n, t, tools, file) for n, t in _table(raw.get("hosts", {}), None, file, "hosts").items()}
    commands = _commands(_table(raw.get("telegram", {}), {"commands"}, file, "telegram"), file)
    given = {k: raw[k] for k in ("env", "tool_procs", "host_free_min_gb", "nfs_export") if k in raw}
    given.update({f"ssh_{k}": v for k, v in ssh.items()})
    sched = _build(Scheduler, raw.get("scheduler", {}), file, "scheduler")
    if sched.backend not in BACKENDS:
        raise ConfigError(f"{file}: scheduler.backend {sched.backend!r} is not one of {', '.join(BACKENDS)}")
    if sched.backend == "local" and set(hosts) - {"local"}:
        raise ConfigError(f"{file}: scheduler.backend \"local\" runs on the head node; [hosts] may name only local")
    if sched.backend in SCHEDULERS and not sched.tree_root:
        raise ConfigError(f"{file}: scheduler.backend {sched.backend!r} needs scheduler.tree_root")
    given["scheduler"] = sched
    hints = get_type_hints(Site)
    for key, value in given.items():
        if not _typed(value, hints[key]):
            raise ConfigError(f"{file}: {key.replace('ssh_', 'ssh.', 1)} must be {_type_name(hints[key])}, not {type(value).__name__}")
    return Site(path=file, scratch=_need(raw, "scratch", file, ""), hosts=hosts, tools=tools, commands=commands, **given)


def load_user(path: PathLike = DEFAULT_USER) -> User:
    """Load user.toml; a missing file is a user without channels."""
    file = Path(os.path.abspath(Path(path).expanduser()))
    if not file.exists():
        return User(path=file)
    raw = _table(_read(file), {"schema", "digest_at", "alerts", "telegram", "ntfy", "mail"}, file, "")
    _schema(raw, file)
    at = raw.get("digest_at", "")
    if not isinstance(at, str) or at and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ConfigError(f"{file}: digest_at must be HH:MM or empty, not {at!r}")
    kinds = raw.get("alerts", [])
    if not isinstance(kinds, list) or not all(k in OPT_IN for k in kinds):
        raise ConfigError(f"{file}: alerts is a list of {', '.join(OPT_IN)}, not {kinds!r}")
    return User(path=file, digest_at=at, alerts=kinds,
                telegram=_telegram(raw["telegram"], file) if "telegram" in raw else None,
                ntfy=_channel(Ntfy, raw["ntfy"], file, "ntfy", "token_file") if "ntfy" in raw else None,
                mail=_channel(Mail, raw["mail"], file, "mail", "password_file") if "mail" in raw else None)


def _channel(cls: type[T], raw: object, file: Path, at: str, secret: str) -> T:
    """A notifier table; the secret file is relative to `file`, and a `from` key fills `sender`."""
    t = dict(_table(raw, None, file, at))
    if "sender" in t:
        raise ConfigError(f"{file}: unknown key '{at}.sender'")
    if cls is Mail:
        t["sender"] = _need(t, "from", file, at)
        del t["from"]
    if isinstance(t.get(secret), str):
        t[secret] = _path(t[secret], file)
    return _build(cls, t, file, at)


def _host(name: str, raw: object, tools: dict[str, Tool], file: Path) -> Host:
    """A host table; `tools` is a list of names or `{ name = version }` and is kept as a dict."""
    at = f"hosts.{name}"
    raw = dict(_table(raw, None, file, at))
    if isinstance(raw.get("tools"), list):
        raw["tools"] = {str(t): "" for t in raw["tools"]}
    host = _build(Host, raw, file, at, name=name)
    _check_tools(host.tools or {}, tools, file, f"{at}.tools")
    return host


def _check_tools(names: Mapping[str, object], tools: dict[str, Tool], file: Path, at: str) -> None:
    for name in names:
        if name not in tools:
            known = ", ".join(sorted(tools)) or "none"
            raise ConfigError(f"{file}: {at} names unknown tool '{name}' (site has: {known})")


def _telegram(raw: object, file: Path) -> Telegram:
    """The [telegram] table of user.toml."""
    tg = dict(_table(raw, {f.name for f in fields(Telegram)}, file, "telegram"))
    if not isinstance(tg.get("token_file", ""), str):
        raise ConfigError(f"{file}: telegram.token_file must be str, not {type(tg['token_file']).__name__}")
    tg["token_file"] = _path(str(tg.get("token_file") or _default(Telegram, "token_file")), file)
    tg["commands"] = _commands(tg, file)
    _need(tg, "chat_id", file, "telegram")
    return _build(Telegram, tg, file, "telegram")


def _commands(tg: Mapping[str, Any], file: Path) -> dict[str, BotCommand]:
    """The custom bot commands of the [telegram] table `tg`."""
    return {n: _build(BotCommand, t, file, f"telegram.commands.{n}", name=n)
            for n, t in _table(tg.get("commands", {}), None, file, "telegram.commands").items()}


# --- edr.toml and tasks.toml

def load_project(project_dir: PathLike, site_path: PathLike | None = None) -> Project:
    """Load edr.toml, the site it names (or `site_path`, relative to the project), and tasks.toml."""
    root = Path(os.path.abspath(Path(project_dir).expanduser()))
    file = root / "edr.toml"
    raw = _read(file)
    raw = _table(raw, _PROJECT_KEYS, file, "")
    _schema(raw, file)
    name = _need(raw, "project", file, "")
    values: dict[str, object] = {"project": name, "project_root": str(root), "user": getpass.getuser()}
    if site_path is None:
        site = load_site(_path(_need(raw, "site", file, ""), file, values))
    else:
        site = load_site(root / Path(site_path).expanduser())
    values["site_dir"] = str(site.path.parent)

    src = _table(raw.get("source", {}), None, file, "source")
    source = _build(Source, {k: v for k, v in src.items() if k not in ("repo", "worktrees")}, file, "source",
                    repo=_path(_need(src, "repo", file, "source"), file, values),
                    worktrees=_path(_need(src, "worktrees", file, "source"), file, values))
    stages = {n: _stage(n, t, file) for n, t in _table(raw.get("stages", {}), None, file, "stages").items()}
    for stage in stages.values():
        _check_stage(stage, stages, site, file)
    metrics = {n: _metric(n, t, stages, file)
               for n, t in _table(raw.get("metrics", {}), None, file, "metrics").items()}
    tasks, resolver = load_tasks(root / "tasks.toml", site) if (root / "tasks.toml").exists() else (
        {}, _default(Project, "task_resolver"))
    ge_um2 = raw.get("ge_um2", _default(Project, "ge_um2"))
    if "ge_um2" in raw and not (type(ge_um2) in (int, float) and ge_um2 > 0):
        raise ConfigError(f"{file}: ge_um2 is the area of one gate equivalent in um2, a number above 0")
    return Project(
        root=root,
        project=name,
        site=site,
        state_dir=_path(raw.get("state_dir", str(_default(Project, "state_dir"))), file, values),
        data=_path(raw.get("data", str(_default(Project, "data"))), file, values),
        run_prefix=raw.get("run_prefix", _default(Project, "run_prefix")),
        ge_um2=float(ge_um2),
        source=source,
        sync=_build(Sync, raw.get("sync", {}), file, "sync"),
        runtime=_build(Runtime, raw.get("runtime", {}), file, "runtime"),
        safety=_build(Safety, raw.get("safety", {}), file, "safety"),
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
    for key, cls in (("budget", Budget), ("retry", Retry)):
        if key in raw:
            raw[key] = _build(cls, raw[key], file, f"{at}.{key}")
    if "needs" in raw:
        raw["needs"] = _needs(raw["needs"], file, f"{at}.needs")
    if "step_log" in raw:
        log = _table(raw["step_log"], {"file", "regex"}, file, f"{at}.step_log")
        if not (log.get("file") and log.get("regex")):
            raise ConfigError(f"{file}: {at}.step_log needs file and regex")
        try:
            re.compile(str(log["regex"]))
        except re.error as e:
            raise ConfigError(f"{file}: {at}.step_log.regex: {e}") from None
    return _build(Stage, raw, file, at, name=name)


def _needs(raw: object, file: Path, at: str) -> Needs:
    """A needs table; `tools` is a list of names or `{ name = seats }` and is kept as a dict."""
    raw = dict(_table(raw, None, file, at))
    tools = raw.get("tools", {})
    if isinstance(tools, list):
        tools = {str(t): 1 for t in tools}
    if not isinstance(tools, dict) or not all(type(v) is int for v in tools.values()):
        raise ConfigError(f"{file}: {at}.tools must be a list of names or {{ name = seats }}")
    raw["tools"] = tools
    return _build(Needs, raw, file, at)


def _check_stage(stage: Stage, stages: dict[str, Stage], site: Site, file: Path) -> None:
    at = f"stages.{stage.name}"
    if not stage.cmd:
        raise ConfigError(f"{file}: {at} needs cmd")
    if stage.is_group and not stage.task_dir:
        raise ConfigError(f"{file}: {at} is a task group and needs task_dir")
    _check_tools(stage.needs.tools, site.tools, file, f"{at}.needs.tools")


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
    if "area_hier" in raw and not (type(raw["area_hier"]) is int and raw["area_hier"] >= 1):
        raise ConfigError(f"{file}: {at}.area_hier is the deepest depth to keep, a number from 1")
    if "table" in raw:
        t = _table(raw["table"], {"instance", "value", "depth", "part", "local", "where", "top", "max_depth"}, file, f"{at}.table")
        if not (t.get("instance") and t.get("value") and _table(t.get("top", {}), None, file, f"{at}.table.top")):
            raise ConfigError(f"{file}: {at}.table needs instance, value and top")
        _table(t.get("where", {}), None, file, f"{at}.table.where")
        if "max_depth" in t and not (type(t["max_depth"]) is int and t["max_depth"] >= 0):
            raise ConfigError(f"{file}: {at}.table.max_depth is the deepest depth to keep, a number from 0")
    if not (raw.get("file") and raw["stage"]):
        raise ConfigError(f"{file}: {at} needs file and stage")
    if "reduce" in raw and ("regex" not in raw or raw["reduce"] not in _REDUCE):
        raise ConfigError(f"{file}: {at}.reduce needs regex and is one of {', '.join(_REDUCE)}")
    if raw.get("scale") == 0:
        raise ConfigError(f"{file}: {at}.scale is a factor other than 0")
    rule = raw.pop("pass", "")
    try:
        if rule != "":
            pass_rule(rule)
    except (AttributeError, ValueError):
        raise ConfigError(f'{file}: {at}.pass is an operator and a number, such as "== 0"') from None
    if "record" in raw:
        rec = _table(raw["record"], {"stage", "from"}, file, f"{at}.record")
        if not (rec.get("stage") in raw["stage"] and stages[rec["stage"]].steps and "step" in raw
                and type(rec.get("from", 0)) is int):
            raise ConfigError(f'{file}: {at}.record is {{ stage = "<a stage of the metric with steps>", '
                              'from = <step number> }, and the metric needs step')
    return _build(Metric, raw, file, at, name=name, pass_=rule)


def load_tasks(file: Path, site: Site) -> tuple[dict[str, Task], str]:
    """The `[tasks.<id>]` tables and the `[pattern]` resolver of a file in the form of tasks.toml."""
    raw = _table(_read(file), {"tasks", "pattern"}, file, "")
    tasks = {i: _task(i, t, site, file, f"tasks.{i}")
             for i, t in _table(raw.get("tasks", {}), None, file, "tasks").items()}
    pattern = _table(raw.get("pattern", {}), {"resolver"}, file, "pattern")
    return tasks, pattern.get("resolver", _default(Project, "task_resolver"))


def _task(task_id: str, raw: object, site: Site, file: Path, at: str) -> Task:
    raw = _table(raw, None, file, at)
    needs = _needs(raw["needs"], file, f"{at}.needs") if "needs" in raw else None
    budget = _build(Budget, raw["budget"], file, f"{at}.budget") if "budget" in raw else None
    _check_tools(needs.tools if needs else {}, site.tools, file, f"{at}.needs.tools")
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
    for k, v in _table(raw.get("vars", {}), None, file, f"{at}.vars").items():
        if not re.fullmatch(r"[A-Za-z_]\w*", k):
            raise ConfigError(f"{file}: {at}.vars key {k!r} is not an identifier")
        if isinstance(v, (dict, list)):
            raise ConfigError(f"{file}: {at}.vars.{k} must be a string or a number")
    raw["vars"] = {k: str(v) for k, v in raw.get("vars", {}).items()}
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
