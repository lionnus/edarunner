# The Telegram bot

The bot is a thread of `edr watch`. It sends alerts with two buttons,
keeps one pinned board message, and answers commands from one chat. It
uses long polling over outbound HTTPS, so it needs no open port and no
webhook. `docs/reference/configuration.md` lists the keys of
`[telegram]`.

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

Each run line starts with one mark for its state;
`docs/reference/states.md` lists them.

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

`docs/reference/bot.md` lists every built-in command with its arguments.
A handle is `label@batch`, a run id prefix, or `#n` from the last board.

`/status <handle>` shows the mark, the handle and the state, then the
stage and step, the host and the age, the proposed command in monospace,
and the last log line in a `<pre>` block. `/events` shows one line
`HH:MM kind handle` per event, the kind in bold, and the reason indented
under it in italics; it shows a handle in place of a run id. `/hosts`
shows one line per host, `🟢 hostA · 21/32 cores · 195/1538 GB free · gpu -`,
and `no answer` for a host that fails the probe. `/tools` shows
`demo · 3/8 seats free · 2 hosts` per tool. `/help` is prose, so a tap on a
command sends it. `/compare` and `/metric` reply with a `<pre>` block
of aligned columns.

A custom command replies with the output of its program as it is. Give
the program a narrow format, or the phone wraps the lines.

## Custom commands

Everything beyond the built-in list comes from `[telegram.commands.*]`
in `site.toml`, one table per command:

```toml
[telegram.commands.survey]
help = "free cores, RAM and scratch on every host"
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

`docs/reference/bot.md` lists every key, the placeholders a string
renders, and the regex gate on every argument. Write each regex as an
allowlist of the exact values you expect, as the `claude` example does.

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

The table in `edr.toml` replaces `token_file`, `chat_id` and `user_id`
of the site for this project only; the custom commands stay in
`site.toml`. Keep `telegram_poll = true` in a project with its own bot.

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
