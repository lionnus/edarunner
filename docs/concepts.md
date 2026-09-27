# Concepts

This page defines the terms that the rest of the documentation, the board
and the reference use.

![Where each part runs: the head node, the shared filesystem, the compute hosts and the phone](diagrams/where-it-runs.svg)

## Project

A project is one directory with an `edr.toml`. The file names the flow's
git repository, the stages, the metrics and the limits. Next to it live
`tasks.toml`, one `jobs/<batch>.toml` per batch, and `hooks/` for the
functions and probes a key names. `data/` under the project holds the
database, the collected results and the boards; git ignores it.

The site file, `site.toml`, holds the hosts, the tools and the bot. It
lives outside the project and outside git, because it names your
machines. One site file serves every project of a user, and every project
has its own database, state directory and watcher. `edr` works from any
directory below `edr.toml` and sees that project only.

edarunner itself knows no EDA tool. Every command, report file and number
comes from the project file.

## Stage, step and task

A stage is one command of the flow, declared as `[stages.<name>]`. Stages
run in file order. A job runs every stage, or the subset its `stages`
list names.

A step is a point inside a stage that the flow passes, such as
`floorplan` or `cts` inside one tool session. `edr` follows the steps
through a `progress` command, extracts a metric per step, and resumes a
stage from a step through `{checkpoint}`.

A task group is a stage with `foreach = "tasks"`. Its command runs once
per task of the job, `parallel` at a time, each in its own directory with
its own log and budget. A task is one table of `tasks.toml`, and its
keys are placeholders in the command.

## Batch, job and run

A batch is one file `jobs/<batch>.toml`: a source tag and a list of
jobs. A batch name is used once.

A job is one `[[job]]` of a batch: a label, a configuration name the
flow understands, and optional overrides, stages and tasks.

A run is one job started on one host. Its id is
`<date>_<label>_<build_tag>_g<src>`; the date is pinned once per batch,
so a plan and a launch minutes apart name the same run. A handle names
one run in a command: `label@batch`, a run id prefix, or `#n` from the
last board.

A run has a phase and a state. The phase is the driver's word for where
the run is or how it ended, such as `stage:pnr` or `done`. The state is
the watcher's verdict, such as `running`, `dead` or `hung`.
[run.md](run.md) lists the phases, and
[reference/states.md](reference/states.md) every state.

## Source and checkout

`edr checkout <ref>` pins one commit of the flow's repository as a
detached worktree and prints its short hash, the source tag. A batch
names that tag as `source`. Every run of the batch works on a copy of
that tree, so a later commit never changes a running flow. A tree
with uncommitted changes gets the tag `<hash>-dirty-<8 hex>`.

The commands that select results (`edr metrics`, `edr export` and the
bot's `/csv`) call this tag the design, as in `--design 3f9a2c1`. It is
the version of your source, not the name of a chip.

## Host, head node and driver

The head node is the machine that runs `edr` and the watcher. A compute
host is a machine in `[hosts]` of the site file that runs the flow;
`local` is the head node itself. Nothing is installed on a host. With a
`[scheduler]` table in the site file, an HTCondor, Slurm or LSF cluster
picks the host instead, and the driver runs as the job.

The driver is a single file, `edr_driver.py`, that `edr launch` starts on
the host for each run. It runs the stages in order in the run tree,
`<scratch>/<user>/edr/<project>/<run_id>/`, a copy of the checked-out
tree. It writes a heartbeat file every `heartbeat_s` seconds, 60 by
default.

The state directory, `~/.edr/<project>` by default, is on a filesystem
every host mounts. The hosts read the driver copy and the run spec there
and write the heartbeat; the head node reads the heartbeat.

## Database

The run database is one SQLite file, `data/edr.db`, on the head node. It
records every run, stage attempt, metric and collected file, and every
action together with its reason. Only the head node opens it.
[results.md](results.md) lists the tables.

## Watcher

`edr watch` is one process per project on the head node. Every cycle it
reads the heartbeats, classifies each run, collects the results,
extracts the metrics, starts queued jobs, writes the boards and sends the
alerts. It never deletes a run tree; the only files it removes are
expired licence-seat leases ([guarantees.md](guarantees.md)). The
Telegram bot runs as a thread inside the watcher.

## Snapshot

A snapshot is what `edr export` writes: the runs, the metrics and the
collected files of one design, frozen in one directory with a manifest.
Any analysis reads a snapshot, never the live database.
