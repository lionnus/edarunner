# Changelog

## 0.1.0 (unreleased)

The first release.

- `edr`, the controller, with the sixteen verbs of `docs/design.md`
  section 10.
- `edr_driver.py`, the one-file driver for Python 3.6 or newer: stages,
  task groups with a shared queue, licence gates, budgets, retries, the
  heartbeat, the stop and keep files.
- `edr watch`: the classifier of section 7, collection, metric extraction,
  one resume of a dead run, the boards, and the Telegram bot.
- The SQLite ledger, `edr export` with a manifest, and
  `examples/local-demo`.
