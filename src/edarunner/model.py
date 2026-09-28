"""The data model shared by every module.

`config.py` fills these from the TOML files. Nothing here reads a file. A field made with `doc`
carries the meaning of its key and the default the loader applies; the reference page reads both.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any


def doc(text: str, default: Any = MISSING, factory: Callable[[], Any] | None = None, key: str = "",
        shown: str = "") -> Any:
    """A config field with its meaning; `key` is the TOML key when it differs, `shown` a default in prose."""
    meta = {"doc": text, **({"key": key} if key else {}), **({"default": shown} if shown else {})}
    return field(default=default, default_factory=MISSING if factory is None else factory, metadata=meta)


@dataclass
class Host:
    """The host `local` is the head node itself, reached without ssh. A job that names a host outside
    this table is a `check` problem. A tool in `tools` must be declared under `[tools]`."""

    name: str
    cores: int = doc("the cores of the host")
    ram_gb: int = doc("the RAM of the host, in GB")
    scratch: list[str] | None = doc("the scratch roots of this host", None, shown="the site `scratch`")
    tools: dict[str, str] | None = doc("the tools the host has: a list of names, or `{ name = version }`; the "
                                       "version is text the flow may use as `{tool.<name>.version}`", None,
                                       shown="every tool of `[tools]`")
    host_free_min_gb: float | None = doc("the free scratch this host keeps", None, shown="the site `host_free_min_gb`")

    def has(self, tool: str) -> bool:
        """True when the host has `tool`; a host without a `tools` key has every tool."""
        return self.tools is None or tool in self.tools


@dataclass
class Tool:
    """A `[tools.<name>]` table declares one tool of the site. A stage that needs a tool with a probe starts with a gate: the driver runs
    the probe on the host and reads the first line it prints, `free` or `free total`, and waits while
    `free`, less the seats other runs leased in the last `lease_s`, is below the seats the stage
    needs; then it leases its seats in `~/.edr/leases/<tool>/`, which the runs of every project of the
    user share. A probe that fails or prints no number counts as unknown and lets the stage run. A hook that keeps a reserve for others subtracts it before it
    prints. A tool without a probe has no gate; a host either has it or does not. A name that no `[tools]` table
    declares is an error where it appears. The core knows no licence manager;
    `examples/site/hooks/flexlm_free.sh` turns `lmutil lmstat` output into the `free total` line."""

    name: str
    seats: int | None = doc("the seat total, for `edr tools`", None)
    probe: list[str] = doc("an argv list that prints the free seats; it runs on the host with the run placeholders "
                           "filled, and `edr tools` runs it on the head node with `{root}` set to the project "
                           "directory", factory=list)
    licence: str = doc("the licence or concurrency limit that counts the tool in the scheduler; a job asks it for "
                       "the most seats one of its stages needs, for its whole run", "",
                       shown="unset; the scheduler does not count the tool")


@dataclass
class BotCommand:
    """Each `[telegram.commands.<name>]` table defines one custom command of the bot: in `site.toml` for
    everyone of the lab, in `user.toml` for you alone. A command of `user.toml` replaces the one of
    the same name in `site.toml`.

    Every string renders `{project}`, `{site_dir}`, `{user}`, and one `{<name>}` per entry of `args`.
    Here `{root}` and `{project_root}` both give the project directory, not a run tree. A command sent
    as a reply to an alert also renders `{handle}`, `{run_id}`, `{run_root}` and `{host}` of that run;
    `{run_root}` is the run tree. No shell runs between the bot and
    `run[0]`. A program that parses its argument itself, such as `tmux new-session <cmd>`,
    `ssh host <cmd>` or `sh -c`, does run a shell on the rendered value, so gate every placeholder
    inside such a token with an exact allowlist regex. Every value must match its regex in full, or
    the bot replies `refused: <name> must match <regex>`, records the refusal and runs nothing.
    """

    name: str
    help: str = doc("the line in the `/` menu and in `/help`")
    run: list[str] = doc("the argv list; never a shell string")
    args: dict[str, str] = doc("argument name to regex, in order; the last argument takes the rest of the message",
                               factory=dict)
    skip_if: list[str] | None = doc("an argv list; exit 0 makes the bot reply `skip_reply` and run nothing", None)
    skip_reply: str = doc("the reply when `skip_if` passes", "skipped")
    reply: str = doc("the reply on exit 0 instead of the output", "")
    detach: bool = doc("start the command in its own session and reply with the pid; the output goes to "
                       "`data/telegram-<name>.log`. A command that ends within 5 s replies `ended with rc N: "
                       "<last output line>` instead", False)
    timeout_s: int = doc("kill the command after this many seconds", 60)
    cwd: str = doc("the working directory of `run`", "{root}")
    dry_run: bool = doc("reply with the rendered argv and run nothing", False)


@dataclass
class Telegram:
    """`[telegram]` sets up the bot, the one chat it answers and your own custom commands;
    `docs/guides/alerts.md` explains the setup."""

    chat_id: int = doc("the one chat the bot answers; a group id is negative")
    token_file: Path = doc("the bot token, mode 600", Path("~/.config/edarunner/telegram.token"))
    commands: dict[str, BotCommand] = field(default_factory=dict)
    user_id: int | None = doc("the one user whose messages and buttons the bot obeys", None,
                              shown="unset; the chat is the only gate")
    topics: bool = doc("one forum topic per project, which the bot makes the first time the project sends; the chat "
                       "must be a forum group and the bot an admin that may manage topics. Without that right the "
                       "messages go to the main thread", False)


@dataclass
class Ntfy:
    """Each alert goes to an ntfy topic as one push message, with a priority by alert kind;
    `docs/guides/alerts.md` explains the setup."""

    topic: str = doc("the topic; anyone who knows the name can read it, so pick a long random one")
    url: str = doc("the ntfy server", "https://ntfy.sh")
    token_file: Path | None = doc("an access token for a protected topic, mode 600", None)


@dataclass
class Mail:
    """Each alert and each `edr notify` goes out as one mail through an SMTP server. The board is never
    mailed."""

    host: str = doc("the SMTP server")
    sender: str = doc("the From address", key="from")
    to: list[str] = doc("the recipients")
    port: int = doc("the SMTP port", 587)
    starttls: bool = doc("upgrade the connection with STARTTLS before the login", True)
    user: str = doc("the login name", "", shown="the `from` address")
    password_file: Path | None = doc("the password, mode 600; without it there is no login", None)


# The values of `[scheduler] backend`; the last three hand the run to a batch scheduler.
SCHEDULERS = ("condor", "slurm", "lsf")
BACKENDS = ("ssh", "local", *SCHEDULERS)


@dataclass
class Scheduler:
    """`[scheduler]` decides what starts and watches a driver. With `condor`, `slurm` or `lsf` the scheduler picks the host:
    `plan` probes no host, the run tree goes under `tree_root`, and the job's `host` is the name the
    driver writes into its first heartbeat. `docs/guides/site.md` shows a Slurm site file
    and how each setting maps to HTCondor, Slurm and LSF."""

    backend: str = doc("`\"ssh\"` on the site hosts, `\"local\"` on the head node only, or `\"condor\"`, "
                       "`\"slurm\"`, `\"lsf\"`", "ssh")
    submit_via: list[str] = doc("an argv prefix of every scheduler command, such as `[\"ssh\", \"submithost\"]` or "
                                "`[\"docker\", \"exec\", \"pool\"]`; empty runs the command on the head node",
                                factory=list)
    tree_root: str = doc("where the run trees go, `<tree_root>/<run_prefix>/<run_id>`; on a filesystem the head "
                         "node and every node of the scheduler mount. Required with a scheduler", "")
    max_jobs: int = doc("runs of the project in the scheduler at once; a job above it is `queued`, and the watcher "
                        "submits it when a run ends; 0 is no limit", 0)
    queue: str = doc("the Slurm partition or the LSF queue; HTCondor has none", "")
    options: list[str] = doc("raw text: lines appended to the HTCondor submit file, `#SBATCH` options for Slurm, "
                             "extra `bsub` arguments for LSF", factory=list)


@dataclass
class User:
    """`~/.config/edarunner/user.toml` holds what belongs to one person: the chat of the bot, the ntfy
    topic, the mail, the time of the daily digest and your own bot commands. It lies next to the site
    file and stays out of git, so each user of a site file has a chat of their own. Without the file
    edarunner sends nothing."""

    path: Path
    digest_at: str = doc("the local time, `HH:MM`, of the daily digest of every registered project; empty is off", "")
    telegram: Telegram | None = None
    ntfy: Ntfy | None = None
    mail: Mail | None = None


@dataclass
class Site:
    """`site.toml` holds the hosts, the tools and the bot commands of a site; `site` in `edr.toml` names it,
    and `docs/guides/site.md` sets it up. Every project and every user of the machines shares it,
    usually as a clone of the lab's site repository at `~/.config/edarunner/`. Every remote command runs through `sh -c`, so the login
    shell of a host may be `csh` or `tcsh`."""

    path: Path
    scratch: list[str] = doc("scratch roots, in order; the largest writable one is the mount")
    env: dict[str, str] = doc("environment for every command on every host; a project `env` value that "
                             "names one of these variables as `$NAME` builds on the value set here",
                             factory=dict)
    ssh_options: list[str] = doc("the options of every ssh call", key="ssh.options",
                                 factory=lambda: ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
    ssh_timeout_s: int = doc("seconds a remote command may take", 45, key="ssh.timeout_s")
    tool_procs: str = doc("a regex over process names, for the orphan check and the host table", "")
    host_free_min_gb: float = doc("free scratch below which the driver starts nothing new on a host and placement "
                                  "sends no run there; one floor per disk, whatever project fills it", 100.0)
    nfs_export: str = doc("a path the head node reads when ssh to a host fails at collect", "")
    scheduler: Scheduler = field(default_factory=Scheduler)
    hosts: dict[str, Host] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    commands: dict[str, BotCommand] = field(default_factory=dict)


@dataclass
class Needs:
    """The resources a stage, or a task with its own `needs`, must have before it starts."""

    cores: int = doc("the cores; `{cores}` in the stage strings", 1)
    disk_gb: float = doc("free space at the run tree, in GB; below it a task is skipped, and the run fails at "
                         "its first stage", 0.0)
    tools: dict[str, int] = doc("a list of names, or `{ name = seats }`, from the site `[tools]`", factory=dict)
    ram_gb: float = doc("the RAM a scheduler reserves for the job, in GB: the most any stage of the job asks; "
                        "0 takes the scheduler's default", 0.0)


@dataclass
class Budget:
    """A limit the driver applies while the command runs; at the limit the phase becomes
    `OVER_BUDGET:<stage>`."""

    hours: float | None = doc("hours the stage may run, or each task with `per = \"task\"`", None)
    disk_gb: float | None = doc("GB the run tree may grow to", None)
    kill: bool = doc("`SIGTERM` to the process group at the limit; else the command runs to its end", False)
    per: str = doc("`\"stage\"` or `\"task\"`: what `hours` counts", "stage")


@dataclass
class Retry:
    """A stage that fails runs again when `match` is found in the last 80 lines of its log."""

    match: str = doc("a regex over the last 80 log lines")
    wait_s: int = doc("seconds before the next attempt", 900)
    max: int = doc("attempts after the first", 3)


@dataclass
class Stage:
    """A stage is one command of the flow. Stages run in the order of the file. A job runs every
    stage, or the subset its `stages` list names, in that same order.

    A flow that runs several steps inside one tool session stays one stage, and `edr` tracks the
    steps. The driver runs `progress` every 5 s in the stage's `cwd` and takes the first number it
    prints as the number of the step that runs now. `steps[step]` is the step name in the heartbeat
    and on the board, and the checkpoint the watcher resumes from. A numbered step belongs to one
    stage. Once the run reaches a later step, the watcher copies the directory of the step and
    extracts its metrics, whether the stage ends `done` or not.

    A count of the numbered report directories gives the step that runs now when the flow creates
    the directory of a step as the step ends. A flow that creates it when the step starts needs the
    highest directory that is not empty, because a count names the next step, and such a flow can
    leave empty directories for the steps it skips:

    ```toml
    progress = "find reports -mindepth 2 -maxdepth 2 -type f 2>/dev/null | awk -F/ '$(NF-1) ~ /^[0-9]+$/ { print $(NF-1) }' | sort -n | tail -1"
    ```

    The numbering starts at 0 and runs on across the stages. A later stage lists either every name
    from step 0 or only its own names. Both forms below give `synth` the steps 0 to 3 and `pnr` the
    steps 4 and 5:

    ```toml
    [stages.synth]
    steps = ["setup", "analyze", "elaborate", "synth"]

    [stages.pnr]
    steps = ["cts", "route"]  # or all six names: "setup", ..., "cts", "route"
    ```

    A stage without `steps` owns no numbered step.

    A stage with `foreach = "tasks"` is a task group: `cmd` runs once per task of the job,
    `parallel` at a time, each in its own `task_dir` with its own log, budget and result. The
    tasks of a run go through a queue in the state directory, so a second run with the same queue
    takes tasks from the same pool; `docs/guides/run.md` explains the queue and shards.
    """

    name: str
    cmd: str = doc("the command; in a task group it runs once per task", "", shown="required")
    resume: str = doc("the command with `{checkpoint}`, for a resume", "")
    cwd: str = doc("the working directory, relative to the run tree", ".")
    steps: list[str] = doc("the step names the flow passes, indexed by step number", factory=list)
    progress: str = doc("a command that prints the number of the step that runs now", "")
    needs: Needs = doc("`{ cores, disk_gb, tools, ram_gb }`; the table below", factory=Needs)
    budget: Budget = doc("`{ hours, disk_gb, kill, per }`; the table below", factory=Budget)
    retry: Retry | None = doc("`{ match, wait_s, max }`; the table below", None)
    collect: list[str] = doc("paths under the run tree the watcher copies when the stage ends; while a stage with "
                             "steps runs, it copies each numbered directory under them below the step that runs now",
                             factory=list)
    collect_on_request: dict[str, list[str]] = doc("named path sets for `edr continue --collect <name>`", factory=dict)
    prune: dict[str, list[str]] = doc("named path sets for `edr retire --prune <name>`", factory=dict)
    foreach: str = doc("`\"tasks\"` makes the stage a task group", "")
    parallel: int = doc("tasks at once in a group", 1)
    prepare: str = doc("a command once before a group starts", "")
    task_dir: str = doc("the directory of a task, relative to the tree; required in a group", "")
    after_each: str = doc("a command after each task, with `{task_dir}`", "")
    step_log: dict[str, str] = doc("`{ file, regex }`: step start times the flow writes into a collected file; "
                                   "group 1 of the regex is a unix time, and group 2, when present, the step number",
                                   factory=dict)

    @property
    def is_group(self) -> bool:
        return self.foreach == "tasks"


_OPS = {"==": operator.eq, "!=": operator.ne, "<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt}


def pass_rule(rule: str) -> tuple[Callable[[float, float], bool], float]:
    """The operator and the number of a `pass` rule such as `== 0`; ValueError for any other text."""
    rule = rule.strip()
    op = next((o for o in _OPS if rule.startswith(o)), None)
    if op is None:
        raise ValueError(f"{rule!r} starts with none of {', '.join(_OPS)}")
    return _OPS[op], float(rule[len(op):])


@dataclass
class Metric:
    """A metric holds exactly one of the five parsers: `regex`, `csv`, `json`, `python` or `area_hier`. A number
    the flow does not print, such as an energy from a power and a window, comes from a `python`
    hook that reads the input files itself.

    A regex runs with `re.MULTILINE` over the whole file, group 1 of each match is a value, and
    `reduce` picks one of them, the first by default. In a report with one block per scenario and path
    group, such as a `report_qor`, start the regex at the header of its block, or the first match may
    come from another block. The row's `source_file` is then `path:line` of the value.

    A metric row comes from a task that ended `done` or from a stage that exited 0 (done, or over
    budget without a kill). Any other stage gives the rows of the numbered steps that the run has
    passed: `step_runs` holds the step and a later step of the run. There, a report of a step that
    the run never recorded, as in a copied tree, gives no row. A `step = "*"` metric gives one row
    per step directory found, under the stage that owns that step number. A file that does not
    parse gives a failed row: an empty value and the error in `source_file`; the extraction goes
    on. A missing file gives no row, and so does a `python` hook that returns None, for a file where
    the number does not apply.
    """

    name: str
    stage: list[str] = doc("a stage name or a list: the stages whose files hold the number", factory=list,
                           shown="required")
    step: str | None = doc("`\"*\"` for one row per step, a number, or absent", None)
    file: str = doc("the file under the collected results; `{step}` and `{task_dir}` allowed", "",
                    shown="required")
    regex: str = doc("a regex; group 1 of each match is a value", "", shown="one of the five")
    reduce: str = doc("how the values of `regex` become one: `first`, `last`, `min`, `max` or `sum`; a sum "
                      "names the line of its first value", "first")
    csv: dict[str, object] | None = doc("`{ where = { column = value }, column }`; the first row that matches "
                                        "`where`, whose values take the placeholders of `file`", None,
                                        shown="one of the five")
    json: str = doc("a dotted path into a JSON file; a number indexes a list", "", shown="one of the five")
    python: str = doc("a hook that gets the file path and returns a number, or None where the number does not "
                      "apply", "", shown="one of the five")
    area_hier: int = doc("the deepest instance depth to keep from a hierarchical area report, of Synopsys "
                         "`report_area -hierarchy` or of OpenROAD `report_design_area` by hierarchy: the value is "
                         "the top area, and each instance down to this depth becomes a row of the `area` table", 0,
                         shown="one of the five")
    unit: str = doc("the unit, as text", "")
    scale: float = doc("a factor that multiplies each value at extraction, so that the stored number is in `unit`: "
                       "`scale = 1e-6` with `unit = \"ns\"` turns a window the file gives in fs into ns. Every view, "
                       "the export, MLflow and `pass` see the product, and `area_hier` scales its instance rows too", 1.0)
    canonical: str = doc("the METRICS2.1 name of the number, as OpenROAD writes it without the stage prefix: "
                         "`design__instance__area`, `design__instance__count`, `design__instance__utilization`, "
                         "`timing__setup__ws`, `timing__setup__tns`, `power__total`, `runtime__total`; "
                         "empty when the schema has no name", "")
    pass_: str = doc("a rule the value must meet: `==`, `!=`, `<`, `<=`, `>` or `>=` and a number, such as "
                     "`\"== 0\"`; `edr metrics`, `edr compare` and `--over steps` print FAIL next to a value that "
                     "breaks it", "", key="pass", shown="unset")
    record: dict[str, object] | None = doc(
        "`{ stage, from }`: the step of record of a run is its deepest step of `stage` with a value, at or after step "
        "`from`. `edr compare`, compare.html, the MLflow export and the `record` column of metrics.csv take that "
        "step, and a run without one is named missing. The stage needs `steps`, and the metric needs `step`", None,
        shown="unset")
    optional: bool = doc("a file without the number gives no row instead of a failed one: no match of `regex`, "
                         "no row of `csv` that matches `where`, no key of `json`", False)

    def verdict(self, value: float | None) -> str | None:
        """`FAIL` when `value` breaks the `pass` rule, `pass` when it meets it; None without a rule or a value."""
        if not self.pass_ or value is None:
            return None
        op, limit = pass_rule(self.pass_)
        return "pass" if op(value, limit) else "FAIL"


@dataclass
class Task:
    """`tasks.toml` is optional. A task is a table `[tasks.<id>]`, and every key of a task is a
    placeholder `{task.<key>}` in the strings of a task group.

    ```toml
    [tasks.softmax_197]
    kernel = "softmax"
    args = "ROWS=197 COLS=197"
    test = "SOFTMAX_R197_C197"
    needs = { disk_gb = 60 }
    budget = { hours = 8 }
    ```
    """

    id: str
    fields: dict[str, str] = doc("any key; `{task.<key>}` in the stage strings", key="tasks.<id>.<key>", shown="unset")
    needs: Needs | None = doc("`{ cores, disk_gb, tools, ram_gb }`; replaces the stage's", None, key="tasks.<id>.needs")
    budget: Budget | None = doc("`{ hours, disk_gb, kill, per }`; replaces the stage's", None, key="tasks.<id>.budget")


@dataclass
class Source:
    """`[source]` names the git repository of the flow and sets how `edr checkout` pins a version of it."""

    repo: Path = doc("the git repository of the flow")
    worktrees: Path = doc("where `edr checkout` puts a local clone per commit")
    ref: str = doc("the ref `edr checkout` takes without an argument, and the one `edr brief` counts the lag of each "
                   "source against", "HEAD")
    nested: list[str] = doc("nested repositories inside the tree, cloned at the HEAD the repository copy has; a run "
                            "records the commit of each as the parameter `nested.<name>`, and their changes count in "
                            "a dirty tag", factory=list)
    run_id: str = doc("the run id template; the `g` in the default marks the git source tag that follows",
                      "{date}_{label}_{build_tag}_g{source}")
    build_tag: str = doc("a hook that returns the build tag from `(config, overrides, worktree)` or from "
                         "`(config, overrides)`; empty gives the config name followed by `_KEYVALUE` for each "
                         "override, such as `base_FREQ500`", "")


@dataclass
class Sync:
    """The checked-out tree goes to the host with `rsync --delete`, behind the guard."""

    exclude: list[str] = doc("rsync exclude patterns for the copy of the tree; `.git` goes along unless "
                             "the list names it", factory=list)
    after: str = doc("a command on the head node after each sync, with the run placeholders", "")


@dataclass
class Runtime:
    """`setup` is one command that prepares the run tree on the host, such as `uv sync --frozen` for a Python
    environment. The driver runs it in the tree root after the sync and before the first stage, with
    the environment of the stages, and logs it to `log/setup.log`. A failure ends the run
    `FAILED:runtime` before any stage takes a tool seat."""

    setup: str = doc("the command; it takes the run placeholders", "", shown="none")
    when_changed: list[str] = doc("files relative to the tree root; on a tree that ran the same `setup` before, "
                                  "such as under `edr continue`, the command runs again only when one of them "
                                  "changed. Empty runs it every time", factory=list)


@dataclass
class Safety:
    """Every delete target passes this guard; `docs/how-it-works.md` explains it."""

    marker: str = doc("a substring every delete target must hold", "/edr/")
    min_depth: int = doc("the smallest path depth of a delete target", 4)


@dataclass
class Limits:
    """These are the timeouts and thresholds of the driver and the watcher;
    `docs/how-it-works.md` says what each one does."""

    stagger_s: int = doc("pause between two launches of one batch", 120)
    stale_s: int = doc("heartbeat age that marks a run `stale`", 600)
    dead_s: int = doc("heartbeat age that marks a run `dead`", 2700)
    hung_s: int = doc("time without progress that marks a run `hung`", 21600)
    grace_s: int = doc("wait between an alert and the watcher's stop or kill", 3600)
    streak: int = doc("equal failure signatures in a row that stop a task group", 3)
    heartbeat_s: int = doc("period of the heartbeat and of the watcher cycle", 60)
    gate_max_s: int = doc("longest wait at a tool gate", 14400)
    lease_s: int = doc("time a seat lease counts against other runs; the tool holds the seat by then", 600)
    kill_hung: bool = doc("the watcher kills a hung run after `grace_s`", False)
    kill_orphan: bool = doc("the watcher kills an orphan tool process after `grace_s`", False)


@dataclass
class Placement:
    """A job with `host = "auto"` goes to the first host, preferred ones first and then the one with
    the most free cores, that is not avoided, runs fewer than `max_per_host` of your runs, has the free
    cores, RAM and disk the job's first stage needs with its scratch above the host's floor, and has
    every tool the job's stages need. No such host means the job is queued. When no host of the site
    has a tool the job needs, `plan` reports it as a problem."""

    max_per_host: int = doc("the most of your runs on one host, over every registered project: the live "
                            "runs and the launches that wrote no heartbeat yet", 2)
    min_free_cores: int = doc("free cores a host needs to take a run", 16)
    min_free_ram_gb: int = doc("free RAM a host needs, in GB", 60)
    avoid: list[str] = doc("hosts `auto` never picks", factory=list)
    prefer: list[str] = doc("hosts `auto` tries first, in order", factory=list)


@dataclass
class Project:
    """`edr.toml` holds the project, its flow and its limits. `site`, `state_dir`, `data`, `source.repo` and
    `source.worktrees` render at load time with `{project}`, `{project_root}`, `{user}` and
    `{site_dir}`. Every other string keeps its placeholders until `plan`."""

    root: Path  # the project directory
    project: str = doc("the project name, also available as `{project}`")
    site: Site = doc("the path of `site.toml`")
    source: Source = field()
    sync: Sync = field(default_factory=Sync)
    runtime: Runtime = field(default_factory=Runtime)
    safety: Safety = field(default_factory=Safety)
    limits: Limits = field(default_factory=Limits)
    placement: Placement = field(default_factory=Placement)
    stages: dict[str, Stage] = field(default_factory=dict)  # in file order
    metrics: dict[str, Metric] = field(default_factory=dict)
    state_dir: Path = doc("the state directory, on a filesystem every host mounts", Path("~/.edr/{project}"))
    data: Path = doc("the head-node data directory: `edr.db`, `results/`, `board/`", Path("data"))
    run_prefix: str = doc("the run tree prefix under the host scratch", "{user}/edr/{project}")
    env: dict[str, str] = doc("the variables every command of every stage needs, on top of the site `env`; "
                              "a value takes the run placeholders. A `$NAME` or `${NAME}` that the site sets takes "
                              "the site value, so `PATH = \"{root}/.venv/bin:$PATH\"` keeps the site path; a value "
                              "without a reference replaces the site value, and any other `$VAR` expands on the host",
                              factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    task_resolver: str = doc("a hook `id -> table` for ids the file does not list", "", key="pattern.resolver")


@dataclass
class Job:
    """A job describes one run of a batch. `check` and `plan` verify that an override key is an identifier, and that
    a stage of the job uses `{overrides}` in `cmd`, `resume` or `prepare`. They do not know the
    flow's own variables, so a key the flow ignores passes.

    A job with `reuse` runs on the host and the tree of the reused run, and takes its build tag and
    `{tree_id}`; a glob in `label` is an error. Its stages need not use `{overrides}`, since the
    stages of the reused run did. With `restore`, the job takes the source tag, the
    build tag and `{tree_id}` of the reused run but is placed like a new job, so it runs after the
    tree was retired; `docs/guides/cleanup.md` shows the rerun. A task group in a job without `tasks` is a
    plan problem.
    """

    label: str = doc("the run label; unique in the batch")
    config: str = doc("the configuration name the flow takes; `{config}`. Without it the build tag hook gets `\"\"` "
                      "and the run id drops the empty part", "")
    host: str = doc("a host name, or `\"auto\"`", "auto")
    stages: list[str] = doc("the stages to run, a subset of `edr.toml` in file order", factory=list,
                            shown="every stage")
    tasks: list[str] = doc("the task ids of the task groups", factory=list)
    overrides: dict[str, str] = doc("`KEY = VALUE`; `{overrides}` renders them as `KEY=VALUE` tokens", factory=dict)
    vars: dict[str, str] = doc("`{ name = value }`: any value the flow needs, such as `netlist_stage = 11`; "
                               "`{vars.<name>}` in the stage strings, `[env]` and `collect`", factory=dict)
    reuse: dict[str, object] | None = doc(
        "`{ run_id = \"...\" }` with a run id or another handle, or `{ label = \"...\", latest = true }` for the newest "
        "run by start time of that label at the batch's source that ended done, else its newest run. The job starts "
        "on the tree of that run; with `restore = \"<name>\"`, it starts on a fresh tree with the "
        "`collect_on_request.<name>` files of that run copied back from `data/results/`", None)


@dataclass
class Batch:
    """`jobs/<batch>.toml` holds the jobs of one batch on one source. A tag is a short hash, or
    `<hash>-dirty-<8 hex>` for a snapshot of a tree with uncommitted changes. The date is pinned once
    per batch in `<state_dir>/<batch>/RUN_DATE`, so `plan` and `launch` minutes apart name the same run
    ids. A batch name is used once; a second launch of the same batch finds its specs and does
    nothing."""

    batch: str = doc("the batch name", shown="the file stem")
    source: str = doc("a tag from `edr checkout`, or a ref that `edr checkout` has checked out")
    jobs: list[Job] = field()
    path: Path = field()
