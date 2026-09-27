# The driver protocol

This page describes the driver, the spec it reads and the heartbeat it
writes, and which tests and pages must change along with them.

## One file, one run

`edr_driver.py` is a single file for Python 3.6 or newer that uses only
the standard library. It reads one spec and nothing else, runs the stages
in order, and writes one heartbeat. It never imports the package; a feature the 3.6 subset
cannot express belongs in the controller.

`edr launch` copies the file to `<state_dir>/bin/edr_driver-<hash>.py`, where
`<hash>` is the first 8 hex digits of the sha256 of the text, by a
temporary file and a rename (`sync.publish_driver`). A copy that exists
is reused. A new version gets a new name, so a live driver never sees
its text change. The spec records the copy the run started with, and a
resume uses that copy.

The host starts the driver with its own `python3` from the login `PATH`,
in its own session (`backend.SshBackend.submit`):

```sh
setsid nohup python3 <state_dir>/bin/edr_driver-<hash>.py <state_dir>/<batch>/<run_id>.spec.json
```

Each stage command runs through `<shell> -c` in a new session
(`Driver.spawn`), with stdin from `/dev/null` and stdout and stderr
appended to a log in the run tree. Every command starts with a line
`# edr: <cmd>` in its log. The site `env` and the project `[env]` reach
the command through the spec; `$VAR` in a value expands on the host.

| File in the run tree | Holds |
|---|---|
| `log/<stage>.log` | stdout and stderr of a stage command; every attempt is appended |
| `log/<stage>.prepare.log` | the `prepare` command of a task group |
| `log/<stage>.<task>.log` | one task, then its `after_each` |

## The state directory

`state_dir` in `edr.toml` names a directory on a filesystem every host
mounts. The hosts read the driver and the spec there and write the
heartbeat; the head node reads the heartbeat.

