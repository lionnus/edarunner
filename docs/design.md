# Design contract

This file is the contract between the modules. A module that needs a
different shape changes this file first. Version 1.

## 1. What edarunner does

It runs a flow you already have (a Makefile, a script) on hosts you
already reach over ssh. It keeps one ledger of every run: identity,
source versions, host, stages, tasks, metrics, artifacts, events. It
watches the runs without an agent, applies declared limits, and notifies
a phone. It exports a frozen snapshot for a paper. Nothing in the core
knows an EDA tool; the project config and the parsers do.

Three programs:

- `edr`, the controller, Python 3.11 or newer, standard library only, on
  the head node.
- `edr_driver.py`, the driver, one file, Python 3.6 or newer, standard
  library only, copied to a host at launch. It runs one run. It never
  imports the package.
- `edr watch`, the controller as a long-running service on the head node,
  with the Telegram bot as a thread inside it.

## 2. Directories

```
<project>/                 the project directory; `edr` runs here
  edr.toml                 project config (section 3)
  tasks.toml               task table (section 3)
  jobs/<batch>.toml        one batch (section 3)
  hooks/                   scripts the config names
  data/                    head node only: results/, exports/, board/, edr.db
<state>/                   shared filesystem, one per project, e.g. ~/.edr/<project>
  bin/<batch>/edr_driver.py   the driver a batch runs, copied by rename at launch
  <batch>/RUN_DATE         the pinned date of the batch, YYYYMMDD_HHMM
  <batch>/RETIRED          exists once the batch is retired
  <batch>/<run_id>.spec.json   driver input (section 4)
  <batch>/<run_id>.json    heartbeat, written by the driver (section 4)
  <batch>/<run_id>.queue/<stage>/{pending,claimed,done}/<task>   task queue (section 5)
  watch.json               the watcher's own heartbeat
<host scratch>/<run_prefix>/<run_id>/   the run tree on a host; `root` in the spec
  log/<stage>.log          stdout and stderr of a stage or task
```

The site config lives outside the project, default
`~/.config/edarunner/site.toml`, and the Telegram token in
`~/.config/edarunner/telegram.token` with mode 600.

## 3. Configuration files

TOML, read with `tomllib`. Unknown keys are an error. Every path is
absolute or relative to the file that names it. `~` expands. Every
string value may use `{placeholders}` from the set in section 3.5.

### 3.1 edr.toml

```toml
schema = 1
project = "surya"
site = "~/.config/edarunner/site.toml"
state = "~/.edr/{project}"
data = "data"
run_prefix = "{user}/edr/{project}"

[source]
repo = "../rtl"                 # one git repository
worktrees = "../rtl-wt"         # `edr stage` adds worktrees here
ref = "origin/main"             # default ref for `edr stage`
nested = ["nonfree"]            # nested repositories inside the worktree, pinned by their HEAD
run_id = "{date}_{label}_{build_tag}_g{src}"
build_tag = "python:hooks/build_tag.py:build_tag"   # optional; default "{config}[_{overrides}]"

[sync]
exclude = [".venv", "simulation/tests"]
after = "hooks/seed_python.sh {host} {root} {mount}"   # optional, runs on the head node

[safety]
marker = "/edr/"
min_depth = 4

[limits]
stagger_s = 120
stale_s = 600
dead_s = 2700
hung_s = 21600
grace_s = 3600
host_free_min_gb = 100
streak = 3
heartbeat_s = 60
gate_max_s = 14400

[placement]
max_per_host = 2
min_free_cores = 16
min_free_ram_gb = 60
avoid = ["headnode"]
prefer = ["hostB"]

[stages.synth]
after = ""                      # "" first stage; "pnr"; or { stage = "pnr", step = "route" }
cmd = "make pnr RUN={run_id} HW_CONFIG={config} MAX_CORES={cores} LAST_STAGE=synth-final-opto {overrides}"
resume = "make pnr RUN={run_id} HW_CONFIG={config} MAX_CORES={cores} FIRST_STAGE={checkpoint} LAST_STAGE=synth-final-opto {overrides}"
cwd = "."
steps = ["setup-library", "analyze", "elaborate"]
progress = "ls reports 2>/dev/null | grep -cE '^[0-9]+$'"
needs = { cores = 16, disk_gb = 70, licence = "fc" }
budget = { hours = 6, disk_gb = 150, kill = false }
retry = { match = "licen[cs]e", wait_s = 900, max = 3 }
collect = ["reports/", "fc_output.txt"]
collect_on_request = { netlist = ["out/11/"] }
prune = { lib = ["out/lib"], questa = ["simulation/questa/builds"] }

[stages.power]
after = "export"
foreach = "tasks"
parallel = 1
prepare = "make gate-build RUN={run_id}"
task_dir = "simulation/tests/{build_tag}/{task.test}"
cmd = "run.sh {task.kernel} HW_CONFIG={config} {overrides} {task.args}"
after_each = "rm -f {task_dir}/wave.vcd"
needs = { cores = 4, disk_gb = 20, licence = { questa = 1 } }
budget = { hours = 8, per = "task" }
collect = ["{task_dir}/power/"]

[metrics.area_cell_um2]
stage = "synth"
step = "*"
file = "reports/{step}/area_hier.rpt"
regex = '^i_top\s+(\S+)'
unit = "um2"
canonical = "area.cell"

[metrics.power_w]
stage = "power"
file = "{task_dir}/power/reports/power.csv"
csv = { where = { phase = "WHOLE" }, column = "total_w" }
unit = "W"
canonical = "power.total"

[metrics.energy_nj]
expr = "power_w * window_ns"
unit = "nJ"
```

