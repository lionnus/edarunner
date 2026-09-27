# Running

This page follows a run from the launch to the retire: what the driver
does on the host, what it writes, and how to act on a run that stopped.

## The driver

`edr launch` copies `edr_driver.py` to `<state>/bin/edr_driver-<hash>.py`,
where `<hash>` is the first 8 hex digits of the sha256 of the file, writes
one spec per run and starts the driver on the host with the host's own
`python3`:

```sh
setsid nohup python3 <state>/bin/edr_driver-<hash>.py <state>/<batch>/<run_id>.spec.json
```

The driver is one file, Python 3.6 or newer, standard library only. It
reads the spec and nothing else, runs the stages in order, and writes a
heartbeat. Each stage command runs through `bash -c` in its own session,
with stdin from `/dev/null` and stdout and stderr appended to a log in
the run tree. The site `env` and the project `[env]` reach the command
through the spec; `$VAR` in a value expands on the host.

One copy serves every run of that driver version; a copy that exists is
reused. A new version gets a new name, and the copy is made by a
temporary file and a rename, so a live driver never sees its text change.
The spec records the copy the run started with, and the watcher resumes
the run with that copy. Publish the driver only through `edr launch` or
`edr run`.

## The run tree

The tree is `<mount>/<run_prefix>/<run_id>/` on the host, a copy of the
staged worktree made by `rsync --delete`. The flow writes into it. The
driver adds `log/`:

| File | Holds |
|---|---|
| `log/<stage>.log` | stdout and stderr of a stage command; every attempt is appended |
| `log/<stage>.prepare.log` | the `prepare` command of a task group |
| `log/<stage>.<task>.log` | one task, then its `after_each` |

Every command starts with a line `# edr: <cmd>` in its log.

## The state directory

`state` in `edr.toml` names a directory on a filesystem every host
mounts. The hosts read the driver and the spec there and write the
heartbeat; the head node reads the heartbeat.

```
<state>/
  bin/edr_driver-<hash>.py        one driver copy per version
  watch.json                      the watcher's own heartbeat
  <batch>/
    RUN_DATE                      the pinned date, YYYYMMDD_HHMM
    RETIRED                       exists once the batch is retired
    <run_id>.spec.json            the driver's input
    <run_id>.json                 the heartbeat
    <run_id>.driver.log           stdout and stderr of the driver itself
    <run_id>.stop                 the stop file
    <run_id>.keep.json            the keep file
    <run_id>.queue/<stage>/{pending,claimed,done}/<task>
```

### The spec

`edr plan` renders every string of a job into the spec; `--json` shows
it under `data[].spec`. The driver fills two placeholders itself:
`{checkpoint}` in `resume` and `{task_dir}` in `after_each`.

| Key | Meaning |
|---|---|
| `run_id`, `batch`, `project`, `label`, `config`, `host` | identity, copied into the heartbeat |
| `root` | the run tree; the driver exits 2 when it is not a directory |
| `driver` | the driver copy the run started with; a resume uses it |
| `state_file`, `queue_dir` | the heartbeat path and the task queue |
| `shell`, `env` | every command runs through `shell -c` with `env` added |
| `limits` | `host_free_min_gb`, `streak`, `heartbeat_s`, `gate_max_s` |
| `start_at` | `{"stage": name, "checkpoint": null}`; a checkpoint makes the first stage run `resume` |
| `stages` | the stages in run order |

A one-command stage holds `name`, `cwd`, `needs`, `cmd`, `resume`,
`steps`, `progress`, `budget`, `retry` and `licence`. A task group holds
`parallel`, `prepare`, `after_each`, `budget`, `licence` and `tasks`,
each task with `id`, `cmd`, `dir`, `needs` and `budget`.

### The heartbeat

The driver writes `<run_id>.json` by a temporary file and a rename every
`heartbeat_s` seconds and at every phase change, so a reader never sees
a torn file.

| Field | Meaning |
|---|---|
| `phase`, `stage`, `step`, `step_name` | where the run is; `step` comes from `progress` every 5 s |
| `driver_pid`, `pgids` | what `edr stop` signals |
| `stages` | per stage started: `status` (`running`, `done`, `failed`, `over_budget`), `attempt`, `started`, `ended`, `exit`, `log` |
| `tasks` | per task: `phase`, `pid`, `pgid`, `started`, `ended`, `exit`, `signature`, `log` |
| `counts` | `done`, `failed`, `skipped`, `running`, `queued`, over every task group of the run |
| `started`, `updated`, `elapsed_s` | unix times; the watcher reads the age of `updated` |
| `disk_free_gb`, `tree_gb` | free space at `root`; `du -s` of the tree every tenth heartbeat |
| `exit`, `killed_by` | set at the end; `killed_by` is a signal name or `stop` |
| `last_cmd`, `last_log`, `log` | the last command, the last three lines of the current log, its path |
| `keep_hours` | the hours the keep file adds |
| `licence_unknown`, `host_full`, `over_budget`, `looping`, `stop` | flags the watcher classifies on |

