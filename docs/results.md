# Results and analysis

After this page you can get every number of a design out of the run
database and compare runs on the board or the phone. You can hand a
frozen snapshot to a report, a notebook or a dashboard.

## The database

The run database is one SQLite file, `data/edr.db`, on the head node.
Every run, every number and every action lands there.

| Table | One row per | Holds |
|---|---|---|
| `batches` | batch | the project, the source tag, the pinned date, when it was retired |
| `runs` | run | identity (label, config, build tag, source tag, dirty flag), host and root, phase, state, stage and step, exit, times, disk figures, task counts, `tree_id` |
| `stage_runs` | stage or task attempt of a run | status, start and end, exit, failure signature, log path |
| `parameters` | key of a run | `config`, `build_tag`, `src` and each override, as text |
| `metrics` | number | run, stage, step, task, name, canonical name, value, unit, the source file, when it was extracted |
| `artifacts` | collected file | path under `data/results/<run_id>/`, size, when, class (`always` or the `collect_on_request` name) |
| `events` | action | time, actor (`user`, `watch`, `telegram`), run, kind, text with the `--why` |
| `store` | key | one JSON value per key, a small key-value store: the watcher's `progress`, `notified` and `digest`, the `last_board` row order for `#n`, and the `telegram` message ids |
| `area` | instance of a hierarchical area report | run, stage, step, metric name, instance path, depth, area with the children, local area, cell count; the source file is the metric's |
| `step_runs` | step of a run | stage, step number, the unix time the step started, from the driver's `step_times` |
| `host_samples` | host and watcher cycle | cores, load, RAM and scratch total and in use, GPUs and busy GPUs; 30 days are kept |
| `run_samples` | heartbeat of a run | CPU percent and RSS of the run's process groups, tree size, free disk |

`data/results/<run_id>/` holds the collected files in the layout of the
run tree, so a metric's `source_file` is a path under it.

Read the tables with `sqlite3 data/edr.db` when a command does not answer
the question. Only the head node opens the file; a read command without
the file reads an empty database in memory and creates nothing.

The journal mode follows the filesystem of `data/`. On a local disk it is
WAL, so `edr status` reads while the watcher writes. On NFS, SMB, 9p or
FUSE it is DELETE with `synchronous=FULL`, because WAL needs shared memory
that a network filesystem does not give. `edr check` prints a warning line
with the path and the mode. `Database.journal_mode` holds the mode in use.

## Canonical names

The `canonical` column names a number the same way across projects. It
follows METRICS2.1, the schema that OpenROAD writes into its metrics
JSON, without the stage prefix, because the stage is its own column:

| Canonical | Number |
|---|---|
| `design__instance__area` | cell area of the design |
| `design__instance__count` | cell count |
| `design__instance__utilization` | cell area over core area |
| `timing__setup__ws`, `timing__setup__tns` | worst and total setup slack |
| `timing__hold__ws`, `timing__hold__tns` | worst and total hold slack |
| `power__total`, `power__internal__total`, `power__switching__total`, `power__leakage__total` | power and its parts |
| `runtime__total` | the wall time of a step |

A number the schema has no name for, such as an energy, keeps an empty
canonical name or a name of the project. The metric name itself stays the
project's own.

## edr metrics

```sh
edr metrics --design 3f9a2c1
edr metrics --design 3f9a2c1 --stage pnr --step 12
edr metrics --design 3f9a2c1 --csv > metrics.csv
```

One design per table. `--design` is the source tag exactly as `edr checkout`
printed it, so a run on `3f9a2c1-dirty-7b21c0d9` needs that full tag.
The text form shows label, design, stage, step, task, metric, value and
unit; `--csv` writes the columns of `metrics.csv` below. Every row
carries its source file. Read the source of a number before it goes in a
table.

## Extract again

The watcher extracts metrics as the files arrive, using the definitions
in `edr.toml` at that moment. If you add or change a metric later, runs
that have already finished keep the rows from the old definitions.
`edr extract` runs the watcher's extraction again over the files that
were collected for those runs:

