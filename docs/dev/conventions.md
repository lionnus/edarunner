# Conventions

After this page you know the rules every change keeps, where a thing is
documented, and how a release is cut. `CONTRIBUTING.md` at the repository
root says how to report a bug, write a commit message and open a pull
request.

## Rules every change keeps

- Every `rm -rf` and every `rsync --delete` calls `assert_safe_target`
  first, and every path built from a run id calls `assert_run_id`.
  [guarantees.md](../guarantees.md) lists what the guard refuses.
- A dry run writes nothing: no date pin, no spec, no file on a host, no
  database row, no event, not even the database file.
  `tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` proves it
  for the whole flow.
- A read command creates nothing.
- The watcher never deletes. A budget stops or kills; it never removes
  a file.
- A stop signals the pids the driver recorded, never a session name or
  a process pattern.
- A loop that continues after a failure counts what it skipped and
  reports the count at the end. A run with a failed task ends
  `INCOMPLETE`, never `done`.
- The controller uses the standard library and `rich` only. A new
  dependency needs a reason that the standard library cannot meet.
- The driver stays on the Python 3.6 subset, uses the standard library
  only, and never imports the package. A feature that subset cannot
  express belongs in the controller.
- No site string goes into the repository: no host name, licence server,
  user name, chat id, token or unpublished design name. Fixtures and
  pages use `local`, `hostA`, `user`, `demo` and `k_small`, and made-up
  numbers.
- Every function has type hints. Every public function has a one-line
  docstring.
- A comment gives a reason that the code cannot show. It never tells
  what the code was before.

A change that moves a responsibility updates
[architecture.md](architecture.md) in the same commit. A change to the
spec, the heartbeat or a phase updates [driver.md](driver.md).

## Documentation

`docs/reference/` comes from the code;
[reference/README.md](../reference/README.md) names the source of each
page. Document a new command, flag, config key, state or bot command
where you define it, then run `uv run tools/gen_docs.py` before the
commit. CI refuses a stale page.

A change in behaviour updates the page under `docs/` that describes it.
`docs/README.md` says which page holds what. The pages keep these rules:

- A page opens with what the reader can do after it, in one or two
  sentences, then the content.
- No page repeats another. A page links to the one that holds the
  detail. A page under 40 lines merges into its neighbour.
- A page says what the tool guarantees and which code holds it. It tells
  no story of a failure on one farm.
- A result is a report, a notebook, a dashboard or a paper. No page
  assumes one of them.
- Short sentences, active voice, one instruction per sentence, no site
  string, no em dash, no middle dot.

A diagram under `docs/diagrams/` is one hand-written SVG in Helvetica or
Arial. The palette is green `#008000` for a running process, black
`#0d1117` on white, `#e6edf3` on `#0d1117` in dark mode, and grey
`#6e7781` for notes and arrows. A command is a box; a data store is a
cylinder. Render it and look at it after every edit:

```sh
inkscape docs/diagrams/<name>.svg --export-type=png --export-filename=/tmp/<name>.png --export-width=1400
```

## Releases

A release is one commit, `:bookmark: Release X.Y.Z`, on `devel`:

1. Rename the heading `## Unreleased` at the top of `CHANGELOG.md` to
   `## X.Y.Z (YYYY-MM-DD)`.
2. Set `version` in `pyproject.toml` and `__version__` in
   `src/edarunner/__init__.py` to the same value.
3. Remove every alias that the previous release marked `gone in the
   next release`, such as an old command name in `cli.py` or
   `ALIASES` in `notify/telegram/commands.py`.

Merge `devel` into `main` once CI is green, tag `main` as `vX.Y.Z`, and
push the tag. The site deploys from `main`.
