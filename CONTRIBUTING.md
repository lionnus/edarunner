# Contributing

edarunner welcomes bug reports, questions and pull requests. Read this page
before you open a pull request.

## Report a bug or ask a question

Open an issue at https://github.com/lionnus/edarunner/issues for a bug.
Give the `edr` version, the Python version, the command, and its output
with `--json`. Remove every host name, user name and token first.

For a question, write to Lionnus Kesting, user@example.com.

## Propose a change

Open an issue before a large change, so we can agree on the approach. A
small fix can go straight to a pull request.

`docs/architecture.md` names the modules and the rules every change keeps.
A change that moves a responsibility updates that page in the same commit.
A change in behaviour updates the page under `docs/` that describes it.

## Set up a development environment

Install [uv](https://docs.astral.sh/uv/), then run in the checkout:

```sh
uv venv .venv && uv pip install -e '.[dev]'
```

The controller needs Python 3.11 or newer.

## Run the tests

```sh
.venv/bin/python -m pytest -q
```

CI measures the coverage and fails under 88 %:

```sh
.venv/bin/python -m pytest -q --cov=edarunner --cov-fail-under=88
```

`tests/test_driver.py::test_compiles_on_py36` needs a Python 3.6. It uses
`EDR_DRIVER_PYTHON`, else `python3.6` on `PATH`, else `/usr/bin/python3`,
and skips without a 3.6. CI runs the driver tests in a `python:3.6`
container. CI also runs the OpenROAD example `examples/openroad-gcd` in a
container. `docs/ci.md` describes each job.

A change to a module comes with a test in `tests/test_<module>.py`.

## Code rules

- The controller uses the standard library and `rich` only. A new
  dependency needs a reason that the standard library cannot meet.
- The driver `src/edarunner/driver/edr_driver.py` runs on the hosts with
  their own `python3`. It stays on the Python 3.6 subset, uses the
  standard library only, and never imports the package.
- No site string goes into the repository: no host name, licence server,
  user name, chat id, token or unpublished design name. Fixtures use
  `local`, `hostA`, `user`, `demo` and `k_small`, and fake numbers.
- A test writes under `tmp_path` only and uses the host `local` only. It
  starts a driver only through `tests/helpers_driver.py`.
- Every `rm -rf` and every `rsync --delete` calls `assert_safe_target`
  first.
- A dry run writes nothing.
  `tests/test_e2e_local.py::test_dry_run_flow_writes_nothing` checks this.
- Every function has type hints. Every public function has a one-line
  docstring.
- Write minimal comments. A comment gives a reason that the code cannot
  show. It never tells what the code was before.

Write documentation, comments and commit messages in plain technical
English: short sentences, active voice, one instruction per sentence.

## Commit messages

Write one line with a shortcode from
[chipmoji](https://github.com/lionnus/chipmoji) first, and no body:

```
<shortcode> <scope>: <message>
```

The scope is the module or the doc page. Examples from the history:

```
:sparkles: hosts: Mark each resource and sort the hosts by the worst mark
:bug: watch: Extract metrics even when the collect copied no new file
:white_check_mark: tests: Drop the last user name from a test path
:memo: docs: Describe the host marks and the [marks] table
:wrench: config: Add the [marks] thresholds to the site and project files
```

Say what the change does, not how you got there. Keep each commit atomic,
and run the tests green before each one.

A commit carries no AI co-author trailer and no session link. A change
that you make with an AI assistant is your own change. Describe it in the
message like any other.

## Pull requests

Open the pull request against the branch `devel`. CI must be
green: the tests on Python 3.11 and 3.12, the driver on 3.6, the OpenROAD
example, and the docs build once it exists.

Write a short body that says what the change does and why. Put no
trailer in the body.

## Licence

edarunner is under the Apache-2.0 licence. A contribution is under the
same licence. See `LICENSE`.
