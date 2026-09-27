# Configure a project

This page turns your own flow into a project: an `edr.toml` with stages
and metrics, a site file that lists your hosts, and a first batch that
`edr check` accepts.

![The files of a project: what you write, what edr writes, and what lives outside](diagrams/project-files.svg)

## 1. Create the project

```sh
mkdir myflow && cd myflow
edr init --site ~/.config/edarunner
```

`edr init` writes `edr.toml`, a template with one stage and one metric,
and `edr-watch.service`, the systemd unit for the watcher. `--site` is
the directory that holds your private site file, or the file itself.
`edr init` does not write the site file; the template just points at
`~/.config/edarunner/site.toml`.

Put `data/` in the project's `.gitignore`. Commit `edr.toml`,
`tasks.toml`, `jobs/`, `hooks/` and `edr-watch.service`. Keep out of git
the database, results and boards under `data/`, the state directory, the
checked-out clones, the run trees on the hosts, `site.toml` and the Telegram
token.

## 2. Write the site file

The site file holds the hosts, the tools and the bot. It stays outside
the project and outside git, because it names your machines.

```toml
schema = 1
scratch = ["/scratch"]

[hosts.hostA]
cores = 64
ram_gb = 256

[hosts.hostB]
cores = 128
ram_gb = 512
```

Add `[tools.<name>]` when not every host has a tool, or when a stage has
to wait for seats. Add `[telegram]`, `[ntfy]` or `[mail]` for alerts;
[notify.md](notify.md) sets them up.
`examples/site/` shows both with placeholder names. edarunner knows no
licence manager itself; a site hook such as `examples/site/hooks/flexlm_free.sh`
turns the output of a licence tool into the `free total` line a probe
prints. [reference/configuration.md](reference/configuration.md) lists
every key.

## 3. Declare the flow

Edit `edr.toml`: the repository under `[source]`, one `[stages.<name>]`
per command of the flow, and the `[metrics.<name>]` you want in the
database. Stages run in file order.

![How a flow plugs in: the core, the project file and the private site file](diagrams/site-layer.svg)

Cut a stage where the tool session ends, or where you want a budget, a
retry rule or a stop of your own. Everything inside a stage is a step,
which `edr` follows through `progress`. `edr` can extract metrics per
step and resume a stage from a step through `{checkpoint}`. A stage with `foreach = "tasks"`
fans out into tasks that run in parallel on the host and share one queue
across runs. The two configs below show the two shapes a flow takes. The
names and paths are examples.

### One tool session, many steps inside

A commercial place-and-route tool runs synthesis, placement, clock tree
synthesis and routing in one session, because a new session for each would
cost a licence checkout and a library reload. So they stay in one edr
stage, and `edr` tracks each of them as a step through the numbered report
directories.

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

