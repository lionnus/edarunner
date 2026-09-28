# Documentation

edarunner runs the flow you already have on the machines you already use,
over ssh or through a batch scheduler, and keeps all your runs in one
database. This index tells you which page to open. The same pages are
published at <https://lionnus.github.io/edarunner/>.

## Use the tool

| Page | Read it to |
|---|---|
| [install.md](install.md) | install `edr`, run a real OpenROAD flow on one machine, and see what it prints |
| [how-it-works.md](how-it-works.md) | follow one run across the head node, the shared filesystem and a host, and learn what edr does on its own and what it never does |
| [guides/project.md](guides/project.md) | describe your flow: where the configuration lives, stages, metrics, tasks, the environment and the runtime step |
| [guides/site.md](guides/site.md) | describe your machines: hosts, tools and licence seats, a scheduler, and what a host needs |
| [guides/run.md](guides/run.md) | write a batch, launch it, read the board, keep a watcher behind it and act on one run |
| [guides/projects.md](guides/projects.md) | run several projects on the same machines under one supervisor, and share them with a second user |
| [guides/debug.md](guides/debug.md) | find out why a run failed, died or waits |
| [guides/results.md](guides/results.md) | get the numbers out: metrics, compare, runtime, export and MLflow |
| [guides/alerts.md](guides/alerts.md) | get alerts on Telegram, ntfy or mail, and use the bot from the phone |
| [guides/agents.md](guides/agents.md) | let a Claude session or a script operate the farm through `edr` |
| [guides/cleanup.md](guides/cleanup.md) | retire runs and batches, prune trees, and keep the large files |
| [reference/](reference/README.md) | look up a command, a flag, a config key, a placeholder, a run state or a bot command; generated from the code |

## Improve the tool

| Page | Read it to |
|---|---|
| [dev/architecture.md](dev/architecture.md) | find the module that owns a behaviour and follow the data from `plan` to `export` |
| [dev/driver.md](dev/driver.md) | change the driver: the spec, the heartbeat, the stop file, the task queue |
| [dev/testing.md](dev/testing.md) | run the tests, and read what CI runs and where it writes its result |
| [dev/conventions.md](dev/conventions.md) | learn the rules every change follows, document a change, and cut a release |

`CONTRIBUTING.md` at the repository root explains how to report a bug and
open a pull request, and `AGENTS.md` tells an agent how to operate a farm
through `edr`.
