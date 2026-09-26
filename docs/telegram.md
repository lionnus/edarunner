# The Telegram bot

The bot is a thread of `edr watch`. It sends alerts with two buttons,
keeps one pinned board message, and answers commands from one chat. It
uses long polling over outbound HTTPS, so it needs no open port and no
webhook. The code is `src/edarunner/notify/telegram.py`, standard library
only.

## Set up the bot

1. Open @BotFather in Telegram and send `/newbot`. Give the bot a name
   and a user name that ends in `bot`. BotFather answers with the token.
2. Write the token to `~/.config/edarunner/telegram.token` and set the
   mode to 600:

   ```sh
   umask 077; echo '123456:ABC...' > ~/.config/edarunner/telegram.token
   ```

   The bot does not start when the file is missing or readable by the
   group or by others. `edr watch` logs the reason.
3. Add the section to `site.toml`:

   ```toml
   [telegram]
   token_file = "~/.config/edarunner/telegram.token"
   chat_id = 0
   ```

4. Open the new bot on the phone and send `/start`. Start `edr watch`.
   With `chat_id = 0` the bot obeys nobody. It prints the chat id of the
   first message it receives to stderr:

   ```
   telegram: the first message came from chat 987654321; set chat_id = 987654321 in site.toml
   ```

5. Put that number in `chat_id` and restart `edr watch`. The bot now
   publishes its command menu with `setMyCommands` and answers.

## Alerts

The watcher sends one message per event class per run. A repeat for the
same class and run edits that message in place, so an alert never
repeats. An alert carries two inline buttons:

| Button | `callback_data` | Action |
|---|---|---|
| keep 12h | `keep12:<handle>` | `edr keep <handle> --hours 12` |
| ack | `ack:<handle>` | `edr keep <handle> --ack` |

The bot answers every press, appends the result to the alert text, and
keeps the buttons.

## The board

The board is one message in an HTML `<pre>` block, pinned once and
edited silently on every watcher cycle. Its message id lives in
`data/board/telegram.json`, so a restart edits the same message.
`/board` unpins the old message and pins a new one at the bottom of the
chat.

## Built-in commands

A handle is `label@batch`, a run id prefix, or `#n` from the last board.

| Command | Effect |
|---|---|
| `/status` | the narrow board |
| `/events [n]` | the last `n` events, default 10 |
| `/hosts` | free cores, RAM and scratch per host |
| `/lic` | free licence seats |
| `/board` | pin a new board message |
| `/keep <handle> [hours]` | add hours to the running stage or task, default 12 |
| `/ack <handle>` | cancel a pending kill |
| `/stop <handle> [why]` | stop after the running task; never a kill |
| `/compare <handle>...` | metrics side by side |
| `/metric <name> [--design H]` | one metric for every run of a design |
| `/help` | the list above plus the custom commands |

Every reply is a `<pre>` block with the last 4000 characters of the
output.

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

| Key | Meaning |
|---|---|
| `help` | the line in the `/` menu and in `/help` |
| `run` | the argv list; never a shell string |
| `args` | argument name to regex, in order; the last argument takes the rest of the message |
| `skip_if` | an argv list; exit 0 makes the bot reply `skip_reply` and run nothing |
| `skip_reply` | the reply when `skip_if` passes, default `skipped` |
| `reply` | the reply on exit 0 instead of the output |
| `detach` | start the command in its own session and reply with the pid; the output goes to `data/board/telegram-<name>.log` |
| `timeout_s` | kill the command after this many seconds, default 60 |
| `cwd` | the working directory, default `{root}` |
| `dry_run` | reply with the rendered argv and run nothing |

Placeholders render per argument: `{project}`, `{root}` and
`{project_root}` (the project directory), `{site_dir}`, `{user}`, and one
per name in `args`. No shell runs between the bot and `run[0]`. A program
that parses its argument itself, such as `tmux new-session <cmd>`,
`ssh host <cmd>` or `sh -c`, does run a shell on the rendered value. Gate
every placeholder inside such a token with an exact allowlist regex, as
the `claude` example does.

The regex gate: every value must match its regex in full, or the bot
replies `refused: <name> must match <regex>`, records the refusal in the
ledger, and runs nothing. Write the regex as an allowlist of the exact
values you expect.

## Security

- The token is one secret. Keep it in a file with mode 600; the bot
  refuses any other mode.
- The bot obeys one `chat_id`. Every other chat gets no answer, and the
  first message from it makes one ledger event `rejected`.
- The command set, the argument shapes and the working directory come
  from `site.toml` on the head node. The phone chooses among those
  entries and fills gated slots.
- Every command, button press and refusal lands in `events` with actor
  `telegram`.
- A 429 from Telegram makes the bot wait `retry_after` seconds.

## What the bot never does

- It never runs a shell string or free text.
- It never kills a process. `/stop` writes the `after-task` stop file.
- It never runs `retire`, `prune`, `launch` or `rm`.
- It never answers a chat outside the allowlist.
- It writes only `data/board/` itself; `/keep`, `/ack` and `/stop` write
  the keep file and the stop file under `state/<batch>/` through the same
  verbs as the CLI.
