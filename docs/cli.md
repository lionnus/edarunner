# The edr command

```
edr [--json] [--version] <verb> [options]
```

`edr` finds `edr.toml` in the current directory or a parent, so it works
from anywhere below the project. Without one it refuses.

## Common options

`--json` on any verb prints one object instead of the text:

```json
{"code": 0, "data": {}, "output": "the text a person would see"}
```

`code` is the exit code, `data` the verb's result as structured data, and
`output` the text.

`--dry-run` exists on every verb that writes: `init`, `stage`, `plan`,
`launch`, `run`, `keep`, `import`, `export`, `stop`, `retire`, `notify`
and `watch`.
A dry run prints every path and every command with the mark `(dry)` or
the prefix `dry:` and writes nothing: no date pin, no spec, no file on a
host, no ledger row, no event, not even an empty database.

`--why <text>` is required on `stop` and `retire`, and optional on
`import`. The text lands in the events table with the actor.

A read verb (`status`, `events`, `hosts`, `lic`, `metrics`, `check`)
never creates `data/edr.db`. Without a database it reads an empty ledger
in memory.

A table on a terminal has colour: a run is green while it runs, cyan
when queued, yellow when stale, red when dead, hung, over budget, an
orphan or failed, and dim when done or retired. A pipe or the `NO_COLOR`
variable gets the same text with no escape code, and `--json` never
carries any.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | a guard refused, a config file is wrong, or the input is bad |
| 2 | nothing to do |
| 3 | a host did not answer, or a host command failed |
| 130 | interrupted |

The verbs below say where 2 and 3 apply. A refusal prints `edr: <reason>`
on stderr and runs nothing after it.

## Handles

A verb that takes a run accepts three forms:

| Handle | Resolves to |
|---|---|
| `label@batch` | the newest run with that label in that batch |
| a run id prefix | the one run whose id starts with it; an ambiguous prefix is refused |
| `#n` | row `n` of the last board that `edr status` printed |

`--batch` on `status`, `plan`, `launch` and `retire` defaults to
`EDR_BATCH`, then to the newest batch directory in the state.

## status

```
edr status [handle] [--batch B] [--narrow] [--watch] [--live] [--triage] [--digest]
```

Without a handle, the board: one line per run of every batch that is not
retired, live runs first and dead ones on top, with `#n`, label, host,
state, phase, stage/step, heartbeat age, failed/done task counts and the
core hours so far. The state of a live run follows the heartbeat age
(`running`, `stale`, `dead`) or the watcher's last verdict (`hung`,
`host_full`, ...). A finished run shows its phase class: `done`,
`incomplete`, `failed`, `over_budget`, `stopped`, `killed`.

| Flag | Effect |
|---|---|
| `handle` | one run: identity, state, counts, disk, every stage and task row, the metrics, and the log tail from the heartbeat |
| `--batch B` | one batch |
| `--narrow` | 48 columns, two lines per live run, for an ssh app on a phone |
| `--watch` | redraw every `heartbeat_s` seconds; Ctrl-C ends it |
| `--live` | ask each host whether the driver process exists; a gone driver shows `dead` |
| `--triage` | every run not `running`, with one proposed command; `docs/watcher.md` has the table |
| `--digest` | the daily digest that the watcher sends, as plain text; `docs/telegram.md` describes it |

Exit 3 with `--live` when a host did not answer.

## events

```
edr events [--since T] [--run HANDLE] [-n N]
```

The last `N` events (default 50) in time order: time, actor (`user`,
`watch`, `telegram`), run, kind and text. `--since` takes `30m`, `2h`,
`1d` or seconds. Exit 2 when there is none.

## hosts

```
edr hosts [--narrow]
```

Probes every host of the site file and prints one row per host:

| Column | Holds |
|---|---|
| `ok` | the worst mark of the host |
| `host` | the name in the site file |
| `cores` | a mark, cores in use of total, the load average rounded, with a bar |
| `load` | the one-minute load average |
| `ram GB` | a mark, RAM free of total |
| `mount` | the largest writable scratch of the host's list |
| `scratch GB` | a mark, that mount free of total, with a bar of the used part |
| `gpu` | a mark, GPUs idle of total; idle means under 5 % utilisation and under 5 % memory in use |
| `gpu GB` | GPU memory free of total, summed over the GPUs |
| `tools` | processes that match `tool_procs`, ours and others |
| `runs` | our driver processes |