Stage keys: `after`, `cmd`, `resume`, `cwd`, `steps`, `progress`,
`needs`, `budget`, `retry`, `collect`, `collect_on_request`, `prune`,
`foreach`, `parallel`, `prepare`, `task_dir`, `after_each`. A stage
without `foreach` is one command. A stage with `foreach = "tasks"` is a
task group; its `cmd` runs once per task of the job.

Metric keys: `stage` (name or list), `step` (`"*"`, a number, or
absent), `file`, one of `regex` (group 1), `csv` (`where`, `column`),
`json` (a dotted path), `python` (`module.py:function`, gets the file
path, returns a float) or `expr` (over other metric names of the same
run, stage, step and task), `unit`, `canonical`.

### 3.2 site.toml

```toml
schema = 1
scratch = ["/scratch", "/scratch2"]
env = { PATH = "/usr/sepp/bin:$PATH" }
ssh = { options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"], timeout_s = 45 }
tool_procs = "^(vsim|vsimk|pt_shell|fc_shell|dgcom_exec)$"

[hosts.hostB]
cores = 128
ram_gb = 503

[hosts.local]
cores = 8
ram_gb = 32
scratch = ["/tmp/edr"]

[licences.fc]
feature = "Fusion-Compiler-FE-NX"
floor = 10
probe = "lmutil lmstat -a -c 8169@licence.example.com"

[licences.questa]
feature = "msimhdlsim"
floor = 20
seats_per_task = 1
probe = "lmutil lmstat -a -c 8169@licence.example.com"

[telegram]
token_file = "~/.config/edarunner/telegram.token"
chat_id = 123456789

[telegram.commands.survey]
help = "free cores, RAM and scratch on every host"
run = ["edr", "hosts", "--narrow"]
timeout_s = 60

[telegram.commands.claude]
help = "open a Claude remote-control session: /claude <dir>"
args = { dir = "^(backend|paper|rtl|sw)$" }
skip_if = ["tmux", "has-session", "-t", "claude-{project}-{dir}"]
skip_reply = "already open: claude-{project}-{dir}"
run = ["tmux", "new-session", "-d", "-s", "claude-{project}-{dir}", "-c", "{root}/{dir}", "claude remote-control --name {project}-{dir}"]
reply = "session claude-{project}-{dir} started; open the Claude app"
```

