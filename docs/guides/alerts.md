# Get alerts on your phone

`edr watch` can tell you on your phone that a run died, so you don't have to
watch the board. This page sets up the three alert channels (Telegram, ntfy
and mail) and shows what the Telegram bot can do from the phone: the pinned
board, the buttons on an alert, the commands and your own commands.

## Channels

Each channel is a table in your user file,
`~/.config/edarunner/user.toml`, and edarunner sends every alert to
every channel it finds there. The user file lies next to the site file
and stays out of git: the site file describes the machines of a lab,
and the user file names your chat, your ntfy topic and your mail
address, so each user of one site file gets alerts of their own. Add
`user.toml` and the secret files to the `.gitignore` of the site
repository. [reference/configuration.md](../reference/configuration.md)
lists the keys.

| Channel | Alerts | Buttons | Board | Commands |
|---|---|---|---|---|
| Telegram | one message per alert, edited in place | Stop and +6h, +12h, +24h; Stop and Free space on a full host; Continue on a run that ended with stages left | one pinned message | yes |
| ntfy | one push per alert, priority by kind | a copy button and a line per command | on request | no |
| mail | one mail per alert | a line per command | on request | no |

A channel whose secret file is missing, or readable by the group or by
others, stays off, and `edr watch` logs the reason.

### Message kinds

Every channel gets every kind with the same title and the same text.
The title of an alert holds the mark of its state, the project, the
state in plain words, and the run handle or the host. The title of any
other message is `<project>: <kind>`.

| Kind | Sent by | Telegram | ntfy | mail |
|---|---|---|---|---|
| alert: `dead`, `hung`, `looping`, `over_budget`, `host_full`, `superseded`, `held`, `incomplete`, `failed`, `killed`, `stopped` | the watcher, when a run enters the state or its reason changes | one message, edited in place, with the next command; a live run that is `hung`, `looping`, `over_budget` or `superseded` also gets the buttons Stop, +6h, +12h and +24h, one on a full host Stop and Free space, and a run that ended `OVER_BUDGET` or `STOPPED` with stages left Continue | one push per change, with the next command and the button commands, the first three also as copy buttons | one mail per change, with the next command and the button commands |
| alert: `orphan` | the watcher that holds `serve.lock`, once for all projects | one message with the commands to check and end the process, no buttons | one push, the same text | one mail, the same text |
| alert: `clock` | the watcher that holds `serve.lock`, once per host whose clock is more than 60 s off | one message | one push | one mail |
| alert: `watch` | `edr watch --check` and `edr serve --check`; the supervisor, when a watcher exits or stands still | one message | one urgent push | one mail |
| alert: `config` | the watcher, once per error text, when `edr.toml`, `tasks.toml` or the site file stops loading | one message | one push | one mail |
| `digest` | the supervisor once a day at `digest_at` of the user file, for every project in one message, and `edr notify --digest` | one message | one low push | one mail |
| `board` | the supervisor every minute, or a watcher without a supervisor every cycle | one pinned message, edited in place | none | none |
| `board` on request | `edr notify --board` | one new message | one low push | one mail |
| `note` | `edr notify TEXT` | one message | one push, the lowest priority with `--silent` | one mail |
| a command reply, the result of a detached command, a button answer | the bot, for a command from the chat | a reply in the chat | none; ntfy takes no commands | none; mail takes no commands |
| a collect failure, a lease sweep, a resume | the watcher | an event only, in `edr events` and `/events` | an event only | an event only |

ntfy and mail have no callback buttons. The alert text carries each
button as a line `<label>: <command>`, for example
`+6h: edr keep a@demo --hours 6`. On ntfy the first three buttons also
come as copy buttons, which put the command on the clipboard, since ntfy
takes three actions per message. A server without copy buttons refuses the
push with a 400, and the channel sends it again without the buttons.

The board changes every cycle, so the watcher keeps a live copy of it
only as the pinned Telegram message. You can send it, or the digest, to
every channel on request:

```sh
edr notify --board            # the board of edr status
edr notify --digest           # the digest of every project now; the daily one still comes
```

