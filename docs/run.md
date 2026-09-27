# Run and watch

This page covers the life of a batch: launching it, reading the board,
keeping a watcher behind it, acting on a run that stopped, continuing on
an existing tree, and clearing the hosts at the end.

![One run, from a commit to an analysis](diagrams/run-lifecycle.svg)

## Check out the source

```sh
edr checkout origin/main
```

This prints `<src> <path>`: the short hash of the commit and a detached
local clone at `<worktrees>/<src>`. Every run of that source works on a copy
of this tree, so a later commit never changes a running flow. A tree
with uncommitted changes goes in with `edr checkout --dirty <dir>`; its tag
is `<hash>-dirty-<8 hex>`, and `launch` needs `--allow-dirty` for it.

If the batch's source has not been checked out yet, `plan` and `launch`
check it out for you and print a `checkout <src> <path>` line. With
`--dry-run`, they print the git commands instead of running them. This
only works for a clean ref. A dirty snapshot has to be added with
`edr checkout --dirty <dir>` before you plan or launch it.

## Plan and launch

```sh
edr plan sweep1               # run id, host and root per job; writes no spec
edr plan sweep1 --show-spec   # also the env, commands and collect paths of each run
edr launch sweep1 --dry-run   # every path and command, nothing written
edr launch sweep1
```

Read every path of the dry run before the real launch. `launch` copies
the driver into the state directory and syncs the checked-out tree to
each host. It writes one spec per run, the JSON file the driver reads,
and starts one driver per run, `stagger_s` apart. A job that no host fits is
queued, and the watcher starts it when a host frees up. A batch name is
used once: a second launch finds the specs and does nothing.

Confirm within a minute that `edr status` shows a phase past `setup`.
A run that never writes a heartbeat left its reason in
`<state_dir>/<batch>/<run_id>.driver.log`.

## The board

```sh
edr status                    # the board
edr status --live             # asks each host whether the driver exists
edr status base@sweep1        # one run: stages, metrics, log tail
edr status --triage           # every run not running, with one proposed command
edr events --run base@sweep1  # the history of one run, with the reason of every action
```

`base@sweep1` is a handle. A handle names one run: `label@batch`, a run
id prefix, or `#n` from the last board that `edr status` printed. Every
command that acts on one run takes a handle.

A heartbeat file keeps its last phase after the driver dies, so a run
that the board shows as running may already be gone. `--live` asks the
hosts, and the watcher marks such a run `dead` after `dead_s`.

The board shows the phase of a live run and the state of every run. The
phase is the driver's word for where the run is or how it ended. The
state is the watcher's verdict;
[reference/states.md](reference/states.md) lists every state with its
test, its action and the command `--triage` proposes.

| Phase | Exit | Meaning |
|---|---|---|
| `setup` | | the spec is loaded, the first disk check runs, then `[runtime] setup` |
| `gate:<stage>` | | the driver waits for the seats of a tool |
| `stage:<stage>` | | one command runs |
| `retry:<stage>:<n>` | | attempt `n` after a failure that matched `retry.match` |
| `group:<stage>` | | a task group runs |
| `done` | 0 | every stage ran; no task failed or was skipped |
| `INCOMPLETE:<n>f<m>s` | 8 | every stage ran; `n` tasks failed, `m` were skipped |
| `FAILED:<stage>` | 3 | free space at the run tree is below `needs.disk_gb` of the first stage |
| `FAILED:<stage>` | 4 | the tool gate timed out after `gate_max_s` |
| `FAILED:<stage>` | 5 | a command or `prepare` failed with no retry left, or the driver hit an error |
| `FAILED:<stage>` | 2 | a checkpoint on a stage without `resume` |
| `FAILED:runtime` | 5 | `[runtime] setup` failed; no stage ran, and `log/setup.log` says why |
| `OVER_BUDGET:<stage>` | 9 | a budget passed; the command ended or was killed |
| `STOPPED` | 10 | a stop file ended the run |
| `KILLED:<signal>` | 10 | a signal ended the run |

Exit 2 also means bad usage, a spec that does not load or a run tree
that is not a directory; then no heartbeat is written and the reason is
in the driver log. A terminal phase sets `exit`, and the watcher and the
board treat the run as finished.

The driver records `[runtime] setup` as a stage row named `setup`, with
its start, end, outcome and exit code. `edr status <handle>` and
`edr brief --run <handle>` list it before the first stage. When the
`when_changed` files have not changed, the row says `skipped`.

The logs are in the run tree on the host: `log/<stage>.log` for a stage,
`log/<stage>.<task>.log` for a task. `edr status <handle>` shows the
tail, and the watcher copies `log/` to the head node when a stage ends.

## Brief a session