A mark tells how full a resource is. It is 🟢 below the first threshold
of the `[marks]` table, 🟡 from the first, 🟠 from the second and 🔴
from the third. A value exactly at a threshold takes the colour of that
threshold. The used fraction is the load over the cores for `cores`, the
used part of the total for `ram GB` and `scratch GB`, and the busy GPUs
over all GPUs for `gpu`. A host without GPUs shows `-`, and a host that
did not answer shows ⚫. `docs/configuration.md` lists the thresholds.

The rows go by the worst mark, ⚫ first, then 🔴, 🟠, 🟡 and 🟢, and by
host name within one mark.

The GPU columns come from `nvidia-smi`; a host without it shows `-`. A
bar is green below 70 % used, yellow below 90 %, red above. `--narrow`
keeps `ok`, `host`, `cores`, `ram GB`, `scratch GB` and `gpu` in 48
columns, with no space between a mark and its number. A host that did
not answer shows its error in the row. `--json` gives the numbers:
`cores`, `load`, `free_cores`, `free_ram_gb`, `total_ram_gb`, `mount`,
`free_gb`, `total_gb`, `gpus`, `gpus_idle`, `gpu_used_gb`,
`gpu_total_gb`, `our_tool_procs`, `other_tool_procs` and `our_runs`.
Each host also carries `marks`, an object with the marks of `cores`,
`ram`, `scratch` and `gpu`. Exit 3 when a host did not answer.

## lic

```
edr lic
```

Runs every licence probe from the head node and prints pool, used, free,
ours, others and the floor. Exit 3 when a probe failed or the feature
line is missing.

## metrics

```
edr metrics --design H [--stage S] [--step N] [--csv]
```

Every metric of one design: label, design, stage, step, task, name,
value and unit. `--design` is the source tag exactly as `edr stage`
printed it, `-dirty-...` included; it has no default, because one table
holds one design. `--csv` writes the columns of `metrics.csv` (see
`docs/results.md`) to stdout. Exit 2 when there is no row.

## init

```
edr init --site DIR [--dry-run]
```

Writes `edr.toml` and `edr-watch.service` into the current directory.
`--site` is the directory or the file of the site file; `init` does not
write that file. Refuses when `edr.toml` exists.

## check

```
edr check
```

Loads the project, the site and every batch under `jobs/`, imports every
hook, checks the driver file, probes every host once, names every tool
the head node lacks (`ssh`, `rsync`, `git`, `python3`, `nproc`, `df`,
`ps`, `awk`, `stat`, `readlink`, and a `ps` with `etimes`, `pcpu` and
`cputimes`), and plans every batch with those probes. Prints one
`problem:` line per fault and exits 1, or an `ok:` line with the counts
and exits 0.

## stage

```
edr stage [ref] [--dirty DIR] [--dry-run]
```

Fetches, then adds a detached worktree of `ref` (default `source.ref`)
at `<worktrees>/<short hash>`, and clones each `source.nested` repository
into it at the HEAD the repository copy has. Prints `<src> <path>`.
`--dirty DIR` copies a working tree instead, with its diff in
`source.diff`; the tag is `<hash>-dirty-<8 hex>` and prints with
`(dirty)`. A clean tree under `--dirty` is staged as a worktree.

## plan

```
edr plan [batch] [--dry-run]
```

Renders every job of the batch into a run spec and prints `<run id>:
<host or queued> <root>` per job, with `problem:` lines under a job that
cannot run. Writes nothing, with or without `--dry-run`. With `--json`,
`data[].spec` is the full spec of each job. Exit 1 when any job has a
problem.

## launch

```
edr launch [batch] [--only L] [--allow-dirty] [--dry-run]
```

Pins the date of the batch, publishes the driver into the state
directory, syncs the staged tree to each host, writes one spec per run
and starts one driver per run, `stagger_s` apart. Prints `<n> started,
<n> queued, <n> with problems`. `--only a,b` limits the labels. A dirty
source needs `--allow-dirty`. A job whose spec exists is `already
launched`; a batch name is used once.

Exit 0 when a run started or was queued, 2 when every job was already
launched, 1 otherwise.

## run

```
edr run HANDLE --stage S [--tasks ID ...] [--from CHECKPOINT] [--on HOST] [--parallel N] [--dry-run]
edr run HANDLE --collect NAME [--dry-run]
```

