# The Telegram bot

The bot is a thread of `edr watch`. It sends alerts with two buttons,
keeps one pinned board message, and answers commands from one chat. It
uses long polling over outbound HTTPS, so it needs no open port and no
webhook. `docs/configuration.md` lists the keys of `[telegram]`.

## Set up the bot

1. Open @BotFather in Telegram and send `/newbot`. Give the bot a name
   and a user name that ends in `bot`. BotFather answers with the token.
2. Write the token to `~/.config/edarunner/telegram.token` and set the
   mode to 600:

   ```sh
   umask 077; echo '123456:ABC...' > ~/.config/edarunner/telegram.token
   ```

   The bot does not start when the file is missing or when the group or
   others can read it. `edr watch` logs the reason.
3. Add the section to `site.toml`:

   ```toml
   [telegram]
   token_file = "~/.config/edarunner/telegram.token"
   chat_id = 0
   user_id = 0
   ```

4. Open the new bot on the phone and send `/start`. Start `edr watch`.
   With `chat_id = 0` the bot obeys nobody, but it prints the chat id of
   the first message it receives to stderr:

   ```
   telegram: the first message came from chat 987654321; set chat_id = 987654321 in [telegram]
   ```

5. Put that number in `chat_id` and restart `edr watch`. The bot now
   publishes its command menu with `setMyCommands` and answers.

`user_id` is optional. When it is set, the bot obeys the messages and
the button presses of that one user and ignores every other member of
the chat. When it is unset, the chat is the only gate, so the chat must
be a private one; `edr watch` logs a warning to say so. Your user id is
the `from.id` of a message; a bot such as @userinfobot shows it.

## Replies on the phone

Every message starts with one bold line that names the project, so the
messages of two projects in one chat stay apart. Under that line, a
reply is formatted text: one short line per item, a run handle in
monospace, and a count or a note in italics. A tap on a handle copies
it, so you can paste it into `/status <handle>`.

Each run line starts with one mark for its state:

| Mark | States |
|---|---|
| 🟢 | running |
| 🔵 | queued |
| 🟡 | stale, host_full, superseded |
| 🔴 | dead, hung, looping, over_budget, orphan, failed, killed |
| 🟠 | incomplete |
| ⚪ | done |
| ⚫ | retired, stopped, imported |

A `<pre>` block holds only text whose width the bot does not control:
the last log line of `/status <handle>`, the columns of `/compare` and
`/metric`, and the output of a custom command. A message stays under
the limit of 4096 characters; the bot cuts a long reply at a line end.

## Alerts

The watcher sends one message per event class per run. A repeat for the
same class and run edits that message in place, so an alert never
repeats. An alert looks like this:

```
🔴 demo · dead b_nodw@demo
heartbeat older than 90 s, driver 4711 gone on local
edr run b_nodw@demo --stage synth --from elaborate
```

The first line holds the mark of the state, the project and the state
in bold, and the handle in monospace. The second line is the reason.
The third line is the one command that `edr status --triage` proposes
for the run, in monospace. The alert carries two inline buttons:

| Button | `callback_data` | Action |
|---|---|---|
| keep 12h | `keep12:<handle>` | `edr keep <handle> --hours 12` |
| ack | `ack:<handle>` | `edr keep <handle> --ack` |

The bot answers every press, appends the result to the alert text, and
keeps the buttons. A press records one ledger event: the `keep` event of
the action, with the actor `telegram`.

## The board

The board is one message, pinned once and edited silently on every
watcher cycle. Its first line holds the project name and the time of
the last edit. Under it, each run has one line: the mark, the handle,
the state, and the age. A live run also shows its stage and its step
of the total. Live runs come first. The last line, in italics, holds
the count per state:

```
demo · board 14:05
🔴 a@demo dead · synth 3/13 · 1h
🟢 c@demo pnr 4/13 · 0m
⚪ b@demo done · 1h
1 dead · 1 running · 1 done
```

The board shows at most 30 runs and then a line `… and N more`. When
no run is live, a line `nothing live` comes before the counts. Its message id lives in the
ledger's `kv` table under `telegram`, so a restart edits the same message.
`/board` unpins the old message and pins a new one at the bottom of the
chat.

## Built-in commands

A handle is `label@batch`, a run id prefix, or `#n` from the last board.

| Command | Effect |
|---|---|
| `/status` | the board, as pinned |
| `/status <handle>` | the state, stage, step, age, host and last log line of one run |
| `/events [n]` | the last `n` events, default 8, at most 30, newest first |
| `/hosts` | cores, RAM, scratch and GPUs per host, each as used of total |
| `/lic` | licence seats, used of total |
| `/board` | pin a new board message |
| `/keep <handle> [hours]` | add hours to the running stage or task, default 12 |
| `/ack <handle>` | cancel a pending kill |
| `/stop <handle> [why]` | `edr stop --after-task`; never a kill |
| `/compare <handle>...` | metrics side by side |
| `/metric <name> [--design H]` | one metric for every run of a design |
| `/help` | the commands by purpose, plus the custom commands |