A heartbeat keeps its last phase after the driver dies. `edr status
--live` asks the host whether the driver exists; the watcher marks the
run `dead` after `dead_s`.

## Phases and exit codes

| Phase | Exit | Meaning |
|---|---|---|
| `setup` | | the spec is loaded, the first disk check runs |
| `gate:<stage>` | | the driver waits for licence seats |
| `stage:<stage>` | | one command runs |
| `retry:<stage>:<n>` | | attempt `n` after a failure that matched `retry.match` |
| `group:<stage>` | | a task group runs |
| `done` | 0 | every stage ran; no task failed or was skipped |
| `INCOMPLETE:<n>f<m>s` | 8 | every stage ran; `n` tasks failed, `m` were skipped |
| `FAILED:<stage>` | 3 | free space at `root` is below `needs.disk_gb` of the first stage |
| `FAILED:<stage>` | 4 | the licence gate timed out after `gate_max_s` |
| `FAILED:<stage>` | 5 | a command or `prepare` failed with no retry left, or the driver hit an error |
| `FAILED:<stage>` | 2 | a checkpoint on a stage without `resume` |
| `OVER_BUDGET:<stage>` | 9 | a budget passed; the command ended or was killed |
| `STOPPED` | 10 | a stop file ended the run |
| `KILLED:<signal>` | 10 | a signal ended the run |

Exit 2 also means bad usage, a spec that does not load or a `root` that
is not a directory; then no heartbeat is written and the reason is in
`<run_id>.driver.log`. A terminal phase sets `exit`, and the watcher and
the board treat the run as finished.

On `SIGTERM`, `SIGHUP` or `SIGINT` the driver sets `killed_by`, forwards
the signal to every process group it started, waits up to 10 s, sends
`SIGKILL` to what is left, writes a final heartbeat and exits 10.

## Licence gates

A stage with `needs.licence` starts with `gate:<stage>`. The driver runs
the probe of the licence, reads the FlexLM line, and waits while the
free seats minus the seats it needs are below the `floor`, polling every
5 s, up to `gate_max_s`; then the stage fails with exit 4. A probe that
fails counts as unknown: the driver logs it, sets `licence_unknown`, and
runs the stage. In a task group the check runs before each claim, and a
short pool delays the next claim by 5 s.

## Retries

A stage with `retry` that fails is tried again when `retry.match` is
found in the last 80 lines of its log, after `retry.wait_s`, up to
`retry.max` times, with the phase `retry:<stage>:<n>`. Every attempt
appends to the same log and adds an attempt to `stages` in the
heartbeat.

## Budgets and the disk

| Limit | Who applies it | What happens |
|---|---|---|
| `needs.disk_gb` of the first stage | the driver at start | below it the run fails with 3 |
| `needs.disk_gb` of a task | the driver before the claim | below it the task is `skipped` and counted |
| `budget.hours`, `budget.disk_gb` of a stage | the driver while the command runs | at the limit the phase becomes `OVER_BUDGET:<stage>`; with `kill = true` the process group gets `SIGTERM`, else the command runs to its end; the run stops after it with exit 9 |
| `budget.hours` with `per = "task"`, or a task's own | the driver per task | only that task gets `SIGTERM` |
| `limits.host_free_min_gb` | the driver between stages and before each claim | below it nothing new starts and `host_full` is set |
| `limits.streak` | the driver per group | that many equal failure signatures in a row set `looping`; the group claims nothing more |

`edr keep <handle> --hours N` adds `N` hours to the running stage or
task. The driver reads the keep file at every heartbeat; a keep file
older than the start of the current stage or task does not count.
`docs/watcher.md` says what the watcher does at `host_full` and `hung`.
Nothing here deletes a file.

## Resume from a checkpoint

A stage with `resume` can continue from a step. `{checkpoint}` in that
command takes the step name, so a flow with `FIRST_STAGE={checkpoint}`
skips the steps before it. The watcher resumes a `dead` run this way
once, from the last `step_name` of its heartbeat. By hand:

```sh
edr run a@sweep1 --stage pnr --from cts
```

