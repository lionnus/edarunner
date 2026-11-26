# Keep a flow under NDA private

This page shows how to let an agent run a flow whose scripts, tool
output and reports fall under a non-disclosure agreement, without any of
that text reaching the agent. The agent sees the stages, the tools and
their seats, the run states, a set of knobs it may turn, and public forms
of the reports: the area in gate equivalents and the timing paths in a
standard form. A person writes one private file that connects those
knobs to the real tool scripts.

## What stays private and what does not

A vendor licence usually covers the tool's documentation, its command
set and its output; a foundry agreement covers the cell libraries, their
names, their areas and anything that gives away the density of the
process. The design itself, its hierarchy and its timing in nanoseconds
are yours.

| Visible to the agent | Kept from the agent |
|---|---|
| stage names, steps, needs, budgets, states | the commands of a private stage, its `resume`, `progress`, `prepare` and `after_each` |
| tool names and their free seats | the tool scripts, and the lines each knob writes for the tool |
| the knobs, their types, ranges and help | the private file and every string in it |
| `log/<stage>.log`: the lines the private file keeps, redacted by its regexes and its strings | the raw output of the tools, in `.edr/private/log/` on the host |
| `data/results/<run_id>/public/`: area in GE, timing paths, values | the raw reports, in `data/private/<run_id>/` on the head node |
| metrics over the public files | `ge_um2`, cell names, library names, PDK paths |

## The three parts

```
edr.toml                  public: the stages, the knobs, the metrics; the agent edits it
~/nda/croc/private.toml   private: the commands, the knob lines, what may leave; a person writes it
data/results/<run>/public the public form of the reports, which the metrics read
```