A cron line mails the board every morning:

```sh
0 7 * * * cd ~/myflow && edr notify --board
```

## Telegram

One bot serves every registered project. It runs in `edr serve`, or,
without a supervisor, in the first watcher that takes
`~/.edr/serve.lock`. It keeps one pinned board and answers the commands
and the button presses of one chat. Every watcher sends the alerts of
its own project through the same bot. The bot uses long polling over
outbound HTTPS, so it needs no open port and no webhook.
[reference/bot.md](../reference/bot.md) lists every command.

### Set up the bot

1. Open @BotFather in Telegram and send `/newbot`. Give the bot a name
   and a user name that ends in `bot`. BotFather answers with the token.
2. Write the token to `~/.config/edarunner/telegram.token` and set the
   mode to 600:

   ```sh
   umask 077; echo '123456:ABC...' > ~/.config/edarunner/telegram.token
   ```

   The bot does not start when the file is missing or when the group or
   others can read it. `edr serve` logs the reason.
3. Add the section to `~/.config/edarunner/user.toml`:

   ```toml
   [telegram]
   token_file = "~/.config/edarunner/telegram.token"
   chat_id = 0
   user_id = 0
   ```

4. Open the new bot on the phone and send `/start`. Start `edr serve`.
   With `chat_id = 0` the bot obeys nobody, but it prints the chat id of
   the first message it receives to stderr, the journal of the service:

   ```
   telegram: the first message came from chat 987654321; set chat_id = 987654321 in [telegram]
   ```

5. Put that number in `chat_id` and restart `edr serve`. The bot now
   publishes its command menu with `setMyCommands` and answers.

`user_id` is optional. When it is set, the bot obeys the messages and
the button presses of that one user and ignores every other member of
the chat. When it is unset, the chat is the only gate, so the chat must
be a private one; `edr serve` logs a warning to say so. Your user id is
the `from.id` of a message; a bot such as @userinfobot shows it.

### Who the bot obeys

The bot obeys one `chat_id`, and one `user_id` when it is set. Every
other chat or user gets no answer, and the first message from it
records one `rejected` event: in the journal of `edr serve`, or in the
events of the project of a watcher without a supervisor. The token is
the one secret; it lives in a file with mode 600, and the bot refuses
any other mode.

The bot never runs a shell string or free text. A custom command is an
argv list from the site file or the user file, and every argument from
the phone must match its allowlist regex in full. The bot never kills a
process, and never runs `launch` or `rm`. `/stop` and the Stop button
write the `after-task` stop file, `/keep` and the +6h, +12h and +24h
buttons write the keep file, the Free space button runs
`edr retire --host <host> --prune <names>`, and the Continue button runs
`edr continue <handle>`, each through the same code as the command
line. Every action goes into the events of its project with the actor
`telegram`, and a refusal goes where a `rejected` event goes.

### Replies on the phone

Every message starts with one bold line that names the project, so the
messages of two projects in one chat stay apart; a message of the
supervisor names `edr`. Under that line, a reply is formatted text: one
short line per item, a run handle in monospace, and a count or a note in
italics. A tap on a handle copies it, so you can paste it into
`/status <handle>`.

The first line is `<project>: <title>`. Each run line starts with one
mark for its state; [reference/states.md](../reference/states.md) lists
them.

A `<pre>` block holds only text whose width the bot does not control:
the last log lines of `/status <handle>`, the columns of `/compare` and
`/metric`, and the output of a custom command. A message stays under
the limit of 4096 characters; the bot cuts a long reply at a line end.

### Alerts

The watcher sends one message per run and state. A new reason for the
same state edits that message instead of sending another. An alert has
four parts:

- the title: the mark of the state, the project and the state in bold,
  then the run handle or the host in monospace;
- one or two sentences on what edarunner saw and why it matters;
- the facts: the stage and step, the host, and the command line in
  monospace, cut to 120 characters, then the last four lines of the log
  of the running stage or task in one block, each cut to 100 characters;
- what to do: the command to run next in monospace, and what edarunner
  does by itself.