`/status <handle>` shows the mark, the handle and the state, then the
stage and step, the host and the age, the proposed command in monospace,
and the last log line in a `<pre>` block. `/events` shows one line
`HH:MM kind handle` per event, the kind in bold, and the reason indented
under it in italics; it shows a handle in place of a run id. `/hosts`
shows one line per host, with the mark of `edr hosts` and the used of
total of every resource:

```
hostA · 🟢 cores 21/32 · 🟡 ram 93/376 GB · 🟢 scratch 195/1538 GB · 🟢 gpu 0/1
```

The hosts come in the order of `edr hosts`, the worst mark first. A host
without a GPU has no `gpu` part, and a host that fails the probe shows
`⚫ no answer`. `/lic` shows `demo · 3/8 seats used` per licence.
`/help` is prose, so a tap on a command sends it. `/compare` and
`/metric` reply with a `<pre>` block of aligned columns.

A custom command replies with the output of its program as it is. Give
the program a narrow format, or the phone wraps the lines.

## Custom commands

Everything beyond the built-in list comes from `[telegram.commands.*]`
in `site.toml`, one table per command:

```toml
[telegram.commands.survey]
help = "cores, RAM and scratch on every host"
run = ["edr", "hosts", "--narrow"]
timeout_s = 60

[telegram.commands.claude]
help = "open a Claude remote-control session: /claude <dir>"
args = { dir = "^(backend|paper|rtl|sw)$" }
skip_if = ["tmux", "has-session", "-t", "claude-{project}-{dir}"]
skip_reply = "already open: claude-{project}-{dir}"
run = ["tmux", "new-session", "-d", "-s", "claude-{project}-{dir}", "-c", "{root}/{dir}", "claude remote-control --name {project}-{dir}"]
reply = "session claude-{project}-{dir} started; open the Claude app"
```

| Key | Meaning |
|---|---|
| `help` | the line in the `/` menu and in `/help` |
| `run` | the argv list; never a shell string |
| `args` | argument name to regex, in order; the last argument takes the rest of the message |
| `skip_if` | an argv list; exit 0 makes the bot reply `skip_reply` and run nothing |
| `skip_reply` | the reply when `skip_if` passes, default `skipped` |
| `reply` | the reply on exit 0 instead of the output |
| `detach` | start the command in its own session and reply with the pid; the output goes to `data/telegram-<name>.log` |
| `timeout_s` | kill the command after this many seconds, default 60 |
| `cwd` | the working directory, default `{root}` |
| `dry_run` | reply with the rendered argv and run nothing |

Placeholders render per argument: `{project}`, `{root}` and
`{project_root}` (the project directory), `{site_dir}`, `{user}`, and one
per name in `args`. A command sent as a reply to an alert also gets
`{handle}`, `{run_id}`, `{run_root}` and `{host}` of the run of that
alert. No shell runs between the bot and `run[0]`. A program
that parses its argument itself, such as `tmux new-session <cmd>`,
`ssh host <cmd>` or `sh -c`, does run a shell on the rendered value, so
gate every placeholder inside such a token with an exact allowlist regex,
as the `claude` example does.

The regex gate works like this: every value must match its regex in full,
or the bot replies `refused: <name> must match <regex>`, records the
refusal in the ledger, and runs nothing. Write the regex as an allowlist
of the exact values you expect.

## Reply to an alert

A command sent as a reply to an alert acts on the run of that alert, so
it needs no handle. The bot keeps the message id and the run id of every
alert of the last 7 days in the ledger's `kv` table, under `telegram`.

| Reply | Same as |
|---|---|
| `/keep 24` | `/keep <run> 24` |
| `/ack` | `/ack <run>` |
| `/stop disk full` | `/stop <run> disk full` |
| `/status` | `/status <run>` |

A reply that names the run itself, such as `/keep <run> 6`, keeps its
arguments. A reply to an older alert, or to a message that is not an
alert, works like a message without a reply.

A custom command sent as a reply gets four more placeholders from the
run: `{handle}`, `{run_id}`, `{run_root}` and `{host}`. The values come
from the ledger, not from the phone. This entry opens a Claude session
in the tree of the run:

```toml
[telegram.commands.claude_run]
help = "as a reply to an alert: a Claude session in the run tree"
run = ["tmux", "new-session", "-d", "ssh -t {host} 'cd {run_root} && claude remote-control'"]
reply = "Claude session for {handle} started on {host}"
```