```
<state_dir>/
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

## The spec

`launch._spec` renders every string of a job into the spec; `edr plan
--json` shows it under `data[].spec`. The driver fills two placeholders
itself: `{checkpoint}` in `resume` and `{task_dir}` in `after_each`.

| Key | Meaning |
|---|---|
| `run_id`, `batch`, `project`, `label`, `config`, `host` | identity, copied into the heartbeat |
| `root` | the run tree; the driver exits 2 when it is not a directory |
| `driver` | the driver copy the run started with; a resume uses it |
| `record` | `edarunner` (the version that launched), `driver_sha256`, and `tools` with the version the site file gives per tool of the host; the export manifest copies it |
| `state_file`, `queue_dir` | the heartbeat path and the task queue |
| `shell`, `env` | every command runs through `shell -c` with `env` added |
| `limits` | `host_free_min_gb`, `streak`, `heartbeat_s`, `gate_max_s`, `lease_s` |
| `start_at` | `{"stage": name, "checkpoint": null}`; a checkpoint makes the first stage run `resume` |
| `stages` | the stages in run order |
| `collect` | `false` for a run of `edr track` without `--collect`: the watcher collects nothing; the driver does not read it |

A one-command stage holds `name`, `cwd`, `needs`, `cmd`, `resume`,
`steps`, `progress`, `budget`, `retry` and `tools`. A task group holds
`parallel`, `prepare`, `after_each`, `budget`, `tools` and `tasks`, each
task with `id`, `cmd`, `dir`, `needs`, `budget` and, when its own
`needs` names tools, `tools`. A `tools` entry is `{"name", "seats",
"probe", "leases"}`: the seats needed, the probe argv, rendered, and the
lease directory `<state_dir>/leases/<tool>/`, which the launch creates; a
tool without a probe is not in the list.

A change to the spec changes `launch._spec`, the driver and
`tests/test_driver.py` in the same commit. `tests/helpers_driver.py`
renders a spec for the demo flow and starts the driver on it.

## The heartbeat

The driver writes `<run_id>.json` by a temporary file and a rename every
`heartbeat_s` seconds and at every phase change, so a reader never sees
a torn file. A heartbeat keeps its last phase after the driver dies;
the watcher marks such a run `dead` after `dead_s`. The hung check of
the watcher compares `cpu_s` and `log_bytes` from cycle to cycle. A
heartbeat without them, from an older driver, makes the watcher read
the two values over ssh.

| Field | Meaning |
|---|---|
| `phase`, `stage`, `step`, `step_name` | where the run is; `step` comes from `progress` every 5 s |
| `host` | the `host` of the spec; the name `socket.gethostname()` gives when the spec has none |
| `sched_id` | the job id of a scheduler: `EDR_SCHED_ID`, else `SLURM_JOB_ID`, else `LSB_JOBID`; null without one |
| `driver_pid`, `pgids` | what `edr stop` signals |
| `cpu_s`, `rss_gb` | the CPU time in seconds and the summed RSS of every process in `pgids`, read in one pass over `/proc` at every heartbeat; the CPU time includes reaped children. Without `/proc`, both come from `ps -e -o pgid=,cputimes=,rss=`, which gives whole seconds and leaves out the children |
| `log_bytes` | the size of `log` in bytes; every heartbeat |
| `stages` | per stage started: `status` (`running`, `done`, `failed`, `over_budget`), `attempt`, `started`, `ended`, `exit`, `log` |
| `tasks` | per task: `phase`, `pid`, `pgid`, `started`, `ended`, `exit`, `signature`, `log` |
| `counts` | `done`, `failed`, `skipped`, `running`, `queued`, over every task group of the run |
| `started`, `updated`, `elapsed_s` | unix times; the watcher reads the age of `updated` |
| `disk_free_gb`, `tree_gb` | free space at `root`; `du -s` of the tree at most once per ten minutes |
| `cpu_pct` | the CPU use since the previous sample as a percent of one core, computed from two successive `cpu_s` values and the time between them. It is null on the first sample, and a heartbeat less than a second after the previous one keeps the old value. A process group that ended takes its CPU seconds with it, so a drop in `cpu_s` reads as zero |
| `step_times` | per stage, the unix time the driver first saw each step number from `progress`; a resumed step replaces its time in `step_runs` |
| `exit`, `killed_by` | set at the end; `killed_by` is a signal name or `stop` |
| `last_cmd`, `last_log`, `log` | the last command, the last three lines of the current log, its path |
| `keep_hours` | the hours the keep file adds |
| `gate` | why the run waits at a gate, such as `pnr: 1 free, 1 held by others, 1 needed`; null when it does not |
| `host_full`, `over_budget`, `looping`, `stop` | flags the watcher classifies on |

The exit code of the driver names its terminal phase. [run.md](../run.md)
lists the phases and the codes as a user reads them on the board.

## Signals, the stop file and the keep file

On `SIGTERM`, `SIGHUP` or `SIGINT` the driver sets `killed_by`, forwards
the signal to every process group it started, waits up to 10 s, and
sends `SIGKILL` to what is left. Then it writes a final heartbeat and
exits 10 with the phase `KILLED:<signal>`.

The driver reads the stop file next to the spec every 0.5 s while a
command runs or it waits at a gate. The content `after-task`, which
`edr stop --after-task` writes, lets a one-command stage run to its end
and a task group finish its running tasks. Then the run ends `STOPPED`
with exit 10. The content `now` makes the driver signal its groups at
once.

The driver reads the keep file at every heartbeat. `hours` adds to the
budget of the running stage or task; a keep file older than the start
of that stage or task does not count. `ack` is for the watcher, which
reads the same file.

## The task queue

A task group runs its tasks through
`<queue_dir>/<stage>/{pending,claimed,done}/`. Before the group starts,
the driver creates `pending/<id>` per task; a task with `done/<id>` or
a claim is not created again. A claim renames `pending/<id>` to
`claimed/<id>.<run_id>`; a rename that fails means another driver took
the task. At the end the claim moves to `done/<id>`. The rename is
atomic on one filesystem, so two drivers with the same `queue_dir` share
one pool. `edr continue` gives its run a queue of its own.

A task that fails gets a `signature`, the last log line with every digit
removed. `streak` equal signatures in a row set `looping`, and the group
claims nothing more.

## What a change touches

| A change to | Also changes |
|---|---|
| the spec | `launch._spec`, `tests/test_driver.py`, the table above |
| a heartbeat field | `watch.ingest`, `board.py`, the table above; `cpu_s` and `log_bytes` also `watch._signature` |
| a phase or an exit code | `board.state_of`, `watch.STATES`, the table in `run.md` |
| the queue layout | `collect.py`, which reads the task directories from the spec |

`tests/test_driver.py::test_compiles_on_py36` compiles the file with a
Python 3.6; [testing.md](testing.md) says where that interpreter comes
from.
