# Set up a project

This page describes your flow to edarunner. By the end you have an
`edr.toml` with stages and metrics that `edr check` accepts. The hosts
come from the site file, which [site.md](site.md) sets up.

![The files of a project: what you write, what edr writes, and what lives outside](../diagrams/project-files.svg)

## Where the configuration lives

A project is a directory with an `edr.toml` in it. `tasks.toml`, one
`jobs/<batch>.toml` per batch and `hooks/` for any Python or shell
helpers sit next to it, and `edr` writes the database, the collected
results and the boards under `data/`. `edr` works from any directory
below `edr.toml` and sees that project only. From any other directory,
`edr -P <name>` or the variable `EDR_PROJECT` picks a project by its
name. The names come from the registry, one link per project under
`~/.edr/projects/`, which `launch`, `continue`, `track`, `import` and
`watch` write; `edr register` writes the link by hand.

Put the project on the head node, inside the work it belongs to, and
run `edr` there. A paper project, for example, keeps `edr/` at its root,
next to the checkouts of the flow, the result data and the paper.
`source.repo` in `edr.toml` names the flow's repository, and every path
in `edr.toml` is relative to the file.

Set `project` in `edr.toml` to a name that says what the project is.
`edr init` takes the name of the directory, which may be only `edr`, and
the name appears in the state directory, the run trees and every alert.
Two directories cannot share a name, since they would share the state
directory and the run trees: a command that would register the second
one refuses and names the first, and `edr check` reports it.

Whether the directory goes into git is your choice. To version it,
commit `edr.toml`, `tasks.toml`, `jobs/`, `hooks/` and
`edr-watch.service`, and keep `data/` and the checked-out clones out.
The site file names your machines and is shared by every project of the
lab, so it lives apart from the project; [site.md](site.md) says where.

## Create the project

```sh
mkdir -p mypaper/edr && cd mypaper/edr
edr init --site ~/.config/edarunner
```

`edr init` writes `edr.toml`, a template with one stage and one metric,
and `edr-watch.service`, the systemd unit for the watcher. `--site` is
the site file or the directory that holds it. `edr init` does not write
the site file itself.

Then edit `edr.toml`. Name the repository under `[source]`, add one
`[stages.<name>]` table for each command of the flow, and add a
`[metrics.<name>]` table for each number you want in the database.
[reference/configuration.md](../reference/configuration.md) lists every
key.

## Stages, steps and tasks

A stage is one command of the flow. The stages run in the order of the
file, and a job runs all of them or the subset that its `stages` list
names. Cut a stage where the tool session ends, or where you want a
budget, a retry rule or a stop of your own.

A step is a point inside a stage that the flow passes, such as
`floorplan` or `cts` inside one tool session. The `progress` command
prints the current step number, and `steps` names the steps. With steps,
`edr` shows the step on the board, extracts a metric per step, and can
resume a stage from a step through `{checkpoint}` in its `resume`
command.

A stage with `foreach = "tasks"` is a task group. Its command runs once
per task of the job, `parallel` at a time, each in its own directory with
its own log and budget. A task is one table of `tasks.toml`, and its keys
become placeholders such as `{task.kernel}`:

```toml
# tasks.toml
[tasks.k_small]
kernel = "gemm"
test = "GEMM_M64_N64"
args = "M=64 N=64"
```

A flow usually takes one of the two shapes below. Their names and paths
are only examples.

### One tool session, many steps inside

A commercial place-and-route tool runs synthesis, placement, clock tree
synthesis and routing in one session, because a new session for each
would cost a licence checkout and a library reload. They stay in one
stage, and `edr` follows each of them as a step through the numbered
report directories:

