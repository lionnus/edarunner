# Changelog

## 0.1.0 (unreleased)

The first release.

- `edr`, the controller, with the seventeen verbs of `docs/design.md`
  section 10, among them `import` for a tree that edr did not make.
- `edr_driver.py`, the one-file driver for Python 3.6 or newer: stages,
  task groups with a shared queue, licence gates, budgets, retries, the
  heartbeat, and the stop and keep files.
- `edr watch`: the classifier of section 7, collection, metric extraction,
  one resume of a dead run, the boards, and the Telegram bot.
- The SQLite ledger, `edr export` with a manifest, and
  `examples/local-demo`.
- A project `[env]` table rendered per run, and the `{tree_id}`
  placeholder that a chain of reuse keeps. Both came out of the first run
  on real hosts: a flow needs the venv of its tree on `PATH`, and a reused
  tree keeps its own run directory.