```sh
edr extract base@g8 --dry-run   # counts only, writes nothing
edr extract --batch g8
edr extract --design 3f9a2c1
```

For each run it prints how many rows are new, changed, unchanged and
failed. A row counts as changed when its value, canonical name or unit
differs, or when it is missing its area rows, and `extract` replaces it.
A failed row is a file that did not parse. Rows that the new extraction
no longer finds are left in place. Every run gets an `extract` event with
the counts.

## A number the flow does not print

A metric has one of five parsers: `regex`, `csv`, `json`, `python` or `area_hier`. A
number that comes from other numbers, such as an energy from a power and
a window, needs a `python` hook. The hook gets the path of `file` and
reads the other files itself:

```toml
[metrics.energy_nj]
stage = "power"
file = "{task_dir}/power/phases.json"
python = "hooks/energy.py:energy_nj"
unit = "nJ"
```

```python
# hooks/energy.py
import csv, json
from pathlib import Path

def energy_nj(path):
    path = Path(path)
    window_ns = json.loads(path.read_text())["window_ns"]
    with open(path.parent / "reports" / "power.csv", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["phase"] == "WHOLE")
    return float(row["total_w"]) * window_ns
```

An exception in the hook gives a row with an empty value and the error
in `source_file`. `examples/local-demo/hooks/energy.py` is this hook.

`--run <handle> --over steps` prints one run along its steps, one column
per metric. With `--metric`, the one metric, its change from the step
before and the source file of each value:

```
$ edr metrics --run base@g8 --over steps --metric wns_ns
stage   step  name              wns_ns       Δ  source
pnr        8  synth-init-opto    0.001       -  flow/runs/<run>/reports/8/qor.rpt
pnr        9  synth-final-opto   0.001       0  flow/runs/<run>/reports/9/qor.rpt
pnr       10  cts                    0  -0.001  flow/runs/<run>/reports/10/qor.rpt
pnr       11  route             -0.056  -0.056  flow/runs/<run>/reports/11/qor.rpt
pnr       12  route-opt          0.003   0.059  flow/runs/<run>/reports/12/qor.rpt
```

## Hierarchical area

A metric with `area_hier = <depth>` reads a hierarchical area report,
Synopsys `report_area -hierarchy` or the OpenROAD area by hierarchy. Its
value is the top area. Each instance down to that depth becomes a row of
the `area` table: the top is `<top>` at depth 0, and a child is its path
from the top. Depth 3 or 4 keeps the blocks a paper compares; the leaf
levels of a large design add millions of rows.

```toml
[metrics.area_hier_um2]
stage = ["pnr"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
area_hier = 4
unit = "um2"
canonical = "design__instance__area"
```

`edr metrics --design <src> --instance i_top/i_streamer --depth 3` prints
the area rows of that subtree. `edr compare A B --area --depth 2` puts the
blocks of two or more runs side by side, at the last step with an area
report that every run has:

```
$ edr compare base@g8 lanes16@g7 lanes4@g7 --area --depth 2 --instance i_top
area um2 at depth 2
instance            base  lanes16   lanes4  Δ lanes16     Δ %  Δ lanes4     Δ %
i_top/i_engine   96580.1  96092.1  95942.9     -488.1   -0.5%    -637.3   -0.7%
i_top/i_accum    36583.7  36169.4  36001.1     -414.3   -1.1%    -582.6   -1.6%
i_top/i_lanes    32453.8  19234.8   9372.4   -13219.0  -40.7%  -23081.4  -71.1%
i_top/i_stream   14592.5  14738.5  14395.8      146.0   +1.0%    -196.7   -1.3%
<top>           208030.0 193918.6 182995.8   -14111.4   -6.8%  -25034.2  -12.0%
base: pnr step 12, flow/runs/<run>/reports/12/area_hier.rpt
...
```

## Compare runs per step