[metrics.area_cell_um2]
stage = ["pnr"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
regex = '^i_top\s+(\S+)'
unit = "um2"
canonical = "design__instance__area"

[metrics.power_w]
stage = "power"
file = "{task_dir}/power/reports/power.csv"
csv = { where = { phase = "WHOLE", depth = "0" }, column = "total_w" }
unit = "W"
canonical = "power__total"

[metrics.window_ns]
stage = "power"
file = "{task_dir}/power/phases.json"
json = "window_ns"
unit = "ns"

[metrics.energy_nj]
stage = "power"
file = "{task_dir}/power/phases.json"
python = "hooks/energy.py:energy_nj"   # power_w from reports/power.csv times window_ns from phases.json
unit = "nJ"
canonical = "energy"
```

The stages run in file order, `pnr` then `power`. A job that lists
`stages = ["power"]` with `reuse` runs the kernels on an existing
netlist. The job sets `vars = { netlist_step = 11 }` to name the step
directory that holds it; without it, `{vars.netlist_step}` has no value.

Three details matter in this shape:

- `{tree_id}` names the tree the flow writes in. A run that reuses a tree
  has its own `{run_id}`, but the flow's run directory must keep the
  tree's id.
- The flow needs its own `PATH`. The project `[env]` table carries it on
  top of the site's `PATH`; [the environment](#the-environment) below
  shows how the two combine.
- A number the flow does not print comes from a `python` hook that reads
  the files the flow does print, such as the energy from a power and a
  window. [results.md](results.md) shows the hook.

### One command per step

An open flow with one script or `make` target per step, and a checkpoint
between steps, maps each step to its own stage. This is the shape of
`examples/openroad-gcd/`, which CI runs: the GCD design of
OpenROAD-flow-scripts (ORFS) on nangate45, with one `make` target for each
of `synth`, `floorplan` and `place`.

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

[stages.place]
cmd = ". $ORFS/env.sh && make -C $FLOW_HOME DESIGN_CONFIG={root}/config.mk WORK_HOME={root} {overrides} place"
budget = { hours = 0.5 }
collect = ["reports/", "logs/"]

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

The synthesis area comes from the Yosys report, and the numbers of the
later stages come from the METRICS2.1 JSON that ORFS writes at each step.

If your source keeps a file that git ignores, such as a PDK directory,
`edr checkout --dirty <tree>` snapshots the working tree instead of cloning
a commit. The run id then carries `-dirty-<hash of the diff>`.

### The environment

The site file and the project file each have an `[env]` table. A command
on the host sees both, and the project wins where both set a variable.
When a project value names a variable the site sets, as `$NAME` or
`${NAME}`, edr puts the site's value in its place first. The project
value then builds on the site's instead of replacing it:

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

### The environment of a stage command

The driver runs each stage command as `/bin/bash -c "<cmd>"`: a
non-login shell with no terminal. Standard input is `/dev/null`, and the
output goes to the stage log. Bash reads neither `~/.bash_profile` nor
`~/.bashrc` for it, so a `PATH`, a module or an alias that your login
sets up is not there.

The command inherits the environment of the driver. On a site host that
is what ssh gives a command without a terminal, and it has no `TERM`
variable. On the host `local` it is the environment of the `edr` process
that started the driver, so a flow can work under `local` from your
terminal and still fail on a site host. On top of it come the site
`[env]`, then the project `[env]` as the section above describes, then
`EDR_SRC`, `EDR_RUN_ID` and `EDR_TREE_ID`. Nothing else is added.

A tool that calls `tput`, or a script that runs `clear`, fails without
`TERM`. Set it in the project `[env]`:

```toml
[env]
TERM = "xterm"
```

`edr plan --show-spec` prints the environment and the commands of each
run as the driver will get them, and runs nothing. `edr launch --dry-run
--show-spec` does the same for a launch.

### Source and runtime on the host

#### What the host gets

`edr checkout` makes a local clone of the pinned
commit under `source.worktrees`, and clones each `source.nested`
repository inside it. `edr launch` copies that clone to the host with
`rsync --delete`, `.git` included. The host gets a normal git checkout
of the pinned commit, and every git command works there as it does on
your desk. On the head node a local clone shares its objects with your
repository through hard links, so it takes little space. The host gets a
full copy of `.git`; `du -sh .git` in your repository tells you its size.

`[sync] exclude` lists the paths that stay on the head node, such as a
virtual environment or a build directory:

```toml
[sync]
exclude = [".venv", "build"]
```

If you list `.git` there, the host copy has no history and the flow
cannot ask git. A source that an older edr checked out as a git
worktree has a `.git` file that points at the head node, and git on the
host fails on it. Remove that tree with `git worktree remove` and run
`edr checkout` again to get a clone.

#### How the flow finds its version

The flow asks git, as it would
anywhere else: `git describe --always --dirty` gives the commit, and
`git -C <nested> describe --always --dirty` gives the commit of a nested
repository. A dirty snapshot is a clone too, with your uncommitted
changes copied over it, so `--dirty` reports them. Every command on the
host also gets three variables: `EDR_SRC` is the source tag of the
batch, `EDR_RUN_ID` the run id, and `EDR_TREE_ID` the id of the tree the
run writes in (`{tree_id}`).

#### How the Python environment is made

`[runtime] setup` is one command
that the driver runs on the host, in the root of the run tree, after the
copy and before the first stage. It runs with the environment of the
stages, and its output goes to `log/setup.log`. If it fails, the run
ends as `FAILED:runtime` before any stage waits for a tool seat. For a
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

`uv sync --frozen` builds `.venv` in the run tree from the lock file and
does not resolve the dependencies again. The cache and the Python
installs lie on the scratch disk of the host, the same filesystem as the
run tree. uv then links the packages from its cache into `.venv` instead
of downloading or copying them again, and your home directory stays
small. The `PATH` line makes
`python` in every stage the one from `.venv`. The head node's `.venv`
stays behind because it points at an interpreter path that may not
exist on the host. `uv` itself must be on the `PATH` of the host; the
site `[env]` table is the place to add its directory.

`when_changed` names files relative to the tree root. On a tree that
already ran the same `setup`, such as a run of `edr continue`, the
command runs again only when one of these files changed. Without
`when_changed`, the command runs at the start of every run. Any other
setup command works the same way, for example `make deps` or
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

#### What edr check verifies

`check` loads the files, probes the hosts
and plans every batch. It does not run `setup` and does not look for git
or uv on the hosts. A missing tool shows at the first launch: the run
ends as `FAILED:runtime` within a minute, and `log/setup.log` in the run
tree says why.

## 4. Write a batch

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

`source` is the tag that `edr checkout` printed; [run.md](run.md) shows
the checkout. A job is one run: a label, an optional configuration name
the flow understands, optional overrides that become `KEY=VALUE` tokens in
the command, and optional `stages` and `tasks` lists. The run id is
`<date>_<label>_<build_tag>_g<src>`. A job without `config` and overrides
has an empty build tag, and the run id drops that part with its `_`.

The `vars` table holds any other value the flow needs. Each key becomes a
placeholder `{vars.<name>}` for the stage strings, `[env]` and `collect`,
so a key must be an identifier. `overrides` reach the command as tokens,
while `vars` appear only where a string names them.

## 5. Check

```sh
edr check
```

`check` loads every file, imports the hooks, and probes every host over
ssh for its cores, load, RAM and scratch. It names every tool the head
node lacks, and plans every batch under `jobs/`. For each fault it prints
a `problem:` line and exits 1. Otherwise it prints `ok:` with the number
of hosts, stages, metrics and batches, and exits 0.

## A scheduler

A lab with HTCondor, Slurm or LSF lets the scheduler pick the host. The
driver is then the job itself: the scheduler starts it, and the driver
writes the same heartbeat as on an ssh host. The site file names the
backend in `[scheduler]`:

```toml
schema = 1
scratch = []

[scheduler]
backend = "slurm"                   # "condor", "slurm" or "lsf"
submit_via = ["ssh", "submithost"]  # empty: the head node submits itself
tree_root = "/net/share/{user}"     # a filesystem every node mounts
max_jobs = 20                       # runs of the project in the scheduler at once
queue = "long"                      # the Slurm partition or the LSF queue
options = ["--account=chip"]        # raw submit lines or arguments

[tools.fc]
seats = 10
probe = ["bash", "{site_dir}/hooks/flexlm_free.sh", "27000@licence.example.com", "FEATURE-NAME"]
licence = "fc"                      # the licence name in the scheduler
```

| Backend | Submits | Asks each cycle | Stops | Handle |
|---|---|---|---|---|
| `condor` | `condor_submit -terse <run_id>.sub` | `condor_q <ids> -af ClusterId ProcId JobStatus HoldReason` | `condor_rm` | `condor:<cluster>.<proc>` |
| `slurm` | `sbatch --parsable <run_id>.sbatch` | `squeue -h -o "%i %T %r" -j <ids>`, then `sacct` for the jobs that left the queue | `scancel` | `slurm:<jobid>` |
| `lsf` | `bsub ...` | `bjobs -noheader -o "jobid stat" <ids>` | `bkill` | `lsf:<jobid>` |

The submit file and the script lie next to the spec in the state
directory, so `submit_via` must reach a host that reads the state
directory at the same path. Every scheduler command runs through
`submit_via`, and one command per cycle asks for every run at once.

`plan` probes no host under a scheduler. The run tree goes to
`<tree_root>/<run_prefix>/<run_id>`, and `launch` copies the source there
from the head node. A job with `host = "auto"` goes wherever the
scheduler puts it; a named host becomes a requirement of the job. The
host of a run is the name the driver writes into its first heartbeat.
Above `max_jobs` a job is `queued`, and the watcher submits it when a
run of the project ends.

The job asks for the most cores, `needs.ram_gb` and `needs.disk_gb` of
its stages, the sum of the stage budgets as a wall time when every stage
has one, and one scheduler licence per tool with `licence`:

| Request | HTCondor | Slurm | LSF |
|---|---|---|---|
| cores | `request_cpus = N` | `--cpus-per-task=N --nodes=1` | `-n N -R "span[hosts=1]"` |
| `needs.ram_gb` | `request_memory` in MB | `--mem` in MB | `-R "rusage[mem=NGB]" -M NGB` |
| `needs.disk_gb` | `request_disk` in KB | none; the driver checks it | `-R "rusage[tmp=NGB]"` |
| wall time | `periodic_remove` after the seconds | `--time=H:MM:00` | `-W H:MM` |
| licence | `concurrency_limits = fc:1` | `--licenses=fc:1` | `-R "rusage[fc=1]"` |
| a named host | `requirements = (Machine == "h")` | `--nodelist=h` | `-m h` |
| no restart | `periodic_hold = NumJobStarts > 1` | `--no-requeue` | `-rn` |
| `options` | lines before `queue` | `#SBATCH` lines | arguments before the driver |

The licence is a second gate. The scheduler counts it for the whole job,
from the start of the job to its end. The probe of the tool still runs
before each stage, because the licence server also serves users outside
the scheduler. `edr check` asks HTCondor (`condor_config_val -negotiator
FC_LIMIT`) or Slurm (`scontrol show lic fc`) whether the licence exists,
because a name the scheduler does not know never holds a job back. It
says so in a warning when it cannot ask, and always for LSF.

What stays in the site file under a scheduler: the `[tools]` names,
seats and probes, `env`, and the bot. `[hosts]` is optional and serves
only `edr hosts`, `retire` and `collect` over ssh. `scratch`,
`tool_procs` and `[placement]` in `edr.toml` have no effect. The
watcher looks for orphans only on ssh hosts; a scheduler ends the
processes of its own jobs.

## Next

[run.md](run.md) checks out the source, launches the batch and runs the
watcher behind it.
