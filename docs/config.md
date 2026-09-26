# Configuration files

`config.py` reads four TOML files with `tomllib`. An unknown key, a
missing required key, or a broken reference raises `ConfigError` with the
file and the key path. Required keys have no default in the tables below.

Rules that apply to every file:

- A path is absolute, or relative to the file that names it. `~` expands.
- A string may hold `{placeholders}` (section 5). `${VAR}` belongs to the
  shell and stays as it is.
- Table order is file order. The stages run in the order of `edr.toml`.
- A hook is `python:<file>:<function>`; `<file>` is relative to the
  project directory. `load_hook` also accepts `<file>:<function>`.

## 1. edr.toml

| Key | Meaning | Default |
|---|---|---|
| `schema` | file format version; only `1` | `1` |
| `project` | project name; `{project}` | required |
| `site` | path of `site.toml`; `load_project(..., site_path)` replaces it | required unless given |
| `state` | shared state directory | `"~/.edr/{project}"` |
| `data` | head node data directory | `"data"` |
| `run_prefix` | run tree prefix under the host scratch; rendered at `plan` | `"{user}/edr/{project}"` |

### [source]

| Key | Meaning | Default |
|---|---|---|
| `repo` | the git repository | required |
| `worktrees` | where `edr stage` adds worktrees | required |
| `ref` | default ref for `edr stage` | `"HEAD"` |
| `nested` | nested repositories, pinned by their HEAD | `[]` |
| `run_id` | run id template | `"{date}_{label}_{build_tag}_g{src}"` |
| `build_tag` | hook that returns the build tag | `""` (`{config}[_{overrides}]`) |

### [env]

Variables the flow needs in every command of every stage, on top of the
site `env`. Values take the run placeholders (`{root}`, `{build_tag}`) and
`$VAR` expands on the host. Example: `PATH = "{root}/.venv/bin:$PATH"` for a
flow that calls `python` from the venv of the tree.

### [sync]

| Key | Meaning | Default |
|---|---|---|
| `exclude` | rsync exclude patterns | `[]` |
| `after` | command on the head node after the sync | `""` |

### [safety]

| Key | Meaning | Default |
|---|---|---|
| `marker` | substring every delete target must hold | `"/edr/"` |
| `min_depth` | minimum path depth of a delete target | `4` |

### [limits]

| Key | Meaning | Default |
|---|---|---|
| `stagger_s` | pause between two launches | `120` |
| `stale_s` | heartbeat age that marks a run stale | `600` |
| `dead_s` | heartbeat age that marks a run dead | `2700` |
| `hung_s` | time without progress that marks a run hung | `21600` |
| `grace_s` | wait before a kill | `3600` |
| `host_free_min_gb` | host free space below which nothing new starts | `100.0` |
| `streak` | equal failure signatures in a row that stop a group | `3` |
| `heartbeat_s` | heartbeat and watch cycle period | `60` |
| `gate_max_s` | maximum wait at a licence gate | `14400` |
| `kill_hung` | the watcher kills a hung run after `grace_s` | `false` |
| `kill_orphan` | the watcher kills an orphan tool process after `grace_s` | `false` |

### [placement]

| Key | Meaning | Default |
|---|---|---|
| `max_per_host` | runs per host | `2` |
| `min_free_cores` | free cores a host needs | `16` |
| `min_free_ram_gb` | free RAM a host needs | `60` |
| `avoid` | hosts never used by `auto` | `[]` |
| `prefer` | hosts tried first by `auto` | `[]` |

### [stages.<name>]

| Key | Meaning | Default |
|---|---|---|
| `after` | `""` (first), a stage name, or `{ stage = "pnr", step = "route" }`; the stage must exist | `""` |
| `cmd` | the command; runs once per task in a group | required |
| `resume` | the command with `{checkpoint}` for a resume | `""` |
| `cwd` | working directory, relative to the run tree | `"."` |
| `steps` | step names the flow passes | `[]` |
| `progress` | command that prints the current step number | `""` |
| `needs` | `{ cores, disk_gb, licence }`; `licence` is a name or `{ name = seats }` and must exist in the site | `{ cores = 1, disk_gb = 0.0 }` |
| `budget` | `{ hours, disk_gb, kill, per }`; `per` is `"stage"` or `"task"` | `{ kill = false, per = "stage" }` |
| `retry` | `{ match, wait_s, max }`; `match` is required | none |
| `collect` | paths the watcher copies | `[]` |
| `collect_on_request` | named path sets for `run --collect` | `{}` |
| `prune` | named path sets for `retire --prune` | `{}` |
| `foreach` | `"tasks"` makes a task group | `""` |
| `parallel` | tasks at once in a group | `1` |
| `prepare` | command once before a group | `""` |
| `task_dir` | task directory template; required in a group | `""` |
| `after_each` | command after each task | `""` |

### [metrics.<name>]