`edr compare A B` without `--area` prints one row per stage, step, task
and metric, the value of each run, and the delta and the percent of each
run to the first. A percent across a sign change, such as a slack from
+1 ps to -1 ps, is left out. `--metric` (repeatable), `--stage` and
`--step` narrow the rows; `--json` keeps the source file of every value.

```
$ edr compare base@g8 lanes16@g7 --stage pnr --metric wns_ns --metric cells
stage  step  name       task  metric    base  lanes16  Δ lanes16     Δ %
pnr      11  route            cells   535811   489276     -46535   -8.7%
pnr      11  route            wns_ns  -0.056   -0.038      0.018  +32.1%
pnr      12  route-opt        cells   536547   489861     -46686   -8.7%
pnr      12  route-opt        wns_ns   0.003   -0.001     -0.004       -
```

## Runtime

`edr runtime <handle>` prints where the time of a run went: a row per
stage attempt from `stage_runs`, a row per step under it, and one row per
task group with the task count, the summed task time and the longest
task. A step starts when the driver first sees its number, or at the time
that the stage's `step_log` finds in a collected file:

```toml
[stages.pnr]
step_log = { file = "log/pnr.log", regex = 'STARTING STAGE .*\((\d+) sec\)' }
```

Group 1 is a unix time. Group 2, when present, is the step number;
without it the lines count from the stage's first step, so a stage that
resumed from a checkpoint needs group 2. The source column names the
table, or the file and line of each time:

```
$ edr runtime base@g8
stage  step  what              started            wall  source
pnr       7  synth-logic-opto  2026-09-08 22:58    51m  log/pnr.log:32269
pnr       8  synth-init-opto   2026-09-08 23:50  2h10m  log/pnr.log:74936
pnr      10  cts               2026-09-09 03:30  2h11m  log/pnr.log:272008
pnr      11  route             2026-09-09 05:41  2h20m  log/pnr.log:400002
```

`edr runtime --batch <b>`, or several handles, prints one row per run
with the wall time of each stage and the total.

## Hosts and runs over time

The watcher keeps each cycle's host probes in `host_samples`.
`edr hosts --history [--since 1d]` prints one row per host with a line of
cores in use, RAM, scratch and busy GPUs over the window, each with its
peak and its last value, and `status.html` draws the cores and the RAM of
each host over the last day.

The driver samples its own process groups at every heartbeat: the CPU
since the last sample and the RSS, and the tree size at most once per ten
minutes. The watcher keeps each heartbeat in `run_samples`, and
`edr status <handle>` shows the four lines over the life of the run.

## Compare runs

Every watcher cycle writes `data/board/`:

| File | Holds |
|---|---|
| `board.json` | the runs of the live batches, the last 50 events and the host probes, for a script |
| `status.html` | a phone-width page: every run in board order, the last 50 events, the hosts, and a chart of the cores and RAM in use per host over the last day |
| `compare.html` | the runs with their parameters as columns and a filter per column, a compare table of the final metrics with the difference to the first ticked run, the area delta of two runs, and four plots |

`compare.html` is one self-contained page over the database's runs, parameters,
metrics and the last area report of each run down to depth 3. A filter keeps
the rows whose cell holds its text; `>n` and `<n` compare numbers. Its tables
work as they are. The plots need Plotly: a metric over the steps with the step
names, a scatter of any two columns, the power parts (`power__*` without
`power__total`), and parallel coordinates over every shown run, with an axis
per parameter that differs and one for the chosen metric. The page loads `data/board/plotly.min.js` when
that file exists, else the CDN URL; the watcher downloads nothing, so put
the file there yourself for a head node without internet.

The pages are files. Open them in a browser, or serve the directory:

```sh
cd data/board && python -m http.server --bind 127.0.0.1 8000
```

On the phone, `/compare <handle>...` puts the metrics of several runs
side by side, `/metric <name>` shows one metric per run, `/board` sends
the two pages as files, and `/csv <design>` sends `metrics.csv`;
[telegram.md](telegram.md) has the bot.

## edr export