The same command without a reply answers `bad placeholder 'host'` and
runs nothing.

## edr notify

`edr notify TEXT` sends one message to the chat, or to the topic of the
project, with the project name in the bold first line:

```sh
edr notify "session backend: the sweep is done"
edr notify --silent "session backend: waiting for input"
edr notify --dry-run "test"       # prints the message, sends nothing
```

It exits 1 when no bot is configured or the send failed. It runs from
any directory below `edr.toml`.

A Claude Code hook can call it, so a session reports to the phone. Put
this into `.claude/settings.json` of the repository where the session
runs, and replace the path with the project directory:

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command",
      "command": "cd ~/work/backend && edr notify \"session $(basename \"$CLAUDE_PROJECT_DIR\"): $(jq -r .message)\""}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "cd ~/work/backend && edr notify --silent \"session $(basename \"$CLAUDE_PROJECT_DIR\"): turn ended\""}]}]
  }
}
```

The `Notification` hook gets a JSON object on stdin, and `jq` takes its
`message`. The `Stop` hook runs at the end of every turn, so it sends
silently.

## One chat, or one per project

The default is one bot in one chat for every project of a site. The bold
first line of every message names the project, so the messages of two
projects stay apart.

Telegram lets one consumer poll a bot token. Two watchers on one token
fight over the updates and each sees half of them. So with one bot, set
`telegram_poll = false` in `edr.toml` of every project but one. A
project without the poll sends its alerts and its board to the chat, but
its alerts carry no buttons. The commands reach the one watcher that
polls, and act on its project only.

A chat per project, such as one Telegram group per project, needs a bot
per project, because each watcher must poll its own token. To set it
up:

1. Make one more bot with @BotFather, as in "Set up the bot". Write its
   token to its own file, mode 600, for example
   `~/.config/edarunner/myflow.token`.
2. Make a group, add the bot, and send `/start` in the group.
3. Add a `[telegram]` table to `edr.toml` of that project:

   ```toml
   [telegram]
   token_file = "~/.config/edarunner/myflow.token"
   chat_id = 0
   ```

4. Restart the watcher of that project. It prints the chat id of the
   group to stderr. A group id is negative. Put it in `chat_id` and
   restart the watcher again.

The table in `edr.toml` replaces `token_file`, `chat_id`, `user_id` and `topic_id`
of the site for this project only; the custom commands stay in
`site.toml`. Keep `telegram_poll = true` in a project with its own bot.

## Topics: one group, one thread per project

A Telegram group with Topics on is a forum: each topic is a thread with
its own id. `topic_id` in `[telegram]` puts every message of a project
into one thread: the alerts, the board, the replies and the pinned
board. The bot then obeys a command or a button press only when it
comes from that thread. It ignores a command from another thread
without an event, because the watcher of another project answers it.

To set it up:

1. Make a group and turn on Topics in the group settings.
2. Add the bot of each project and make it an admin with the right to
   pin messages. An admin bot also receives the plain words of the
   reply keyboard.
3. Make one topic per project.
4. Start the watcher of the project without `topic_id` and send
   `/status` in its topic. The watcher prints the id of the topic to
   stderr:

   ```
   telegram: a message came from topic 17 of chat -1001234; set topic_id = 17 in [telegram] of edr.toml
   ```

5. Put the id into `edr.toml` of that project and restart its watcher:

   ```toml
   [telegram]
   chat_id = -1001234
   topic_id = 17
   ```

Without `topic_id`, the bot answers a command in the thread it came
from, and it sends its alerts and its board to the main thread. Each
project that answers commands still needs its own bot, because one
token has one poller.

## Security

- The token is the one secret. Keep it in a file with mode 600; the bot
  refuses any other mode.
- The bot obeys one `chat_id`, and one `user_id` when it is set. Every
  other chat or user gets no answer, and the first message from it makes
  one ledger event `rejected`.
- The command set, the argument shapes and the working directory come
  from `site.toml` on the head node. The phone chooses among those
  entries and fills the gated slots.
- Every command, action and refusal lands in `events` with the actor
  `telegram`. A button press or `/keep`, `/ack` and `/stop` records the
  event of its action only.
- A 429 from Telegram makes the bot wait `retry_after` seconds.

## What the bot never does

- It never runs a shell string or free text.
- It never kills a process. `/stop` writes the `after-task` stop file.
- It never runs `retire`, `prune`, `launch` or `rm`.
- It never answers a chat outside the allowlist.
- It writes only `data/board/` itself; `/keep`, `/ack` and `/stop` write
  the keep file and the stop file under `state/<batch>/` through the same
  verbs as the CLI.
