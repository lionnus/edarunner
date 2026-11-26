# Agents in a project with a flow under NDA

The flow of this project runs commercial tools on a library under a
non-disclosure agreement. You operate it through `edr` alone, as the
core's `AGENTS.md` describes, and you tune it through the knobs.

## Never

- Never read `../nda/`, `data/private/`, the `nonfree/` part of a checkout
  under `../rtl-wt/`, the state directory `~/.edr/` or a run tree, and never
  ask a host for them. `edr` reads what it needs there for you.
- Never try to recover a text that edr printed as `‹nda›` or `‹private›`.
- Never write a tool command, a library name or an area in um2 into a file
  of this project.

## Owns

You own `edr.toml`, `jobs/` and your notes in this directory. You may add
stages, batches and metrics over the files in `data/results/<run_id>/public/`.

## Tune the flow

`edr brief` lists the knobs with their types, ranges and help. A job sets
them in `knobs = { ... }`, and `edr plan` refuses a name or a value outside
them. A setting that no knob covers needs a person: write what you need and
why into `data/handoff/knobs.md`, and leave the flow as it is.

## Read the numbers

The area is in gate equivalents (`unit = "GE"`; `--unit kGE` prints it in
kGE), per instance in `public/synth_area.csv` and through
`edr compare --instances --metric area_ge`. The timing paths, worst first,
are in `public/*_timing.json`, with the class and drive of each cell; a
summary gives the setup (`max`) and the hold (`min`) slack per path group.
The public log of a stage holds the lines the private file lets through,
and ends with the count of the lines held back.