The host `local` runs on the head node without ssh. A host missing from
`[hosts]` is an error at `check`. The licence probe output is parsed
with the FlexLM line `Users of <feature>: (Total of N licenses issued;
Total of M licenses in use)`; free is N minus M. A probe that fails
counts as "unknown" and is an event, never a pass.

### 3.3 tasks.toml

```toml
[tasks.softmax_197x197]
kernel = "softmax"
args = "SOFTMAX_ROWS=197 SOFTMAX_COLS=197 NORM_MODE=2"
test = "SOFTMAX_R197_C197_NM2"
needs = { disk_gb = 60 }
budget = { hours = 8 }

[pattern]
resolver = "python:hooks/tasks.py:spec_of"   # optional: id -> table for ids the file does not list
```

Every key of a task is a placeholder `{task.<key>}` in the stage
strings. `needs` and `budget` override the stage's.

### 3.4 jobs/<batch>.toml

```toml
batch = "iccd2026_g10"
source = "3f9a2c1"              # a worktree made by `edr stage`; "3f9a2c1-dirty-7b21c0d9" for a dirty snapshot

[[job]]
label = "gwaihir64_l8"
config = "gwaihir64_l8"
host = "auto"                   # or a host name
stages = ["synth", "pnr", "export", "power"]
tasks = ["mxgemm_bf16", "softmax_197x197"]
overrides = { DW = 0 }
netlist_stage = 11
reuse = { label = "gwaihir64_l8", latest = true }   # start on the tree of an existing run
```

Two jobs must not share a label. `overrides` keys must exist in the
resolved configuration or `check` refuses. `reuse` names a run id or a
label with `latest`; a label glob is an error.

### 3.5 Placeholders

`{project} {project_root} {site_dir} {user} {date} {batch} {label}
{config} {build_tag} {src} {run_id} {tree_id} {root} {host} {mount} {cores}
{overrides} {checkpoint} {step} {task_dir} {task.<key>} {netlist_stage}`.
`{tree_id}` is the run id of the tree the flow writes in: the reused run's
id for a job with `reuse`, else `{run_id}`. A flow that names its run
directory after the run uses `{tree_id}` there. `{overrides}` renders as
`KEY=VALUE` tokens separated by spaces. A placeholder without a value
is an error at `plan`.

## 4. Run spec and heartbeat

`edr plan` renders every string of a job into a run spec; `edr launch`
writes it to `<state>/<batch>/<run_id>.spec.json` and starts the driver
with that path as its only argument. The driver reads nothing else.

```json
{"schema": 1, "run_id": "20261002_1130_gwaihir64_l8_N8..._g3f9a2c1", "batch": "iccd2026_g10",
 "project": "surya", "label": "gwaihir64_l8", "config": "gwaihir64_l8", "host": "hostB",
 "root": "/scratch2/user/edr/surya/20261002_1130_...", "state_file": "/home/user/.edr/surya/iccd2026_g10/20261002_1130_....json",
 "queue_dir": "/home/user/.edr/surya/iccd2026_g10/20261002_1130_....queue",
 "shell": "/bin/bash", "env": {"PATH": "/usr/sepp/bin:/usr/bin:/bin"},
 "limits": {"host_free_min_gb": 100, "streak": 3, "heartbeat_s": 60, "gate_max_s": 14400},
 "start_at": {"stage": "synth", "checkpoint": null},
 "stages": [
   {"name": "synth", "cmd": "make pnr RUN=... LAST_STAGE=synth-final-opto", "cwd": "/scratch2/.../root",
    "steps": ["setup-library", "analyze"], "progress": "ls reports 2>/dev/null | grep -cE '^[0-9]+$'",
    "needs": {"cores": 16, "disk_gb": 70}, "licence": {"name": "fc", "feature": "Fusion-Compiler-FE-NX", "floor": 10, "probe": "lmutil lmstat -a -c 8169@..."},
    "budget": {"hours": 6, "disk_gb": 150, "kill": false}, "retry": {"match": "licen[cs]e", "wait_s": 900, "max": 3},
    "resume": "make pnr RUN=... FIRST_STAGE={checkpoint} LAST_STAGE=synth-final-opto"},
   {"name": "power", "parallel": 1, "prepare": "make gate-build RUN=...", "after_each": "rm -f {task_dir}/wave.vcd",
    "needs": {"cores": 4, "disk_gb": 20}, "licence": {"name": "questa", "feature": "msimhdlsim", "floor": 20, "seats_per_task": 1, "probe": "..."},
    "budget": {"hours": 8, "per": "task"},
    "tasks": [{"id": "softmax_197x197", "cmd": "run.sh softmax ...", "dir": "/scratch2/.../simulation/tests/N8.../SOFTMAX_R197_C197_NM2",
               "needs": {"disk_gb": 60}, "budget": {"hours": 8}}]}
 ]}
```