More work on the tree of an existing run: one stage, on the same tree,
as a new run in the batch of that run with the label `<label>.<stage>`.
`--tasks` names the tasks of a task group, `--parallel` its width, `--on`
the host (default: the tree's host). `--from` fills `{checkpoint}` in
the stage's `resume` command, and is refused when the stage has none.

`--collect NAME` instead copies the `collect_on_request` list `NAME` of
every stage from the tree into `data/results/<run id>/`. Exit 3 when a
copy failed.

## keep

```
edr keep HANDLE [--hours N] [--ack] [--dry-run]
```

Writes the keep file of a live run. `--hours` (default 12 when `--ack` is
absent) adds hours to the budget of the running stage or task; `--ack`
cancels a pending kill or stop of the watcher. Exit 2 when the run has
ended.

## import

```
edr import --run-id R --label L --config C --src HASH (--host HOST --root PATH | --results DIR [--tasks ID ...])
           [--batch B] [--phase P] [--build-tag TAG] [--why TEXT] [--dry-run]
```

Records a run the package did not make. With `--host` and `--root`, the
tree on that host, so `reuse` and `edr run` can continue it. With
`--results DIR`, a directory of collected files of a run whose tree is
gone: it is linked as `data/results/<run id>` and the project's metrics
are extracted from it; `--tasks` names the tasks whose files it holds.
`--batch` defaults to `imported`, `--phase` to `done`. The run id must
start with `YYYYMMDD_HHMM_`.

## export

```
edr export --design SRC --out DIR [--labels a,b] [--with-logs] [--dry-run]
```

Writes a snapshot of one design to `DIR`: `manifest.json`, `runs.csv`,
`metrics.csv` and the collected files of the newest run per label.
`--design` matches the source tag exactly. `log/` and `*.log` stay out
unless `--with-logs`. Refuses a `DIR` that exists and is not empty.
`docs/results.md` explains the layout.

## stop

```
edr stop HANDLE --why TEXT [--after-task] [--now] [--dry-run]
```

Stops one run through the pids the driver recorded, never through a
session name or a process pattern.

| Form | Effect |
|---|---|
| plain | `SIGTERM` to the driver and every process group of the run; waits up to 60 s |
| `--after-task` | writes the stop file; a task group finishes its running tasks and claims no more, a one-command stage runs to its end |
| `--now` | `SIGTERM`, then `SIGKILL` after 30 s |

A queued run is marked `stopped` and never starts. A run that ended is
exit 2. A run whose heartbeat has no driver pid is refused. When the
driver is still alive after the wait, the verb prints `still alive; use
--now` and exits 3.

## retire

```
edr retire HANDLE --why TEXT [--collect NAME,...] [--prune T] [--uncollected] [--dry-run]
edr retire --batch B --why TEXT [--collect NAME,...] [--prune T] [--uncollected] [--dry-run]
```

Removes the run tree on the host, or with `--prune T` the paths that
`prune.T` names in the stages, after the guard on every target. `--batch`
retires every run of the batch and marks it `RETIRED`, so the watcher
skips it. A live run gets the phase `ABANDONED:<why>`.

`--collect NAME,...` first copies the named `collect_on_request` lists of
every run into `data/results/<run id>/`, and removes nothing when one
copy failed. It is the archive step of `docs/running.md`.

`retire` refuses a run whose driver is alive, a live run that has no
heartbeat yet and started less than `dead_s` ago, a tree that another
live run uses, a tree shared with a run whose results are not collected
(retire them together with `--batch`), and a tree whose own results are
not collected unless `--uncollected`. Exit 2 when the batch has no run,
3 when an `rm` failed.

## notify

```
edr notify TEXT [--silent] [--dry-run]
```

Sends one message through every notifier that the site configures. The
first line names the project and the word `note`, as in every message of
the bot; `TEXT` follows as plain text. `--silent` sends it without a
sound. `--dry-run` prints the message and sends nothing. Exit 0 when
every notifier sent it, 1 when no notifier is configured or a send
failed. `docs/telegram.md` shows a Claude Code hook that calls it.

## watch

```
edr watch [--once] [--check] [--dry-run]
```

The watcher loop: one cycle every `heartbeat_s` seconds, with the
Telegram bot as a thread when the site file configures it. `--once` runs
one cycle and exits 1 when the cycle failed or the config did not load.
`--check` exits 1 and sends an alert when the watcher's own heartbeat
is older than three cycles; a cron line runs it. `--dry-run` reads and
classifies every run, prints the states and writes nothing.

`docs/watcher.md` explains the cycle. The boards land in `data/board/`;
`python -m http.server --bind 127.0.0.1 8000` in that directory serves
them.
