# Run and follow a batch

How do I start a sweep and keep an eye on it? This page writes a batch,
launches it, reads the board, keeps a watcher behind it, and acts on a
single run. It assumes a project that `edr check` accepts;
[project.md](project.md) sets one up.

## Write a batch

A batch is one file under `jobs/` with a source tag and a list of jobs:

```toml
# jobs/sweep1.toml
batch = "sweep1"
source = "3f9a2c1"

[[job]]
label = "base"
config = "base"

[[job]]
label = "base_dw0"
config = "base"
overrides = { DW = 0 }
vars = { netlist_step = 11 }
```

`source` is the tag that `edr checkout` prints, the short hash of the
commit. A job is one run: a label, an optional configuration name that
the flow understands, optional overrides that reach the command as
`KEY=VALUE` tokens through `{overrides}`, and optional `stages`, `tasks`
and `host` entries. The `vars` table holds any other value the flow
needs, and each key becomes a placeholder `{vars.<name>}` for the stage
strings, `[env]` and `collect`.

The run id is `<date>_<label>_<build_tag>_g<src>`. The build tag is the
configuration name followed by `_KEYVALUE` for each override, such as
`base_DW0`; a job without either has an empty build tag, and the run id
drops that part with its `_`. A batch name is used once. A second
launch of the same batch finds the specs and starts nothing, so a new
sweep needs a new name.

## Check out and launch

```sh
edr checkout origin/main      # prints <src> <path>
edr plan sweep1               # run id, host and root per job; writes nothing
edr plan sweep1 --show-spec   # also the env, commands and collect paths of each run
edr launch sweep1 --dry-run   # every path and command, nothing written
edr launch sweep1
```

`edr checkout` prints the source tag and the path of the pinned clone.
When the batch names a clean source that is not checked out yet, `plan`
and `launch` check it out themselves and print a `checkout <src> <path>`
line. A tree with uncommitted changes needs `edr checkout --dirty <dir>`
first, and `launch --allow-dirty`.