The heartbeat, `<state>/<batch>/<run_id>.json`, written by an atomic
rename every `heartbeat_s` and at every phase change:

```json
{"schema": 1, "run_id": "...", "batch": "...", "label": "...", "config": "...", "host": "hostB", "root": "...",
 "driver_pid": 4242, "pgids": [4300],
 "phase": "stage:synth", "stage": "synth", "step": 9, "step_name": "synth-final-opto",
 "stages": {"synth": {"status": "done", "attempt": 1, "started": 1790000000, "ended": 1790003600, "exit": 0, "log": "/scratch2/.../log/synth.log"}},
 "tasks": {"softmax_197x197": {"phase": "running", "pid": 4300, "pgid": 4300, "started": 1790000000, "ended": null, "exit": null, "signature": null}},
 "counts": {"done": 0, "failed": 0, "skipped": 0, "running": 1, "queued": 3},
 "started": 1790000000, "updated": 1790000060, "elapsed_s": 60,
 "disk_free_gb": 761, "tree_gb": 12.4,
 "exit": null, "killed_by": null, "last_cmd": "make pnr ...", "last_log": "...", "log": "/scratch2/.../log/synth.log"}
```

Phases: `setup`, `gate:<stage>`, `stage:<stage>`, `retry:<stage>:<n>`,
`group:<stage>`, and the terminal ones `done`, `INCOMPLETE:<n>f<m>s`,
`FAILED:<stage>`, `OVER_BUDGET:<stage>`, `STOPPED`, `KILLED:<signal>`.
`stages` holds one entry per stage the driver started: `status`
(`running`, `done`, `failed`, `over_budget`), `attempt`, `started`,
`ended`, `exit`, `log`; a retry adds an attempt. A terminal phase sets
`exit` to a number. `counts.failed` and
`counts.skipped` count across every task group of the run.

## 5. The driver

`python3 edr_driver.py <spec.json>`. Written in the Python 3.6 subset:
no walrus, no dataclasses, no f-string `=`, `subprocess.run` with
`stdout=PIPE`. Standard library only. Exit codes: 0 done, 8 incomplete,
9 over budget, 10 stopped, 5 failed stage, 3 disk, 4 gate, 2 bad spec.

Behaviour, in order:

1. Load the spec. Set `phase = setup`. Start the heartbeat thread. Check
   `needs.disk_gb` of the first stage against the free space of `root`;
   refuse with 3.