```toml
[env]
PATH = "{root}/.venv/bin:$PATH"          # the flow calls `python` from the venv of its tree

[stages.pnr]
cmd = "make -C flow pnr RUN={tree_id} CONFIG={config} MAX_CORES={cores} {overrides}"
resume = "make -C flow pnr RUN={tree_id} CONFIG={config} MAX_CORES={cores} FIRST_STAGE={checkpoint} {overrides}"
steps = ["setup", "analyze", "elaborate", "constraints", "synth-map", "floorplan", "pg",
         "synth-logic-opto", "synth-init-opto", "synth-final-opto", "cts", "route", "route-opt"]
progress = "ls flow/runs/{tree_id}/reports 2>/dev/null | grep -cE '^[0-9]+$'"
needs = { cores = 16, disk_gb = 70, tools = ["pnr"] }
budget = { hours = 14, disk_gb = 150 }
retry = { match = "licen[cs]e", wait_s = 900, max = 3 }
collect = ["flow/runs/{tree_id}/reports/"]
collect_on_request = { netlist = ["flow/runs/{tree_id}/out/{vars.netlist_step}/"] }
prune = { lib = ["flow/runs/{tree_id}/out/library"] }

[stages.power]
foreach = "tasks"
parallel = 1
task_dir = "sim/tests/{build_tag}/{task.test}"
cmd = "env RUN_DIR={root}/flow/runs/{tree_id} NETLIST={root}/flow/runs/{tree_id}/out/{vars.netlist_step}/top.v flow/power/run.sh {task.kernel} CONFIG={config} {task.args}"
after_each = "rm -f {task_dir}/wave.vcd"
needs = { cores = 4, disk_gb = 60, tools = { sim = 1 } }
budget = { hours = 8, disk_gb = 400, per = "task" }
collect = ["{task_dir}/power/"]
```

A job that lists `stages = ["power"]` with `reuse` runs the kernels on
the netlist of an earlier run, and sets `vars = { netlist_step = 11 }` to
name the step directory that holds it. Without that value,
`{vars.netlist_step}` has nothing to fill in and the plan stops.
`{tree_id}` names the tree the flow writes in: a run that reuses a tree
has its own `{run_id}`, but the flow's run directory keeps the id of the
tree.

### One command per step

An open flow with one script or `make` target per step, and a checkpoint
between steps, maps each step to its own stage. This is the shape of
`examples/openroad-gcd/`, which CI runs:

```toml
[stages.synth]
cmd = ". $ORFS/env.sh && make -C $FLOW_HOME DESIGN_CONFIG={root}/config.mk WORK_HOME={root} {overrides} synth"
needs = { cores = 1, disk_gb = 0.5 }
budget = { hours = 0.5 }
collect = ["reports/", "logs/"]

[stages.floorplan]
cmd = ". $ORFS/env.sh && make -C $FLOW_HOME DESIGN_CONFIG={root}/config.mk WORK_HOME={root} {overrides} floorplan"
budget = { hours = 0.5 }
collect = ["reports/", "logs/"]
```

## Metrics

A metric names a file in the run tree and a way to read one number out
of it. The watcher reads it once the file has been collected from a
stage that ended `done`:

```toml
[metrics.area_synth_um2]
stage = "synth"
file = "reports/nangate45/gcd/base/synth_stat.txt"
regex = 'Chip area for (?:top )?module \S+:\s+([0-9.]+)'
unit = "um2"
canonical = "design__instance__area"

[metrics.wns_place_ns]
stage = "place"
file = "logs/nangate45/gcd/base/3_5_place_dp.json"
json = "detailedplace__timing__setup__ws"
unit = "ns"
canonical = "timing__setup__ws"
```

`step = "*"` with `{step}` in the file reads one value per step. Besides
`regex` and `json`, a metric can read a CSV row, a hierarchical area
report or call a Python hook; [results.md](results.md) shows each of
them and the canonical names.