Read every path of the dry run before the real launch. Within a minute
of the launch, `edr status` should show each run past the `setup`
phase. A run that never writes a heartbeat has left its reason in
`<state_dir>/<batch>/<run_id>.driver.log`; [debug.md](debug.md) goes on
from there. [how-it-works.md](../how-it-works.md#launch) says what
`launch` does on the head node and on the host.

## Read the board

```sh
edr status                    # the board
edr status --live             # asks each host whether the driver exists
edr status base@sweep1        # one run: stages, steps, metrics, log tail
edr status --triage           # every run not running, with one proposed command
edr events --run base@sweep1  # the history of one run, with the reason of every action
```

`base@sweep1` is a handle. A handle names one run as `label@batch`, as a
prefix of the run id, or as `#n`, the row number on the last board that
`edr status` printed. Every command that acts on one run takes a handle.

The board shows a phase and a state for each run. The phase is the
driver's word for where the run is or how it ended; the state is the
watcher's verdict, and [reference/states.md](../reference/states.md)
lists every state with its test and the command that `--triage`
proposes. A heartbeat file keeps its last phase after the driver dies,
so use `--live` before you trust a running count.

`edr hosts` shows the load and the free resources of each host, and
`edr tools` the free licence seats.

### Phases

| Phase | Exit | Meaning |
|---|---|---|
| `setup` | | the spec is loaded, the disk check runs, then `[runtime] setup` |
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

Exit 2 also means bad usage, a spec that does not load, or a run tree
that is not a directory; then no heartbeat is written and the reason is
in the driver log. A terminal phase sets `exit`, and from then on the
watcher and the board treat the run as finished.

The driver records `[runtime] setup` as a stage row named `setup`, with
its start, end, outcome and exit code, and `edr status <handle>` lists
it before the first stage. When the `when_changed` files have not
changed, the row says `skipped`.

## Keep a watcher behind the batch

The watcher collects the results, extracts the metrics, starts queued
jobs and sends the alerts, so it should run as long as the project
does. As a systemd user service, with the unit that `edr init` wrote:

```sh
cp edr-watch.service ~/.config/systemd/user/edr-myflow.service
systemctl --user daemon-reload
systemctl --user enable --now edr-myflow
loginctl enable-linger "$USER"     # the service survives a logout and a reboot
```

Without systemd, `tmux new -d -s edr-myflow 'edr watch'` does the same
job. Either way, a cron line tells you when the watcher stopped:

```
*/10 * * * * cd ~/myflow && edr watch --check
```

`edr watch --check` reads `<state_dir>/watch.json`, the watcher's own
heartbeat. When the file is missing or older than three cycles, it
prints why, sends an alert and exits 1. `edr watch --once` runs a single
cycle and exits 1 when the cycle failed.
[how-it-works.md](../how-it-works.md#the-watchers-cycle) lists what one
cycle does, and [alerts.md](alerts.md) sets up the channels.

## Act on one run

```sh
edr keep base@sweep1 --hours 6                    # more time for the running stage or task
edr keep base@sweep1 --ack                        # cancel the pending kill or stop of the watcher
edr stop base@sweep1 --after-task --why "superseded by sweep2"
edr stop base@sweep1 --why "wrong config"         # SIGTERM to the driver and its process groups
edr stop base@sweep1 --now --why "host full"      # then SIGKILL after 30 s
```

`keep` adds hours to the budget of the running stage or task. The driver
reads the keep file at every heartbeat, and a keep file older than the
start of the current stage or task does not count. `--ack` cancels the
pending kill of a `hung` run and the stop of a `host_full` one, and any
keep file holds off the stop of a `superseded` run.

`stop` and `retire` need `--why`. The text goes into the event log with
the name of whoever acted, and `edr events --run <handle>` shows it
later. Try `--after-task` first, then a plain `stop`, then `--now`;
[how-it-works.md](../how-it-works.md#stop-and-keep) says what each one
signals.

## More work on an existing tree

`edr continue <handle> --stage <S>` starts one stage on the tree of a
run that ended: more tasks of a task group, a stage the job skipped, or
a resume. The new run joins the batch of the old one, so
`retire --batch` takes both. It has its own id and heartbeat, with the
time of the call as its date and the label `<label>.<stage>`, and it
keeps `{tree_id}` of the original run, so the flow keeps writing into the
same directory. `--tasks` names the tasks, `--parallel` the width, and
`--on` another host when the tree is reachable there.

A job in a batch file does the same through `reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true }
```

A task group takes its tasks from a queue in the state directory. Two
drivers with the same queue share one pool, so a spec written by hand
with that `queue_dir` adds a shard, while `edr continue` gives its run a
queue of its own. [dev/driver.md](../dev/driver.md#the-task-queue) shows
the queue.

## Import a run

`edr import` records a run that edarunner did not launch. With `--host`
and `--root` it records a tree on a host, which `reuse` and
`edr continue` can then use:

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --src abc1234 \
    --host hostA --root /scratch/user/edr/myflow/20260830_0000_base_base_gabc1234
```

With `--results` it links an archive of collected files instead, so
`metrics` and `export` cover a result whose tree is gone:

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --src abc1234 \
    --results /archive/base --tasks softmax_197 --why "tree gone, reports kept"
```

This links `/archive/base` as `data/results/<run id>` and extracts the
project's metrics from it. The directory holds the collected files in
the layout of the run tree, the same paths that `collect` names.
`--config` is optional and defaults to empty. An imported run goes into
the batch `imported` unless `--batch` names another, and it carries the
import time as `started` and `ended`. `edr retire` deletes an imported tree only
when its path carries the safety marker.

## Track a run started elsewhere

`edr track` runs one command under the driver in the foreground, on the
machine where you call it, and records it as a run of the project. Use
it when a job script, a Makefile or a lab scheduler starts the work and
you want the board, the alerts, the metrics and `export` for it:

```sh
edr track --label base --stage pnr --collect -- make pnr CONFIG=base
```

The run gets one stage named by `--stage`. When `edr.toml` has a stage
of that name, the run takes its `steps`, `progress`, `budget`, `retry`
and `needs.tools`; the command replaces the stage's `cmd` as given, with
no placeholder. The tree is `--root`, the current directory by default,
and the driver writes `log/<stage>.log` there. `--src` defaults to the
source tag of the tree, and a tree outside git needs it. The batch is
`--batch`, `track` by default.

`edr track` then replaces itself with the driver. The pid stays the
same, so a signal to the job reaches the driver, and the job ends with
the exit code of its phase in the table above. With `--collect`, the
watcher copies the stage's collect paths and extracts the metrics when
the run ends; the head node reads the tree at the same path, so the tree
must be on a filesystem the head node mounts. The watcher checks the
driver over ssh to the run's host, and a host it cannot reach keeps the
run `stale`, never `dead`.