2. For each stage from `start_at.stage`:
   - `gate`: if the stage has a licence, run the probe, parse free seats,
     wait while free minus the seats it needs is below `floor`, up to
     `gate_max_s`, then fail with 4. A probe that fails counts as unknown:
     log it, record `licence_unknown` in the heartbeat, and go on.
   - one command: run `cmd` (or `resume` with `{checkpoint}` filled when
     `start_at.checkpoint` is set and this is the first stage; a checkpoint
     on a stage without `resume` is a bad spec, exit 2) through
     `shell -c`, in `cwd`, with `env`, in a new session
     (`start_new_session=True`), stdin from `/dev/null`, stdout and stderr
     appended to `log/<stage>.log`. Record the pgid. Poll every 5 s: run
     `progress` and update `step`; on failure with `retry.match` in the
     last 80 log lines, wait `retry.wait_s` and run again up to
     `retry.max`; after `budget.hours`, if `budget.kill` kill the group
     else let it end but mark `OVER_BUDGET:<stage>` and stop after it;
     if `tree_gb` passes `budget.disk_gb`, same rule.
   - task group: run `prepare` once. Create `queue/<stage>/pending/<id>`
     for each task with `O_EXCL` (a file that exists or a `done/<id>` that
     exists is skipped). Loop: while running tasks are fewer than
     `parallel`, claim one by renaming `pending/<id>` to
     `claimed/<id>.<run_id>`; before the start, check the task's
     `needs.disk_gb` against free space (skip and count `skipped` when
     short), and the licence seats. Start it as a command above, in its
     own session, log `log/<stage>.<id>.log`. On end: record exit, move
     the claim to `done/<id>`, run `after_each` with `{task_dir}`, count
     `done` or `failed`; a failure gets a `signature`, the last log line
     with digits removed; `streak` equal signatures in a row stop the
     group (`looping`). Per-task `budget.hours` kills that task only. The
     group ends when `pending` is empty and nothing runs.
3. Between stages and inside a group, check the host free space against
   `limits.host_free_min_gb`; below it, start nothing new and record
   `host_full` in the heartbeat.
4. End: `done` when every stage ran and `counts.failed` and
   `counts.skipped` are zero; else `INCOMPLETE:<n>f<m>s`.

`edr keep` writes `<run_id>.keep.json` next to the spec, `{"hours": N,
"ack": true}`. The driver reads it at every heartbeat and adds the hours
to the budget of the current stage or task. The watcher reads `ack` and
cancels a pending kill.

Signals: `SIGTERM`, `SIGHUP` and `SIGINT` set `killed_by`, kill every
recorded pgid with the same signal, write a final heartbeat with phase
`KILLED:<signal>` (or `STOPPED` when a `stop` file exists next to the
spec) and exit 10. A `stop` file with content `after-task` makes a group
finish the running tasks and claim nothing more.

The heartbeat thread also computes `tree_gb` (a `du -s` of `root` every
10 cycles), `disk_free_gb`, and the log tail.

## 6. Executors

`edr launch` starts the driver on a host with the host's own `python3`,
resolved from the login `PATH` before the site `env` is applied; the site
`env` reaches the driver's children through the spec:

- `local`: `Popen([python3, driver, spec], start_new_session=True,
  stdin=DEVNULL, stdout/stderr to <state>/<batch>/<run_id>.driver.log)`.
- ssh: `ssh <opts> <host> "setsid nohup python3 <driver> <spec> >
  <driver.log> 2>&1 < /dev/null &"`. The driver path is the copy in
  `<state>/bin/<batch>/`, readable from the host over the shared
  filesystem. `python3` is the host's own.

`edr stop` reads `pgids` and `driver_pid` from the heartbeat and signals
them on the host with `kill -TERM -- -<pgid>`, waits `grace_s` (or
`--now` for `SIGKILL` after 30 s), and never uses `pgrep` or a session
name. `--after-task` writes the `stop` file instead.

## 7. The watcher

`edr watch` runs a cycle every `heartbeat_s`, and once with `--once`:

1. Read every heartbeat of every batch without `RETIRED`. Upsert `runs`
   and `stage_runs`. Write `watch.json` with its own timestamp.
2. Classify each run that is not terminal:

| State | Test | Action |
|---|---|---|
| running | heartbeat age below `stale_s` | none |
| stale | age between `stale_s` and `dead_s` | event |
| dead | age above `dead_s` and no driver process on the host (`ps -p <driver_pid>` over ssh) | event, notify |
| hung | age fresh, a task or stage running, and no progress for `hung_s` (log size, step, `tree_gb`, or CPU time of the pgid unchanged) | event, notify; kill after `grace_s` only if `limits.kill_hung` |
| orphan | a process matching `tool_procs` of the user on a host with no run that owns it | event, notify; kill after `grace_s` only if `limits.kill_orphan` |
| looping | driver reported it | event, notify |
| over_budget | driver reported it | event, notify |
| host_full | the host free space below `host_free_min_gb` | notify, then after `grace_s` `stop --now` our newest task there; never delete |
| superseded | a newer batch runs the same label at another `src` | notify; `stop --after-task` unless `--keep` was given |