`edr brief` prints the project as Markdown for someone who has never
seen it: the source and its checked-out trees, the stages, the hosts
with the marks of their last probe, the tool seats, the runs per batch,
every run that needs a decision with the command the triage proposes,
and the last ten events. `edr brief --run base@sweep1` tells the story
of one run, from its stage and step times and its events to the last
20 lines of its log and its metrics. Both take `--json`. A Claude Code
session reads the briefing before its first prompt when the project's
`.claude/settings.json` runs it as a `SessionStart` hook; the hook's
output becomes part of the session's context:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "cd \"$CLAUDE_PROJECT_DIR\" && edr brief"}]}
    ]
  }
}
```

## The watcher

`edr watch` runs on the head node, one process per project. It reads the
heartbeats, classifies the runs, collects the results, extracts the
metrics, starts queued jobs and sends the alerts. It never deletes a
tree, and the only files it removes are stale seat leases. As a systemd
user service:

```sh
cp edr-watch.service ~/.config/systemd/user/edr-myflow.service
systemctl --user daemon-reload
systemctl --user enable --now edr-myflow
loginctl enable-linger "$USER"     # the service survives a logout and a reboot
```

Without systemd, `tmux new -d -s edr-myflow 'edr watch'` does the same
job. In both cases a cron line tells you when the watcher stopped:

```
*/10 * * * * cd ~/myflow && edr watch --check
```

`--check` reads `<state_dir>/watch.json`, the watcher's own heartbeat. When
the file is older than three cycles, or missing, it prints why, sends an
alert and exits 1. `edr watch --once` runs one cycle and exits 1 when
the cycle failed.

One cycle runs every `heartbeat_s` seconds. The config is reloaded at
the start of each cycle, so an edit to `edr.toml` takes effect without a
restart; a config that does not load skips the cycle and keeps the
service up.

1. Read every heartbeat of every batch without `RETIRED`, and write the
   runs and their stage rows into the run database.
2. Classify each live run, write the state, add an event on a change of
   state, send an alert for the states that notify, and after `grace_s`
   take the action of the state.
3. Collect: copy `log/` and the `collect` paths of every finished stage
   and task into `data/results/<run_id>/`, plus the step directories of
   a running stage that are older than 10 minutes. Extract every metric
   whose file has arrived from the stages and tasks that ended `done`,
   and write the parameters of the run once: `config`, `build_tag`, `src`
   and the overrides.
4. Resume a `dead` run once, when its stage has `resume` and no process
   group of the run is alive on the host.
5. Find orphans: processes of the current user that match `tool_procs`
   on a host and belong to no live run tree.
6. Sweep the seat leases in `<state_dir>/leases/`. A lease older than
   2 minutes is stale when its run has no live heartbeat, is `dead`,
   `retired` or `abandoned`, has left the lease's stage, or when the
   lease is older than the stage budget. The watcher removes it and
   writes a `lease` event with the reason.
7. Launch queued jobs whose host now fits, one per batch per cycle.
8. Write the boards under `data/board/` and the pinned Telegram board;
   [results.md](results.md) lists the files.
9. Send the daily digest once a day, at the first cycle after
   `limits.digest_at`.
10. Write `<state_dir>/watch.json` with the time, the cycle count and the pid.

Its memory between cycles is three rows of the database's `store` table.
`progress` holds what each run looked like last time; `notified` the
states, the alerts sent and the grace clocks; `digest` the day and the
time of the last digest.

Under a scheduler three more states come from the scheduler, not from
the heartbeat. `pending`: the job waits in the scheduler queue and the
driver has not written a heartbeat. `held`: the scheduler will not run
the job until a person releases it, for example after a second start or
a memory limit; it alerts, and `edr stop` removes the job. `suspended`:
the scheduler stopped the job, so its heartbeat stands still; the run is
not `dead` while the scheduler reports it suspended. A job that leaves
the queue before its first heartbeat ends `FAILED:scheduler`.

The watcher sends an alert when a run enters one of the states that
[reference/states.md](reference/states.md) marks, to every channel
configured in `site.toml`: Telegram, ntfy or mail. There is one message
per run and state. On Telegram a new reason for the same state edits that
message; ntfy and mail send a new one. [notify.md](notify.md) sets up the
channels, and [guarantees.md](guarantees.md) says what the watcher does
on its own and what it never does.

## Limits: gates, retries, budgets and the disk

A stage whose `needs.tools` names a tool with a probe starts with
`gate:<stage>`. The driver runs the probe argv from the spec in the run
tree, with the spec `env`. The first number on the first line is the
free seats. Two drivers that read the same free seat would both start,
so the driver also leases the seats it takes. The seats it may use are
the free seats less the seats that other stages and tasks leased in the
last `lease_s` seconds. After `lease_s` the tool holds its seat, and the
probe no longer reports it free.

When enough seats are left, the driver writes one lease file per seat,
`<state_dir>/leases/<tool>/<run_id>.<stage>.<n>` (with `.<task>` after
the stage in a task group), by a temporary file and a rename. The file
holds the run id, the stage, the driver pid, the host, the time and the
stage budget in seconds. Then it counts again, and backs off when an
older lease of another run leaves too few seats, so of two drivers that
read the same free seat, the later one waits.

The driver waits while the seats are short. It polls every 5 s, up to
`gate_max_s`; then the stage fails with exit 4. The reason goes into the
driver log as `gate <stage>: wait for <tool>: <free> free, <held> held
by others, <needed> needed`, into the heartbeat as `gate`, and into
`edr status <handle>`. A probe that fails or prints no number counts as
unknown: the driver logs it, leases the seats and runs the stage. In a
task group the check and the lease run before each claim, with the
task's own `tools` when it has them. A short pool delays the next claim
by 5 s.

The driver removes its lease files when the stage or the task ends, on
every exit path: a failure, an exception, a stop and a signal. A driver
killed with `SIGKILL` leaves its leases, and the watcher sweeps them. The
driver knows no licence manager; the site hook does that work.

A stage with `retry` that fails is tried again when `retry.match` is
found in the last 80 lines of its log, after `retry.wait_s`, up to
`retry.max` times, with the phase `retry:<stage>:<n>`. Every attempt
appends to the same log and adds an attempt to `stages` in the
heartbeat.

| Limit | Who applies it | What happens |
|---|---|---|
| `needs.disk_gb` of the first stage | the driver at start | below it the run fails with 3 |
| `needs.disk_gb` of a task | the driver before the claim | below it the task is `skipped` and counted |
| `budget.hours`, `budget.disk_gb` of a stage | the driver while the command runs | at the limit the phase becomes `OVER_BUDGET:<stage>`; with `kill = true` the process group gets `SIGTERM`, else the command runs to its end; the run stops after it with exit 9 |
| `budget.hours` with `per = "task"`, or a task's own | the driver per task | only that task gets `SIGTERM` |
| `limits.host_free_min_gb` | the driver between stages and before each claim | below it nothing new starts and `host_full` is set; after `grace_s` the watcher stops the newest run on that host |
| `limits.streak` | the driver per group | that many equal failure signatures in a row set `looping`; the group claims nothing more |
| `limits.hung_s` | the watcher | a run with no progress for that long is `hung`; the watcher kills it only with `kill_hung` |

A task that fails gets a signature, the last log line with every digit
removed; `edr status <handle>` shows it per task. Nothing in this table
deletes a file.

## Act on a run

```sh
edr keep base@sweep1 --hours 6                    # more time for the running stage or task
edr keep base@sweep1 --ack                        # cancel the pending kill or stop of the watcher
edr stop base@sweep1 --after-task --why "superseded by sweep2"
edr stop base@sweep1 --why "wrong config"         # SIGTERM to the driver and its process groups
edr stop base@sweep1 --now --why "host full"      # then SIGKILL after 30 s
```

`keep` adds hours to the budget of the running stage or task. The driver
reads the keep file at every heartbeat; a keep file older than the start
of the current stage or task does not count. `--ack` cancels the pending
kill of `hung` and the stop of `host_full`; any keep file holds off the
`superseded` stop.

`stop` and `retire` need `--why`. The text goes into the event log
together with who acted, and `edr events --run <handle>` shows it later. Try
`--after-task` first, then a plain `stop`, then `--now`;
[guarantees.md](guarantees.md) says what each one signals. A queued run
that `edr stop` marks `stopped` never starts.

## Resume from a checkpoint

A stage with `resume` can continue from a step. `{checkpoint}` in that
command takes the step name, so a flow with `FIRST_STAGE={checkpoint}`
skips the steps before it. The watcher resumes a `dead` run this way
once, from the last `step_name` of its heartbeat. By hand:

```sh
edr continue a@sweep1 --stage pnr --from cts
```

Without `--from` the stage starts from its first step, which in many
flows deletes the checkpoints it would need. `edr status --triage`
proposes the command with `--from` filled from the heartbeat.

## More work on an existing tree

`edr continue <handle> --stage <S>` starts one stage on the tree of a run
that ended: more tasks of a task group, a stage the job skipped, or a
resume. The new run joins the batch of that run, so `retire --batch`
takes both. It has its own id and heartbeat, with the time of the call
as its date and the label `<label>.<stage>`. It keeps `{tree_id}` of the
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

A task group runs its tasks through a queue in the state directory. Two
drivers with the same queue share one pool, so a spec written by hand
with that `queue_dir` adds a shard; `edr continue` gives its run a queue of
its own. [dev/driver.md](dev/driver.md) shows the queue.

## Import a run

`edr import` records a run that edarunner did not launch. With `--host`
and `--root` it records a tree on a host:

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --src abc1234 \
    --host hostA --root /scratch/user/edr/myflow/20260830_0000_base_base_gabc1234
```

