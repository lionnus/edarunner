# The driver

`edr_driver.py` runs one run on one host. It is one file, Python 3.6 or
newer, standard library only, and it never imports the package. `edr
launch` copies it to `<state>/bin/<batch>/edr_driver.py` and starts it with
one argument, the spec:

```sh
python3 <state>/bin/<batch>/edr_driver.py <state>/<batch>/<run_id>.spec.json
```

The driver reads nothing else. Section 5 of `docs/design.md` is the
contract; this file explains how to read what the driver writes and how to
drive it by hand.

## The run spec

`edr plan` renders every string of a job, and `edr launch` writes the
result to `<state>/<batch>/<run_id>.spec.json`.

| Key | Meaning |
|---|---|
| `run_id`, `batch`, `project`, `label`, `config`, `host` | identity; copied into the heartbeat |
| `root` | the run tree on the host; it must exist, or the driver exits 2 |
| `state_file` | the heartbeat path |
| `queue_dir` | the task queue, `<state>/<batch>/<run_id>.queue` |
| `shell`, `env` | every command runs through `shell -c`, with `env` added to the environment |
| `limits` | `host_free_min_gb`, `streak`, `heartbeat_s`, `gate_max_s` |
| `start_at` | `{"stage": name, "checkpoint": null}`; a checkpoint makes the first stage run `resume`; a stage without `resume` fails with 2 |
| `stages` | the list below, in run order |

A stage entry holds `name`, `cwd` and `needs`, plus one of two shapes. A
one-command stage has `cmd`, `resume`, `steps`, `progress`, `budget`,
`retry` and `licence`. A task group has `parallel`, `prepare`,
`after_each`, `budget`, `licence` and `tasks`, where each task holds `id`,
`cmd`, `dir`, `needs` and `budget`.

The driver fills two placeholders itself: `{checkpoint}` in `resume` and
`{task_dir}` in `after_each`. Every other placeholder is rendered before
the driver sees the spec.

## The heartbeat

The driver writes `state_file` through a temporary file and a rename,
every `heartbeat_s` seconds and at every phase change, so a reader never
sees a torn file.

| Field | Meaning |
|---|---|
| `phase`, `stage`, `step`, `step_name` | where the run is; `step` comes from the `progress` command every 5 s |
| `driver_pid`, `pgids` | what `edr stop` signals |
| `stages` | per stage the driver started: `status` (`running`, `done`, `failed`, `over_budget`), `attempt`, `started`, `ended`, `exit`, `log` |
| `tasks` | per task: `phase`, `pid`, `pgid`, `started`, `ended`, `exit`, `signature`, `log` |
| `counts` | `done`, `failed`, `skipped`, `running`, `queued`, across every group of the run |
| `started`, `updated`, `elapsed_s` | unix times; the watcher reads the age of `updated` |
| `disk_free_gb`, `tree_gb` | free space at `root`; `du -s` of `root` every tenth heartbeat |
| `exit`, `killed_by` | set at the end; `killed_by` is a signal name or `stop` |
| `last_cmd`, `last_log`, `log` | the last command, the last three lines of the current log, the current log path |
| `keep_hours` | the hours the keep file adds |
| `licence_unknown`, `host_full`, `over_budget`, `looping`, `stop` | flags the watcher classifies on; `licence_unknown` follows the last probe, `looping` clears when a group starts |

## Phases

| Phase | Meaning |
|---|---|
| `setup` | the spec is loaded; the first disk check runs |
| `gate:<stage>` | the driver waits for licence seats |
| `stage:<stage>` | one command runs |
| `retry:<stage>:<n>` | attempt `n` after a failure that matched `retry.match` |
| `group:<stage>` | a task group runs |
| `done` | every stage ran; no task failed or was skipped |
| `INCOMPLETE:<n>f<m>s` | every stage ran; `n` tasks failed, `m` were skipped |
| `FAILED:<stage>` | a command failed with no retry left, the gate timed out, or the disk was short |
| `OVER_BUDGET:<stage>` | a budget passed; the command ended or was killed |
| `STOPPED` | a stop file ended the run |
| `KILLED:<signal>` | a signal ended the run |

A terminal phase sets `exit`. The watcher and the board treat a run with a
terminal phase as finished.

## Exit codes

| Code | Phase |
|---|---|
| 0 | `done` |
| 2 | bad usage, bad spec, or `root` is not a directory; no heartbeat is written |
| 3 | `FAILED:<stage>`; free space at `root` is below `needs.disk_gb` of the first stage |
| 4 | `FAILED:<stage>`; the licence gate timed out after `gate_max_s` |
| 5 | `FAILED:<stage>`; a command failed, `prepare` failed, or the driver hit an error |
| 8 | `INCOMPLETE:<n>f<m>s` |
| 9 | `OVER_BUDGET:<stage>` |
| 10 | `STOPPED` or `KILLED:<signal>` |

