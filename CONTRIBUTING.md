# Contributing

edarunner welcomes bug reports, questions and pull requests. Read this page
before you open a pull request.

## Report a bug or ask a question

Open an issue at https://github.com/lionnus/edarunner/issues for a bug.
Give the `edr` version, the Python version, the command, and its output
with `--json`. Remove every host name, user name and token first.

Ask a question in an issue as well. If it cannot be public, write to
Lionnus Kesting, lkesting@iis.ee.ethz.ch.

## Propose a change

Open an issue before a large change, so we can agree on the approach. A
small fix can go straight to a pull request.

[docs/dev/architecture.md](docs/dev/architecture.md) explains which module
does what, and [docs/dev/conventions.md](docs/dev/conventions.md) lists the
rules every change follows and how to document it. When a change alters
behaviour, update the page under `docs/` that describes that behaviour.

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

`tests/test_driver.py::test_compiles_on_py36` skips unless
`EDR_DRIVER_PYTHON`, or else `python3.6` on `PATH`, is a Python 3.6. CI runs the driver tests against
the Python 3.6 of a `python:3.6` container, and the OpenROAD example
`examples/openroad-gcd` in the `openroad/orfs` image.
[docs/dev/testing.md](docs/dev/testing.md) describes each job.

Add a test for a change to a module in `tests/test_<module>.py`.

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

Don't add co-author or tool trailers. You are responsible for every change
you submit, whatever tools helped you write it.

## Pull requests

Open the pull request against the `devel` branch. CI must pass;
[docs/dev/testing.md](docs/dev/testing.md) lists the jobs, including the
strict docs build.

Keep the description short: what the change does and why. Leave out
trailers there too.

## Licence

edarunner is under the Apache-2.0 licence, and so is every contribution.
See [LICENSE](LICENSE).
