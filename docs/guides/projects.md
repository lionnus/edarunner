# Run several projects

This page is for the second project and every one after it: several
flows, or several papers, on the same machines. Each project keeps its
own files and runs, one supervisor watches all of them, and the machines,
the seats and the phone are shared. It assumes one project that runs as
[run.md](run.md) shows.

## What each project keeps

A project is one directory with its own `edr.toml`. Nothing of one
project appears in the tables of another:

| Each project has its own | Where |
|---|---|
| flow, stages, metrics, limits and placement rules | `edr.toml`, `tasks.toml`, `jobs/` |
| runs, events, metrics and results | `data/edr.db`, `data/results/`, `data/exports/` |
| specs, heartbeats, stop and keep files, task queues | the state directory, `~/.edr/<project>` by default |
| run trees on the hosts | `<scratch>/<user>/edr/<project>/<run_id>/` |
| watcher | one `edr watch --served` process, which the supervisor starts |

The project name is the `project` key of `edr.toml`, and it must be
unique among your projects: it names the state directory, the run trees
and the registry link.

## The registry

`~/.edr/projects/<name>` is a link to the directory of each project.
`launch`, `continue`, `track`, `import` and `watch` write it, and
`edr register` writes it by hand:

```sh
cd ~/papers/second/edr && edr register
edr projects                          # every project, its watcher and its live runs
```

A name that belongs to another directory stops the command, so two
directories never share one state directory. `edr unregister` removes
the link and nothing else; the database, the state and the run trees
stay. To move a project, move its directory and run `edr register` in
the new place: the old link names a directory without that project, so
`register` replaces it.

Any command acts on one project from any directory with `-P`:

```sh
edr -P second status
EDR_PROJECT=second edr status --triage
```

`EDR_PROJECT` is refused inside the directory of another project, so a
variable left in a shell never acts on the wrong one.

## One supervisor for all of them

`edr serve` keeps one watcher per registered project, restarts a watcher
that exits or stands still, and does the work that belongs to you and
not to one project. Install it once per user, as
[run.md](run.md#keep-a-watcher-behind-the-batch) shows; a project that
you register later gets its watcher within a minute.

```sh
edr serve --dry-run                   # what the supervisor would do for each project
edr projects                          # who watches each project now
```

A project whose files do not load gets one alert and no watcher until
you fix them; the other projects go on. A watcher that is already
running goes on with the last config that loaded.

## What the projects share

A host is one machine for every project, so these count over every
registered project:

- `max_per_host` of a project is the number of your runs on a host,
  whatever project they belong to. A launch counts the live heartbeats
  of every project, plus the launches that wrote no heartbeat yet.
- The seat leases of a tool lie in `~/.edr/leases/<tool>/` for every
  project, so the drivers of two projects never take the same last seat.
- `host_free_min_gb` belongs to the site or the host, not to a project:
  one disk has one floor. While a host stays under it, the supervisor
  stops the newest run on that host, of any project, once per `grace_s`.
  A keep does not hold that stop off.
- The orphan check, the lease sweep and the clock check run once for all
  projects, and a tool process gets one alert, not one per project.

Seats are first come, first served between your projects. A large batch
of one project can hold every seat of a tool, so bound it with the size
of the batch and with `parallel`.

## Look at every project

```sh
edr status --all                      # the board of every project, with a project column
edr hosts                             # the free room of each host and your runs there, by project
edr status --digest --all             # the digest of every project
```

`edr hosts` shows the hosts where a run can start first, and says when
your own runs fill a host, since that is where they block other people's
work.

## On the phone

One bot serves every project. The pinned board holds the live runs of
all of them and the hosts that hold them, and one digest a day covers
every project. `/status <project>` shows the board of one project, and
a handle may name its project, as in `/status second/base@sweep1`.
[alerts.md](alerts.md#one-bot-for-every-project) lists the rules, and
[alerts.md](alerts.md#topics) gives each project a forum topic of its
own.

## A second user

Two people who share the machines share the site file and nothing else.
Each has a user root `~/.edr`, a registry, a supervisor, a bot and a
`~/.config/edarunner/user.toml` with their own chat. Each sees the runs
of the other as load on the hosts and as tool processes of other users
in `edr hosts`. The seat leases of one user do not count for the other,
so keep the floor of seats that the site probe leaves for others.

## From one watcher per project

An older setup ran one `edr watch` unit per project. To move to the
supervisor:

1. Upgrade edarunner, and wait until no run of any project waits at a
   gate. A driver that started before the upgrade keeps the lease
   directory of its spec, so for `lease_s` after a launch the old and the
   new drivers do not see each other's seats.
2. Move `[telegram]` with its chat, `[ntfy]` and `[mail]` from the site
   file into `user.toml`, and `digest_at` from `[limits]` of each
   `edr.toml` into `user.toml`. Remove `telegram_poll`, `[telegram]` and
   `[marks]` from each `edr.toml`, and move `host_free_min_gb` from its
   `[limits]` to the site file or to a host there. `edr check` names
   every key that no longer loads.
3. Stop and disable each old unit, such as
   `systemctl --user disable --now edr-<project>`.
4. Run `edr register` in each project directory, and check the list with
   `edr projects`.
5. Run `edr serve --dry-run`, then install the unit as
   [run.md](run.md#keep-a-watcher-behind-the-batch) shows.
6. Check `edr projects` and `edr status --all`, and unpin the old boards
   in the chat.

The runs keep going while no watcher runs: the driver reads its own
budgets, gates, stop and keep files, and the new watcher collects what
ended in between.
