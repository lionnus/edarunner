# Testing and CI

After this page you can run the suite, the driver tests on Python 3.6 and
the OpenROAD example, and read a CI result without the Actions UI.

## Run the tests

Install [uv](https://docs.astral.sh/uv/), then in the checkout:

```sh
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

CI measures the coverage and fails under 88 %:

```sh
.venv/bin/python -m pytest -q --cov=edarunner --cov-fail-under=88
```

A test writes under `tmp_path` only, uses the host `local` only, and
starts a driver only through `tests/helpers_driver.py`. No test reaches
`api.telegram.org`; `tests/conftest.py` refuses every call, and a test
that needs HTTP patches `urlopen` itself. A change to a module comes
with a test in `tests/test_<module>.py`.

`tests/helpers_backend.py` holds `FakeBackend`. It records every submit
and stop, and answers `alive` from a scripted list of states per handle.
A watcher test passes it as `backend=` to `watch.cycle`.

`tests/test_driver.py::test_compiles_on_py36` needs a Python 3.6. It
uses `EDR_DRIVER_PYTHON`, else `python3.6` on `PATH`, else
`/usr/bin/python3`, and skips without a 3.6.

`examples/openroad-gcd/run.sh` runs a real open flow under `edr` on the
head node; it needs the ORFS tools, and CI runs it in a container.

## The CI jobs

`.github/workflows/ci.yml` runs on every push and pull request.

| Job | Runs |
|---|---|
| `tests` | `pytest` with coverage on Python 3.11 and 3.12. The run fails under 88 %. The 3.11 run uploads `coverage.svg`. |
| `driver` | `tests/test_driver.py` in a `python:3.6` container, the floor of the compute hosts. The image's interpreter is linked to `/usr/bin/python3`, the fallback of `tests/helpers_driver.py` and of `test_compiles_on_py36`. |
| `openroad` | `examples/openroad-gcd/run.sh` in the `openroad/orfs` image: the GCD design through synth, floorplan and place under `edr`. About 2 min, 1 min of it the image pull. |
| `condor` | `tools/harness/condor.sh` with `RUNTIME=docker`: the `htcondor/mini` image as a one-machine pool, and the local demo through the `condor` backend with `submit_via = ["docker", "exec", "-u", "edr", "edr-mini"]`. It asserts that two runs end `done`, that the concurrency limit `fc` of one runs them one after the other, and that `edr stop` ends a third run `KILLED`. |
| `slurm` | `tools/harness/slurm.sh`: the `giovtorres/slurm-docker-cluster` compose setup with `Licenses=fc:1`, and the same demo and assertions through the `slurm` backend with `submit_via = ["docker", "exec", "slurmctld"]`. |
| `docs` | `tools/gen_docs.py --check`: the pages under `docs/reference/` must equal what the code generates. |
| `status` | after every other job, also after a failure: writes the outcome into the branch `ci-status`. |

## The scheduler harness

`tests/test_schedulers.py` needs no scheduler. It compares the rendered
submit file, `sbatch` script and `bsub` argv with the files in
`tests/golden/`, and puts shims of `condor_submit`, `condor_q`,
`condor_rm`, `sbatch`, `squeue`, `sacct`, `scancel`, `bsub`, `bjobs` and
`bkill` on an otherwise empty `PATH` to check the argv and the parse of
their output. The real schedulers run in containers under
`tools/harness/`; its README says how to run them on a desk machine.

## The ci-status branch

`status` force-pushes an orphan branch `ci-status` with `GITHUB_TOKEN`.
The branch has one commit and holds these files.

| File | Holds |
|---|---|
| `status.json` | the commit, the run id and url, the timestamp, the result and the steps of every job, and the conclusion |
| `coverage.svg` | the badge of the `tests` job |
| `openroad/` | `run.log`, `metrics.csv` and `files.txt` of the OpenROAD run |
| `logs/` | the log of every job, from the Actions API |

Read a result without the Actions UI:

```sh
git fetch origin ci-status
git show origin/ci-status:status.json
```

Wait for the run of the checked-out commit; a run takes several minutes:

```sh
until git fetch -q origin ci-status && git show origin/ci-status:status.json | grep -q "$(git rev-parse HEAD)"; do sleep 60; done
```

Every branch writes the same branch, so `ci-status` shows the newest run
of the repository, not the newest run of one branch.

## The documentation site

`.github/workflows/pages.yml` builds the pages under `docs/` with MkDocs
Material. On a pull request it only builds, and a broken link or anchor
fails the job. On a push to `main` it also deploys the site to
<https://lionnus.github.io/edarunner/>. The navigation follows
`docs/.pages` and `docs/dev/.pages`; MkDocs shows `README.md` as the index
of a directory.

Build the site locally from the repository root:

```sh
uv run --extra docs mkdocs build --strict   # writes site/
uv run --extra docs mkdocs serve            # serves it at http://127.0.0.1:8000
```