Only the files that a stage's `collect` list names reach the head node,
so every metric file must lie under a `collect` path. A large file that
you need only now and then goes under `collect_on_request` instead;
[cleanup.md](cleanup.md#keep-the-large-files) fetches it.

## The environment

The site file and the project file each have an `[env]` table. A command
on the host sees both, and the project wins where both set a variable.
When a project value names a variable the site sets, as `$NAME` or
`${NAME}`, edr puts the site's value in its place first, so the project
value builds on the site's:

```toml
# site.toml
[env]
PATH = "/opt/tools/bin:$PATH"

# edr.toml
[env]
PATH = "{root}/.venv/bin:$PATH"
```

The run gets `PATH = "{root}/.venv/bin:/opt/tools/bin:$PATH"`, and the
host expands the last `$PATH` to its own path. A project value without
such a reference, such as `LM_LICENSE_FILE = "2020@lic"`, replaces the
site's value.

Beyond these two tables, a stage command gets very little. The driver
runs it as `/bin/bash -c "<cmd>"`: a non-login shell without a terminal,
with standard input from `/dev/null`. Bash reads neither
`~/.bash_profile` nor `~/.bashrc` for it, so a `PATH`, a module or an
alias that your login sets up is missing. The command inherits the
environment of the driver, which on a site host is what ssh gives a
command without a terminal, with no `TERM` variable. On the host `local`
it is the environment of the `edr` process that started the driver, so a
flow can work under `local` from your terminal and still fail on a site
host. On top of that it gets the site `[env]`, the project `[env]`, and three
variables: `EDR_SOURCE`, the source tag of the batch, `EDR_RUN_ID`, the run
id, and `EDR_TREE_ID`, the id of the tree the run writes in.

A tool that calls `tput`, or a script that runs `clear`, fails without
`TERM`. Set it in the project:

```toml
[env]
TERM = "xterm"
```

`edr plan --show-spec` prints the environment and the commands of each
run as the driver will get them.

## The runtime step

The host gets a git clone of the pinned commit with its `.git`, so the
flow can ask git for its version there: `git describe --always --dirty`
gives the commit, and `git -C <nested> describe --always --dirty` the
commit of a nested repository. The head node's virtual environment does
not travel, because it points at an interpreter that may not exist on
the host. `[sync] exclude` lists the paths that stay behind. Do not list
`.git` there, or the flow on the host cannot ask git.

`[runtime] setup` is one command that the driver runs in the root of the
run tree after the copy and before the first stage, with the environment
of the stages. Its output goes to `log/setup.log`, and a failure ends the
run as `FAILED:runtime` before any stage takes a licence seat. For a
flow with a `uv.lock`:

```toml
[runtime]
setup = "uv sync --frozen"
when_changed = ["uv.lock"]

[env]
UV_CACHE_DIR = "{mount}/{user}/uv-cache"
UV_PYTHON_INSTALL_DIR = "{mount}/{user}/uv-python"
PATH = "{root}/.venv/bin:$PATH"

[sync]
exclude = [".venv"]
```

`uv sync --frozen` builds `.venv` in the run tree from the lock file
without resolving the dependencies again. The cache and the Python
installs lie on the scratch disk of the host, on the same filesystem as
the run tree, so uv links the packages from its cache instead of copying
them, and your home directory stays small. `uv` itself must be on the
host's `PATH`; the site `[env]` is the place to add its directory.

`when_changed` names files relative to the tree root. On a tree that
already ran the same `setup`, such as a run of `edr continue`, the
command runs again only when one of these files changed; without
`when_changed` it runs at the start of every run. Any other command
works the same way, such as `make deps` or
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Check the project

```sh
edr check
```

`check` loads every file, imports the hooks, probes every host for its
cores, load, RAM and scratch, names every tool the head node lacks, and
plans every batch under `jobs/`. For each fault it prints a `problem:`
line and exits 1; otherwise it prints `ok:` with the number of hosts,
stages, metrics and batches. It does not run `[runtime] setup` and does
not look for git or uv on the hosts, so a tool missing there shows at
the first launch as `FAILED:runtime`, with the reason in `log/setup.log`.

[run.md](run.md) writes the first batch and launches it.

## A second project

A second flow gets its own directory with its own `edr.toml`. It has its
own database and results under `data/`, its own state directory
`~/.edr/<project>`, its own run trees and its own watcher. The site file
is shared. Nothing of one project appears in the tables of another.

The machines are shared too, so some things count over every registered
project. `max_per_host` counts your runs on a host, whatever project they
belong to; the seat leases of all projects share one directory; and one
watcher, the one that holds `~/.edr/serve.lock`, checks the orphans,
stops the newest run of a full host and sweeps the leases for all of
them. `edr hosts` shows your runs on each host by project,
`edr status --all` the board of every project, and `edr projects` the
projects with their watchers. With
one Telegram bot for both, set `telegram_poll = false` in one of them;
[alerts.md](alerts.md#one-chat-or-one-per-project) explains why.
