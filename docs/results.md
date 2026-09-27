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

## A number the flow does not print

A metric has one of four parsers: `regex`, `csv`, `json` or `python`. A
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

## Compare runs

Every watcher cycle writes `data/board/`:

| File | Holds |
|---|---|
| `board.json` | the runs of the live batches, the last 50 events and the host probes, for a script |
| `status.html` | a phone-width page: every run in board order, the last 50 events, the hosts |
| `compare.html` | the runs with their parameters as columns, a compare table of the final metrics with the difference to the first ticked run, and four plots |

`compare.html` is one self-contained page over the database's runs, parameters
and metrics. Its tables work as they are. The plots (a metric over the
steps, a scatter of any two columns, the power phases, parallel
coordinates) need Plotly. The page loads `data/board/plotly.min.js` when
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
| `manifest.json` | `producer`, `created`, `schema`, `project`, `source`, `runs` (id, label, config, build tag, source, host, phase), `tables` with the row counts, `files` with path, size and sha256, `incomplete` with the runs still live or with a failed task |
| `runs.csv` | `run_id,label,config,build_tag,design,host,phase,started,ended` |
| `metrics.csv` | `run_id,label,config,design,stage,step,task,metric,canonical,value,unit,source` |
| `<label>/` | `data/results/<run_id>/` of that run, without `log/` and `*.log` unless `--with-logs` |

The directory is written under a temporary name and renamed at the end,
so a reader never sees a half snapshot. A `--out` that exists and is not
empty is refused. `--dry-run` lists the files.
[reference/cli.md](reference/cli.md) lists every flag of `metrics` and
`export`.

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