```sh
edr export --design 3f9a2c1 --out exports/3f9a2c1 [--labels base,base_dw0] [--with-logs]
```

The export takes the newest run per label whose source tag equals
`--design`, and writes:

```
exports/3f9a2c1/
  manifest.json
  runs.csv
  metrics.csv
  <label>/...           the collected files of that run
```

| File | Holds |
|---|---|
| `manifest.json` | `producer`, `created`, `schema`, `project`, `source`, `runs` (id, label, config, build tag, source, host, phase, and a `record`), `tables` with the row counts, `files` with path, size and sha256, `incomplete` with the runs still live or with a failed task |
| `runs.csv` | `run_id,label,config,build_tag,design,host,phase,started,ended` |
| `metrics.csv` | `run_id,label,config,design,stage,step,task,metric,canonical,value,unit,source` |
| `<label>/` | `data/results/<run_id>/` of that run, without `log/` and `*.log` unless `--with-logs` |

A run's `record` holds what made it, as far as edarunner knows it: the
host, the start and end, the edarunner version and the sha256 of the
driver from the spec, the version of each tool that the site file gives
for the host, and the start, end and status of each stage.

The directory is written under a temporary name and renamed at the end,
so a reader never sees a half snapshot. A `--out` that exists and is not
empty is refused. `--dry-run` lists the files.
[reference/cli.md](reference/cli.md) lists every flag of `metrics` and
`export`.

## MLflow

`edr export --mlflow <dir> [--design <src>]` writes the run database into
a local MLflow tracking store, for `mlflow ui`. It needs the extra:
`pip install 'edarunner[mlflow]'`.

```sh
edr export --mlflow data/mlflow --design 3f9a2c1
mlflow ui --backend-store-uri sqlite:///data/mlflow/mlflow.db --host 127.0.0.1 --port 5000
```

Each run becomes one MLflow run in one experiment per project, named by
its label and tagged with `edr.run_id`, `edr.project`, `edr.design`,
`edr.batch`, `edr.host` and `edr.phase`. The parameters are the
`parameters` table. A metric is logged at its step, and a task metric
under `<name>/<task>`. The stage and step times are the metrics
`runtime_s/<stage>`, `runtime_s/step` at the step number and
`runtime_s/total`. The collected files up to 1 MiB are the artifacts. A
run already in the store is skipped, so the export can run again after
each batch. An SQLite older than 3.31 cannot hold the SQL store; the
export then writes the file store `mlruns/` and the URI to pass is
`file://<dir>/mlruns`.

The MLflow UI gives a sortable runs table with a search syntax, a compare
page with parallel coordinates, scatter, box and contour plots, a
parameter diff, and a chart of each metric over its steps. It shows the
last value of a metric, not its source file, and it has no view of the
hierarchical area, of the runtime per step side by side, or of the hosts.
Those stay with `edr compare`, `edr runtime` and the board.

## An analysis reads snapshots

A report, a notebook, a dashboard or a paper never reads the database.
The database changes with every watcher cycle, and a number you quote
must stay the number you read. So the analysis keeps one snapshot per
design under its own `data/`, pinned by the source tag. Every table and
figure comes from `runs.csv` and `metrics.csv` of that snapshot:

```
report/
  data/3f9a2c1/           an export, copied as is
  data/7c0d9e2/
  Makefile                reads data/<pin>/metrics.csv; the pin is the tag
```

The tag in the directory name is the commit the numbers came from, and
the manifest's sha256 per file lets a `make check` prove the copy is the
one that was exported. A new design version is a new export in a new
directory, never a change to an old one. A caption or a chart title that
names the tag then stays true. Say the tag next to every number you
publish.

A snapshot is small by design. A large collected file, a VCD or a full
netlist, belongs to `data/results/` on the head node, not to a snapshot.
Leave it out of `collect`, name it under `collect_on_request`, and fetch
it with `edr continue <handle> --collect <name>` or `edr retire --collect`
when you need it; [run.md](run.md) shows both.
