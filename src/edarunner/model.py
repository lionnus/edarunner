"""The data model shared by every module. See docs/design.md section 3.

`config.py` fills these from the TOML files. Nothing here reads a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Host:
    name: str
    cores: int
    ram_gb: int
    scratch: list[str] | None = None  # None: the site default


@dataclass
class Licence:
    name: str
    feature: str
    floor: int
    probe: str
    seats_per_task: int = 1


@dataclass
class BotCommand:
    name: str
    help: str
    run: list[str]
    args: dict[str, str] = field(default_factory=dict)  # arg name -> regex
    skip_if: list[str] | None = None
    skip_reply: str = ""
    reply: str = ""
    detach: bool = False
    timeout_s: int = 60
    cwd: str = ""
    dry_run: bool = False


@dataclass
class Telegram:
    token_file: Path
    chat_id: int
    commands: dict[str, BotCommand] = field(default_factory=dict)
    user_id: int | None = None  # None: the chat is the only gate


@dataclass
class Site:
    path: Path
    scratch: list[str]
    env: dict[str, str]
    ssh_options: list[str]
    ssh_timeout_s: int
    tool_procs: str
    hosts: dict[str, Host]
    licences: dict[str, Licence] = field(default_factory=dict)
    nfs_export: str = ""
    telegram: Telegram | None = None


@dataclass
class Needs:
    cores: int = 1
    disk_gb: float = 0.0
    licence: str | dict[str, int] | None = None  # "fc" or {"questa": 1}


@dataclass
class Budget:
    hours: float | None = None
    disk_gb: float | None = None
    kill: bool = False
    per: str = "stage"  # "stage" or "task"


@dataclass
class Retry:
    match: str
    wait_s: int = 900
    max: int = 3


@dataclass
class Stage:
    name: str
    after: str | dict[str, str] = ""  # "", "pnr", or {"stage": "pnr", "step": "route"}
    cmd: str = ""
    resume: str = ""
    cwd: str = "."
    steps: list[str] = field(default_factory=list)
    progress: str = ""
    needs: Needs = field(default_factory=Needs)
    budget: Budget = field(default_factory=Budget)
    retry: Retry | None = None
    collect: list[str] = field(default_factory=list)
    collect_on_request: dict[str, list[str]] = field(default_factory=dict)
    prune: dict[str, list[str]] = field(default_factory=dict)
    foreach: str = ""  # "" or "tasks"
    parallel: int = 1
    prepare: str = ""
    task_dir: str = ""
    after_each: str = ""

    @property
    def is_group(self) -> bool:
        return self.foreach == "tasks"


@dataclass
class Metric:
    name: str
    stage: list[str]
    step: str | None = None  # "*", a number as text, or None
    file: str = ""
    regex: str = ""
    csv: dict[str, object] | None = None  # {"where": {...}, "column": "..."}
    json: str = ""
    python: str = ""  # "module.py:function"
    expr: str = ""
    unit: str = ""
    canonical: str = ""


@dataclass
class Task:
    id: str
    fields: dict[str, str]  # every key of the task table, for {task.<key>}
    needs: Needs | None = None
    budget: Budget | None = None


@dataclass
class Source:
    repo: Path
    worktrees: Path
    ref: str
    nested: list[str]
    run_id: str
    build_tag: str  # "" or "python:file.py:function"


@dataclass
class Sync:
    exclude: list[str]
    after: str = ""


@dataclass
class Safety:
    marker: str
    min_depth: int = 4


@dataclass
class Limits:
    stagger_s: int = 120
    stale_s: int = 600
    dead_s: int = 2700
    hung_s: int = 21600
    grace_s: int = 3600
    host_free_min_gb: float = 100.0
    streak: int = 3
    heartbeat_s: int = 60
    gate_max_s: int = 14400
    kill_hung: bool = False
    kill_orphan: bool = False


@dataclass
class Placement:
    max_per_host: int = 2
    min_free_cores: int = 16
    min_free_ram_gb: int = 60
    avoid: list[str] = field(default_factory=list)
    prefer: list[str] = field(default_factory=list)


@dataclass
class Project:
    root: Path  # the project directory
    project: str
    site: Site
    state: Path
    data: Path
    run_prefix: str
    source: Source
    sync: Sync
    safety: Safety
    limits: Limits
    placement: Placement
    stages: dict[str, Stage]  # in file order
    metrics: dict[str, Metric]
    env: dict[str, str] = field(default_factory=dict)  # the flow's own additions, rendered per run
    tasks: dict[str, Task] = field(default_factory=dict)
    task_resolver: str = ""  # "python:file.py:function" or ""


@dataclass
class Job:
    label: str
    config: str
    host: str = "auto"
    stages: list[str] = field(default_factory=list)  # [] means every stage
    tasks: list[str] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)
    netlist_stage: int | None = None
    reuse: dict[str, object] | None = None  # {"run_id": ...} or {"label": ..., "latest": True}


@dataclass
class Batch:
    batch: str
    source: str  # a short hash, or "<hash>-dirty-<8 hex>"
    jobs: list[Job]
    path: Path
