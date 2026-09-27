# The watcher

`edr watch` runs on the head node, one process per project. It reads the
heartbeats, classifies every run, collects the results, extracts the
metrics, starts queued jobs and sends the alerts. It never deletes a
file or a tree. `docs/getting-started.md` shows how to run it as a
service.

## The cycle

One cycle runs every `heartbeat_s` seconds. The config is reloaded at
the start of each cycle, so an edit to `edr.toml` takes effect without a
restart; a config that does not load skips the cycle and keeps the
service up.

1. Read every heartbeat of every batch without `RETIRED`, and write the
   runs and their stage rows into the run database.
2. Classify each live run (the table below), write the state, add an
   event on a change of state, send an alert for the states that
   notify, and after `grace_s` take the action of the state.
3. Collect: copy `log/` and the `collect` paths of every finished stage
   and task into `data/results/<run_id>/`, plus the step directories of
   a running stage that are older than 10 minutes. Extract every metric
   whose file has arrived from the stages and tasks that ended `done`,
   and write the params of the run once: `config`, `build_tag`, `src`
   and the overrides.
4. Resume a `dead` run once, when its stage has `resume` and no process
   group of the run is alive on the host.
5. Find orphans: our processes that match `tool_procs` on a host and
   belong to no live run tree.
6. Launch queued jobs whose host now fits, one per batch per cycle.
7. Write the boards and the pinned Telegram board.
8. Send the daily digest once a day, at the first cycle after
   `limits.digest_at`.
9. Write `<state>/watch.json` with the time, the cycle count and the pid.

Its memory between cycles is three rows of the database's `store` table:
`progress`, what each run looked like last time, `notified`, the
states, the alerts sent and the grace clocks, and `digest`, the day and
the time of the last digest.

## Run states

`docs/reference/states.md` lists every state: the test that finds it,
whether it alerts, what the watcher does after `grace_s`, its mark, and
the command `edr status --triage` proposes. The state is the watcher's
verdict on a run; the phase is the driver's word in the heartbeat for
where the run is or how it ended, and `docs/running.md` lists the
phases. A live run gets its state from the heartbeat and the database; a
finished run from its phase. A job
without a host is `queued`, and a `queued` run that `edr stop` marked
`stopped` never starts.

`edr keep <handle> --ack` writes the keep file with `ack`, which cancels
the pending kill of `hung` and the stop of `host_full`. Any keep file
holds off the `superseded` stop.

## Notifications

The watcher sends one alert per run and state for the states that
`docs/reference/states.md` marks. A repeat with a new reason edits the
earlier message in place, so an alert never repeats. An alert carries
three buttons: keep 12 h, acknowledge (`ack`) and stop. The only channel
today is Telegram;
`docs/telegram.md` explains it.

## The boards

Every cycle writes `data/board/`:

| File | Holds |
|---|---|
| `board.json` | the runs of the live batches, the last 50 events and the host probes, for a script |
| `status.html` | a phone-width page: every run in board order, the last 50 events, the hosts |
| `compare.html` | the runs with their params as columns, a compare table of the final metrics with the difference to the first ticked run, and four plots |

The row order of the last text board is the `last_board` row of the
database's `store` table, so `#n` resolves.

`compare.html` is one self-contained page over the database's runs, params
and metrics. Its tables work as they are. The plots (a metric over the
steps, a scatter of any two columns, the power phases, parallel
coordinates) need Plotly. The page loads `data/board/plotly.min.js` when
that file exists, else the CDN URL; the watcher downloads nothing, so put
the file there yourself for a head node without internet.

The pages are files. Open them in a browser, or serve the directory:

```sh
cd data/board && python -m http.server --bind 127.0.0.1 8000
```

## The service

`edr init` writes `edr-watch.service`, a systemd user unit with the
project directory as `WorkingDirectory`, `edr watch` as `ExecStart` and
`Restart=always`. `docs/getting-started.md` has the install lines.
`loginctl enable-linger` keeps the service up after a logout and a
reboot. Without systemd, `edr watch` in a tmux session does the same
job.

## The check

`edr watch --check` reads `<state>/watch.json`. When the file is older
than three cycles, or missing, it prints why, sends an alert and exits
1. A cron line every few minutes catches a watcher that died:

```
*/10 * * * * cd ~/myflow && edr watch --check
```

`edr watch --once` runs one cycle and exits 1 when the cycle failed.

## What the watcher never does

- It never deletes a file or a tree.
- It never kills without `kill_hung` or `kill_orphan`, and never after
  an `ack`.
- It never resumes a run twice.
- It never reads a batch with `RETIRED`.
- It never downloads anything.