3. Collect: for each run, `rsync` the stage `collect` paths of every
   finished stage or task into `data/results/<run_id>/`, and the passed
   steps of a running stage (a step directory older than 10 min is
   final). Count failures. Fall back to the NFS export path of the host
   when ssh fails and `site.nfs_export` is set.
4. Extract every metric whose file arrived and is not in `metrics` yet.
   Write `params` from the resolved configuration once.
5. Retry a dead run's last stage from its last step through `resume`,
   once, when the spec has `resume` and no recorded pgid of the run is
   alive on the host; while one is, log one event and wait.
6. Launch queued jobs whose host now fits, one per batch per cycle.
7. Notify through every configured channel: one message per event class
   per run, edited on change where the channel allows it.
8. Write `data/board/board.json`, `data/board/status.html` and
   `data/board/compare.html`.

`edr watch --check` exits non-zero and notifies when `watch.json` is
older than three cycles; a cron line runs it.

## 8. Ledger

SQLite, `data/edr.db`, opened by the head node only. WAL mode.

```sql
CREATE TABLE batches(batch TEXT PRIMARY KEY, project TEXT, source TEXT, created INTEGER, retired INTEGER, run_date TEXT);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, batch TEXT, label TEXT, config TEXT, build_tag TEXT, src TEXT, dirty INTEGER,
  host TEXT, root TEXT, created INTEGER, phase TEXT, state TEXT, stage TEXT, step INTEGER, exit INTEGER, killed_by TEXT,
  started INTEGER, updated INTEGER, disk_free_gb REAL, tree_gb REAL, counts TEXT);
CREATE TABLE stage_runs(run_id TEXT, stage TEXT, task TEXT, attempt INTEGER, started INTEGER, ended INTEGER,
  status TEXT, exit INTEGER, signature TEXT, log TEXT, PRIMARY KEY(run_id, stage, task, attempt));
CREATE TABLE params(run_id TEXT, key TEXT, value TEXT, source TEXT, PRIMARY KEY(run_id, key));
CREATE TABLE metrics(run_id TEXT, stage TEXT, step INTEGER, task TEXT, name TEXT, canonical TEXT, value REAL, unit TEXT,
  source_file TEXT, extracted_at INTEGER, PRIMARY KEY(run_id, stage, step, task, name));
CREATE TABLE artifacts(run_id TEXT, path TEXT, bytes INTEGER, collected_at INTEGER, class TEXT, PRIMARY KEY(run_id, path));
CREATE TABLE events(id INTEGER PRIMARY KEY, ts INTEGER, actor TEXT, run_id TEXT, kind TEXT, text TEXT);
```

`state` is the watcher's classification. `task` is `''` for a stage
row. Every CLI action that changes something inserts an event with
`actor` (`user`, `agent`, `watch`, `telegram`) and the `--why` text.

## 9. Export

`edr export --design SRC --out DIR [--labels a,b]` writes `DIR/manifest.json`
(producer, created, schema, sources with commits, runs, tables, files
with size and sha256, incomplete), `DIR/runs.csv`
(`run_id,label,config,design,host,phase,started,ended`), `DIR/metrics.csv`
(`run_id,label,config,design,stage,step,task,metric,canonical,value,unit,source`)
and the `collect` files of the chosen runs under `DIR/<label>/`. The
directory is written under a temporary name and renamed at the end.

## 10. CLI

Seventeen verbs. Every verb takes `--json`. Every verb that writes takes
`--dry-run`. `stop` and `retire` take `--why`. `--batch` defaults to
`EDR_BATCH` or the newest batch. Exit codes: 0 done, 1 refused by a
guard or bad input, 2 nothing to do, 3 some hosts failed. A handle is
`label@batch`, a run id prefix, or `#n` from the last board.

