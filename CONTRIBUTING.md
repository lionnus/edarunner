# Contributing

## Where things live

`docs/architecture.md` names the modules, what each owns, and the rules
every change keeps. A change that moves a responsibility updates that
page in the same commit. The user pages under `docs/` describe the
behaviour; a change in behaviour changes the page that describes it.

## Standard library, plus rich in the controller

The controller needs Python 3.11 or newer, the standard library and
`rich` for the tables on a terminal. The driver stays standard library
only. `pytest` is the only development dependency, and `matplotlib` the
only optional extra, for plots. A pull request that adds a dependency
needs a reason the standard library cannot meet.

## The driver rule

`src/edarunner/driver/edr_driver.py` runs on the compute hosts with their
own `python3`, which is 3.6 on some of them. So the file:

- is one file and never imports the package
- uses the Python 3.6 subset: no walrus, no dataclasses, no f-string `=`,
  no `from __future__ import annotations`, no `capture_output`,
  `subprocess.run` with `stdout=PIPE`, type comments instead of
  annotations
- keeps working on every newer Python

`tests/test_driver.py::test_compiles_on_py36` compiles it with
`EDR_DRIVER_PYTHON`, else `python3.6` on `PATH`, else `/usr/bin/python3`,
and skips unless that interpreter is 3.6. CI runs the driver tests in a
`python:3.6` container. A feature that the 3.6 subset cannot express
belongs in the controller, not in the driver.

## Tests

```sh
uv sync --extra dev
uv run pytest -q
```

or, with the virtual environment of the checkout:

```sh
.venv/bin/python -m pytest -q
```

Rules for a test:

- It writes under `tmp_path` only, and never touches `~/.edr`,
  `~/.config/edarunner`, `/tmp/edr-demo` or a real scratch.
- It uses the host `local` only and never opens an ssh connection.
- It starts no driver except through `tests/helpers_driver.py`, which runs
  the driver as a subprocess of `/usr/bin/python3` under `tmp_path`.
- It builds on `examples/local-demo`. The `demo` fixture in
  `tests/test_cli.py` copies the demo to `tmp_path`, points `HOME` and the
  scratch there, and changes into the copy.
- A guard test proves that a dry run leaves the state directory unchanged:
  `tests/test_e2e_local.py::test_dry_run_flow_writes_nothing`.

A change to a module comes with a test in `tests/test_<module>.py`.

## Fixtures with fake numbers and fake names

The demo flow sleeps for seconds and writes fake reports. Its numbers are
made up: an area of `1000 + 10.5 * step`, a slack of `-0.0<step>`, a power
of 0.250 W, a window of 3400 ns. Keep it that way. A fixture never carries
a number from a real design, a real host name, a real user name or a real
licence server. Use `local`, `hostA`, `user`, `demo` and `k_small`.

## No site strings in the public repository

The repository is public, so these never go into a commit:

- host names of a site, licence server addresses, FlexLM ports
- user names, home directories, chat ids, tokens
- design names, configuration names and numbers of unpublished work

The `site.toml` of a real site lives outside the project, by default at
`~/.config/edarunner/site.toml`, and the Telegram token in
`~/.config/edarunner/telegram.token`. `examples/local-demo/site.toml`
holds the host `local` only. Grep the diff for the names of your site
before you push.

## Style

- Type hints on every function, and a one-line docstring on every public
  function.
- A comment says why, never what; the code and the names say what. No
  comment describes what the code was before.
- Documentation, comments and commit messages in plain technical English:
  short sentences, active voice, one instruction per sentence.

## Commit messages

One line, with a chipmoji shortcode first, from
https://github.com/lionnus/chipmoji:

```
<shortcode> [scope][:] <message>
```

```
:bug: driver: Count a skipped task in the terminal phase
:white_check_mark: watch: Test the host_full grace clock
:memo: Add the driver document
:construction_worker: ci: Run the driver tests on python:3.6
```

No body. Say what the change does, not what you did to get there.
