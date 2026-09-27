# Notifications

After this page the alerts of a project reach you by Telegram, ntfy or
mail. Each channel is a table in `site.toml`, and `edr watch` uses every
table it finds. [reference/configuration.md](reference/configuration.md)
lists the keys.

| Channel | Alerts | Buttons | Board | Commands |
|---|---|---|---|---|
| Telegram | one message per alert, edited in place | keep, ack, stop | one pinned message | yes |
| ntfy | one push per alert, priority by kind | a line with the command | no | no |
| mail | one mail per alert | a line with the command | no | no |

A channel whose secret file is missing, or readable by the group or by
others, stays off. `edr watch` logs the reason. `edr notify "<text>"`
sends one message through every channel that is on.

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
and a stale watcher come with the urgent priority. The daily digest comes
with a low one, and `edr notify --silent` with the lowest.

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
out per alert, per daily digest and per `edr notify`. The board changes
every minute, so it is never mailed. To mail it on request, send it with
`edr notify "$(edr status)"`.
