# Guarantees

After this page you know what `edr` never does, what a dry run and a
guard promise, what the watcher does on its own, and how a stop works.
Each guarantee names the code that holds it, so you can read it.

## What edr never does

| edr never | Held by |
|---|---|
| deletes on its own. Only `edr retire` removes a tree, after `--why`, a guard on every target and a check that the results are on the head node. The watcher stops or kills a run; it removes no file but a stale seat lease. | `cli.cmd_retire`, `cli._retire_targets`, `watch._act`, `watch.sweep_leases` |
| signals by a session name or a process pattern. A stop signals the `driver_pid` and the `pgids` the heartbeat recorded, and refuses a pid or a group id of 1 or lower. | `launch.stop`, `backend.check_pid`, `hosts.Ssh.kill_pgid` |
| runs an `rm -rf` or an `rsync --delete` without the guard below. | `guards.assert_safe_target`, `sync.sync_tree`, `cli._retire_targets`, `cli._worktree_target` |
| deletes the source repository, or a tree another run uses. `retire` refuses a root a live run uses, and a root shared with a run whose results are not collected. | `cli._worktree_target`, `cli._refuse_shared_root` |
| overwrites a driver that runs. A new driver version gets a new file name with the hash of its text, written by a temporary file and a rename. | `sync.publish_driver` |
| writes a torn heartbeat or spec. Both go to disk by a temporary file and a rename. | `Driver.beat`, `config.save_json` |
| starts a stage in your shell's session. The driver starts in its own session, and every stage command in a new one, so a signal to a tool's group never reaches the shell you stand in. | `backend.SshBackend.submit`, `Driver.spawn` |
| hides a failed task. A task group counts every failure, and the run ends `INCOMPLETE:<n>f<m>s` with exit 8, never `done`. `streak` equal failures in a row stop the group. | `Driver.end_task`, `Driver.main` |
| takes a heartbeat as proof of life. A run is `stale` after `stale_s` and `dead` after `dead_s` with no driver on the host; `edr status --live` asks the hosts. | `watch.classify`, `cli._mark_live` |
| mixes two designs in one table. `metrics` and `export` take `--design` with no default, and the tag matches exactly. | `cli.cmd_metrics`, `export._select` |
| creates a file on a read. A read command without a database reads an empty one in memory. | `cli.Ctx`, `cli._READ_COMMANDS` |
| reads a retired batch. `RETIRED` in the batch directory keeps the watcher and the board off it. | `cli.cmd_retire`, `watch.read_heartbeats` |
| starts two stages on one free seat. The tool gate leases each seat by a rename, and the later of two drivers that read the same free seat backs off. | `Driver.take`, `Driver.release`, `watch.sweep_leases` |
| launches a batch twice. A job whose spec exists is already launched; a new sweep needs a new batch name. | `launch.launch` |

## Dry runs

Every command that writes takes `--dry-run`;
[reference/cli.md](reference/cli.md) marks them. A dry run prints every
path and every command with the mark `(dry)` or the prefix `dry:`, and
writes nothing:

- no date pin in `<state_dir>/<batch>/RUN_DATE`
- no spec, no driver copy, no stop file, no keep file
- no database row, no event, not even the database file
- no `data/board/` and no `data/results/`

`tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` runs the whole
flow dry, then checks that the state directory does not exist, the scratch
is empty, and the worktree is the same byte for byte.

Do the dry run first. Read every target path. Then run the command.

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
tree it names. `guards.assert_run_id(id)` refuses an id that does not
start with `YYYYMMDD_HHMM_`.

`hosts.Ssh.kill_pgid(host, pgid, sig)` refuses a group id of 1 or lower,
and a signal name with characters other than capital letters and digits.
`kill -TERM -- -0` would signal every process of the user.
`backend.check_pid` refuses a driver pid of 1 or lower for the same reason.

`retire` refuses a run whose driver is alive and a live run with no
heartbeat yet. It refuses a root another live run uses, a root shared
with an uncollected run, and a tree whose own results are not collected
unless `--uncollected`. The checked-out tree that `retire --batch` removes
passes `assert_safe_target` too, and `source.repo` is never a target.

A guard raises `Refuse`. The command prints `edr: <reason>` to stderr,
exits 1, and runs nothing after the refusal. The events table gets no
row, because nothing happened.

## The watcher

The watcher acts on its own only after `grace_s` and only as
[reference/states.md](reference/states.md) says per state:

- It stops the newest run of a full host with `--now`, unless that run
  has an `ack`.
- It stops a `superseded` run after its task, unless the run has a keep
  file.
- It sends `SIGTERM` to a `hung` run only with `kill_hung` and no `ack`,
  and to an orphan process only with `kill_orphan`.
- It resumes a `dead` run once, from its last step, when the stage has
  `resume` and no process group of the run is alive.
- It launches a queued job when a host fits, one per batch per cycle.
- It removes a stale seat lease, with a `lease` event that says why.

It never deletes a tree or any file but a stale seat lease, never reads a batch with `RETIRED`,
never downloads anything, and never resumes a run twice
(`watch.cycle`, `watch._act`, `watch._resume`, `watch.sweep_leases`).

## The database

SQLite documents that WAL does not work on a network filesystem: every
process must share one memory index, and two hosts do not. A project
directory under a home on NFS puts `data/edr.db` there. The database reads
the statfs type of its directory at open and uses the DELETE journal with
`synchronous=FULL` on NFS, SMB, 9p and FUSE, and WAL elsewhere. `edr check`
warns when the database sits on such a filesystem. A local `data/` is still
the better place, since the watcher and each `edr` call take a file lock
there that NFS gives only through its lock daemon (`db.Database`,
`cli.cmd_check`).

## A stop

`edr stop <handle> --after-task` writes the stop file next to the spec.
The driver lets a one-command stage run to its end and a task group
finish its running tasks; then the run ends `STOPPED`.

`edr stop <handle>` sends `SIGTERM` to the driver and to every process
group in the heartbeat, then waits up to 60 s. The driver's own handler
forwards the signal to its groups, waits 10 s, sends `SIGKILL` to what
is left, writes a final heartbeat and exits 10. A driver still alive
after the wait gives exit 3, and `--now` is the next step: `SIGTERM`,
then `SIGKILL` after 30 s (`launch.stop`, `Driver.on_signal`).

A stop takes one handle. A queued run is marked `stopped` and never
starts. The stop button of an alert asks first and then runs the
`--after-task` form.

## The bot

The Telegram bot obeys one `chat_id`, and one `user_id` when it is set.
Every other chat or user gets no answer, and the first message from it
makes one database event `rejected`. The token is the one secret; it
lives in a file with mode 600, and the bot refuses any other mode
(`notify/telegram/bot.py`).

The bot never runs a shell string or free text. A custom command is an
argv list from `site.toml` on the head node, and every argument from the
phone must match its allowlist regex in full
(`notify/telegram/custom.py`). The bot never kills a process, and never
runs `retire`, `prune`, `launch` or `rm`. `/stop` writes the
`after-task` stop file, and `/keep` and `/ack` write the keep file,
through the same commands as the CLI. Every command, action and refusal
lands in `events` with the actor `telegram`.
