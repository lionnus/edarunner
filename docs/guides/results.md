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
| `task_fields` | key of a task of a run | the fields the task ran with, as text, each with its origin; [Tasks as data](#tasks-as-data) lists them |
| `flags` | flag of a run | run, task, check and text, rewritten at each extraction; [Parameters and checks](#parameters-and-checks) explains them |
| `metrics` | number | run, stage, step, task, name, canonical name, value after `scale`, unit, the source file or the error of a failed row, when it was extracted |
| `artifacts` | collected file | path under `data/results/<run_id>/`, size, when, class (`always` or the `collect_on_request` name) |
| `events` | action | time, actor (`user`, `watch`, `telegram`), run, kind, text with the `--why` |
| `store` | key | one JSON value per key, a small key-value store: the watcher's `progress` and `notified`, the `last_board` row order for `#n`, and under `telegram` the message ids and the forum topic of the project |
| `instances` | instance and part of an `area_hier` or `table` metric | run, stage, step, task, metric name, part, instance path, depth, value with the children, local value, cell count; the source file is the metric's |
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

## Readable numbers

A view for people prints every number the same way: six significant
digits, every digit before the decimal point, and no exponent. So
`123.4560000000001` prints as `123.456`, `0.30000000000000004` as `0.3`
and `48213000000.0` as `48213000000`. `board.num` holds the rule.
`edr metrics`, `edr compare`, `--over steps`, `edr status`, `edr brief`,
the bot's `/compare` and `/metric`, and compare.html print through it.
`--json`, `--csv`, the export and MLflow keep the value as it is stored.

The format cannot choose the unit. A flow that writes a window in
femtoseconds gives `48213000000 fs` in every view. The `scale` key stores
that window as `48213 ns` instead:

```toml
[metrics.window_ns]
stage = "power"
file = "{task_dir}/power/phases.json"
json = "window_dur"
scale = 1e-6
unit = "ns"
```

`scale` multiplies each value when it is extracted, so the database holds
the number in the unit that `unit` names. Every view, `--json`, the export
and MLflow get that number, and a `pass` rule compares it. An `area_hier`
or `table` metric scales its rows of the `instances` table as well. A changed `scale`
reaches the rows already in the database when `edr extract` runs; see
[Extract again](#extract-again).

A view for people names a metric by its key in `edr.toml`, such as
`window_ns` above. The canonical name appears only where a program reads
the rows: in `--json` and in the `canonical` column of `metrics.csv`.
`--metric` takes either name.

## Run identity

The identity of a number is the source tag of its run, the diff of a
dirty tag, the commit of each nested repository, and the parameters of
the run: its vars and overrides, and the values read from its own files.
Give the tag next to every number you publish.

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
| `extract` | every key that a `[parameters.<name>]` table reads from the run's files | each extraction: the watcher's collect, `edr extract` and `edr import`; see [Parameters and checks](#parameters-and-checks) |

The watcher reads the overrides, the vars and the nested commits from the
run's spec, which `launch` wrote, and never from the batch file. An edit
of the batch file after the launch therefore changes no record. The
spec also holds the command of each stage and task as it was rendered,
and an export carries these commands in the `record` of each run, so a
netlist path or a clock period that the command line sets stays visible.
`edr compare` prints the parameters that differ between the runs it
compares; [Compare runs side by side](#compare-runs-side-by-side) shows
the block. That block, compare.html and the MLflow export show one value
per key, and a key with an `extract` row shows the value read from the
run's files, since the run was built with it. The fields of each task of
a run belong to its identity as well; [Tasks as data](#tasks-as-data)
describes them.

## Parameters and checks

The parameters that a run declares say what it should be: the overrides
and vars of its job, and the source tag of its checkout. What the flow
ran with is in the run's own files: the knob line a stage writes at the
head of its log, a JSON file of settings, or a report that names the
commit the flow built. A `[parameters.<name>]` table reads such values,
and the checks compare them with what the run claims to be.

```toml
# The tool's command line at the head of the stage log:
#   set ENABLE_X 1; set LANES 8; ...
[parameters.knobs]
stage = "pnr"
file = "log/pnr.log"
regex = 'set (?P<key>\w+) (?P<value>[^;]+);'
head_bytes = 65536

# The commit that the flow built, as its build report names it.
[parameters.source]
stage = "pnr"
file = "reports/build.rpt"
regex = '^commit:\s*(\S+)'
```

A table names a stage, a file under the collected results, and one of
three parsers:

| Parser | Keys it gives |
|---|---|
| `regex` | the key `<name>` with group 1 of the first match; with the named groups `key` and `value`, a key for every match, and the first match of a key wins |
| `json` | every key of a flat object at a dotted path of a JSON file; `json = ""` takes the whole file |
| `python` | the dict that a hook returns; the hook gets the path of the file |

`head_bytes` limits a `regex` to the start of the file. A stage log can
be large, and it can hold a later command line, for example from a
resume, while the knob line at its head is the one that built the run.

Each value goes into the `parameters` table as text, with the origin
`extract`. The watcher reads the tables at every collect, and `edr
extract` and `edr import` at every extraction; the new values replace
the ones from before. A table reads the files of every stage in the
run's spec, or of every stage for a run without a heartbeat such as an
import, as soon as the file is there, so a stage that failed or still
runs has its parameters too. A file that does not parse gives the flag
`parameters.<name>` with the error, and a missing file gives nothing.
When two tables give the same key, the first table wins.

Every extraction of a run then rewrites its flags. A flag is a row of
the `flags` table: the run, a task or none, the check, and a text. Three
checks are built in:

| Check | Flags |
|---|---|
| `declared_vs_observed` | a key whose extracted value differs from its value under another origin, such as an override of the job or the source tag of the checkout; `1` and `1.0` count as equal |
| `same_parameters` | two runs with different labels at one source whose extracted parameters are all equal, so one of them is not the build its label names; a run that continues another on its tree, with the same `tree_id`, is not compared with it |
| `same_results` | two tasks of one run with equal values in every metric that `[checks] same_results` lists, so the flow ran one test under two names |

`same_parameters` compares a run with every other run of its source, so
an extraction rewrites the `same_parameters` flags of all the runs at
that source.

```toml
[checks]
same_results = ["window_ns", "power_w"]
python = "hooks/checks.py:check"
```

The `python` hook holds the project's own rules. It gets the run's row,
its parameter rows (`key`, `value`, `origin`) and its metric rows
(`stage`, `step`, `task`, `name`, `value`, `unit`, `source_file`), and
returns a `(task, check, text)` for each flag, with the task `""` for
the whole run. This one flags a power window that ended at the cap of
the simulation instead of at the end of the kernel:

```python
# hooks/checks.py
CAP_NS = 5000.0  # the longest window the flow records


def check(run, parameters, rows):
    return [(m["task"], "capped_window", f"the window is the cap of {CAP_NS:g} ns")
            for m in rows if m["name"] == "window_ns" and m["value"] == CAP_NS]
```

A hook that raises an exception gives the flag `checks.python` with the
error, so a broken rule shows up instead of passing in silence.

`edr extract` prints the flags of each run under its counts:

```
$ edr extract lanes4@sweep1
20260902_0221_lanes4_demo_g3f9a2c1: 6 new, 0 changed, 0 unchanged, 0 failed, 0 removed, 14 parameters, 3 flags
  flag declared_vs_observed: ENABLE_X is 1 in the run's files and 0 by spec
  flag same_results k_big: window_ns and power_w equal those of k_small
  flag same_results k_small: window_ns and power_w equal those of k_big
```

`edr status <handle>` shows the flags under the identity of the run,
`edr brief` lists every flagged run with a count per check, and `edr
brief --run <handle>` lists each flag. An export writes `flags.csv`,
lists the flags in its manifest, and names the checks that flag a run in
the `flags` column of `runs.csv`. A flag hides no number from any view:
read the run's files before you use its numbers, then fix the job or the
flow so that the next run is clean.

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
file differs, or when its instance rows differ, and `extract` replaces it.
A row written by an earlier parser therefore gets the source file of the
current one.

A row that the extraction no longer gives is removed with its instance rows
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

A metric has one of six parsers: `regex`, `csv`, `json`, `python`,
`area_hier` or `table`. A number that comes from other numbers, such as an energy
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

## Per-instance numbers

Some numbers come per instance: the area of each block from a
hierarchical area report, or the power of each block in each phase of a
power run. Two parsers read such a file into the `instances` table, one
row per run, stage, step, task, metric, part and instance. A part is a
phase, a trace slice or a scenario; an area report has none. A row holds
the instance path, its depth with the top at 0, the value with the
children, the local value without them when the file has it, and the
cell count of an area report. The metric row itself holds the value of
the top, so every view that shows one number per run reads it as usual.

`area_hier = <depth>` reads Synopsys `report_area -hierarchy` or the
OpenROAD area by hierarchy. The top is `<top>` at depth 0, a child is
its path from the top, and each instance down to that depth becomes a
row:

```toml
[metrics.area_um2]
stage = ["pnr", "export"]
step = "*"
file = "flow/runs/{tree_id}/reports/{step}/area_hier.rpt"
area_hier = 3
unit = "um2"
record = { stage = "pnr", from = 11 }
```

`table` reads a tidy CSV with one row per instance and part, such as the
power of each block in the whole window and in each trace slice:

```
phase,instance,full,depth,total_w
WHOLE,chip,chip,0,0.2500
WHOLE,i_top,chip/i_top,1,0.2400
WHOLE,u_core,chip/i_top/u_core,2,0.1250
...
TRACE_0,chip,chip,0,0.3100
```

```toml
[metrics.power_inst_w]
stage = "power"
file = "{task_dir}/power/reports/inst.csv"
table = { instance = "full", value = "total_w", depth = "depth", part = "phase", where = { phase = ["WHOLE", "TRACE_*"] }, top = { phase = "WHOLE", depth = "0" }, max_depth = 3 }
unit = "W"
```

`instance`, `value`, `depth`, `part` and `local` name the columns of the
file. The value of the metric comes from the first row that `top`
matches, here the design total of the whole window, and its
`source_file` names the line of that row. The rows that `where` matches,
down to depth `max_depth`, are stored. A value of `where` or `top` is a
glob or a list of globs. Without `depth`, the depth is the count of `/`
in the path. Give `instance` a column of full paths: a leaf name such as
`u_reg` repeats under every lane, and an instance that repeats within a
part gives a failed row.

`edr metrics --source <source> --instance GLOB --depth N` prints the
stored rows. The glob runs over the path, and `*` also matches `/`:

```
$ edr metrics --source 3f9a2c1 --metric area_um2 --instance '*/u_add'
label  source   stage  step  task  metric    part  instance           depth  value  local  cells  unit
base   3f9a2c1  pnr      12        area_um2        i_top/u_sum/u_add      3  12800      0      -  um2
```

### Compare instances

`edr compare A B --instances` puts the instances of one metric side by
side, each run at the [step of record](#stage-of-record) of the metric,
or at the deepest step that every run has when the metric has no
`record`. It prints one row per instance at `--depth` (default 1), then
three rows: `<sum>` adds up the rows shown, `<other>` is the top less
that sum, and `<top>` is the total, printed once. At depth 0, `<top>` is
the only row. The rows shown and `<other>` always add up to the top:

```
$ edr compare base@g8 noadd@g8 --instances --metric area_um2 --depth 2
area_um2 in um2 at depth 2
instance      base (pnr 12)  noadd (pnr 12)  Δ noadd     Δ %
i_top/u_core         102400          102400        0   +0.0%
i_top/u_sum           51200           38400   -12800  -25.0%
i_top/u_vec           25600           25600        0   +0.0%
<sum>                179200          166400   -12800   -7.1%
<other>               25600           25600        0   +0.0%
<top>                204800          192000   -12800   -6.2%
base: reports/12/area_hier.rpt
noadd: reports/12/area_hier.rpt
```

`--instance GLOB` keeps the instances whose path matches. An instance
that a run lacks counts as 0 there, so its delta is its full value, and
its percent reads `gone`, or `new` when the first run lacks it:

```
$ edr compare base@g8 noadd@g8 --instances --metric area_um2 --depth 3 --instance 'i_top/u_sum/*'
area_um2 in um2 at depth 3
instance           base (pnr 12)  noadd (pnr 12)  Δ noadd      Δ %
i_top/u_sum/u_add          12800               -   -12800     gone
<sum>                      12800               0   -12800  -100.0%
<other>                   192000          192000        0    +0.0%
<top>                     204800          192000   -12800    -6.2%
```

A task metric needs `--task`. `--part` picks the part; without it, the
view takes the part that `top` names, the whole window here:

```
$ edr compare base@g8 noadd@g8 --instances --metric power_inst_w --task k_small --depth 2
power_inst_w in W at depth 2, task k_small, part WHOLE
instance            base  noadd  Δ noadd     Δ %
chip/i_top/u_core  0.125  0.125        0   +0.0%
chip/i_top/u_sum   0.062  0.048   -0.014  -22.6%
chip/i_top/u_vec   0.031  0.031        0   +0.0%
<sum>              0.218  0.204   -0.014   -6.4%
<other>            0.032  0.032        0   +0.0%
<top>               0.25  0.236   -0.014   -5.6%
```

When the runs have more than one instance metric, the command refuses
and names them, so that `--metric` picks one. It also refuses a task
that no run has and a part that a run lacks, and lists the ones there
are. `--csv` writes the instance column and one column per run, the
three rows at the end included, and `--json` adds the delta of each
row.

An area in um2 prints in gate equivalents with `--unit kGE` or
`--unit MGE`, in `edr compare` and in `edr metrics`, and `--json` and
`--csv` then hold the same numbers under that unit. Set the area of one
gate equivalent of your library in `edr.toml`; an export records it in
its manifest:

```toml
ge_um2 = 0.2
```

### How deep to store

Store depth 3 at every step: the top, the blocks and their main parts.
Each deeper level multiplies the rows, and most of the deep rows are the
cells of regular arrays that no number reads. A made-up design with 40
instances down to depth 3 and 3,000 below it, in 20 runs of 10 steps
each, stores 8,000 rows at depth 3 and 608,000 at depth 8.

A table stores its rows once per task and part, so it grows faster than
an area report. Let `where` keep only the parts that your tables read,
and lower `max_depth` for the levels you need only now and then: those
still come from the file on demand, as below.

A number from a deeper level comes from the report on demand. A
`--depth` deeper than the rows in the database reads the file that the
metric row of each run cites, from `data/results/`, at the step of
record. Say the four lanes of a lane bank sit at depth 5:

```
$ edr compare base@g8 noadd@g8 --instances --metric area_um2 --depth 5 --instance '*/u_lane_*' --unit kGE
area_um2 in kGE at depth 5
instance                             base (pnr 12)  noadd (pnr 12)  Δ noadd    Δ %
i_top/u_vec/u_bank/u_lanes/u_lane_0             32              32        0  +0.0%
i_top/u_vec/u_bank/u_lanes/u_lane_1             32              32        0  +0.0%
i_top/u_vec/u_bank/u_lanes/u_lane_2             32              32        0  +0.0%
i_top/u_vec/u_bank/u_lanes/u_lane_3             32              32        0  +0.0%
<sum>                                          128             128        0  +0.0%
<other>                                        896             832      -64  -7.1%
<top>                                         1024             960      -64  -6.2%
```

A table works the same way: a `--depth` deeper than its `max_depth`
reads the CSV, with every part, also the ones that `where` left out.

## Compare runs side by side

`edr compare A B` without `--instances` prints one row per task and metric:
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
ids after it. `--instances` names its columns the same way.

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
task. The source column names the table, or the file and line of each
time.

A step starts when the driver first sees its number. When the flow
writes the start of each step into a log, the stage's `step_log` reads
it from the collected files. With `{step}` in the file, each step has a
log of its own, and its first matching line starts it; `*` matches any
part of a name:

```toml
[stages.pnr]
step_log = { file = "reports/{step}/*.log", regex = 'STARTING STAGE .*\((\d+) sec\)' }
```

Group 1 is a unix time. Group 2, when present, is the step number. One
log of many steps, without `{step}`, counts its lines from the stage's
first step, so a stage that resumed from a checkpoint needs group 2
there. When the log and the driver both know a step, the log wins, since
a progress command that counts report directories sees each step one
step early.

A step ends when the next step of its stage starts, else when its stage
ends, else at the mtime of its own log file. A line of a later stage
never ends a step: in one log of two stages, the last step of the first
stage ends with its stage, or stays open when the run has no stage rows.
A stage without an end counts up to now while the run lives, and up to
its last heartbeat when its driver died, and so does its last step. A
time that counts up to now, or that has no end at all, is open, and the
total then reads as a lower bound that names it:

```
$ edr runtime a@demo
stage  step  what                         started            wall  source
pnr       -  attempt 1, running, open     2026-01-12 09:00  6h40m  stage_runs
pnr       4  cts                          2026-01-12 09:00  2h10m  reports/4/cts.log:4
pnr       5  route, open                  2026-01-12 11:10  4h30m  step_runs
total     -  at least, open: pnr 5 route  -                 6h40m  -
```

Step 4 comes from its log, which the watcher collected once step 5
began. Step 5 still runs, so its start comes from the driver. An imported
run has no stage rows, so its total sums its steps, and its last step
ends at the mtime of its own log.

`edr runtime --batch <b>`, or several handles, prints one row per run
with the wall time of each stage and the total, and an `open` column
when a run has an open time.

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

`edr hosts --history --batch <b>` puts the two together for one batch.
The window runs from the start of the batch's first run to the last
heartbeat of its last run, or to now while one of its runs lives. Each
host the batch ran on gets its line, and under it an indented line of
the batch's own use: the CPU of its runs in cores, their RSS and the
size of their trees, summed at each host sample and drawn on the scale
of the host. The gap between the two lines is the work of everything
else on the host.

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
the power parts (the metrics whose canonical name is `power__*`, without
`power__total`), and parallel
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
  task_fields.csv
  instances.csv
  flags.csv
  sources/<tag>/source.diff   the diff of each dirty source
  <label>/...                 the collected files of that run
```

| File | Holds |
|---|---|
| `manifest.json` | `producer`, `created`, `schema` (3), `project`, `sources`, `runs` (id, label, config, build tag, source, host, phase, and a `record`), `tables` with the row counts, `ge_um2` with the gate equivalent of the project or `null`, `dirty_sources` with the base, the nested commits and the sha256 of the diff of each dirty source, `flags` with the rows of `flags.csv`, `files` with path, size and sha256, `incomplete` with every exported run whose phase is not `done`, and `skipped` with the other runs of each label and source; an entry of `incomplete` or `skipped` holds the run id, label, source and phase |
| `runs.csv` | `run_id,label,config,build_tag,source,host,phase,started,ended,batch,dirty,tree_id,retired,flags`; `retired` is 1 for a run that was retired, or whose batch was, and `flags` names the checks that flag the run, separated by spaces |
| `parameters.csv` | `run_id,label,source,key,value,origin`, the rows of the `parameters` table for each exported run |
| `task_fields.csv` | `run_id,label,source,task,key,value,origin`, the rows of the `task_fields` table for each exported run |
| `flags.csv` | `run_id,label,source,task,check,text`, the rows of the `flags` table for each exported run; [Parameters and checks](#parameters-and-checks) explains them |
| `sources/<tag>/source.diff` | the copy of `data/sources/<tag>/source.diff` for each dirty source |
| `metrics.csv` | `run_id,label,config,build_tag,source,host,stage,step,task,metric,canonical,value,unit,source_file,record`; `build_tag` and `host` are those of the run, and `record` marks the [step of record](#stage-of-record) |
| `instances.csv` | `run_id,label,source,stage,step,task,metric,part,instance,depth,value,local,cells,unit`, the rows of the `instances` table for each exported run; join it to `metrics.csv` on run, stage, step, task and metric to keep the step of record |
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
there failed, stopped or has not ended. Read `flags` as well: a run listed
there may not be the build its label names.

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
instances, of the runtime per step side by side, or of the hosts.
Those stay with `edr compare`, `edr runtime` and the board.

## An analysis reads snapshots

An analysis should never read the live database, because the database
changes with every watcher cycle and a number you quote must stay the
number you read. Instead, the analysis keeps one snapshot per
source under its own `data/`, pinned by the source tag. Every table and
figure comes from `runs.csv`, `metrics.csv`, `parameters.csv`,
`task_fields.csv` and `instances.csv` of that snapshot:

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

## Tasks as data

A task group runs one command per task, and the fields of the task fill
its placeholders, such as `{task.kernel}`, `{task.test}` and
`{task.args}` of the demo's `tasks.toml`. The same fields say what a
number of the task means: two energies compare only when both kernels
did the same work, and the fields give that work.

Each run records the fields of each task as the task ran, in the
`task_fields` table, one row per run, task and key, with its origin:

| Origin | Written by |
|---|---|
| `spec` | the watcher, from the fields that `launch` wrote into the run's spec |
| `resolver` | `edr import`, from `tasks.toml` as it is at the import |
| `import` | `edr import --task-fields FILE`, for a task that ran with other fields |

`launch` resolves each task once, through its `[tasks.<id>]` table or
the `[pattern]` resolver, and writes the fields into the spec.
Extraction fills `{task.<key>}` in the `file` of a metric from these
fields, so an edit of `tasks.toml` after the launch changes neither the
fields of a run nor the files its metrics read.

An import resolves each task through `tasks.toml` as it is at the
import, and a task of an old run may have run with other fields under
the same id. Write the fields it ran with into a file in the form of
`tasks.toml`, and pass it with `--task-fields`:

```toml
# ran.toml: k_big ran with a limit that tasks.toml no longer gives
[tasks.k_big]
kernel = "softmax"
test = "SOFTMAX_R197"
args = "ROWS=197 LIMIT=-4.0"
```

```sh
edr import --run-id 20260830_0000_base_base_gabc1234 --label base --source abc1234 \
    --results /archive/base --tasks k_small k_big --task-fields ran.toml
```

`k_big` takes its table from the file, with the origin `import`.
`k_small` is not in the file and takes the fields of `tasks.toml`, with
the origin `resolver`. The directory of each task and the files of its
metrics follow from these fields. Only the `[tasks.<id>]` tables of the
file count, and a table of a task that `--tasks` does not name is
ignored, so the `tasks.toml` of an old commit also works as the file:
`git show <commit>:tasks.toml > ran.toml`.

A task id that ran with two sets of fields gives numbers that look
comparable and are not. `edr extract` warns about each such task of the
runs it reads, and `edr check` about every such task of the database,
with the fields that differ and the runs of each set:

```
warning: task k_big ran with 2 sets of fields: args="ROWS=197 LIMIT=-4.0" in base@imported; args="ROWS=197" in a@sweep1
```

The line names up to five runs of each set; `edr extract --json` lists
every run under `clashes`.

Give a task a new id when its fields change, so that each id names one
set of fields.

`edr export` writes the rows of each exported run to `task_fields.csv`.
The analysis joins that file to `metrics.csv` on `run_id` and `task`,
and computes a number per unit of work itself, such as the energy per
output of a GEMM of the demo:

```python
import csv

fields = {}
for r in csv.DictReader(open("task_fields.csv")):
    fields.setdefault((r["run_id"], r["task"]), {})[r["key"]] = r["value"]
for m in csv.DictReader(open("metrics.csv")):
    f = fields.get((m["run_id"], m["task"]), {})
    if m["metric"] == "energy_nj" and m["value"] and f.get("kernel") == "gemm":
        args = dict(kv.split("=") for kv in f["args"].split())
        print(m["label"], m["task"], float(m["value"]) * 1e3 / (int(args["M"]) * int(args["N"])), "pJ per output")
```

A field per number, such as `m = 64` next to `args`, saves the analysis
the parsing of `args`.

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
