# Get the results out

This page shows how to get the numbers of a design out of the project
database, how to compare runs on the terminal, the board or the phone,
and how to hand a frozen snapshot to an analysis.

## The database

The project database is `data/edr.db`. It holds every run of every
batch, every number and every action.

| Table | One row per | Holds |
|---|---|---|
| `batches` | batch | the project, the source tag, the pinned date, when it was retired |
| `runs` | run | identity (label, config, build tag, source tag, dirty flag), host and root, phase, state, stage and step, exit, times, disk figures, task counts, `tree_id`, the cores the run reserved |
| `stage_runs` | stage or task attempt of a run | status, start and end, exit, failure signature, log path |
| `parameters` | key and origin of a run | the parameters of the run as text, each with its origin; [Run identity](#run-identity) lists them |
| `metrics` | number | run, stage, step, task, name, canonical name, value, unit, the source file or the error of a failed row, when it was extracted |
| `artifacts` | collected file | path under `data/results/<run_id>/`, size, when, class (`always` or the `collect_on_request` name) |
| `events` | action | time, actor (`user`, `watch`, `telegram`), run, kind, text with the `--why` |
| `store` | key | one JSON value per key, a small key-value store: the watcher's `progress` and `notified`, the `last_board` row order for `#n`, and under `telegram` the message ids and the forum topic of the project |
| `area` | instance of a hierarchical area report | run, stage, step, metric name, instance path, depth, area with the children, local area, cell count; the source file is the metric's |
| `step_runs` | step of a run | stage, step number, the unix time the step started, from the driver's `step_times` |
| `host_samples` | host and watcher cycle | cores, load, RAM and scratch total and in use, GPUs and busy GPUs; 30 days are kept |
| `run_samples` | heartbeat of a run | CPU percent and RSS of the run's process groups, tree size, free disk |

`data/results/<run_id>/` holds the collected files in the layout of the
run tree, so a metric's `source_file` is a path under it.

Read the tables with `sqlite3 data/edr.db` when a command does not answer
the question. Only the head node opens the file, and a read command
without the file reads an empty database in memory and creates nothing.
`edr metrics`, `edr compare`, `edr runtime`, `edr events` and
`edr coverage` open the database read-only. They write nothing next to it, not even the `-wal`
and `-shm` files of SQLite, so they also read a copy in a directory you
cannot write to.
[how-it-works.md](../how-it-works.md#where-the-results-end-up) says how
the database works on a network filesystem.

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

## Run identity

The identity of a number is the source tag of its run, the diff of a
dirty tag, the commit of each nested repository, and the parameters of
the run. Give the tag next to every number you publish.

The source tag is the short hash of the commit that `edr checkout`
pinned. A tree with changes gets `<hash>-dirty-<8 hex>`, where the hex
digits start the sha256 of its diff. The diff holds the changes to
tracked files, every untracked file that git does not ignore, and the
same for each repository that `source.nested` names, so an edit in a
nested flow repository gives a new tag as well. `edr checkout --dirty`
writes the diff to `source.diff` and its base to `source.json`, in the
clone and in `data/sources/<tag>/`. `edr retire --batch` removes the
clone, but `data/sources/<tag>/` stays. `source.json` names the base
commit and the commit of each nested repository. `git apply source.diff`
in a clone of the base, with each nested repository at its commit, gives
back every file of the tree that git does not ignore.

The `parameters` table holds one row per run, key and origin:

| Origin | Keys | Written by |
|---|---|---|
| `spec` | `config`, `build_tag`, each override under its own name, and `vars.<name>` for each var of the job | the watcher at the first collect of the run |
| `checkout` | `source`, and `nested.<name>` with the commit of each nested repository | the watcher at the first collect of the run |
| `import` | `config`, `build_tag`, `source`, and each `--param KEY=VALUE` | `edr import` |

The watcher reads the overrides, the vars and the nested commits from the
run's spec, which `launch` wrote, and never from the batch file. An edit
of the batch file after the launch therefore changes no record. The
spec also holds the command of each stage and task as it was rendered,
and an export carries these commands in the `record` of each run, so a
netlist path or a clock period that the command line sets stays visible.
`edr compare` prints the parameters that differ between the runs it
compares; [Compare runs side by side](#compare-runs-side-by-side) shows
the block.

## Which run a command takes

A label can have several runs: a run that failed and its rerun, or the
same label at a clean and at a dirty source tag. Wherever edarunner takes
one run of a label, it takes it by one rule. Among the runs of that label
at one source tag, it takes the newest run by start time that ended
`done`; when none ended `done`, it takes the newest run. The run id
breaks a tie. `db.pick` holds the rule, and four places use it:

- the handle `label@source`, such as `base@3f9a2c1`;
- `edr export`, which holds one run per label and source;
- `edr coverage`, which checks a demand list against one run per label
  and source ([Coverage of a demand list](#coverage-of-a-demand-list));
- `reuse = { label = "base", latest = true }` in a batch file, which
  looks only at the runs of the batch's source.

The handle `label@batch` names the one run of that label in that batch.
A batch can hold two runs of one label, for example after two imports or
two `edr continue` runs of one stage. Such a handle is refused, and the
error lists each run with its source and phase:

```
$ edr status base@sweep1
edr: base@sweep1: 2 runs have label 'base' in batch 'sweep1'; name one by its run id or as label@source: 20260902_0221_base_demo_g3f9a2c1 (3f9a2c1, FAILED:pnr), 20260902_0221_base_demo_g3f9a2c1-dirty-7b21c0d9 (3f9a2c1-dirty-7b21c0d9, done)
```

A name after the `@` that is both a batch and a source is refused as
well. Where two runs share a label and a batch, the triage, `edr brief`,
the event lists and the alerts name each of them by the shortest prefix
of its run id that no other run id starts with, so every command they
propose acts on one run. `edr stop`, `edr retire` and `edr continue`
print the run id and the phase of the run they act on to stderr before
anything else, also with `--dry-run`.

## edr metrics

```sh
edr metrics --source 3f9a2c1
edr metrics --source 3f9a2c1 --stage pnr --step 12
edr metrics --source 3f9a2c1 --csv > metrics.csv
```

`--source` is the source tag exactly as `edr checkout` printed it, so a
run on `3f9a2c1-dirty-7b21c0d9` needs that full tag. Give `--source`
more than once to read several tags in one table. The text form shows
label, source, stage, step, task, metric, value and unit; `--csv` writes
the columns of `metrics.csv` below. Every row carries its source tag and
its source file, so you can check where a number came from before you
put it in a table. A value that breaks the `pass` rule of its metric
shows FAIL next to it; see
[Reports with several blocks](#reports-with-several-blocks).

## Extract again

The watcher extracts metrics as the files arrive, using the definitions
in `edr.toml` at that moment. If you add or change a metric later, runs
that have already finished keep the rows from the old definitions.
`edr extract` rebuilds the rows of those runs from their collected files
and the current definitions:

```sh
edr extract base@g8 --dry-run   # counts only, writes nothing
edr extract --batch g8
edr extract --source 3f9a2c1
```

For each run it prints how many rows are new, changed, unchanged, failed
and removed, and a line for each metric that failed, with the count and
the first error:

```
$ edr extract base@g8
20260902_0221_base_demo_g3f9a2c1: 0 new, 2 changed, 30 unchanged, 1 failed, 4 removed
  energy_nj: 1 failed: simulation/tests/demo/SOFTMAX_R197/power/phases.json: power.csv has no WHOLE row
```

A row counts as changed when its value, canonical name, unit or source
file differs, or when its area rows differ, and `extract` replaces it.
A row written by an earlier parser therefore gets the source file of the
current one.

A row that the extraction no longer gives is removed with its area rows
when it is a failed row, when no metric defines it any more, or when the
extraction read its stage and task. That covers the rows of a metric you
deleted from `edr.toml`, the instances below a smaller `area_hier` depth,
and a step that another stage owns now. Two kinds of values stay. A value
whose source file is gone from `data/results/` cannot be read again, so
`extract` keeps it and counts it as kept without a file. A value of a
stage or a task that the extraction did not read stays as it is: a stage
outside the stages of the run's spec, or a task that `tasks.toml` no
longer resolves.

Each run gets an `extract` event with the same text, and its rows are
written in one transaction. `--dry-run` prints the counts and writes
nothing; run it first.

## Reports with several blocks

Many reports hold one block per scenario and path group: a `report_qor`
of Fusion Compiler or PrimeTime, or several OpenSTA checks written to one
file. A regex runs with `re.MULTILINE` over the whole file, and group 1
is the value. By default the first match wins, as with `re.search`, so a
regex that is not tied to one block reads whatever block comes first.

`examples/local-demo` writes such a report. Its hold scenario comes
first, and a hold block has no setup slack:

```
Scenario           'func_fast'
Timing Path Group  'reg2reg'
----------------------------------------
Worst Hold Violation:           -0.001
No. of Hold Violations:              1
----------------------------------------

Scenario           'func_slow'
Timing Path Group  'in2reg'
----------------------------------------
Critical Path Slack:              0.01
No. of Violating Paths:              0
----------------------------------------

Scenario           'func_slow'
Timing Path Group  'reg2reg'
----------------------------------------
Critical Path Slack:             -0.05
No. of Violating Paths:              5
----------------------------------------
```

The regex `Timing Path Group\s+'reg2reg'[\s\S]*?Critical Path Slack:\s+(\S+)`
looks right, but its first match starts at the hold block of `reg2reg`,
finds no slack there and runs on into the next block. It reads 0.01, the
slack of `in2reg`, and the extraction reports no failure. Start the regex
at the header of the block you mean, so that it names the scenario and
the path group:

```toml
[metrics.wns_reg2reg_ns]
stage = ["synth", "pnr"]
step = "*"
file = "reports/{step}/qor.rpt"
regex = '''^Scenario\s+'func_slow'\nTiming Path Group\s+'reg2reg'\n(?:.*\n)*?Critical Path Slack:\s+(\S+)'''
unit = "ns"
canonical = "timing__setup__ws"
```

`reduce` reads every match instead of the first one and makes one value
of them: `first`, `last`, `min`, `max` or `sum`. A regex tied to the
setup scenario alone matches once per path group, so `reduce = "min"`
gives the worst setup slack over all groups. The demo's `wns_ns` works
this way.

The row of a regex value names its line: `source_file` is `path:line`,
such as `reports/5/qor.rpt:18`. For `min` and `max` it is the line of the
value that was taken, and for `sum` the line of the first match. Open the
report at that line to see which block a number came from.

A `pass` rule turns a number into a verdict. It is an operator, `==`,
`!=`, `<`, `<=`, `>` or `>=`, and a number:

```toml
[metrics.setup_violations]
stage = ["synth", "pnr"]
step = "*"
file = "reports/{step}/qor.rpt"
regex = '''^Scenario\s+'func_slow'\n(?:.*\n)*?No\. of Violating Paths:\s+(\d+)'''
reduce = "sum"
pass = "== 0"
unit = "paths"
```

`edr metrics` and `edr compare` print FAIL next to a value that breaks
its rule, and their `--json` rows carry a `verdict` of `pass` or `FAIL`.
Put the rule on the violation count, not on the slack: a report can
print a slack of -0.000 while paths still fail.

Check a new metric with `--over steps` before you quote it.
`edr metrics --run <handle> --over steps` prints one run along its steps,
one column per metric, and a `verdict` column when a metric has a pass
rule. A step fails when one of its values breaks its rule. With
`--metric`, it prints only that metric, with its change from the step
before and the source of each value, so you can check at every step that
the value came from the block you meant:

```
$ edr metrics --run base@g8 --over steps --metric wns_ns
stage   step  name              wns_ns       Δ  source
pnr        8  synth-init-opto   -0.011       -  flow/runs/<run>/reports/8/qor.rpt:64
pnr        9  synth-final-opto  -0.013  -0.002  flow/runs/<run>/reports/9/qor.rpt:64
pnr       10  cts               -0.024  -0.011  flow/runs/<run>/reports/10/qor.rpt:64
pnr       11  route             -0.071  -0.047  flow/runs/<run>/reports/11/qor.rpt:64
pnr       12  route-opt         -0.035   0.036  flow/runs/<run>/reports/12/qor.rpt:64
```

## Stage of record

A metric with `step = "*"` has one value per step, but a table that
shows one number per run needs one step per run. The deepest step is
often the wrong one, for two reasons:

- The runs of one sweep can end at different steps. Say the small builds
  of a sweep finish route-opt at step 12, while the large builds stop
  after route at step 11 because route-opt does not fit their budget.
  The deepest step that all of them have is 11, so a table of both
  would show the small builds before route-opt.
- A later stage can own later steps. An export stage that runs eco-route
  and fillers after route-opt writes its own area reports at steps 13 to
  15. Eco-route adds cells, so the area of an export step is not the
  routed area, and the deepest step of any stage reads high.

`record` names the step of record of a metric:

```toml
[metrics.area_um2]
stage = ["pnr", "export"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
area_hier = 3
unit = "um2"
record = { stage = "pnr", from = 11 }
```

The step of record of a run is its deepest step of `stage` with a value,
at or after step `from`. Only a step that the stage owns counts, so an
export step never stands in for a pnr step. Here a small build is at
step 12 and a large build at step 11. `from` keeps out a run that
stopped before route: its deepest pnr step holds a placed design, and a
table that compared it with routed designs would mislead. Such a run has
no step of record and is named missing.

Every view that shows one value per run takes the step of record and
names the stage and step next to the value:

```
$ edr compare small@g8 large@g8 early@g8 --metric area_um2
metric    task             small            large  early  Δ large      Δ %  Δ early  Δ %
area_um2        51230.4 (pnr 12)  164808 (pnr 11)      -   113578  +221.7%        -    -
missing: early has pnr steps 8 to 9
```

- `edr compare` shows each run at its step of record. A run without one
  is named missing, and the command exits 2. `--step N` puts every run
  at step N, and a run without step N is named missing the same way.
- compare.html shows each run at its step of record, and `missing` for
  a run without one.
- `edr status --metric` shows each run at its step of record, and
  `missing` for a run without one; see
  [An overview of runs](#an-overview-of-runs).
- The MLflow export logs the value at the step of record again as
  `record/<metric>`.
- `metrics.csv` has a `record` column: 1 on the row at the step of
  record, 0 on the other rows of a metric with `record`, and empty for a
  metric without it. Keep the rows with `record` equal to 1 and you have
  one row per run and metric; a run with none has no step of record.

For a metric without `record`, `edr compare` shows each run at the
deepest step that every run has, and compare.html shows the last step of
each run. Both name the step. Give `record` to every metric that a paper
or a report quotes, the timing metrics included, so the verdict of a
build comes from the step its area comes from.

## A number the flow does not print

A metric has one of five parsers: `regex`, `csv`, `json`, `python` or
`area_hier`. A number that comes from other numbers, such as an energy
from a power and a window, needs a `python` hook. The hook gets the path
of `file` and reads the other files itself:

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

An exception in the hook gives a failed row, and a hook that returns
None gives no row; see [Numbers that did not parse](#numbers-that-did-not-parse).
`examples/local-demo/hooks/energy.py` is this hook.

The values of a csv `where` take the placeholders of `file`, so each
task of a task group can read its own row of one shared file, such as
the cycle counts of a bench suite:

```toml
[metrics.cycles]
stage = "power"
file = "bench/suite.csv"
csv = { where = { name = "{task.test}" }, column = "cycles" }
unit = "cycles"
```

## Numbers that did not parse

A file that is there but does not parse gives a failed row: its value
is empty, and `source_file` holds the file and the error. The
watcher, `edr import` and `edr extract` all store failed rows, and the
views print `failed:` and the error in place of the value:

```
$ edr metrics --source 3f9a2c1 --stage power --metric power_w
label  source   stage  step  task     metric                                                                                                   value  unit
base   3f9a2c1  power     -  k_big    power_w  failed: simulation/tests/demo/SOFTMAX_R197/power/reports/power.csv: no row matches {'phase': 'WHOLE'}  W
base   3f9a2c1  power     -  k_small  power_w                                                                                                   0.25  W
```

A missing file gives no row. When the watcher reads the file again and
it parses, the value fills the failed row; `edr extract` rewrites it at
once.

Some numbers exist in some runs only, such as the power of a block that
one build does not have. Mark such a metric `optional`. A file without a
match of its `regex`, without a row that matches its csv `where`, or
without its `json` key then gives no row instead of a failed one:

```toml
[metrics.blk_b_w]
stage = "power"
file = "{task_dir}/power/reports/power.csv"
csv = { where = { phase = "WHOLE", instance = "u_blk_b" }, column = "total_w" }
optional = true
unit = "W"
```

A `python` hook decides for itself. It returns None when the number does
not apply to the file, such as a trace number of a task without a trace,
and it raises an exception when the file is wrong.

## Hierarchical area

A metric with `area_hier = <depth>` reads a hierarchical area report,
Synopsys `report_area -hierarchy` or the OpenROAD area by hierarchy. Its
value is the top area. Each instance down to that depth becomes a row of
the `area` table: the top is `<top>` at depth 0, and a child is its path
from the top. A depth of 3 or 4 is usually enough to compare the main
blocks; the leaf levels of a large design add millions of rows.

```toml
[metrics.area_hier_um2]
stage = ["pnr"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
area_hier = 4
unit = "um2"
canonical = "design__instance__area"
```

`edr metrics --source <source> --instance i_top/i_streamer --depth 3` prints
the area rows of that subtree. `edr compare A B --area --depth 2` puts the
blocks of two or more runs side by side, each at its
[step of record](#stage-of-record), or at the deepest step that every run
has when the area metric has no `record`. The header names the stage and
step of each run:

```
$ edr compare base@g8 lanes16@g7 lanes4@g7 --area --depth 2 --instance i_top
area um2 at depth 2
instance        base (pnr 12)  lanes16 (pnr 12)  lanes4 (pnr 12)  Δ lanes16     Δ %  Δ lanes4     Δ %
i_top/i_engine        96580.1           96092.1          95942.9     -488.1   -0.5%    -637.3   -0.7%
i_top/i_accum         36583.7           36169.4          36001.1     -414.3   -1.1%    -582.6   -1.6%
i_top/i_lanes         32453.8           19234.8           9372.4   -13219.0  -40.7%  -23081.4  -71.1%
i_top/i_stream        14592.5           14738.5          14395.8      146.0   +1.0%    -196.7   -1.3%
<top>                208030.0          193918.6         182995.8   -14111.4   -6.8%  -25034.2  -12.0%
base: flow/runs/<run>/reports/12/area_hier.rpt
...
```

## Compare runs side by side

`edr compare A B` without `--area` prints one row per task and metric:
the value of each run with its stage and step, and the delta and the
percent of each run to the first. Each run is at its
[step of record](#stage-of-record), or at the deepest step that every
run has for a metric without `record`. `--step N` puts every run at
step N. A percent across a sign change, such as a slack from +1 ps to
-1 ps, is left out. A value that breaks the `pass` rule of its metric
shows FAIL next to it. `--metric` (repeatable) and `--stage` narrow the
rows; `--json` keeps the source file and the verdict of every value and
lists the missing runs.

```
$ edr compare base@g8 lanes16@g7 --metric wns_ns --metric cells
metric  task             base          lanes16  Δ lanes16    Δ %
cells         536547 (pnr 12)  489861 (pnr 12)     -46686  -8.7%
wns_ns         0.003 (pnr 12)  -0.001 (pnr 12)     -0.004      -
$ edr compare base@g8 lanes16@g7 --metric wns_ns --metric cells --step 11
metric  task             base          lanes16  Δ lanes16     Δ %
cells         535811 (pnr 11)  489276 (pnr 11)     -46535   -8.7%
wns_ns        -0.056 (pnr 11)  -0.038 (pnr 11)      0.018  +32.1%
```

When the runs come from more than one source tag, each column is
`label@source`, a line above the table names the tags, and `--json` sets
`mixed_sources`. Two runs with the same name get a prefix of their run
ids after it. `--area` names its columns the same way.

```
$ edr compare base@3f9a2c1 base@3f9a2c1-dirty-7b21c0d9 --stage pnr --step 12 --metric cells
mixed sources: 3f9a2c1, 3f9a2c1-dirty-7b21c0d9
metric  task     base@3f9a2c1  base@3f9a2c1-dirty-7b21c0d9  Δ base@3f9a2c1-dirty-7b21c0d9    Δ %
cells         536547 (pnr 12)              536102 (pnr 12)                           -445  -0.1%
```

Above the table, compare prints one line per parameter whose value
differs between the runs, with the value of each run, or `-` for a run
that lacks the key. The source is not repeated there, because the column
names carry it. A difference in method, such as the netlist that a power
run read, shows there as a line of its own. `--json` lists these
parameters under `parameters`.

```
$ edr compare base@3f9a2c1 base@7c0d9e2 --metric energy_nj
mixed sources: 3f9a2c1, 7c0d9e2
parameter           base@3f9a2c1  base@7c0d9e2
vars.netlist_stage  11            15

metric     task     base@3f9a2c1  base@7c0d9e2  Δ base@7c0d9e2    Δ %
energy_nj  k_small         412.7         446.1            33.4  +8.1%
```

## An overview of runs

`edr status --metric NAME` adds a column per metric to the board, so one
table lists the runs with the numbers a report quotes. `--metric` is
repeatable and takes a metric name or a canonical name, and `--source`
(repeatable) keeps the runs of those source tags:

```
$ edr status --batch g8 --metric area_um2 --metric setup_violations
#   label  source   host   state   phase       stage/step  age  fail/done  core-h          area_um2   setup_violations
#1  early  3f9a2c1  host1  failed  FAILED:pnr  pnr/9        2d      0f/0d    11.2           missing    52 FAIL (pnr 9)
#2  large  3f9a2c1  host2  done    done        pnr/11       1d      0f/0d    30.1   164808 (pnr 11)  208 FAIL (pnr 11)
#3  small  3f9a2c1  host1  done    done        pnr/12       1d      0f/0d    13.4  51230.4 (pnr 12)         0 (pnr 12)
missing: early@g8 has pnr steps 8 to 9
```

A cell holds the value of the run at the
[step of record](#stage-of-record) of its metric, with FAIL when the
value breaks the `pass` rule, and the stage and step it comes from. A
metric without `record` shows the last step of each run. A run without
a step of record shows `missing`, and a line under the board names the
steps it has. A task group has one value per task, so its rows are left
out; `edr metrics` lists them.

`--csv` writes the same rows as CSV: `run_id`, `label`, `batch`,
`source`, `host`, `state` and `phase`, then four columns per metric,
such as `area_um2`, `area_um2_stage`, `area_um2_step` and
`area_um2_verdict`, where the verdict is `pass`, `FAIL` or empty. The
lines that name the missing runs go to stderr.
`--json` adds `metrics`, one row per metric shaped like a row of
`edr compare --json`, and `missing`. A canonical name that two metrics
share is refused; name one of them.

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
| `compare.html` | the runs with their parameters as columns and a filter per column, a compare table of each metric at its step of record, else at its last step, with the stage and step of each value and the difference to the first ticked run, the area delta of two runs, and four plots |

`compare.html` is one self-contained page over the database's runs,
parameters, metrics and the area report of each run at its step of
record, else at its last step, down to depth 3. A filter keeps the rows
whose cell holds its text; `>n` and `<n` compare numbers. The tables need nothing else, but the plots need Plotly. They show
a metric over the steps with the step names, a scatter of any two columns,
the power parts (`power__*` without `power__total`), and parallel
coordinates over every shown run, with an axis per parameter that differs
and one for the chosen metric. The page loads `data/board/plotly.min.js`
when that file exists, else the CDN URL; the watcher downloads nothing, so
put the file there yourself for a head node without internet.

The pages are files. Open them in a browser, or serve the directory:

```sh
cd data/board && python -m http.server --bind 127.0.0.1 8000
```

On the phone, `/compare <handle>...` puts the metrics of several runs
side by side as `edr compare` does, `/metric <name>` shows one metric
per run, `/board` sends the two pages as files, and `/csv <source>`
sends `metrics.csv`;
[alerts.md](alerts.md#files) has the bot.

## edr export

```sh
edr export --source 3f9a2c1 --out exports/3f9a2c1 [--labels base,base_dw0] [--with-logs]
```

The export takes one run per label whose source tag equals `--source`,
by the rule of [Which run a command takes](#which-run-a-command-takes),
and writes:

```
exports/3f9a2c1/
  manifest.json
  runs.csv
  metrics.csv
  parameters.csv
  sources/<tag>/source.diff   the diff of each dirty source
  <label>/...                 the collected files of that run
```

| File | Holds |
|---|---|
| `manifest.json` | `producer`, `created`, `schema` (2), `project`, `sources`, `runs` (id, label, config, build tag, source, host, phase, and a `record`), `tables` with the row counts, `dirty_sources` with the base, the nested commits and the sha256 of the diff of each dirty source, `files` with path, size and sha256, `incomplete` with every exported run whose phase is not `done`, and `skipped` with the other runs of each label and source; an entry of `incomplete` or `skipped` holds the run id, label, source and phase |
| `runs.csv` | `run_id,label,config,build_tag,source,host,phase,started,ended,batch,dirty,tree_id,retired`; `retired` is 1 for a run that was retired, or whose batch was |
| `parameters.csv` | `run_id,label,source,key,value,origin`, the rows of the `parameters` table for each exported run |
| `sources/<tag>/source.diff` | the copy of `data/sources/<tag>/source.diff` for each dirty source |
| `metrics.csv` | `run_id,label,config,source,stage,step,task,metric,canonical,value,unit,source_file,record`; `record` marks the [step of record](#stage-of-record) |
| `<label>/` | `data/results/<run_id>/` of that run, without `log/` and `*.log` unless `--with-logs`; `<label>@<source>/` when the runs come from more than one tag |

`--source` may be given more than once, for a table that needs a done
run at a dirty tag next to the runs of the clean tag, for example. The
export then holds one run per label and source. When its runs come from
more than one tag, the files of a run go under `<label>@<source>/`:

```
$ edr export --source 3f9a2c1 --source 3f9a2c1-dirty-7b21c0d9 --out exports/3f9a2c1-both
exports/3f9a2c1-both: 3 runs (1 not done), 1 skipped, 14 files
```

Read `incomplete` before you use a number of the export: a run listed
there failed, stopped or has not ended.

A run's `record` holds what made it, as far as edarunner knows it: the
host, the start and end, the edarunner version and the sha256 of the
driver from the spec, the version of each tool that the site file gives
for the host, the start, end and status of each stage, and under
`commands` the command of each stage and task as the spec rendered it.

A dirty source backs a published number only when the export carries
its diff. Its entry in `dirty_sources` has a `diff_sha256` of `null`
when edarunner never kept the diff, as for a dirty tag that `edr import`
recorded.

The directory is written under a temporary name and renamed at the end,
so a reader never sees a half snapshot. A `--out` that exists and is not
empty is refused. `--dry-run` lists the files.
[reference/cli.md](../reference/cli.md) lists every flag of `metrics` and
`export`.

## MLflow

`edr export --mlflow <dir> [--source <source>]` writes the project database into
a local MLflow tracking store, for `mlflow ui`. It needs the extra:
`pip install 'edarunner[mlflow]'`.

```sh
edr export --mlflow data/mlflow --source 3f9a2c1
mlflow ui --backend-store-uri sqlite:///data/mlflow/mlflow.db --host 127.0.0.1 --port 5000
```

Each run becomes one MLflow run in one experiment per project, named by
its label and tagged with `edr.run_id`, `edr.project`, `edr.source`,
`edr.batch`, `edr.host` and `edr.phase`. The parameters are the
`parameters` table. A metric is logged at its step, and a task metric
under `<name>/<task>`. The value at the step of record is logged again
as `record/<name>`, at that step. The stage and step times are the metrics
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

An analysis should never read the live database, because the database
changes with every watcher cycle and a number you quote must stay the
number you read. Instead, the analysis keeps one snapshot per
source under its own `data/`, pinned by the source tag. Every table and
figure comes from `runs.csv`, `metrics.csv` and `parameters.csv` of
that snapshot:

```
report/
  data/3f9a2c1/           an export, copied as is
  data/7c0d9e2/
  Makefile                reads data/<pin>/metrics.csv; the pin is the tag
```

The tag in the directory name is the commit the numbers came from, and
the manifest's sha256 per file lets a `make check` prove the copy is the
one that was exported. A new source is a new export in a new
directory, never a change to an old one. A caption or a chart title that
names the tag then stays true. Say the tag next to every number you
publish.

A snapshot is small by design. A large collected file, a VCD or a full
netlist, belongs to `data/results/` on the head node, not to a snapshot.
Leave it out of `collect`, name it under `collect_on_request`, and fetch
it with `edr continue <handle> --collect <name>` or `edr retire --collect`
when you need it; [cleanup.md](cleanup.md#keep-the-large-files) shows both.

## Coverage of a demand list

An analysis that sums over many tests, such as the energy of a model
over its kernels, needs a run for each test. When one has none, a script
falls back to an estimate or leaves the test out, and nothing warns.
Write the tests that the analysis charges into a demand list, a CSV file
with one row per test, and check it with `edr coverage`:

```
build_tag,stage,task,source
cfg_a,bench,k_small,7c0d9e2
cfg_a,power,k_small,3f9a2c1
cfg_a,power,k_big,3f9a2c1
cfg_a,power,k_wide,3f9a2c1
```

A row names a `label` or a `build_tag`, or both, a `stage` and a `task`,
and it may pin a `source`. An empty task means the numbers of the stage
itself, and an empty source means any source. Other columns are ignored,
so the analysis can keep its own columns in the file.

```
$ edr coverage demand.csv
build_tag  stage  task     source   status     runs
───────────────────────────────────────────────────────────
cfg_a      bench  k_small  7c0d9e2  held       rtl@7c0d9e2
cfg_a      power  k_small  3f9a2c1  held       base@3f9a2c1
cfg_a      power  k_big    3f9a2c1  elsewhere  base@7c0d9e2
cfg_a      power  k_wide   3f9a2c1  missing
2 of 4 rows held
```

For each label and source, `edr coverage` looks at one run, the one that
`label@source` names by the rule of
[Which run a command takes](#which-run-a-command-takes). A row gets the
first status of this table that fits:

| Status | The row |
|---|---|
| `held` | has a value in the run at its source |
| `running` | has no value yet, and the run at its source has not ended |
| `failed` | has no value, and the run at its source ended in a phase other than `done` |
| `elsewhere` | has a value only in runs at other sources |
| `missing` | has a value in no run |

The runs column names the runs behind the status as `label@source`, with
the phase when it is not `done`. The command exits 1 when a row is not
held, so a Makefile rule can stop before it builds a table on a gap.
`--json` gives each row with its status and runs.

A build tag matches every run of one build: the backend runs, the runs
that continue them, and the bench runs of the RTL recorded with the same
tag, a suite imported with `edr import --build-tag` or a bench tracked
with `edr track --build-tag`. One demand list by build tag then covers
the RTL cycle counts and the energies of each test. A label
matches that label only. A run that `edr continue` starts on a tree has
the label `<label>.<stage>`, so a demand by label misses it and a demand
by build tag finds it.
