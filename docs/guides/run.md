# Run and follow a batch

This page shows how to start a sweep and follow it: you write a batch,
launch it, read the board, keep a watcher running, and act on a single
run. It assumes a project that `edr check` accepts, which
[project.md](project.md) sets up.

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

The run id is `<date>_<label>_<build_tag>_g<source>`. The build tag is the
configuration name followed by `_KEYVALUE` for each override, such as
`base_DW0`; a job without either has an empty build tag, and the run id
drops that part with its `_`. A batch name is used once. A second
launch of the same batch finds the specs and starts nothing, so a new
sweep needs a new name.

## Check out and launch

```sh
edr checkout origin/main      # prints <source> <path>
edr plan sweep1               # run id, host and root per job; writes nothing
edr plan sweep1 --show-spec   # also the env, commands and collect paths of each run
edr launch sweep1 --dry-run   # every path and command, nothing written
edr launch sweep1
```

`edr checkout` prints the source tag and the path of the pinned clone.
When the batch names a clean source that is not checked out yet, `plan`
and `launch` check it out themselves and print a `checkout <source> <path>`
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

`base@sweep1` is a handle. A handle names one run as `label@batch`, as
`label@source`, as a prefix of the run id, or as `#n`, the row number on
the last board that `edr status` printed. Every command that acts on one
run takes a handle. A `label@batch` that names more than one run is
refused with the list of its runs, and `label@source` takes the newest
run of the label at that source tag that ended `done`;
[results.md](results.md#which-run-a-command-takes) gives the rule. The
board shows the source tag of each run next to its label.

The board shows a phase and a state for each run. The phase is the
driver's word for where the run is or how it ended; the state is the
watcher's verdict, and [reference/states.md](../reference/states.md)
lists every state with its test and the command that `--triage`
proposes. A heartbeat file keeps its last phase after the driver dies,
so use `--live` before you trust a running count.

`edr hosts` shows the free room of each host and your live runs on it,
the hosts where a run can start first, and `edr tools` shows the free
licence seats. `edr status --all` prints the board of every registered
project, and `edr projects` lists the projects with their watchers.

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
does. One supervisor per user, `edr serve`, keeps one watcher per
registered project; `launch` registered the project already. Run the
supervisor as a systemd user service:

```sh
edr serve --unit > ~/.config/systemd/user/edr-serve.service
systemctl --user daemon-reload
systemctl --user enable --now edr-serve
loginctl enable-linger "$USER"     # the service survives a logout and a reboot
```

The supervisor must not run from a checkout that you edit: a `git pull`
there would change the code under the running watchers.
`edr serve --unit` prints a unit that runs the `edr` of a tool install,
such as the one from `uv tool install git+https://github.com/lionnus/edarunner`.
When it runs from a checkout, an editable install, it first installs a
copy of the checkout's HEAD commit with `uv tool install` and prints a
unit that runs that copy. After an upgrade, run it again and restart
the service.

A cron line tells you when the supervisor stopped. It reads
`~/.edr/serve.json` and no project file, so it works while every
`edr.toml` is broken. cron has a short `PATH`, so name `edr` in full:

```
*/10 * * * * timeout 120 $HOME/.local/bin/edr serve --check
```

`edr serve --check` prints why, sends an alert and exits 1 when the file
is missing or older than three cycles. `edr serve --dry-run` lists the
registered projects and what the supervisor would do for each, and
`edr projects` shows who watches each project now.
[projects.md](projects.md) shows the supervisor with several projects.

One watcher runs per project: a second one, `edr watch` or
`edr watch --once`, finds `<state_dir>/watch.lock` taken, names the pid
of the first and exits 2. Without systemd, `tmux new -d -s edr 'edr serve'`
does the same job. `edr watch --once` runs a single cycle of one project
and exits 1 when the cycle failed.
[how-it-works.md](../how-it-works.md#the-watchers-cycle) lists what one
cycle does, and [alerts.md](alerts.md) sets up the channels.

## Act on one run

```sh
edr keep base@sweep1 --hours 6                    # 6 more hours, and no automatic stop or kill for as long
edr stop base@sweep1 --after-task --why "superseded by sweep2"
edr stop base@sweep1 --why "wrong config"         # SIGTERM to the driver and its process groups
edr stop base@sweep1 --now --why "host full"      # then SIGKILL after 30 s
```

`keep` adds hours to the budget of the running stage or task. The driver
reads the keep file at every heartbeat, and a keep file older than the
start of the current stage or task does not count. For as many hours
from the time you write it, the watcher neither kills the run as `hung`
nor stops it as `superseded`. A keep never holds off the stop of a
`host_full` run, since a full disk blocks every other user of the host;
free scratch there instead, as [cleanup.md](cleanup.md#free-a-full-host)
shows.

`stop` and `retire` need `--why`. The text goes into the event log with
the name of whoever acted, and `edr events --run <handle>` shows it
later. Try `--after-task` first, then a plain `stop`, then `--now`;
[how-it-works.md](../how-it-works.md#stop-and-keep) says what each one
signals.

A stage that passes `budget.hours` without `kill = true` runs to its
end, and then the run ends `OVER_BUDGET:<stage>` without the stages
after it. A keep written while the stage runs clears that mark, and the
run goes on as usual. In the same way, `stop --after-task` during a
one-command stage ends the run `STOPPED` once that stage is over. Either
way the stages after it are left, and `edr continue <handle>` runs them.

## More work on an existing tree

`edr continue <handle>` runs the stages that the tree of a run has left,
as one new run on the same tree:

```sh
edr continue base@sweep1 --dry-run   # the stages left, the run id, the host and the root
edr continue base@sweep1
```

Every run is launched with the stages of its job. The tree of a run
holds every run with the same root, including the runs that `continue`
started, and the stages left are those after the last one that ended
with exit 0, in the order of `edr.toml`. The newest record of a stage
counts (`launch.stages_left`). Say the pnr stage of `base` went
over its budget: the call above runs export and power. If that new run
goes over its budget in export, `edr continue base@sweep1` runs power
alone.

`continue` refuses to guess in two cases. While a run on the tree has
not ended, such as a continue that is still running, a second call
starts nothing. And when the first stage left started but did not end
with exit 0, because it failed, was killed or was stopped halfway, a new
run would start that stage from its first step. A flow that sets up its
library in that first step deletes the checkpoints that a resume needs,
so name the stages yourself, with a checkpoint:

```sh
edr continue base@sweep1 --stage pnr export power --from cts
```

`--stage` takes one or more stages and runs them in the order given, and
`--from` fills `{checkpoint}` in the resume command of the first one.
When no stage is left, `continue` says so and exits 2. A task group that
stopped taking tasks, after a stop or at its budget, still ends with
exit 0, so `continue` counts it as done; run its other tasks with
`--stage <group> --tasks <id>...`.

The new run joins the batch of the old one, so `retire --batch` takes
both. It has its own id and heartbeat, with the time of the call as its
date, and it keeps `{tree_id}` of the original run, so the flow keeps
writing into the same directory. Its label is `<label>.<stage>` for one
stage, such as `base.power`, and `<label>.<first>-<last>` for several,
such as `base.export-power`; when the batch has that label already, the
next one is `base.export-power.2`. The new run takes the job of the run
it continues, with its tasks, vars and overrides, also when `continue`
made that run. `--tasks` names the tasks, `--parallel` the width, and
`--on` another host when the tree is reachable there.

On the phone, the alert of a run that ended `OVER_BUDGET` or `STOPPED`
with stages left has a Continue button that does the same;
[alerts.md](alerts.md#alerts) shows it.

A job in a batch file runs stages on the tree of an earlier run through
`reuse`:

```toml
[[job]]
label = "base"
config = "base"
stages = ["power"]
tasks = ["softmax_197"]
reuse = { label = "base", latest = true }
```

With `label`, the job takes a run of that label at the `source` of its
batch file: the newest by start time that ended `done`, else the newest
one. A run whose tree is gone is refused, never replaced by an older
tree. `run_id` names one run by its id or by any other handle.

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
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --source abc1234 \
    --host hostA --root /scratch/user/edr/myflow/20260830_0000_base_base_gabc1234
```

With `--results` it links an archive of collected files instead, so
`metrics` and `export` cover a result whose tree is gone:

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --config base --source abc1234 \
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
and the driver writes `log/<stage>.log` there. `--source` defaults to the
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
