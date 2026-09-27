# Declaring a flow

Two flows have run through `edr` on real hosts. They differ in shape, and
the two configs below show both patterns. The names and paths are
examples; the site names live in the private site file.

## One tool session, many steps inside

A commercial place-and-route flow runs its stages inside one tool
session. A new session per stage would cost a licence checkout and a
library reload, so the stages stay inside one command and `edr` tracks
them as steps through the numbered report directories.

```toml
[env]
PATH = "{root}/.venv/bin:$PATH"          # the flow calls `python` from the venv of its tree

[stages.pnr]
cmd = "make -C flow pnr RUN={tree_id} CONFIG={config} MAX_CORES={cores} {overrides}"
resume = "make -C flow pnr RUN={tree_id} CONFIG={config} MAX_CORES={cores} FIRST_STAGE={checkpoint} {overrides}"
steps = ["setup", "analyze", "elaborate", "constraints", "synth-map", "floorplan", "pg",
         "synth-logic-opto", "synth-init-opto", "synth-final-opto", "cts", "route", "route-opt"]
progress = "ls flow/runs/{tree_id}/reports 2>/dev/null | grep -cE '^[0-9]+$'"
needs = { cores = 16, disk_gb = 70, licence = "pnr" }
budget = { hours = 14, disk_gb = 150 }
retry = { match = "licen[cs]e", wait_s = 900, max = 3 }
collect = ["flow/runs/{tree_id}/reports/"]
collect_on_request = { netlist = ["flow/runs/{tree_id}/out/{netlist_stage}/"] }
prune = { lib = ["flow/runs/{tree_id}/out/library"] }

[stages.power]
after = { stage = "pnr", step = "route" }
foreach = "tasks"
parallel = 1
task_dir = "sim/tests/{build_tag}/{task.test}"
cmd = "env RUN_DIR={root}/flow/runs/{tree_id} NETLIST={root}/flow/runs/{tree_id}/out/{netlist_stage}/top.v flow/power/run.sh {task.kernel} CONFIG={config} {task.args}"
after_each = "rm -f {task_dir}/wave.vcd"
needs = { cores = 4, disk_gb = 60, licence = { sim = 1 } }
budget = { hours = 8, disk_gb = 400, per = "task" }
collect = ["{task_dir}/power/"]

[metrics.area_cell_um2]
stage = ["pnr"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
regex = '^i_top\s+(\S+)'
unit = "um2"
canonical = "area.cell"

[metrics.power_w]
stage = "power"
file = "{task_dir}/power/reports/power.csv"
csv = { where = { phase = "WHOLE", depth = "0" }, column = "total_w" }
unit = "W"
canonical = "power.total"

[metrics.window_fs]
stage = "power"
file = "{task_dir}/power/phases.json"
json = "window_dur"
unit = "fs"

[metrics.energy_nj]
expr = "power_w * window_fs / 1000000"
unit = "nJ"
canonical = "energy"
```

The first run taught three lessons:

- `{tree_id}` names the tree the flow writes in. A run that reuses a tree
  (to run more kernels on an existing netlist) has its own `{run_id}`, but
  the flow's run directory must keep the tree's id.
- The flow needs its own `PATH`. The project `[env]` table carries it, and
  `$PATH` expands on the host.
- A whole-window power row may carry no duration. The energy then comes
  from the window that the phase file records, through an `expr` metric.

## One command per step

An open flow with a script per step and a checkpoint between steps maps
one stage to one step. Here the flow runs inside a tool container, and
the container version is part of the command, because the flow scripts
match the tool version they were written for.

```toml
[env]
PATH = "/usr/sepp/bin:$PATH"

[stages.synth]
cwd = "yosys"
cmd = "oseda -2026.04 ./run_synthesis.sh --synth"
needs = { cores = 8, disk_gb = 5 }
budget = { hours = 2 }
collect = ["yosys/reports/", "yosys/croc.log"]

[stages.floorplan]
after = "synth"
cwd = "openroad"
cmd = "oseda -2026.04 ./run_backend.sh --floorplan"
budget = { hours = 1 }
collect = ["openroad/reports/"]

[stages.place]
after = "floorplan"
cwd = "openroad"
cmd = "oseda -2026.04 ./run_backend.sh --placement"
budget = { hours = 3 }
collect = ["openroad/reports/"]

[metrics.area_synth_um2]
stage = "synth"
file = "yosys/reports/croc_area.rpt"
regex = "Chip area for module '\\\\croc_chip':\\s+([0-9.]+)"
unit = "um2"
canonical = "area.cell"

[metrics.wns_place_ns]
stage = "place"
file = "openroad/reports/02_croc.placement.rpt"
regex = 'wns max\s+(-?[0-9.]+)'
unit = "ns"
canonical = "timing.wns"
```

The source of this flow keeps its PDK in a directory that git ignores, so
`edr stage --dirty <tree>` snapshots the working tree instead of adding a
worktree. The run id then carries `-dirty-<hash of the diff>`.

## Which shape to choose

Cut a stage where the tool session ends, or where you want a budget, a
retry rule or a stop of your own. Everything inside a stage is a step,
which `edr` tracks through `progress`, extracts metrics from per step, and
resumes through `{checkpoint}`. A stage with `foreach = "tasks"` fans out
into tasks that run in parallel on the host and share one queue across
shards.
