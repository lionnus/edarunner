# Documentation

edarunner runs a flow you already have on hosts you reach over ssh, and
keeps one database of every run. This index says which page answers
what. The same pages are published at <https://lionnus.github.io/edarunner/>.

## Use the tool

| Page | Read it to |
|---|---|
| [install.md](install.md) | install `edr`, run a real OpenROAD flow on one machine, find the complete setup, try the tool-free demo, and see what a host needs |
| [concepts.md](concepts.md) | know what a project, a stage, a run, a batch, a host, the database, the watcher and a snapshot are |
| [configure.md](configure.md) | turn your own flow into a project: the project file, the site file, a batch |
| [run.md](run.md) | launch a batch, read the board, run the watcher, resume, and clear the hosts |
| [results.md](results.md) | get the numbers out: the database, `edr metrics`, the compare board, `edr export` and what reads a snapshot |
| [notify.md](notify.md) | get the alerts by Telegram, ntfy or mail |
| [telegram.md](telegram.md) | get the alerts and the board on your phone, and act on a run from there |
| [guarantees.md](guarantees.md) | know what `edr` never does, what a dry run and a guard promise, and how a stop works |
| [reference/](reference/README.md) | look up a command, a flag, a config key, a placeholder, a run state or a bot command; generated from the code |

## Improve the tool

| Page | Read it to |
|---|---|
| [dev/architecture.md](dev/architecture.md) | find the module that owns a behaviour and follow the data from `plan` to `export` |
| [dev/driver.md](dev/driver.md) | change the driver: the spec, the heartbeat, the stop file, the task queue |
| [dev/testing.md](dev/testing.md) | run the tests, and read what CI runs and where it writes its result |
| [dev/conventions.md](dev/conventions.md) | keep the rules every change keeps, document a change, and cut a release |

`CONTRIBUTING.md` at the repository root says how to report a bug and
open a pull request. `AGENTS.md` tells an agent how to operate a farm
through `edr`.