| Key | Meaning | Default |
|---|---|---|
| `stage` | a stage name or a list; every name must exist | `[]` (only with `expr`) |
| `step` | `"*"`, a number, or absent | none |
| `file` | file to read; required unless `expr` | `""` |
| `regex` | group 1 is the value | one of five |
| `csv` | `{ where = { col = value }, column }` | one of five |
| `json` | dotted path into the file | one of five |
| `python` | hook that gets the file path and returns a float | one of five |
| `expr` | expression over other metrics of the same run, stage, step and task | one of five |
| `unit` | unit text | `""` |
| `canonical` | name shared across projects | `""` |

A metric holds exactly one of `regex`, `csv`, `json`, `python`, `expr`.

## 2. site.toml

| Key | Meaning | Default |
|---|---|---|
| `schema` | only `1` | `1` |
| `scratch` | scratch roots, tried in order | required |
| `env` | environment for every command on a host | `{}` |
| `ssh` | `{ options, timeout_s }` | `{ options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"], timeout_s = 45 }` |
| `tool_procs` | regex of tool process names for the orphan check | `""` |
| `nfs_export` | export path used when ssh fails at collect | `""` |

### [hosts.<name>]

| Key | Meaning | Default |
|---|---|---|
| `cores` | cores | required |
| `ram_gb` | RAM | required |
| `scratch` | host-specific scratch roots | site `scratch` |

The host `local` is the head node without ssh.

### [licences.<name>]

| Key | Meaning | Default |
|---|---|---|
| `feature` | FlexLM feature name | required |
| `floor` | free seats to keep | required |
| `probe` | command that prints `lmstat` output | required |
| `seats_per_task` | seats one task takes | `1` |

### [telegram]

| Key | Meaning | Default |
|---|---|---|
| `token_file` | bot token, mode 600 | `"~/.config/edarunner/telegram.token"` |
| `chat_id` | the one chat the bot answers | required |

### [telegram.commands.<name>]

| Key | Meaning | Default |
|---|---|---|
| `help` | help text | required |
| `run` | argv list, never a shell string | required |
| `args` | argument name to regex | `{}` |
| `skip_if` | argv; a zero exit skips `run` | none |
| `skip_reply` | reply when skipped | `""` |
| `reply` | reply after `run` | `""` |
| `detach` | do not wait for `run` | `false` |
| `timeout_s` | kill `run` after this | `60` |
| `cwd` | working directory of `run` | `""` |
| `dry_run` | log the command, do not run it | `false` |

## 3. tasks.toml

Optional. `load_project` reads it when it exists.

| Key | Meaning | Default |
|---|---|---|
| `tasks.<id>.<key>` | any key; each is `{task.<key>}` in the stage strings | none |
| `tasks.<id>.needs` | `{ cores, disk_gb, licence }`; overrides the stage's | none |
| `tasks.<id>.budget` | `{ hours, disk_gb, kill, per }`; overrides the stage's | none |
| `pattern.resolver` | hook `id -> table` for ids the file does not list | `""` |

`resolve_task(project, id)` looks in the table first, then calls the
resolver. A resolver that returns nothing makes `ConfigError`.

## 4. jobs/<batch>.toml

| Key | Meaning | Default |
|---|---|---|
| `batch` | batch name | the file stem |
| `source` | a short hash from `edr stage`, or `<hash>-dirty-<8 hex>` | required |

### [[job]]

| Key | Meaning | Default |
|---|---|---|
| `label` | run label; unique in the batch | required |
| `config` | hardware configuration | required |
| `host` | a host name or `"auto"` | `"auto"` |
| `stages` | stage names to run; every name must exist | `[]` (every stage) |
| `tasks` | task ids for the task groups | `[]` |
| `overrides` | `KEY = VALUE`; values become text | `{}` |
| `netlist_stage` | stage number of the netlist to collect | none |
| `reuse` | `{ run_id }`, or `{ label, latest = true }`; a glob label is an error | none |

`load_batch(project, "demo")` reads `jobs/demo.toml`; a path that ends
in `.toml` is read as given, relative to the project directory.

## 5. Placeholders

`placeholders(project, **extra)` returns `project`, `project_root`,
`site_dir`, `user` and every item of `extra`. A dict value flattens to
dotted keys, so `task=task.fields` gives `{task.kernel}`. The dict
`overrides` renders as `KEY=VALUE` tokens separated by spaces.

`render(template, values)` fills `{name}` and `{a.b}`. A name that
`values` does not hold raises `ConfigError` that names the placeholder.
`load_project` renders `site`, `state`, `data`, `source.repo` and
`source.worktrees` at load time with `project`, `project_root`, `user`
and `site_dir`. Every other string keeps its placeholders for `plan`.

`{tree_id}`: the run id of the tree the flow writes in (the reused run's id under `reuse`, else `{run_id}`).
