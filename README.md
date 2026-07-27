# Claude Code Watcher — TUI

> [Version française](README_FR.md)

A terminal UI (Textual) that monitors all running Claude Code sessions on your machine in a live table — keyboard-driven, runs anywhere a terminal does.

<p align="center">
  <img src="doc/tui-en.gif" alt="Claude Code Watcher TUI tracking several sessions in a live table and toggling the cards layout" width="800">
</p>

## Features

- Detects all active Claude Code sessions automatically
- Shows each session's status in **real time**:
  - **Waiting** (orange) — Claude replied, waiting for your input
  - **Working** (amber) — Claude is processing your message, with tool name
  - **Idle** (green) — session paused
- Context window usage (`ctx%`) shown when available
- Spawned **subagent count** per session (`N agents`), with each agent detailed in the row tooltip — toggle off in Settings
- Background **daemon** shown as a non-focusable `(D)` row (hideable in Settings)
- Optional **sort by idle time** (`s`) — most-recently-idle sessions on top
- Optional **idle duration** (`i`) on idle rows — approx (`02:24`, minute res) or precise (`02:24:23`)
- Git **worktree** sessions resolved to their real project, tagged `↳ WT: <name>`
- Press `Enter`/`Space` or click a row to focus the session's terminal window (click can be turned off in Settings)
- Cards mode (`c`) for a more spacious layout
- Header shows the installed version with an update indicator (green = up to date, red = a newer release is available)
- **Settings screen** (`p`) — pick the language and toggle every display option in one place (also persisted)
- Language auto-detected from system locale (`fr` / `en`), changeable any time in the settings screen
- **Remote machines** — sessions from other hosts running `claude-watcher-webui`, merged into the same list and marked `<name>:<path>` (read-only; see [Remote sessions](#remote-sessions))

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (auto-installed by the installer if missing)
- `wmctrl` and `xdotool` for terminal focus

## Install

```bash
curl -fsSL https://github.com/claude-watcher/tui/releases/latest/download/install.sh | bash
```

Pin a specific version instead of the latest:

```bash
curl -fsSL https://github.com/claude-watcher/tui/releases/download/v1.3.1/install.sh | bash
```

To **upgrade**, just re-run the `latest` one-liner.

The installer will:
1. Install `uv` if missing, check for `wmctrl`/`xdotool`
2. Download the script to `~/.local/bin/claude-watcher-tui`
3. Set your language (prompted when run in a terminal; `CW_LANG=fr|en` otherwise)
4. Write `~/.config/claude-watcher/config.ini` (shared config, skipped if it already exists)

<details>
<summary>From a local clone (development)</summary>

```bash
git clone https://github.com/claude-watcher/tui
cd tui
./install.sh          # installs the checked-out script, no download
```
</details>

> **No hook to install:** status comes from Claude Code's own session files —
> nothing is added to `settings.json`.

## Usage

```bash
uv run ~/.local/bin/claude-watcher-tui
```

> **Not on your `PATH`?** `~/.local/bin` is on `PATH` by default on most distros,
> but not all. If the command isn't found, add this to `~/.profile` (or your shell
> rc) and re-login:
> ```bash
> export PATH="$PATH:$HOME/.local/bin"
> ```

### Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate sessions |
| `Enter` / `Space` / click | Focus session's terminal (click-to-focus can be disabled in Settings) |
| `p` | **Settings** — language + display options (apply & save instantly) |
| `k` | Close the selected session (idle only) — confirm, then sends `SIGTERM` |
| `a` | About / update info |
| `q` | Quit |
| `c` `t` `h` `s` `i` | Quick toggles (also in Settings): cards · topic · tooltip · sort · idle duration |

### CLI flags

```
--lang fr|en        force language (default: auto-detected)
--refresh-ms MS     refresh interval (default: 2000)
--once              print sessions as plain text and exit (debug/scripting)
--cards             start in cards layout
--no-topic          hide the per-session topic line (toggle live with 't')
--no-agents         hide the spawned-subagent count per session
--hide-daemons      hide the Claude Code background daemon rows (marked (D))
--no-hover          disable the hover tooltip (toggle live with 'h')
--no-click-focus    clicking a row no longer focuses its terminal (Enter/Space still do)
--sort default|idle sort order (default: default; toggle live with 's')
--idle-format none|loose|precise  idle duration on idle rows (default: none; cycle live with 'i')
--remote NAME=URL   watch a machine running claude-watcher-webui (repeatable)
--no-local          only show remote sessions (no local /proc scan)
```

## Remote sessions

Point the watcher at other machines running
[`claude-watcher-webui`](https://github.com/claude-watcher/webui) and their sessions
appear in the same list, marked `<name>:<path>` (the scp convention). Remote rows are
**read-only**: no focus, no close. A remote that stops answering is marked stale with the
age of its data, and every configured remote shows up in the status line under the
counters with its health — `lab ok 3` (reachable) is never confused with `lab down`.

### On the remote machine first

There is a server half, and it is not optional:

1. Install and **run** [`claude-watcher-webui`](https://github.com/claude-watcher/webui)
   on that host — the watcher is only a consumer of its `GET /api/sessions`.
2. webui defaults to `APP_HOST=127.0.0.1`, so out of the box it is reachable **only from
   the machine itself**. To watch it from elsewhere, either bind it wider or tunnel to it
   (see below).
3. Binding a non-loopback `APP_HOST` (e.g. `0.0.0.0`) with **no** `APP_AUTH_TOKEN` is
   **refused at startup** — set a token, or opt in explicitly with
   `APP_ALLOW_INSECURE_BIND=true`. That token is the one you give the watcher.

> **webui speaks plain HTTP.** It terminates no TLS (there is no `ssl_certfile` knob), so
> `https://box:8000/` does **not** work against it — the connection fails with
> `SSL: RECORD_LAYER_FAILURE`. Use `http://`, or put a reverse proxy (nginx, Caddy,
> Traefik) in front of it and point the watcher at the proxy's `https://` URL.

The safest shape needs no proxy and keeps the token off the wire — an SSH tunnel to a
loopback URL:

```bash
ssh -N -L 8001:127.0.0.1:8000 box &          # webui stays bound to 127.0.0.1 on `box`
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://127.0.0.1:8001
```

### Declaring remotes

Persistent remotes live in `~/.config/claude-watcher/config.ini` (shared with the GTK
widget, so you declare them once for both):

```ini
[remotes]
poll_ms = 2000              # remote poll interval, separate from refresh_ms.
                            # Default 2000, floored at 250 — below that you are
                            # hammering the host, not watching it.

[remote:lab]
url = http://box:8000/      # the ONLY required key; a section without it is ignored
token = s3cr3t
enabled = true              # 1/yes/true/on · 0/no/false/off. Anything else is
                            # refused at startup rather than defaulting to "on"
label = lab                 # optional, defaults to the section name
```

The file is forced to mode `0600` whenever the watcher writes it, because it may hold
tokens. If you create or edit it by hand, `chmod 600 ~/.config/claude-watcher/config.ini`
yourself — nothing re-chmods a file the watcher never wrote.

For a one-off look at a machine, use the flag — it is never written to the config file:

```bash
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://box:8000
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://remote:s3cr3t@box:8000/
CW_REMOTE_TOKEN_LAB=s3cr3t uv run ~/.local/bin/claude-watcher-tui --remote lab=http://box:8000
```

Token resolution order, first match wins:

1. the URL's userinfo — `https://remote:<token>@host/` (the token is the **password**;
   `https://<token>@host/` with no colon works too)
2. `CW_REMOTE_TOKEN_<NAME>` — the name uppercased, non-alphanumerics replaced by `_`
   (`--remote my-lab=…` → `CW_REMOTE_TOKEN_MY_LAB`)
3. the `token` key of a matching `[remote:<name>]` section
4. none — the remote is polled unauthenticated

However it is resolved, the token is sent as an `X-API-Key` **header** and never as a
query parameter — webui accepts the token in a header only (`X-API-Key`,
`Authorization: Bearer`, `Authorization: Basic`), and it logs `query_params` on every
request, so a token in the URL would be both rejected and written to the server's log in
clear. A query you pass in the remote URL is still forwarded untouched — the watcher does
not rewrite your URL, and a reverse proxy may need its own parameters — but it will not
authenticate you, and it is masked everywhere the watcher displays it.

> **The token must be ASCII.** HTTP header values are latin-1, so a token outside that
> range would authenticate as a different string; webui refuses such a token at startup
> rather than serving unexplained 401s.

> **A token passed in `--remote` is visible to every user on the machine** via
> `/proc/<pid>/cmdline`, which is world-readable (`-r--r--r--`), while
> `/proc/<pid>/environ` is owner-only (`-r--------`). On a shared host, use
> `CW_REMOTE_TOKEN_<NAME>` or the config file (`0600`) instead.

> **A token sent to an `http://` remote travels in clear**, and the watcher will not stop
> you. Use an SSH tunnel to a loopback URL, or a reverse proxy terminating `https://`
> (certificates are then verified, with no option to disable it).

Only `http` and `https` URLs are polled: a scheme-less `--remote lab=box` or a `file://`
typo is reported as an error on that remote instead of being fetched.

### Failure modes, and what the watcher does about them

| Situation | Behaviour |
|---|---|
| Slow or hung host | 5 s connect/read timeout **and** a 5 s total read budget; one thread per remote, so only that host is delayed |
| Huge response | read capped at 4 MiB, poll recorded as failed |
| Repeated failures | exponential backoff, capped at 60 s |
| HTTP 401 / 403 | shown as an auth error, retried no sooner than every 5 min |
| Redirects | **not followed** — a 302 would replay your token to the redirect target |
| Over 500 sessions | truncated, and the status line says `lab ok 500/612` |
| First poll still in flight | `lab starting`, not `lab down` |
| Poll thread gone | `lab poller stopped` — never a stale-looking `ok` |

Remotes are read at startup: adding or removing one means restarting the watcher (the
settings screen lists them read-only, with their redacted URL and health). Pointing a
remote at your own machine with the local scan on lists every session twice — once bare,
once prefixed; that is a configuration choice, not a bug.

## How it works

For the technical details — session detection, click-to-focus internals, the
config file format, and known limitations — see [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md).
