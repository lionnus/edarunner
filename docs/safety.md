# Safety

Every rule here comes from one failure on a real farm. The failure is named
so the rule keeps its reason. The code that holds the rule is named so you
can read it.

## Rules and incidents

| Rule | Incident | Code |
|---|---|---|
| A stop signals the `driver_pid` and the `pgids` from the heartbeat. It never uses a session name or a `pgrep` pattern. | `tmux kill-server` ended two user sessions that were not part of the batch. | `launch.stop`, `Ssh.kill_pgid` |
| Every `rm -rf` and every `rsync --delete` passes its target through a guard first. | `rsync --delete` with an empty run id would have erased every run on a host. | `guards.assert_safe_target`, `sync.sync_tree`, `cli.cmd_retire` |
| The driver starts in its own session, and every stage command in a new session. A stop signals the tool's group, never the shell you stand in. | A kill from inside the tmux pane took the pane down. SIGHUP reached the whole group, and four runs of about 7.5 h died. | `launch.start_driver`, `Driver.spawn` |
| A group that goes on after a failure counts it. The run ends `INCOMPLETE:<n>f<m>s` with exit 8, never `done`. `streak` equal failures in a row stop the group. | "kernel failed, continuing" hid 38 failed kernels for two hours. | `Driver.end_task`, `Driver.main` |
| A state file is not proof of life. The watcher marks a run `stale` after `stale_s` and `dead` after `dead_s` with no driver on the host. `edr status --live` asks the hosts. | Eight runs showed one phase for 35 h with nothing behind them. The farm sat idle while the board said "9 running". | `watch.classify`, `cli._mark_live` |
| Nothing deletes on its own. The watcher stops a run; it never removes a file. `retire` needs `--why` and refuses a tree whose results are not in `data/results/`. | A VCD that took 3.5 h to write was deleted before its number was read. | `watch._act`, `cli._retire_targets` |
| Disk has a floor and a budget. Below `host_free_min_gb` the driver starts nothing new. After `grace_s` the watcher stops the newest task on that host. `budget.disk_gb` caps one tree. | Two runs filled two hosts for 18 days. | `Driver.host_full`, `Driver.budget_over`, `watch._act` |
| The watcher watches itself. It writes `watch.json` every cycle. `edr watch --check` from cron exits 1 and notifies when that file is older than three cycles. A run with no progress for `hung_s` is `hung`. | A supervisor died and nobody noticed. The runs hung for two weeks. | `watch.check`, `watch.classify` |
| The driver is published by a temporary file and a rename, never by a write in place. | A `cp` over a running script moved the text under a live bash. The driver read garbage, logged one failure twice and died. | `sync.publish_driver` |
| A retired batch keeps its state. `RETIRED` in the batch directory tells the watcher to skip it. | Ten deleted runs still showed as live, and the stall check reported them for days. | `cli.cmd_retire`, `watch.read_heartbeats` |
| One table holds one design. `metrics` and `export` take `--design` and have no default. | Three versions of one design were live together. A power run started on the wrong one and cost 7.5 h. | `cli.cmd_metrics`, `cli.cmd_export` |

## Guards

`guards.assert_safe_target(path, marker, min_depth)` refuses:

- an empty path
- a relative path
- `/`, `/home`, `/usr`, `/tmp`, `/scratch`, `/scratch2`, `/var`, `/opt`
- the home directory of the user
- an empty marker, or a path without the marker (`safety.marker`, default `/edr/`)
- a path with fewer than `safety.min_depth` components (default 4)

`guards.assert_run_id(id)` refuses an id that does not start with
`YYYYMMDD_HHMM_`.

`Ssh.kill_pgid(host, pgid, sig)` refuses a group id of 1 or lower, and a
signal name with characters other than capital letters and digits.
`kill -TERM -- -0` would signal every process of the user.

`retire` refuses when the driver of the run is alive on the host. Stop it
first.

A guard raises `Refuse`. The verb prints `edr: <reason>` to stderr, exits 1,
and runs nothing after the refusal. The events table gets no row, because
nothing happened.

## Dry runs