Without `--from` the stage starts from its first step, which in many
flows deletes the checkpoints it would need. `edr status --triage`
proposes the command with `--from` filled from the heartbeat.

## More work on an existing tree

`edr run <handle> --stage <S>` starts one stage on the tree of a run
that ended: more tasks of a task group, a stage the job skipped, or a
resume. The new run joins the batch of that run, so `retire --batch`
takes both. It has its own id and heartbeat, with the time of the call
as its date and the label `<label>.<stage>`, and `{tree_id}` of the
original run, so the flow keeps writing into the same directory. A run
on an imported tree joins `imported`.
`--tasks` names the tasks, `--parallel` the width, `--on` another host
when the tree is reachable there.

A job in a batch file does the same through `reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true }
```

## Shards and the queue

A task group runs its tasks through
`<queue_dir>/<stage>/{pending,claimed,done}/`. Before the group starts,
the driver creates `pending/<id>` per task; a task with `done/<id>` or
a claim is not created again. A claim renames `pending/<id>` to
`claimed/<id>.<run_id>`; a rename that fails means another driver took
the task. At the end the claim moves to `done/<id>`. The rename is
atomic on one filesystem, so two drivers with the same `queue_dir` share
one pool: a spec written by hand with that `queue_dir` adds a shard.
`edr run` gives its run a queue of its own.

A task that fails gets a `signature`, the last log line with every digit
removed; `edr status <handle>` shows it per task.

## Import

`edr import` records a run the package did not make.

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --src abc1234 \
    --host hostA --root /scratch/user/edr/myflow/20260830_0000_base_base_gabc1234
```

records a tree, so `reuse` and `edr run` can continue it. A tree is a
delete target only when its path carries the safety marker.

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --src abc1234 \
    --results /archive/base --tasks softmax_197 --why "tree gone, reports kept"
```

links `/archive/base` as `data/results/<run id>` and extracts the
project's metrics from it, so `metrics` and `export` cover a result
whose tree is gone. The directory holds the collected files in the run
layout, the same paths `collect` names. An imported run carries the
import time as `started` and `ended`.

## Retire and prune

`edr retire <handle> --why <text>` removes the run tree on the host with
`rm -rf`. `--prune <name>` removes only the paths that `prune.<name>`
names in the stages, such as a library or a build directory, and keeps
the tree. `--batch <B>` retires every run of a batch and writes
`RETIRED`, so the watcher skips it and the board drops it. It then
removes the staged tree of the batch's source under `source.worktrees`
when no other batch that is not retired has the same source: a worktree
with `git worktree remove --force`, a dirty snapshot with a plain
delete, both on the head node. The source repository is never a target.

Every target passes the guard first: an absolute path with the safety
marker and at least `min_depth` components, not `/`, not the home
directory, not a one-component path. `retire` also refuses:

- a run whose driver is alive on the host; stop it first
- a live run with no heartbeat yet, for `dead_s` after its start
- a tree that another live run uses
- a tree shared with a run whose results are not collected; retire them
  together with `--batch`
- a tree whose own results are not in `data/results/`, unless
  `--uncollected`

Run `edr watch --once` before a retire, so the results are on the head
node, and `--dry-run` first, which prints every `rm -rf` target and the
staged tree.

## Archive, then clear the hosts

At the end of a project the trees leave the hosts and the head node keeps
what a paper, a review or a rerun needs. `data/results/<run id>/` is that
archive: the watcher fills it with `collect` while a run goes, and
`retire --collect` adds the larger files before the tree goes.

Name the larger files once, per stage, under `collect_on_request`:

```toml
[stages.export]
collect_on_request = { netlist = ["out/15/"], power_inputs = ["out/15/", "sdc/", "spef/"] }
```

Then, per batch:

```sh
edr watch --once                                     # the reports of every finished run
edr retire --batch sweep2 --collect netlist,power_inputs --why "project done" --dry-run
edr retire --batch sweep2 --collect netlist,power_inputs --why "project done"
```

The dry run lists every copy and every `rm -rf`. The real run copies
first and deletes nothing when a copy failed, so a tree is gone only
when its files are on the head node. The `artifacts` table records
every copied file with its class, `always` or the list name, and
`edr export` takes the small files from the same directory.

A rerun starts from the archive through `restore` on `reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true, restore = "power_inputs" }
```

`edr launch` stages the source tag of the archived run, syncs a fresh
tree to a host that fits, copies the `power_inputs` files of the
archived run into it at their old paths, and starts the stage. The new
run keeps the `tree_id` and the build tag of the archived one, so every
path in the flow resolves as before.