After that, `reuse` and `edr continue` can use the tree. `edr retire`
deletes it only when its path carries the safety marker.

With `--results` it links an archive of the collected files instead:

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --src abc1234 \
    --results /archive/base --tasks softmax_197 --why "tree gone, reports kept"
```

This links `/archive/base` as `data/results/<run id>` and extracts the
project's metrics from it, so `metrics` and `export` cover a result
whose tree is gone. The directory holds the collected files in the run
layout, the same paths `collect` names. An imported run carries the
import time as `started` and `ended`.

## Track a run started elsewhere

`edr track` runs one command under the driver in the foreground, on the
machine where you call it, and records it as a run of the project. Use
it when a job script, a Makefile or a lab scheduler starts the work, and
you want the board, the alerts, the metrics and `export` for it.

```sh
edr track --label base --stage pnr --collect -- make pnr CONFIG=base
```

The run gets one stage with the name `--stage`. When `edr.toml` has a
stage with that name, the run takes its `steps`, `progress`, `budget`,
`retry` and `needs.tools`, so the step names, the budget and the tool
gate work. The command replaces the stage's `cmd` as given, with no
placeholder. A name that `edr.toml` does not know gives a bare stage.

The tree is `--root`, the current directory by default. The driver
writes `log/<stage>.log` there. The run id follows `source.run_id`,
with the label as config, `track` as the build tag, and `--src` as the
source tag. `--src` defaults to the tag of the tree, and a tree outside
git needs it. The batch is `--batch`, `track` by default.

`edr track` then replaces itself with the driver. The pid stays the
same, so a signal to the job reaches the driver, and the job ends with
the exit code of the phase in the table above: 0 for `done`, 5 for a
failed command. A job script can test the code as it would test the
command.

With `--collect`, the watcher copies the collect paths of the stage and
extracts the metrics when the run ends, as for a launched run. The head
node reads the tree at the same path, so the tree must be on a
filesystem that the head node mounts. Without `--collect`, the watcher
records the run and collects nothing. `--dry-run` prints the spec and
runs nothing.

The watcher checks that the driver lives over ssh to the host of the
run. A host that ssh cannot reach keeps the run `stale`, never `dead`.

## Retire, prune and archive

`edr retire <handle> --why <text>` removes the run tree on the host with
`rm -rf`. `--prune <name>` removes only the paths that `prune.<name>`
names in the stages, such as a library or a build directory, and keeps
the tree. `--batch <B>` retires every run of a batch and writes
`RETIRED`, so the watcher skips it and the board drops it. It then
removes the checked-out tree of the batch's source under
`source.worktrees`, unless another batch that is not retired has the
same source. A clone and a dirty snapshot go with a plain delete on the
head node; a git worktree that an older edr made goes with `git worktree
remove --force`. If the tree does not pass the guard, for example
because `source.worktrees` lies outside the marker path, `retire` keeps
it, prints a `worktree kept` line and still removes the run trees. You
can then remove the tree by hand.

Every target passes the guard of [guarantees.md](guarantees.md) first.
`retire` refuses a tree whose results are not collected, a tree a live
run uses, and a tree shared with an uncollected run.
[reference/cli.md](reference/cli.md) lists every refusal and every flag.
Run `edr watch --once` before a retire, so the results are on the head
node, and `--dry-run` first, which prints every `rm -rf` target and the
checked-out tree.

At the end of a project the trees leave the hosts and the head node keeps
what an analysis, a review or a rerun needs. `data/results/<run id>/` is
that archive: the watcher fills it with `collect` while a run goes, and
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

The dry run prints the file count of every list and every `rm -rf`
target. The real run copies first and deletes nothing when a copy
failed, so a tree is gone only when its files are on the head node. The
`artifacts` table records every copied file with its class, `always` or
the list name, and `edr export` takes the small files from the same
directory. `edr continue <handle> --collect <name>` copies a list without a
retire.

A rerun starts from the archive through `restore` on `reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true, restore = "power_inputs" }
```

`edr launch` checks out the source tag of the archived run, syncs a
fresh tree to a host that fits, copies the `power_inputs` files of the
archived run into it at their old paths, and starts the stage. The new
run keeps the `tree_id` and the build tag of the archived one, so every
path in the flow resolves as before.

## A second project

One project is one directory with an `edr.toml`. A second flow, on
another repository, gets its own directory. With it come its own database
and results under `data/`, its own state directory `~/.edr/<project>`,
its own trees under `<scratch>/<user>/edr/<project>/` and its own
watcher unit. The site file is shared. Nothing of one project appears in
the tables of another, and `edr` in a directory sees that project only.
With one Telegram bot for both, set `telegram_poll = false` in one of
them; [telegram.md](telegram.md) says why.