Every verb that writes takes `--dry-run`: `init`, `stage`, `plan`, `launch`,
`run`, `keep`, `export`, `stop`, `retire`, `watch`. A dry run prints every
path and every command with the mark `(dry)` or the prefix `dry:`, and
writes nothing:

- no date pin in `<state>/<batch>/RUN_DATE`
- no spec, no driver copy, no stop file, no keep file
- no ledger row, no event
- no `data/board/` and no `data/results/`

`tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` runs the whole
flow dry, then checks that the state directory does not exist, the scratch
is empty, and the worktree is the same byte for byte.

Run the dry twin first. Read every target path. Then run the verb.

## Budgets

Three tables in `edr.toml` limit a run. `docs/config.md` lists every key.

| Table | Keys | Who applies it |
|---|---|---|
| `needs` of a stage or task | `cores`, `disk_gb`, `licence` | placement at `plan`; the driver before a stage and before each task |
| `budget` of a stage or task | `hours`, `disk_gb`, `kill`, `per` | the driver while the command runs |
| `[limits]` | `stale_s`, `dead_s`, `hung_s`, `grace_s`, `host_free_min_gb`, `streak`, `heartbeat_s`, `gate_max_s`, `kill_hung`, `kill_orphan` | the driver and the watcher |

What happens at a limit:

- `needs.disk_gb` short before the first stage: the driver exits 3. Short
  before a task: the task is skipped and counted.
- `needs.licence` below `floor` for `gate_max_s`: the driver exits 4. A probe
  that fails counts as unknown, and the stage runs.
- `budget.hours` passed with `kill = false`: the command runs to its end, the
  phase becomes `OVER_BUDGET:<stage>`, and the driver exits 9 after it. With
  `kill = true` the group gets `SIGTERM`. With `per = "task"` only that task
  is killed.
- `budget.disk_gb` passed by `tree_gb`: the same as `hours`.
- `streak` equal failure signatures in a row: the group claims nothing more.
- free space below `host_free_min_gb`: the driver starts nothing new and
  sets `host_full`; the watcher stops the newest task on the host after
  `grace_s`.

`edr keep <handle> --hours N` adds `N` hours to the running stage or task.
`--ack` cancels a pending kill of the watcher. The driver reads the keep
file at every heartbeat.

The watcher kills only when the config says so. `kill_hung` and
`kill_orphan` are `false` by default. Without them a `hung` or `orphan`
state is an event and a notification.

## States

The watcher classifies every live run each cycle. Section 7 of
`docs/design.md` is the contract.

| State | Test | Action |
|---|---|---|
| `running` | heartbeat younger than `stale_s` | none |
| `stale` | heartbeat between `stale_s` and `dead_s`; or older, but the driver is alive or the host did not answer | event |
| `dead` | heartbeat older than `dead_s` and no driver process on the host | event, notify; one resume from the last step when the stage has `resume` and no recorded pgid is alive |
| `hung` | heartbeat fresh, a stage or task runs, and nothing changed for `hung_s`: phase, step, `tree_gb`, log tail, task counts, log size, CPU time of the groups | event, notify; `SIGTERM` to the groups after `grace_s` only with `kill_hung` and no `ack` |
| `orphan` | a process of the user that matches `tool_procs` on a host, outside every live run tree | event, notify; `SIGTERM` after `grace_s` only with `kill_orphan` |
| `looping` | the driver set `looping` | event, notify |
| `over_budget` | the driver set `over_budget` | event, notify |
| `host_full` | the driver set `host_full` | notify; after `grace_s`, `stop --now` on the newest run of that host, unless that run has `ack`; never a delete |
| `superseded` | a newer batch runs the same label at another `src` | notify; `stop --after-task`, unless the run has a keep file |

A finished run takes its state from the phase: `done`, `incomplete`,
`failed`, `over_budget`, `stopped`, `killed`. A job without a host is
`queued`; the watcher launches it when a host fits.

`edr status --triage` lists every run that is not `running`, with one
proposed command per state.

## What the watcher never does

- It never deletes a file or a tree.
- It never kills without `kill_hung` or `kill_orphan`, and never after an
  `ack`.
- It never resumes a run twice.
- It never reads a batch with `RETIRED`.
