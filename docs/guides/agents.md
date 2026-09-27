# Operate it with an agent

Can a Claude session or a script run the farm for me? Yes, through `edr`
alone. This page shows what an agent reads at the start of a session,
how it calls `edr`, and how it reports back to your phone. The rules the
agent follows are in
[AGENTS.md](https://github.com/lionnus/edarunner/blob/main/AGENTS.md) at
the root of the repository.

## Give the agent the rules

`AGENTS.md` is the operating guide for an agent: start with the
briefing, act only through `edr`, pass `--json`, do a dry run before
every write, give a reason for every stop and retire, and never kill,
delete or overwrite anything by hand. Copy it into the project directory
or point the session at it. `edr brief` ends with the paths of the
project's own `CLAUDE.md` and `AGENTS.md` when they exist, so a session
finds them.

## Start a session with the briefing

`edr brief` prints the project as Markdown for a reader who has never
seen it: the source and its checked-out trees, the stages, the hosts
with the marks of their last probe, the tool seats, the runs per batch,
every run that needs a decision with the command the triage proposes,
and the last ten events. `edr brief --run <handle>` tells the story of
one run; [debug.md](debug.md#read-the-story-of-the-run) shows it.

A Claude Code session reads the briefing before its first prompt when
the project's `.claude/settings.json` runs it as a `SessionStart` hook.
The output of the hook becomes part of the session's context:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "cd \"$CLAUDE_PROJECT_DIR\" && edr brief"}]}
    ]
  }
}
```

## Call edr

Every command takes `--json` and prints one object:

```json
{"code": 0, "data": {}, "output": "the text a person would see"}
```

`code` is the exit code: 0 done, 1 refused or bad input, 2 nothing to
do, 3 some hosts failed. A command can refine a code, such as 3 on
`edr stop` when the driver is still alive;
[reference/cli.md](../reference/cli.md) lists the codes of each command.
The agent acts on `data` and quotes `output` when it reports.

Every command that writes takes `--dry-run`, which prints every path and
every command and writes nothing. `edr stop` and `edr retire` refuse to
run without `--why`, and the reason goes into the event log together
with the actor, so `edr events --run <handle>` later shows who did what
and why. `edr status --triage` lists every run that needs a decision
with one proposed command, and
[reference/states.md](../reference/states.md) says what each state
means before the agent runs that command.

## Report to the phone

`edr notify` sends a message through every alert channel of the project,
so a session can tell you when it needs input or when a turn ends. Put
this into `.claude/settings.json` of the repository where the session
runs, with the path of the project directory:

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command",
      "command": "cd ~/myflow && edr notify \"session $(basename \"$CLAUDE_PROJECT_DIR\"): $(jq -r .message)\""}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "cd ~/myflow && edr notify --silent \"session $(basename \"$CLAUDE_PROJECT_DIR\"): turn ended\""}]}]
  }
}
```

The `Notification` hook gets a JSON object on standard input, and `jq`
takes its `message`. The `Stop` hook runs at the end of every turn, so it
sends silently.

The other way round, a custom bot command can open a Claude session from
the phone, in the project or in the tree of the run an alert names;
[alerts.md](alerts.md#custom-commands) shows both entries.
