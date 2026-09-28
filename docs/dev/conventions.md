# Conventions

This page lists the rules every change follows, where each thing is
documented, and how to cut a release. `CONTRIBUTING.md` at the repository
root explains how to report a bug, write a commit message and open a pull
request.

## Rules for every change

- Every `rm -rf` and every `rsync --delete` calls `assert_safe_target`
  first, and every path built from a run id calls `assert_run_id`.
  [how-it-works.md](../how-it-works.md#launch) lists what the guard refuses.
- A dry run writes nothing: no date pin, no spec, no file on a host, no
  database row, no event, not even the database file.
  `tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` proves it
  for the whole flow.
- A read command creates nothing.
- The watcher never deletes a run tree. A budget stops or kills a run,
  and the only files the watcher removes are expired seat leases.
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

When a change alters behaviour, update the page under `docs/` that
describes it; `docs/README.md` says which page holds what. For the pages:

- Write plain, natural English for an engineer who is new to the tool.
- Say what the tool does and which code enforces it.
- Link to the page that holds a detail instead of repeating it.
- Keep host names, user names and other site details out of the pages.

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

1. Rename the `Unreleased` heading at the top of `CHANGELOG.md` to
   `## X.Y.Z (YYYY-MM-DD)`.
2. Set `version` in `pyproject.toml` and `__version__` in
   `src/edarunner/__init__.py` to the same value.

Merge `devel` into `main` once CI is green, tag `main` as `vX.Y.Z`, and
push the tag. The site deploys from `main`.