`status [handle] [--batch B] [--narrow] [--watch] [--live] [--triage]`,
`events [--since T] [--run R] [-n N]`, `hosts [--narrow]`, `lic`,
`metrics --design H [--stage S] [--step N] [--csv]`, `init --site DIR`,
`check`, `stage REF [--dirty DIR]`, `plan BATCH`, `launch BATCH [--only L]
[--allow-dirty]`, `run HANDLE --stage S [--tasks ...] [--from CHECKPOINT]
[--on HOST] [--parallel N] [--collect NAME]`, `keep HANDLE [--hours N]
[--ack]`, `export ...`, `stop HANDLE [--after-task] [--now] --why`,
`retire HANDLE|--batch B [--prune T] [--uncollected] --why`, `watch
[--once] [--check] [--serve PORT]`, `import --run-id R --label L --config C
--src H --host HOST --root PATH [--batch B] [--phase P] [--build-tag T]`
(records a tree that edr did not make, so `reuse` can continue it; it is
never a delete target unless its path carries the marker).

The narrow board fits 48 columns: two lines per live run, dead first.

## 11. Telegram

A thread of `edr watch`, started only when `[telegram]` has a readable
token file. Long polls `getUpdates` with `timeout=60`. Obeys one
`chat_id`; every other chat gets no answer and one event. Built-in
commands: `/status`, `/events`, `/hosts`, `/lic`, `/board`, `/keep
<handle> [hours]`, `/ack <handle>`, `/stop <handle>` (after task only),
`/compare <handle>...`, `/metric <name> [--design H]`. Custom commands
from `[telegram.commands.*]`: `run` is a list, never a shell string;
each argument must match its regex; `skip_if` and `detach` as in the
site example. Alerts carry two inline buttons, `keep 12h` and `ack`,
with `callback_data` `keep12:<handle>` and `ack:<handle>`. The board is
one pinned message edited in place each cycle, in an HTML `<pre>` block,
silent. Every action lands in `events` with actor `telegram`.

## 12. Guards

`guards.assert_safe_target(path, marker, min_depth)` refuses an empty or
relative path, a filesystem root, `$HOME`, `/scratch*`, a path without
the marker, and a path shallower than `min_depth`. `guards.assert_run_id`
refuses an id that does not start with `YYYYMMDD_HHMM_`. Every `rm -rf`
and every `rsync --delete` calls one of them first. A dry run writes
nothing, not even the date pin.

## 13. Tests

`pytest` in `tests/`, standard library plus pytest. The driver tests run
the driver as a subprocess with `/usr/bin/python3` (3.6 on the
development host) against `examples/local-demo`, whose flow is shell
scripts that sleep for seconds and write fake reports and a fake
`power.csv`. End to end on the `local` host: `init`, `check`, `plan`,
`launch`, two `watch --once` cycles, `status`, `metrics`, `export`,
`stop`, `retire`. A guard test proves that a dry run leaves the state
directory byte-identical. CI runs the tests on 3.11 and 3.12, and the
driver tests in a `python:3.6` container.

## 14. Module ownership

| Module | Owns |
|---|---|
| `config.py` | loading and validation of the four TOML files, placeholders, dataclasses `Project`, `Site`, `Stage`, `Task`, `Job` |
| `guards.py` | the two guards and the `Refuse` exception |
| `ledger.py` | schema, upserts, queries, `board.json` |
| `runid.py`, `stagectl.py` | run id, date pin, build tag hook, `edr stage` with worktrees and the dirty snapshot |
| `hosts.py` | ssh wrapper with timeouts, host probe, placement, `local` |
| `sync.py`, `launch.py` | rsync of the tree, driver copy by rename, spec rendering, start of the driver |
| `driver/edr_driver.py` | section 5 |
| `watch.py`, `collect.py`, `metrics.py` | section 7, 3.1 metrics |
| `export.py` | section 9 |
| `notify/__init__.py`, `notify/telegram.py` | the notifier interface and the bot |
| `board.py` | narrow text, `status.html`, `compare.html` |
| `cli.py` | section 10 |