## The queue

A task group runs its tasks through
`<queue_dir>/<stage>/{pending,claimed,done}/`.

1. Before the group starts, the driver creates `pending/<id>` for each
   task with `O_EXCL`. A task that already has `done/<id>` is not created
   again.
2. To claim a task, the driver renames `pending/<id>` to
   `claimed/<id>.<run_id>`. A rename that fails means another driver took
   the task.
3. At the end of the task, `claimed/<id>.<run_id>` moves to `done/<id>`.

The rename is atomic on one filesystem, so two drivers with the same
`queue_dir` share one queue. A spec written by hand with the same
`queue_dir` adds a shard, while `edr run` gives its run a queue of its
own. The group ends when `pending` is empty and nothing runs.

Before each claim the driver checks three things: the free space against
the task's `needs.disk_gb` (a task that does not fit is `skipped`), the
host floor `host_free_min_gb` (below it the driver waits), and the licence
seats (when they are short, the driver waits 5 s).

A task that fails gets a `signature`, the last log line with every digit
removed. `limits.streak` equal signatures in a row set `looping`, and the
group claims nothing more.

## The stop file

The stop file is `<run_id>.stop`, next to the spec. `edr stop
--after-task` writes it with the content `after-task`, and the driver
reads it every 0.5 s.

| Content | Effect |
|---|---|
| `after-task` | a group finishes the running tasks and claims nothing more; a one-command stage runs to its end; the run ends `STOPPED`, exit 10 |
| `now`, or empty | `SIGTERM` to every process group; `STOPPED`, exit 10 |

## The keep file

The keep file is `<run_id>.keep.json`, next to the spec, with the content
`{"hours": N, "ack": true}`. `edr keep` writes it. At every heartbeat the
driver reads `hours` and adds it to `budget.hours` of the running stage
or task. A keep file older than the start of the current stage or task
does not count.

`ack` is for the watcher. It cancels a pending kill and the `host_full`
stop.

## Signals

On `SIGTERM`, `SIGHUP` or `SIGINT` the driver sets `killed_by`, forwards
the same signal to every recorded process group, writes a final heartbeat
and exits 10. The phase is `KILLED:<signal>`, or `STOPPED` when a stop
file exists. The driver waits up to 10 s for the groups, then sends
`SIGKILL`.

`edr stop <handle>` sends `SIGTERM` to `driver_pid` and to every pgid,
waits `grace_s`, and with `--now` sends `SIGKILL` after 30 s.

## Logs

| File | Holds |
|---|---|
| `<state>/<batch>/<run_id>.driver.log` | stdout and stderr of the driver itself: tracebacks, failed probes |
| `<root>/log/<stage>.log` | stdout and stderr of a stage command; every attempt is appended |
| `<root>/log/<stage>.prepare.log` | the `prepare` command of a group |
| `<root>/log/<stage>.<task>.log` | one task, then its `after_each` |

Every command starts with a line `# edr: <cmd>` in its log.

## Run the driver by hand

The normal way is `edr launch`, or `edr run <handle> --stage <S> --from
<checkpoint>` for more work on an existing tree. Both write the spec,
publish the driver by rename and start it in its own session. Run the
driver by hand only when you debug the driver or a flow.

1. Get a spec. After a launch it exists at
   `<state>/<batch>/<run_id>.spec.json`. Without a launch,
   `edr --json plan <batch>` prints one per job under `data[].spec`; write
   one to a file. Its `root` must exist and hold the tree, so `rsync` the
   staged worktree there first.
2. For a resume, set `start_at` to `{"stage": "pnr", "checkpoint":
   "route"}`. The driver then runs `resume` with `{checkpoint}` filled.
3. Start the driver in its own session, with the site `env` set:

   ```sh
   setsid nohup python3 <state>/bin/<batch>/edr_driver.py <spec> > <run_id>.driver.log 2>&1 < /dev/null &
   ```

   On the head node, `python3 src/edarunner/driver/edr_driver.py <spec>`
   in a terminal works too. `Ctrl-C` then ends the run as `KILLED:SIGINT`.
4. Read the heartbeat with `cat <state_file>`. Follow a stage with
   `tail -f <root>/log/<stage>.log`.
5. Stop it with `kill -TERM <driver_pid>`, or write `after-task` to
   `<run_id>.stop` next to the spec.

The driver looks for the stop file and the keep file next to the spec you
passed, not next to `state_file`. Keep the spec in `<state>/<batch>/`;
otherwise `edr stop --after-task` and `edr keep` write where the driver
does not look.

`tests/helpers_driver.py` builds a spec for the demo flow under `tmp_path`
and starts the driver with `/usr/bin/python3`. It is the shortest example
of a hand-made spec.
