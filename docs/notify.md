# Notifications

edarunner sends its alerts to Telegram, ntfy and mail. Each channel is a
table in `site.toml`, and `edr watch` sends every alert to every channel
it finds there. [reference/configuration.md](reference/configuration.md)
lists the keys.

| Channel | Alerts | Buttons | Board | Commands |
|---|---|---|---|---|
| Telegram | one message per alert, edited in place | keep, ack, stop | one pinned message | yes |
| ntfy | one push per alert, priority by kind | a copy button and a line per command | on request | no |
| mail | one mail per alert | a line per command | on request | no |

A channel whose secret file is missing, or readable by the group or by
others, stays off. `edr watch` logs the reason. `edr notify "<text>"`
sends one message through every channel that is on.

## Message kinds

Every channel gets every kind with the same title and the same text.
The title is `<project>: <kind>`, and an alert title adds the run handle.

| Kind | Sent by | Telegram | ntfy | mail |
|---|---|---|---|---|
| alert: `dead`, `hung`, `looping`, `over_budget`, `host_full`, `superseded`, `held`, `incomplete`, `failed`, `killed` | the watcher, when a run enters the state or its reason changes | one message, edited in place, with the next command and the keep, ack and stop buttons | one push per change, with the next command, the button commands and three copy buttons | one mail per change, with the next command and the button commands |
| alert: `orphan` | the watcher | one message with the kill command, no buttons | one push, the same text | one mail, the same text |
| `watch stale` | `edr watch --check` | one message | one urgent push | one mail |
| `digest` | the watcher once a day at `digest_at`, and `edr notify --digest` | one message | one low push | one mail |
| `board` | the watcher every cycle | one pinned message, edited in place | none | none |
| `board` on request | `edr notify --board` | one new message | one low push | one mail |
| `note` | `edr notify TEXT` | one message | one push, the lowest priority with `--silent` | one mail |
| a command reply, the result of a detached command, a button answer | the bot, for a command from the chat | a reply in the chat | none; ntfy takes no commands | none; mail takes no commands |
| a collect failure, a lease sweep, a resume | the watcher | an event only, in `edr events` and `/events` | an event only | an event only |

ntfy and mail have no callback buttons. The alert text carries each
button as a line `<label>: <command>`, for example
`ack: edr keep a@demo --ack`. On ntfy a copy button also puts the
command on the clipboard. A server without copy buttons refuses the
push with a 400, and the channel sends it again without the buttons.

The board changes every cycle, so the watcher keeps a live copy of it
only as the pinned Telegram message. You can send it, or the digest, to
every channel on request:

```sh
edr notify --board            # the board of edr status
edr notify --digest           # the digest now; the watcher still sends its own
```

A cron line mails the board every morning:

```sh
0 7 * * * cd ~/myflow && edr notify --board
```

## Telegram

The bot sends the alerts, keeps the board and answers commands from one
chat. [telegram.md](telegram.md) shows the setup.

## ntfy

```toml
[ntfy]
topic = "edr-3c9f1e7a52b4"
url = "https://ntfy.sh"                             # the default
token_file = "~/.config/edarunner/ntfy.token"       # for a protected topic only
```

Subscribe to the topic in the ntfy app. Anyone who knows the name of a
topic on a public server can read it, so use a long random name, or a
protected topic with a token. A dead, failed or killed run, a full host
and a stale watcher come with the urgent priority. The daily digest and
the board come with a low one, and `edr notify --silent` with the lowest.

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

The login name is `user`, or the `from` address without it. A mail goes
out per alert, per daily digest and per `edr notify`. The watcher never
mails the board. Send it with `edr notify --board`, or as plain text with
`edr notify "$(edr status)"`.
