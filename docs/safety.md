# Safety

Every rule here comes from one failure on a real farm. The table names the
failure, so the rule keeps its reason, and the code that holds the rule,
so you can read it.

## Rules and incidents

| Rule | Incident | Code |
|---|---|---|
| A stop signals the `driver_pid` and the `pgids` from the heartbeat. It never uses a session name or a `pgrep` pattern. | `tmux kill-server` ended two user sessions that were not part of the batch. | `launch.stop`, `Ssh.kill_pgid` |
| Every `rm -rf` and every `rsync --delete` passes its target through a guard first. | `rsync --delete` with an empty run id would have erased every run on a host. | `guards.assert_safe_target`, `sync.sync_tree`, `cli.cmd_retire` |
| The driver starts in its own session, and every stage command in a new session. A stop signals the tool's group, never the shell you stand in. | A kill from inside the tmux pane took the pane down. SIGHUP reached the whole group, and four runs of about 7.5 h died. | `launch.start_driver`, `Driver.spawn` |
| A group that goes on after a failure counts it. The run ends `INCOMPLETE:<n>f<m>s` with exit 8, never `done`. `streak` equal failures in a row stop the group. | "kernel failed, continuing" hid 38 failed kernels for two hours. | `Driver.end_task`, `Driver.main` |
| A state file is not proof of life. The watcher marks a run `stale` after `stale_s` and `dead` after `dead_s` with no driver on the host. `edr status --live` asks the hosts. | Eight runs showed one phase for 35 h with nothing behind them. The farm sat idle while the board said "9 running". | `watch.classify`, `cli._mark_live` |
| Nothing deletes on its own. The watcher stops a run; it never removes a file. `retire` needs `--why` and refuses a tree whose results are not in `data/results/`. | A VCD that took 3.5 h to write was deleted before its number was read. | `watch._act`, `cli._retire_targets` |
| Disk has a floor and a budget. Below `host_free_min_gb` the driver starts nothing new. After `grace_s` the watcher stops the newest run on that host. `budget.disk_gb` caps one tree. | Two runs filled two hosts for 18 days. | `Driver.host_full`, `Driver.budget_over`, `watch._act` |
| The watcher watches itself. It writes `watch.json` every cycle. `edr watch --check` from cron exits 1 and notifies when that file is older than three cycles. A run with no progress for `hung_s` is `hung`. | A supervisor died and nobody noticed. The runs hung for two weeks. | `watch.check`, `watch.classify` |
| The driver is published by a temporary file and a rename under a name that carries the hash of its text, never by a write in place. | A `cp` over a running script moved the text under a live bash. The driver read garbage, logged one failure twice and died. | `sync.publish_driver` |
| A retired batch keeps its state. `RETIRED` in the batch directory tells the watcher to skip it. | Ten deleted runs still showed as live, and the stall check reported them for days. | `cli.cmd_retire`, `watch.read_heartbeats` |
| One table holds one design. `metrics` and `export` take `--design` and have no default, and the tag matches exactly. | Three versions of one design were live together. A power run started on the wrong one and cost 7.5 h. | `cli.cmd_metrics`, `export._select` |
| A tree that another run uses is never a delete target. `retire` refuses a root a live run uses and a root shared with a run whose results are not collected. | Every power run on a reused tree has that tree as its root. A retire of one would have taken the netlist of the others. | `cli._refuse_shared_root` |

## Guards

`guards.assert_safe_target(path, marker, min_depth)` refuses:

- an empty path, or a relative one
- `/`
- the home directory of the user
- a path with one component, such as `/scratch`
- an empty marker, or a path without the marker (`safety.marker`,
  default `/edr/`)
- a path with fewer than `safety.min_depth` components (default 4)

A `..` segment is resolved first, so it cannot carry the marker past the
tree it names.

`guards.assert_run_id(id)` refuses an id that does not start with
`YYYYMMDD_HHMM_`.

`Ssh.kill_pgid(host, pgid, sig)` refuses a group id of 1 or lower, and a
signal name with characters other than capital letters and digits,
because `kill -TERM -- -0` would signal every process of the user.
`launch.stop` refuses a driver pid of 1 or lower for the same reason.

`retire` refuses a run whose driver is alive, a live run with no
heartbeat yet, a root another live run uses, and a root shared with an
uncollected run. `docs/running.md` lists them with the way out. The
checked-out tree that `retire --batch` removes passes `assert_safe_target`
too, and `source.repo` is never a target.

A guard raises `Refuse`. The command prints `edr: <reason>` to stderr, exits
1, and runs nothing after the refusal. The events table gets no row,
because nothing happened.

## Dry runs

Every command that writes takes `--dry-run`; `docs/reference/cli.md` marks
them. A dry run prints every path and every command with the mark `(dry)` or
the prefix `dry:`, and writes nothing:

- no date pin in `<state_dir>/<batch>/RUN_DATE`
- no spec, no driver copy, no stop file, no keep file
- no database row, no event, not even the database file
- no `data/board/` and no `data/results/`

`tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` runs the whole
flow dry, then checks that the state directory does not exist, the scratch
is empty, and the worktree is the same byte for byte.

Do the dry run first. Read every target path. Then run the command.

## Limits

`docs/running.md` says what the driver does at each `needs`, `budget`
and `[limits]` value, and `docs/watcher.md` what the watcher does with
each state. The short form: the driver skips, stops or kills its own
processes; the watcher kills only with `kill_hung` or `kill_orphan`, and
never after an `ack`; nothing deletes.