`private` in `edr.toml` names the private file. Keep it outside the
project. The scripts it calls usually come with the flow, as a private
part of the flow repository such as `nonfree/`, so they land in each
checkout under `source.worktrees` and in each run tree; the agent's
settings must deny those directories, as
[the limits](#the-limits-of-the-boundary) below show.

## Declare the knobs

A knob is one setting a job may choose. The knobs are the public
interface of the flow: the agent tunes the tools through them and
through nothing else.

```toml
[knobs.clock_ns]
default = 2.0
min = 0.5
max = 20.0
help = "the target clock period"

[knobs.retime]
default = false
help = "move registers across the logic during synthesis"
stages = ["synth"]

[knobs.effort]
choices = ["low", "medium", "high"]
default = "medium"
help = "the effort of synthesis and placement"
```

A job sets them by name, and `edr plan` refuses a name or a value outside
this table:

```toml
[[job]]
label = "fast_rt"
knobs = { clock_ns = 1.6, retime = true }
```

Every stage gets each value as `EDR_KNOB_<NAME>`, such as
`EDR_KNOB_CLOCK_NS=1.6`, and as `{knob.<name>}` in its strings. A value
off its default adds a token to the build tag, here
`retime_clock_ns1p6`, and the `parameters` table records each value as
`knob.<name>`, so `edr compare` and `compare.html` put the knobs next to
the numbers. `edr brief` lists the knobs with their help, which is where
an agent learns what it can change.

Knobs work in any project. A flow without a private file reads them from
its environment or its placeholders.

## Mark the private stages

A private stage names no command in `edr.toml`:

```toml
private = "~/nda/croc/private.toml"

[stages.synth]
private = true
needs = { cores = 8, disk_gb = 20, tools = ["fc"] }
budget = { hours = 6 }
```

`edr check` refuses a private stage that sets `cmd`, `resume`,
`progress`, `prepare`, `after_each`, `collect` or `collect_on_request`:
its files leave the host only through the private file
(`config._check_stage`). No public `collect` of any stage copies a
withheld path of the run tree: `.edr/private/`, the `collect` paths of the
private stages, and the `paths` of the private file. Each copy excludes
them relative to its own root, so `collect = ["./"]` copies the rest of
the tree and leaves them out, and an absolute path or one with `..` is
not copied at all (`collect._Copier.excludes`).

## Write the private file

The private file gives the commands, maps the knobs onto the tool, and
says what may leave. A person under the agreement writes it by hand.

```toml
schema = 1

# The area of one NAND2 of the library; every published area is divided by it.
ge_um2 = 0.0   # the real value of your library

# Text that never leaves, as regexes: library and cell prefixes, PDK paths, the foundry.
redact = ['(?i)libname\w*', 'CELLPFX_\w+', '/path/to/pdk\S*']

# The output lines of a private stage that reach log/<stage>.log; every other line stays on the host.
keep = ['^\[flow\]', '^(Error|Warning): .*\(\w+-\d+\)$']

# Cell classes for the published timing paths; a group `drive` adds the drive strength.
cells = { NAND2 = 'ND2X(?P<drive>\d+)', INV = 'INVX(?P<drive>\d+)', DFF = 'DFF' }

env = { PDK_ROOT = "/path/to/pdk" }

# Paths of the run tree that no public collect copies, such as the private part of the flow.
paths = ["nonfree/"]

[stages.synth]
cmd = "fc_shell -f {root}/nonfree/synth.tcl -x 'source {knob_file}'"
collect = ["nonfree/reports/synth/"]

[knobs.clock_ns]
line = "set CLOCK_NS {value}"

[knobs.retime]
line = { true = "<the tool's retiming switch>", false = "" }

[knobs.effort]
line = { low = "<line for low>", medium = "", high = "<line for high>" }
```

Before each private stage, the driver writes `{knob_file}`, one line per
knob that acts on the stage, in the order of `edr.toml`, so the tool
script sources a single file in its own language. A knob that the private
file does not map reaches the stage through `EDR_KNOB_<NAME>` alone.

[reference/configuration.md](../reference/configuration.md#the-private-file)
lists every key.

## What happens to the output

The driver runs a private command with its output going to
`.edr/private/log/<stage>.log` in the run tree. Every 5 seconds, and when
the command ends, it copies the new lines that a `keep` regex matches
into `log/<stage>.log`, with every `redact` match replaced by `‹nda›`,
and ends with a count of the lines it held back (`edr_driver.Mirror`).
The tool writes its log itself, so a tool never waits for the driver, and
it keeps running when the driver dies. The heartbeat names the command
as `the private command of stage <name>`, and the signature of a failed
task is a hash of its raw last line, so two equal failures still stop a
looping group while the line itself stays on the host.

Without `keep`, the public log holds only edr's own lines. Start with
the lines your flow prints itself and the error and warning lines with
their message ids; every line you add is a line the agent can use to
debug a failure.

## Publish the reports

The watcher copies the `collect` paths of a private stage into
`data/private/<run_id>/`, and the raw logs into
`data/private/<run_id>/log/`. Then each `[publish.<name>]` turns one raw
file into a public file of `data/results/<run_id>/public/`, before the
metrics are read (`private.publish_run`).

```toml
[publish.synth_area]
stage = "synth"
file = "nonfree/reports/synth/area_hier.rpt"
kind = "area"
depth = 4

[publish.synth_timing]
stage = "synth"
file = "nonfree/reports/synth/timing.rpt"
kind = "timing"
paths = 50

[publish.synth_qor]
stage = "synth"
file = "nonfree/reports/synth/qor.rpt"
kind = "values"
values = { cells = 'Leaf Cell Count:\s+(\d+)', core_ge = { regex = 'Core Area:\s+([0-9.]+)', ge = true } }
```

| `kind` | public file |
|---|---|
| `area` | `<name>.csv`: `instance, depth, ge, local_ge, cells` from a Synopsys `report_area -hierarchy` or an OpenROAD hierarchical area report, in gate equivalents |
| `timing` | `<name>.json`: per path its group, type, start and end point, slack, arrival and required time, the cells on the data path and each point with the class of its cell; a summary of the setup (`max`) and the hold (`min`) paths, per path group; from a Synopsys `report_timing` or an OpenROAD `report_checks` |
| `values` | `<name>.json`: one number per regex; `ge = true` divides an area by `ge_um2` |
| `text` | `<name>.txt`: the lines of `keep`, redacted |
| `python` | `<name>.json`: the dict of a hook `(path, ctx)`, with `ctx.ge_um2`, `ctx.cell(name)` and `ctx.redact(text)` |

Every string in a public file passes the redaction, so an instance named
after a foundry macro shows as `‹nda›`. A file that does not convert
gives one `publish` event, which names the publish and never quotes the
file. `edr extract` publishes again from the raw files, so a change of
`cells` or `ge_um2` reaches the old runs.

The metrics of `edr.toml` then read the public files with the usual
parsers:

```toml
[metrics.area_ge]
stage = "synth"
file = "public/synth_area.csv"
table = { instance = "instance", value = "ge", local = "local_ge", depth = "depth", top = { instance = "<top>" } }
unit = "GE"
better = "lower"

[metrics.wns_synth_ns]
stage = "synth"
file = "public/synth_timing.json"
json = "summary.max.worst_slack"
unit = "ns"
better = "higher"
```

`edr compare --instances --metric area_ge` shows the hierarchy in GE,
and `--unit kGE` prints a GE metric in kGE.

## What edr redacts

Every string of the private file, in pieces of at least 8 characters
between its placeholders, becomes `‹private›`, and every match of a
`redact` regex becomes `‹nda›`. One combined regex makes a single pass, so
one replacement never cuts the text another pattern needs. The driver
applies both to each kept line before it reaches the public log. edr
applies them again to what it prints and sends (`private.redact`):

- every line `edr` prints or logs, including errors and `--json`
  (`cli.main`, `private.Stream`, `private.LogFilter`);
- `--show-spec`, the `spec` of `edr plan`, `launch` and `continue` with
  `--json`, `edr track --dry-run`, and the commands in the manifest of
  `edr export`, where a field of the private file reads `‹private›`
  (`private.public_spec`);
- the manifest of `edr export`;
- every event, and so every alert, digest and board built from events
  (`Database.add_event`);
- every Telegram call, ntfy post and mail, and every text file the bot
  sends;
- `status.html`, `compare.html` and `board.json`;
- the command line of a tool process in an orphan alert or its Check
  button, which shows only the process name once a private file is
  loaded.

A literal is found only as the private file writes it. A script path that
a tool prints in another form, a piece shorter than 8 characters, or a
text that HTML escaping or a cut changed, passes. That is why the keep
list decides first what may leave the host, and why the `redact` regexes
should name the library, cell and PDK strings themselves rather than
leave them to the literals. Names that the reports carry, such as the
start and end points or the scenario of a timing path, pass the `redact`
regexes only.

## The limits of the boundary

edr runs as you, and so does an agent that runs in your shell. edr keeps
the private text out of what it prints, but it cannot stop another
program of the same user from reading a file. The private text lies in
these places on the head node: the private file and its hooks, the
private part of each checkout under `source.worktrees`, `data/private/`,
the specs in the state directory (`~/.edr/<project>/`, which hold the
rendered commands and knob lines), and the run trees of the host `local`. The boundary therefore
holds against a mistake, such as an agent that reads a log tail or greps
the results, and not against an agent that sets out to read the private
file. Close the rest with the agent's own settings, and for a hard
boundary run the agent as another Unix user that cannot read the private
paths.

For Claude Code, the sandbox is the setting that holds for shell
commands; a `Read` deny rule covers only the file tools. Let `edr` run
outside the sandbox and deny the private paths to everything else:

```json
{
  "permissions": {
    "deny": ["Read(~/nda/**)", "Read(//path/to/project/data/private/**)", "Read(~/.edr/**)",
             "Read(//path/to/rtl-wt/**/nonfree/**)", "Read(//path/to/scratch/**)"]
  },
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "excludedCommands": ["edr *"],
    "filesystem": { "denyRead": ["~/nda", "/path/to/project/data/private", "~/.edr", "/path/to/scratch"] }
  }
}
```

`allowUnsandboxedCommands: false` keeps the agent from retrying a command
outside the sandbox. Give `denyRead` absolute directories, and add the
private part of each checkout as it appears; whether `denyRead` takes a
glob is not documented, so list the directories themselves. An agent that
works only through `edr` never needs the state directory or a run tree.

Every command of a Bash call must match `excludedCommands` for the call
to leave the sandbox, so `edr status; cat x` stays inside it. Check the
current keys in the Claude Code settings documentation before you rely on
them.

Put the rules in the project's `AGENTS.md` as well: never read the
private file, `data/private/` or `.edr/private/` of a run tree, and ask a
person for a knob that does not exist yet.

## What it costs

On a host the cost is one read of the new bytes of a raw log every 5
seconds and one regex test per line. On the head node a publish parses
each raw report once, when it is newer than its public file, and
`paths` bounds the size of a timing file. Next to a tool run of minutes
or hours, neither shows.

What an agent can do changes more than what the machines do:

- It cannot edit the tool scripts, so the knobs set the space it can
  search. Give it many knobs with ranges and choices: the clock, retiming,
  the effort, the utilization and the aspect ratio, the fanout and
  transition limits, the threshold-voltage mix, the CTS skew target. An
  agent explores a wide space of knobs faster than a person does.
- It debugs from the kept log lines. Keep the error and warning lines
  with their message ids, and your flow's own progress lines; a public
  log with no lines leaves it guessing at every failure.
- The area per instance in GE and the timing paths with cell classes and
  drive strengths carry what a design decision needs: which block is
  large, where the logic is deep, where the tool buffers. The absolute
  area in um2 rarely changes an RTL decision.
- It still edits the design, the batches, the stages and the metrics
  over the public files, so most of its work is the same as on an open
  flow.
- A setting no knob covers takes a person: a new knob in `edr.toml` and
  its line in the private file. Ask the agent to write such a request
  into a hand-off file instead of working around the knobs.
