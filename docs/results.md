# Results

The run database is one SQLite file, `data/edr.db`, on the head node. Every
run, every number and every action lands there. `edr export` writes a
frozen snapshot of one design from it for a paper.

## The tables

| Table | One row per | Holds |
|---|---|---|
| `batches` | batch | the project, the source tag, the pinned date, when it was retired |
| `runs` | run | identity (label, config, build tag, source tag, dirty flag), host and root, phase, state, stage and step, exit, times, disk figures, task counts, `tree_id` |
| `stage_runs` | stage or task attempt of a run | status, start and end, exit, failure signature, log path |
| `parameters` | key of a run | `config`, `build_tag`, `src` and each override, as text |
| `metrics` | number | run, stage, step, task, name, canonical name, value, unit, the source file, when it was extracted |
| `artifacts` | collected file | path under `data/results/<run_id>/`, size, when, class (`always` or the `collect_on_request` name) |
| `events` | action | time, actor (`user`, `watch`, `telegram`), run, kind, text with the `--why` |
| `store` | key | one JSON value per key, a small key-value store: the watcher's `progress` and `notified`, the `last_board` row order for `#n`, and the `telegram` message ids |

`data/results/<run_id>/` holds the collected files in the layout of the
run tree, so a metric's `source_file` is a path under it.

Read the tables with `sqlite3 data/edr.db` when a command does not answer
the question. Only the head node opens the file; a read command without a
the file reads an empty database in memory and creates nothing.

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

`docs/reference/cli.md` lists every flag of `metrics` and `export`.

The directory is written under a temporary name and renamed at the end,
so a reader never sees a half snapshot. A `--out` that exists and is not
empty is refused. `--dry-run` lists the files.

## A paper reads snapshots

A paper repository never reads the database. It keeps one snapshot per
design under its own `data/`, pinned by the source tag, and builds every
table and figure from `runs.csv` and `metrics.csv`:

```
paper/
  data/3f9a2c1/           an export, copied as is
  data/7c0d9e2/
  Makefile                reads data/<pin>/metrics.csv; the pin is the tag
```

The tag in the directory name is the commit the numbers came from, and
the manifest's sha256 per file lets a `make check` prove the copy is the
one that was exported. A new design version is a new export in a new
directory, never a change to an old one, so a caption that names the
tag stays true.

## What goes in git

In the project repository: `edr.toml`, `tasks.toml`, `jobs/`, `hooks/`,
and `edr-watch.service`. Not in git: `data/` (the database, the results,
the boards), the state directory, the worktrees, the run trees on the
hosts, `site.toml` and the Telegram token. Put `data/` in the project's
`.gitignore`.

In the paper repository: the snapshots, small by design. A large
collected file, a VCD or a full netlist, belongs to the results
directory on the head node, not to a snapshot; leave it out of `collect`
and fetch it with `collect_on_request` when you need it.