[What each alert says](#what-each-alert-says) shows every kind. The
alert of a live run carries the buttons that stop it or give it time,
and the alert of a run that ended with stages left carries Continue:

| Alert | Buttons |
|---|---|
| `hung`, `looping`, `over_budget`, `superseded`, while the run lives | Stop, +6h, +12h, +24h |
| `host_full` | Stop, and Free space when the project declares prune targets |
| `over_budget` or `stopped` of a run that ended with stages left on its tree | Continue |

| Button | Action |
|---|---|
| Stop | asks once more, then `edr stop <handle> --after-task` |
| +6h, +12h, +24h | `edr keep <handle> --hours 6`, 12 or 24: that many more hours on the budget of the running stage or task, and for as long the watcher neither kills the run as `hung` nor stops it as `superseded` |
| Free space | asks once more, then `edr retire --host <host> --prune <names>` with every prune name of the project: it removes the prune targets of the finished runs of the project on the full host, except those of a run whose tree has stages left |
| Continue | asks once more, then `edr continue <handle>`: it runs the stages left on the tree of the run as one new run, on the same host |

A keep never holds off the stop of a full host. A full scratch disk
blocks every other user of the host, so after `grace_s` the watcher
stops the newest run there unless the disk gets back above the floor.
An `over_budget` alert of a stage with `kill = true` gets no buttons,
since the driver has sent `SIGTERM` to the stage already. Such a stage
did not end with exit 0, so its run gets no Continue either: the stage
would start over, and `edr continue` needs `--stage` and a checkpoint
with `--from` for it. Each alert says in one line what its buttons do;
the line of Continue names the stages, the host and the tree.

The `callback_data` of a button names the action and the project, such
as `keep6:demo`, and the run is the one of the alert: the watcher that
sent it keeps its message id for 7 days.
[reference/bot.md](../reference/bot.md#alert-buttons) lists them. The
bot answers every press, appends the result to the alert text, and
keeps the buttons. Each press records one event in the database of the
project, the event of that action, with the actor `telegram`.

Stop, Free space and Continue act only on a second tap. The first tap
adds a question to the alert, such as `Stop b_nodw@demo after its
current task?`, and shows two buttons, `Yes, stop` and `No`. `No`
restores the buttons of the alert. A question older than 10 minutes is stale: a tap
on its yes then restores the buttons and does nothing. The bot keeps
each open question on disk, so it survives a restart.

### The board

The board is one message, pinned once and edited silently every cycle.
Its message id stays on disk, so a restart edits the same message.
`/pin` unpins the old message and
pins a new one at the bottom of the chat. Under `edr serve` the board
holds every registered project; a watcher without a supervisor pins the
board of its own project.

The board of every project, which `/status` also sends, has a bold line
per project and under it one line per live run: its number, the mark,
the handle in monospace, which a tap copies, and what the run does. A
line holds the state when it is not `running`, the stage with its step
of the total and the step name, and the time since the run started. The
numbers count on over every project, so `/status #2` names the second
run of the board. A project with no live run gets one line with the
count of its runs that ended in the last 24 hours. Machines lists each
host that holds a run of yours, with its free room and what your runs
use there. The last line counts the live runs per state:

```
edr: board 14:05
demo
#1 🔴 a@demo dead, synth 3/13 elaborate, 5h
#2 🟢 c@demo pnr 9/13 route_opt, 3h
power nothing live, 2 ended in 24 h

Machines
hostA free 21/32 cores, 93/376 GB RAM, 195/1538 GB scratch; yours: demo 2, 16 cores, 120 GB scratch

2 live: 1 dead, 1 running. /status <project> shows one project.
```

The board of one project, which `/status <project>` sends and a lone
watcher pins, has two sections with one line per run:

- Running: the live runs, the worst state first. A stage with steps
  shows `starting` until the first step.
- Finished in the last 24 hours: the state and when the run ended. The
  older runs fold into one line, `and N older runs: /status all`, and
  `/status all` lists every run.

The last two lines, in italics, hold the count per state and a legend
that names each mark of the message in plain words:

```
demo: board 14:05
Running
🔴 a@demo dead, synth 3/13 elaborate, 5h
🟢 c@demo pnr 9/13 route_opt, 3h

Finished in the last 24 hours
⚪ b@demo done, ended 1h ago
and 4 older runs: /status all

1 dead, 1 running, 5 done
🔴 dead (driver gone), 🟢 running, ⚪ done
```

Each section shows at most 30 runs and then a line `… and N more`.
When no run is live, the Running section says `nothing live`.

### Built-in commands

[reference/bot.md](../reference/bot.md) lists every built-in command
with its arguments. A handle is `label@batch`, a run id prefix, or `#n`
from the last board; `project/label@batch` also names the project.

`/status <handle>` shows the mark, the handle and the state, then the
stage and step, the host and the age, the proposed command in monospace,
and the last four log lines in a `<pre>` block. `/events` shows one line
`HH:MM kind handle` per event, the kind in bold, and the reason indented
under it in italics; it shows a handle in place of a run id. `/hosts`
shows one line per host: the mark of `edr hosts`, which says whether a
run can start there, the free room of total, and your runs there by
project with the cores and scratch they use:

```
🟢 hostA free 21/32 cores, 93/376 GB RAM, 195/1538 GB scratch, 1/1 GPUs; yours: demo 1, 8 cores, 40 GB scratch
```

The hosts come in the order of `edr hosts`, the ones where a run can
start first. A line in italics under a host says why no run can start
there, or that your runs fill it. A host that fails the probe shows
`⚫ no answer`. `/tools` shows `fc 3/8 seats used, hostA, hostB` per tool.
`/help` lists the commands as links that you can tap. `/compare` and
`/metric` reply with a `<pre>` block of aligned columns.

A custom command replies with the output of its program as it is. Give
the program a narrow format, or the phone wraps the lines.

### Custom commands

Everything beyond the built-in list comes from `[telegram.commands.*]`,
one table per command: in `site.toml` for everyone of the lab, or in
`user.toml` for you alone. A command of `user.toml` replaces the one of
the same name in `site.toml`.

```toml
[telegram.commands.survey]
help = "cores, RAM and scratch on every host"
run = ["edr", "hosts", "--narrow"]
timeout_s = 60

[telegram.commands.claude]
help = "open a Claude remote-control session: /claude <dir>"
args = { dir = "^(backend|rtl|sw)$" }
skip_if = ["tmux", "has-session", "-t", "claude-{project}-{dir}"]
skip_reply = "already open: claude-{project}-{dir}"
run = ["tmux", "new-session", "-d", "-s", "claude-{project}-{dir}", "-c", "{root}/{dir}", "claude remote-control --name {project}-{dir}"]
reply = "session claude-{project}-{dir} started; open the Claude app"
```

[reference/bot.md](../reference/bot.md) lists every key, the
placeholders a string renders, and the regex gate on every argument.
Write each regex as an allowlist of the exact values you expect, as the
`claude` example does.

A command with `detach` is watched for 5 seconds after the start. When
it ends in that window, the reply is `ended with rc N: <last output
line>` instead of `reply`, so a program that refuses to start, such as a
Claude session in a directory that is not trusted, says why.

### The keyboard

`/start` and `/keyboard` show a reply keyboard under the text field. It
stays until `/keyboard off` removes it. Its buttons are six words:

```
Status   Projects   Hosts
Events   Tools      Digest
```

A tap sends the word as a plain message, and the bot runs the command
of that name: `Status` runs `/status`. The case and spaces around the
word do not matter; any other plain text gets no answer.

In a group, a bot sees plain text only when it is an admin, or when
@BotFather turned its privacy mode off with `/setprivacy`.

### Reactions

The bot reacts to the message of a command:

| Reaction | Meaning |
|---|---|
| 👀 | a command that can take more than a second started: a custom command, `/log` or `/hosts` |
| 👍 | the reply went out |
| 👎 | the command failed or was refused, or the reply did not go out |

Telegram accepts only a fixed set of reaction emoji, and ⏳, ✅ and ❌
are not in it. A chat or a client without reactions makes the call
fail; the bot ignores that failure and answers as usual.

### Files

Three commands answer with a file instead of a message. The phone opens
an HTML file in its browser and a CSV file in a sheet app.

- `/log <handle> [n]` fetches the last `n` lines, default 200, of the
  log of the running or last stage from the host, with the same ssh
  wrapper as `edr`. The file is `<handle>.log`.
- `/board` sends `data/board/compare.html` and `data/board/status.html`
  of the last watcher cycle.
- `/csv <source>` sends `metrics.csv`, the output of
  `edr metrics --source <source> --csv`.

A file over 20 MB is not sent; the bot answers with its size and the
limit instead.

### Reply to an alert

A command sent as a reply to an alert acts on the run of that alert, so
it needs no handle. The watcher that sends an alert with buttons keeps
its message id and run id for 7 days in the `store` table of the
project database, under `telegram`.

| Reply | Same as |
|---|---|
| `/keep 24` | `/keep <run> 24` |
| `/stop disk full` | `/stop <run> disk full` |
| `/status` | `/status <run>` |

A reply that names the run itself, such as `/keep <run> 6`, keeps its
arguments. A reply to an older alert, or to a message that is not an
alert, works like a message without a reply.

A custom command sent as a reply gets four more placeholders from the
run: `{handle}`, `{run_id}`, `{run_root}` and `{host}`. The values come
from the database, not from the phone. This entry opens a Claude session
in the tree of the run:

```toml
[telegram.commands.claude_run]
help = "as a reply to an alert: a Claude session in the run tree"
run = ["tmux", "new-session", "-d", "ssh -t {host} 'cd {run_root} && claude remote-control'"]
reply = "Claude session for {handle} started on {host}"
```

The same command without a reply answers
`/claude_run: missing placeholder {host} in '...'` and runs nothing.

### One bot for every project

One bot in one chat serves every registered project, and the bold first
line of every message names the project; [projects.md](projects.md)
shows what else the projects share. Telegram lets one consumer poll
a bot token, so only the holder of `~/.edr/serve.lock` polls: `edr serve`,
or without it the first watcher that takes the lock. Every other watcher
sends its alerts and polls nothing.

A command acts on every project, or names one as its first word:

- `/status`, `/events`, `/hosts`, `/tools` and `/digest` answer for every
  project. `/status demo` and `/events demo 20` answer for `demo` alone.
- A handle names a run of any project. When two projects know the
  handle, the bot refuses it and names both; write `demo/b_nodw@demo`
  then.
- A reply to an alert acts on the run of that alert.
- `/board`, `/csv` and `/metric` need a project. They take the first
  word, else the project of the alert they reply to, else the only
  project.

### Topics

A Telegram group with Topics on is a forum: each topic is a thread of
its own. With `topics = true` in `[telegram]` of `user.toml`, each
project gets a topic with its name, which the watcher of the project
makes the first time it sends. The topic holds the alerts of the
project, and the pinned board of a watcher that runs without a
supervisor. The global board and the digest of `edr serve` stay in the
main thread. A command in the topic of a project acts on that project:
`/status` there shows its board and `/events` its events. The bot
answers every command in the thread it came from.

To set it up:

1. Make a group and turn on Topics in the group settings.
2. Add the bot and make it an admin with the rights to pin messages and
   to manage topics. An admin bot also receives the plain words of the
   reply keyboard.
3. Set `topics = true` and restart `edr serve`:

   ```toml
   [telegram]
   chat_id = -1001234
   topics = true
   ```

The watcher of a project keeps the id of its topic in the `store` table
of the project database, under `telegram`. A topic that you delete is
made again with the next message. Without the right to manage topics,
the bot logs a warning and sends to the main thread.

## What each alert says

Each example shows the Telegram message as it reads on the phone, with
each button as `[label]`. The first example shows the four log lines of
an alert; the others show one line to stay short. ntfy and mail carry
the same text; mail indents each command and each log line by four
spaces, and a button becomes a line `<label>: <command>`.
[reference/states.md](../reference/states.md) says when the watcher
finds each state.

`dead`: the heartbeat is old and the driver is gone.

```
🔴 demo: driver gone for b_nodw@demo
The run has written no heartbeat for 50m, and its driver 4711 is gone from hostA. Nothing runs until it is resumed.

stage: synth, step 3 elaborate
host: hostA

Information: reading top.v
Information: elaborating top
Warning: latch inferred for q (VER-2)
Information: 4 designs read

Resume it from the last step:
edr continue b_nodw@demo --stage synth --from elaborate
edarunner resumes it once by itself when the stage has a resume command.
```

`hung`: the run is alive but nothing changes.

```
🔴 demo: no progress in b_nodw@demo
The run is alive, but it has shown no progress since 14.01 03:00: the step, the log, the tree size and the CPU time stand still. The tool may wait for a licence, or it is stuck.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

If it is stuck, stop it:
edr stop b_nodw@demo --why hung
edarunner leaves it alone, since kill_hung is off.
+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.

[Stop]  [+6h]  [+12h]  [+24h]
```

`looping`: the same failure, `streak` times in a row.

```
🔴 demo: same failure again in b_nodw@demo
The last 2 tasks failed with the same error, so the driver starts no new task. More retries would fail the same way.

stage: synth, step 3 elaborate
host: hostA
tasks: 4 done, 3 failed

Information: elaborating top

Read the log and fix the cause, then stop the run:
edr stop b_nodw@demo --why looping
+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.

[Stop]  [+6h]  [+12h]  [+24h]
```

`over_budget`: a stage went past its `budget`.

```
🔴 demo: over budget in b_nodw@demo
Stage synth went past its budget of 1 h and 1 GB. The stage runs to its end; more time on the budget before then lets the run go on.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

Stop it now if the rest of the stage is of no use:
edr stop b_nodw@demo --why over-budget
+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.

[Stop]  [+6h]  [+12h]  [+24h]
```

When the stage has run to its end, the run ends `OVER_BUDGET` and the
same message changes. The buttons of the live run go, and a run whose
tree has stages left gets Continue:

```
🔴 demo: over budget in b_nodw@demo
Stage synth went past its budget of 1 h and 1 GB. The run ended OVER_BUDGET. It did not run pnr.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

Run the stages left on the same tree:
edr continue b_nodw@demo
Continue runs pnr as one new run on hostA, in the tree /scratch/user/edr/demo/20260926_1200_b_nodw_demo_DW0_gabc1234. On Telegram it asks once more.

[Continue]
```

`host_full`: the free scratch of a host is below its floor, `host_free_min_gb` of the site or of the host.

```
🟡 demo: disk almost full on hostA
hostA has less than 100 GB of free scratch, so the driver starts nothing new there, and the full disk blocks every other user of hostA. edarunner stops the newest run on hostA 1h after this alert unless the disk gets back above the floor.

stage: synth, step 3 elaborate
run: b_nodw@demo

Information: elaborating top

Free scratch on the host, or stop this run:
edr stop b_nodw@demo --after-task --why host-full
Stop ends the run after its current task, and Free space removes the prune targets netlist of the finished runs of demo on hostA whose trees have no stage left. On Telegram both ask once more.

[Stop]  [Free space]
```

`superseded`: a newer batch runs the same label at another source.

```
🟡 demo: newer run replaces b_nodw@demo
A newer run of the same label runs at another source (20260927_0900_b_nodw_demo_gdef5678). edarunner stops this run after its running task, 1h after this alert, unless it has a keep.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

To keep it running:
edr keep b_nodw@demo --hours 12
+6h, +12h and +24h give the run that much more time on its budget, and for that long edarunner takes no automatic action on it unless its host runs out of scratch. Stop ends the run after its current task; on Telegram it asks once more.

[Stop]  [+6h]  [+12h]  [+24h]
```

`held`: the scheduler holds the job.

```
🟠 demo: scheduler holds b_nodw@demo
The scheduler holds the job, and it starts only after someone releases it.

job: slurm:4711

Release it with the scheduler, or cancel it:
edr stop b_nodw@demo --why held
```

`incomplete`: the run ended with failed or skipped tasks.

```
🟠 demo: failed tasks in b_nodw@demo
The run ended with 3 failed and 1 skipped tasks. Their results are missing.

stage: synth, step 3 elaborate
host: hostA
tasks: 4 done, 3 failed, 1 skipped

Information: elaborating top

See which tasks failed and the log tail:
edr status b_nodw@demo
```

`failed`: the run ended `FAILED`.

```
🔴 demo: failed run b_nodw@demo
The run ended in stage synth with exit code 1.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

See the stage that failed and the log tail:
edr status b_nodw@demo
```

`killed`: a signal ended the run.

```
🔴 demo: killed run b_nodw@demo
A signal ended the run (SIGTERM), so its last stage did not finish.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

See the last stage and the log tail:
edr status b_nodw@demo
```

`stopped`: a stop file ended the run, after `edr stop --after-task` or
the Stop button, or after the stop of a `superseded` run. A run that
stopped at the end of a stage with stages left gets Continue; any other
gets the command that shows its last stage and log tail.

```
⚫ demo: stopped run b_nodw@demo
A stop ended the run in stage synth. It did not run pnr.

stage: synth, step 3 elaborate
host: hostA

Information: elaborating top

Run the stages left on the same tree:
edr continue b_nodw@demo
Continue runs pnr as one new run on hostA, in the tree /scratch/user/edr/demo/20260926_1200_b_nodw_demo_DW0_gabc1234. On Telegram it asks once more.

[Continue]
```

`orphan`: a tool process of yours that no live run owns. The watcher
that holds `serve.lock` checks the processes once for every registered
project. It reads `EDR_RUN_ID` from the environment of the process; the
driver sets it for every stage command of every project.

- A live run of a registered project owns the process.
- A run that ended, or whose driver is gone after `dead_s`, leaves it an
  orphan, and the alert names that run as `project/label@batch`.
- A run id that no registered project knows is left alone, unless the
  working directory or the command line of the process lies in the tree
  `/<project>/<run_id>` of a registered project under its safety
  marker.
- A process without `EDR_RUN_ID` is owned when its working directory or
  command line holds the safety marker of a registered project. Such a
  process belongs to no project, so edarunner never kills it.

A tool process that no run owns:

```
🔴 demo: tool process with no run on hostA
Your process fc_shell runs on hostA, and no edarunner run owns it. It may hold a licence seat.

process: fc_shell, pid 5120
running for: 3h
directory: /home/me/work
fc_shell -f /home/me/work/run.tcl

Check it:
ssh hostA ps -o pid,etime,args -p 5120
If it is yours and stale, end it:
ssh hostA kill 5120
edarunner never kills a process that belongs to no project.
```

A tool that a dead run left behind:

```
🔴 demo: tool process of an ended run on hostA
Your process fc_shell runs on hostA for the run demo/b_nodw@demo, whose driver is gone. It may hold a licence seat.

process: fc_shell, pid 5120
running for: 2d
directory: /scratch/me/edr/demo/20260926_1200_b_nodw_demo_gabc1234/pnr
fc_shell -f /scratch/me/edr/demo/20260926_1200_b_nodw_demo_gabc1234/pnr/run.tcl

Check it:
ssh hostA ps -o pid,etime,args -p 5120
If it is yours and stale, end it:
ssh hostA kill 5120
edarunner never kills it, since kill_orphan is off.
```

`clock`: the clock of a host is more than 60 s off the head node.

```
🔴 demo: clock off on hostA
The clock of hostA is 75 s ahead of the head node. A heartbeat and a seat lease carry the time of the host, so the stale, dead and lease windows move by as much.

skew: +75 s

Ask the admins to sync the host with NTP. edr hosts names every host whose clock is off.
```

`watch`: the supervisor saw a watcher exit or stand still, or a check
found no cycle for three heartbeats. The first line names the project
`edr` for a message of the supervisor.

```
🔴 edr: watcher exited for demo
The watcher of demo exited with code 1. The supervisor starts it again in a minute, and then after 2, 4, 8, 16 and at most 30 minutes while it keeps exiting.

project: /home/me/myflow

Its log is in the journal of the supervisor:
journalctl --user -u edr-serve
```

```
🔴 edr: supervisor stopped
The supervisor has not finished a cycle for 14m. No project is watched until it runs again.

pid: 3141

See why it stopped:
systemctl --user status edr-serve
Start it again:
systemctl --user restart edr-serve
```

`edr watch --check` found no cycle of a lone watcher for three heartbeats:

```
🔴 demo: watcher stopped
The watcher has not finished a cycle for 11m. No alert arrives until it runs again.

pid: 3141
project: /home/me/myflow

Run one cycle that writes nothing; it prints the error that stops the watcher:
edr watch --dry-run
Then restart the watcher service.
```

`config`: a project file stopped loading while the watcher runs. The
watcher goes on with the last config that loaded, and the alert comes
again only when the error text changes.

```
🔴 demo: config does not load
The watcher goes on with the last config that loaded. It reads the heartbeats, sends the alerts and collects the results, but it resumes and launches nothing until the file loads again.

project: /home/me/myflow
/home/me/myflow/edr.toml: unknown key 'limits.stale'

See every problem of the project files:
edr check
```

## ntfy

```toml
[ntfy]
topic = "edr-3c9f1e7a52b4"
url = "https://ntfy.sh"                             # the default
token_file = "~/.config/edarunner/ntfy.token"       # for a protected topic only
```

The table goes into `user.toml`. Subscribe to the topic in the ntfy
app. Anyone who knows the name of a topic on a public server can read
it, so use a long random name, or a protected topic with a token. A
dead, failed or killed run, a full host and a stale watcher come with
the urgent priority. The daily digest and the board come with a low
one, and `edr notify --silent` with the lowest.

## Mail

```toml
[mail]
host = "smtp.example.org"
port = 587                                          # the default
from = "edr@example.org"
to = ["me@example.org"]
starttls = true                                     # the default
password_file = "~/.config/edarunner/smtp.password" # without it there is no login
```

The table goes into `user.toml`. The login name is `user`, or the
`from` address when `user` is not set. A mail goes out per alert, per
daily digest and per `edr notify`. The watcher never mails the board.
Send it with `edr notify --board`, or as plain text with
`edr notify "$(edr status)"`.

## The daily digest

With `digest_at = "08:00"` in `user.toml`, the supervisor sends one
message a day for every registered project, at its first cycle after
08:00 local time. A watcher without a supervisor that holds
`~/.edr/serve.lock` sends it in the same way. The message starts with
the time of the last digest. Each project with a run that ended since
then, or with a live run, gets a block with the parts that have a run:

- ended: the runs that ended since the last digest, with their states;
- live: the live runs, with the stage and the time since the start;
- queued: the queued runs;
- open alerts: live runs in an alert state without a keep that still
  holds.

Any other project gets one line:

```
edr: digest
since 14.01 08:00

demo
ended
⚪ a@demo done
live
🔴 h@demo synth, 2h
🟢 c@demo synth, 2h
queued
🔵 q@demo
open alerts
🔴 h@demo hung

power nothing ended, nothing live
```

The digest reads the database of each project and probes nothing. The
day and the time of the last digest live in `~/.edr/store.json` under
`digest`; the first digest covers the last 24 hours. `/digest` sends
the same text at any time, `edr status --digest` prints the block of
the current project on the terminal, and `edr status --digest --all`
the whole digest. None of them moves the start of the next digest.

## edr notify

`edr notify TEXT` sends one message with the project name in the bold
first line. On Telegram it goes to the chat, or to the topic of the
project:

```sh
edr notify "session myflow: the sweep is done"
edr notify --silent "session myflow: waiting for input"
edr notify --dry-run "test"       # prints the message, sends nothing
edr notify --board                # the board as a new message
edr notify --digest               # the digest of every project now
```

It sends through every channel that is on, so ntfy and mail get the
message too. It exits 1 when no channel is configured or a send failed.
It runs from any directory below `edr.toml`.

A Claude Code hook can call `edr notify`, so that a session reports to
your phone; [agents.md](agents.md#report-to-the-phone) shows the hooks.
