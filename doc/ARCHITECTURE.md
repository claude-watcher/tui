# Claude Code Watcher — TUI — Architecture

Technical reference for how the TUI detects sessions and focuses terminals. For
installation and usage, see the [README](../README.md).

## Configuration

The config file lives at `~/.config/claude-watcher/config.ini` and is shared with
the GTK widget (each tool reads only the keys it understands). The TUI reads:

```ini
[general]
lang = en          # en | fr — auto-detected from system locale if omitted

[display]
refresh_ms = 2000  # refresh interval in milliseconds (inotify drives instant updates; this is the fallback)
cards       = false  # cards layout, blank line between sessions (true | false) — toggle live with 'c'
sort_mode   = default  # default (state then project) | idle (state then most-recently-idle first) — toggle live with 's'
idle_format = none     # idle duration on idle rows: none | loose (minute res, [Nd ]HH:MM) | precise ([Nd ]HH:MM:SS) — cycle live with 'i'

[features]
show_topic   = true   # per-row session topic line (true | false) — toggle live with 't'
show_agents  = true   # per-row spawned-subagent count + tooltip list (true | false)
hide_daemons = false  # hide the Claude Code background daemon rows (true | false)
hover        = true   # hover tooltip with full path + topic (true | false) — toggle live with 'h'
click_focus  = true   # clicking a row focuses its terminal (true | false); off = Enter/Space only

[remotes]
poll_ms = 2000        # remote poll interval, separate from refresh_ms (a network round trip)

[remote:lab]          # one section per remote machine; the name is the on-screen label
url     = http://box:8000/   # the ONLY required key (webui speaks plain HTTP; see README)
token   = s3cr3t      # optional; see the README for the resolution order
enabled = true        # optional, default true; 1/yes/true/on · 0/no/false/off,
                      # anything else is refused at startup
label   = lab         # optional, defaults to the section name
```

`poll_ms` defaults to 2000 and is floored at 250.

No `[remote:*]` section and no `--remote` flag means no poll thread is started and no HTTP
request is ever made — behaviour is exactly what it was before the feature existed.
`save_config()` forces the file to mode `0600` on every write, unconditionally, because it
may hold tokens (it opens with `os.open(..., 0o600)` and chmods an existing file *before*
writing: `Path.touch(mode=…)` does not re-chmod one that already exists, which is exactly
the upgrade path). Each remote gets one daemon thread that polls sequentially and writes
into a cache under a lock — `refresh_sessions()` reads that cache and performs **no HTTP**,
because it runs in the UI loop and one slow host would freeze the screen.

Remote rows are read-only at the choke point: `kill_session()` and `focus_terminal()` both
take the session dict and return `False` on `remote` as their first statement, because a
remote pid `1234` is an unrelated *local* process `1234`. For the same reason a remote
row's `config_dir` never becomes a local inotify watch (`local_config_dirs()`): a remote's
`~/.claude` also exists on this machine, so the naive loop would register a local watch on
a remote's behalf.

Two clocks, deliberately: a remote's **staleness** is stamped with `time.monotonic()` (a
backward NTP step or a resumed laptop must not make a day-dead host look fresh), while a
session's `last_activity` stays on the **wall clock**, because the renderer compares it to
`time.time()` exactly as it does for a local row. The state key is named `received_mono` so
the two cannot be confused.

The whole remote block is **shared verbatim with the GTK widget** — same constants, same
adapter, same poller — and `tests/test_core_parity.py` compares the two files symbol by
symbol on the AST (docstrings, comments and the client-naming tokens excluded), so the next
drift is caught mechanically rather than by a comment nobody reads. **The same guard is
mirrored in the GTK repo**, and it has to be: while it lived over there only, a core change
landing as a TUI-only PR went green here and later reddened an unrelated GTK PR, blaming
the wrong author. The sibling script is found at `../gtk/`, at the CI checkout path
`_gtk/`, or via `CW_GTK_SCRIPT`; missing, the test is *skipped* locally but *fails* under
`CI`. Both CIs check the sibling out branch-to-branch, which makes it an ordering
constraint — a core change is green only once its twin is pushed.

Note that this parity is claimed for the *remote block only*. The rest of the
session-detection backend is derived from the GTK widget and follows it closely, but is not
byte-identical: `scan_local_sessions` and `focus_terminal`, among others, have genuinely
diverged.

CLI flags (`--lang`, `--refresh-ms`, `--cards`, `--no-topic`, `--no-agents`,
`--hide-daemons`, `--no-hover`, `--no-click-focus`, `--sort`, `--idle-format`,
`--remote`, `--no-local`, see the README) override these at launch.
`--remote NAME=URL` merges over a matching section (its URL wins for the run, the
section's other keys survive) and is never persisted. The live
toggles (`c` / `t` / `h` / `s` / `i`) write their new value straight back to
`config.ini`, so a change survives the next restart.

The **settings screen** (`p`, a `ConfigScreen` modal mirroring the GTK widget's
Settings dialog) is the primary UI for those options plus the **language**: each
`Select`/`Switch` applies its change live and persists it to `config.ini` on the
spot (same `save_config` the quick toggles use) — no OK button. This is why the
installer no longer prompts for a language: it is auto-detected from the locale
and changed in-app afterwards. The `c/t/h/s/i` keys remain as hidden power-user
shortcuts; the footer shows only the primary actions (Focus, Kill, Parameters,
About, Quit), with Parameters/About right-aligned via a `1fr` spacer in a
`WatcherFooter`. There is no manual *refresh* key — the inotify watch plus the
polling interval already re-scan continuously, so it would be a no-op.

Inside the settings screen, **arrows move focus between rows** and **Enter/Space
activates** the focused control (toggle a switch / open a menu). The stock
`Select` opens its menu on up/down, which hijacked row navigation, so the selects
are a `_NavSelect` subclass that rebinds up/down to focus movement and keeps
Enter/Space for opening; once a menu is open its overlay handles the arrows.

## Session detection

Status comes from one of two first-party sources, no hook required. The
per-session registry (`~/.claude/sessions/<pid>.json`) is preferred when Claude
Code writes it; otherwise state is derived from the session **transcript**
(`~/.claude/projects/<slug>/<sessionId>.jsonl`). Whether the registry file
exists depends on the Claude Code version, so the TUI uses it when present and
falls back to the transcript when it is not.

Sessions running inside a Claude **worktree** (`<project>/.claude/worktrees/<name>`)
keep their transcript under the *parent project's* slug, not the worktree path.
The TUI detects the marker, resolves to the parent project (so context %, topic
and idle time work), shows the real project path, and adds a `↳ WT: <name>`
sub-line. When the parent transcript can't be confirmed it leaves the raw path
untouched.

1. A single `/proc` pass enumerates both sessions and subagents. Sessions are
   `/proc/<pid>/comm` exact-matching `claude`; field 22 of `/proc/<pid>/stat`
   gives the process `starttime` (in ticks). The same pass also collects
   **subagents** (see step 7). An interactive session and the background daemon
   share `comm == claude`; the daemon is told apart by its `claude daemon …`
   argv and rendered as a non-focusable `(D)` row (excluded from focus and kill,
   hideable via `features.hide_daemons`).
2. **State (registry, when present)** — `~/.claude/sessions/<pid>.json` carries a
   `status` field updated in real time:
   - `busy` / `shell` / `compacting` → **working**
   - `waiting` → **waiting** (Claude is blocked on a permission/notification)
   - `idle` → **idle**
   - `procStart` in the file must match the process `starttime` — a stale file
     from a recycled PID is ignored.
   - a `shell` or `busy` status can stick after the turn actually ended (a
     background shell outliving the turn, or interrupted subagents that never
     flip the status back). When the transcript shows the turn finished
     (`waiting`/`idle`), that status is reconciled down to **background** — a
     low-priority state (`waiting` > `working` > `background` > `idle`) meaning
     work may still run in the background while Claude itself is no longer
     computing. `compacting` is not reconciled — it is genuine, brief
     background work.
   Not every Claude Code version writes this file; when it is absent the TUI
   uses the transcript fallback below.
3. **State (transcript fallback)** — used when no registry file is present.
   Derived from the most recent meaningful entry, bottom-up:
   - `assistant` → classified by `message.stop_reason`: `tool_use` / `pause_turn`
     / still-streaming (`null`) → **working**; a terminal reason (`end_turn`,
     `max_tokens`, `stop_sequence`, `refusal`) → **waiting**.
   - `user` → **working**
   - `system` → **idle**
   This is coarser than the registry: it cannot tell a tool that is *executing*
   (working) from one *awaiting permission approval* (which also ends in an
   `assistant` `tool_use` and genuinely needs the user) — both read as
   **working**.
4. **Context % + current tool** — parsed from the transcript regardless of which
   state source is used. Context % is input tokens / window size; the tool is
   the `name` of the most recent assistant `tool_use` block. With no registry,
   the transcript is located by slugifying `cwd` (see known limitations).
5. **Session topic** (optional, `features.show_topic`) — the per-row line that
   disambiguates several sessions sharing one `cwd`. Read from the transcript's
   `ai-title` event (`aiTitle`, generated by Claude), falling back to the last
   user prompt (`lastPrompt`) until a title exists. Unlike state (read from the
   transcript tail), the title sits near the top, so it is read once in full per
   file, then only the appended delta on later refreshes. The row cells truncate
   (path left-ellipsized, topic to its first line); hovering a row with the mouse
   shows the full `cwd` and full topic in a tooltip.
6. Walk the process tree to find the parent terminal window for click-to-focus.
7. **Subagents** (`Task`-tool background agents, swarm teammates) run the
   *versioned* binary (`comm` is the version string, not `claude`), so they are
   not sessions and never appear as focusable rows. They are matched by their
   exact `--agent-id` / `--parent-session-id` argv tokens and grouped by parent
   `sessionId`. A session that spawned any shows an `N agents` count under its
   state badge (right column, like the GTK widget), and the row tooltip lists
   each (`name`, agent type, model). Optional
   (`features.show_agents`, on by default); when off, subagent detection — and
   the `cmdline` read for every non-`claude` process — is skipped entirely.

### Why the registry instead of hooks

The earlier model installed Claude Code hooks. It couldn't track a genuine
`waiting` status: Claude fires no hook event when the user *approves* a
permission, so a long approved tool stayed stuck on `waiting` until
`PostToolUse`. The registry carries a real `waiting` status, needs no
`settings.json` changes, and works under Wayland. When a Claude Code version
doesn't write the registry, the transcript fallback takes over; it recovers
most of the signal (working vs waiting) but loses the registry's ability to
flag a permission wait distinctly from a running tool.

### Instant refresh

The TUI watches `~/.claude/sessions/` with inotify (an `asyncio` worker calling
`inotify_init1` / `inotify_add_watch` via `ctypes`) — updates appear instantly,
no polling delay. Polling (`refresh_ms`) remains active as a fallback for
new-process detection and elapsed-time updates.

## Click to focus

1. **Kitty** — `kitty @ --to <socket> focus-window --match id:<id>` (precise,
   multi-tab aware)
2. `wmctrl -l -p` → find window by terminal PID → `wmctrl -ia <window_id>`
3. Fallback: `xdotool search --pid <terminal_pid> windowfocus`

Terminal focus on Wayland is limited — cross-application window management is
restricted by Wayland's security model. Kitty with `allow_remote_control` works
on the same workspace only; XWayland terminals (e.g. xterm) work everywhere.

With `features.click_focus = false` (or `--no-click-focus`) mouse clicks on the
table are fully inert — no terminal focus, no cursor move — so clicking the TUI's
own terminal to raise it never steals focus toward another window; Enter/Space
still trigger the focus. Implementation: `SessionTable._on_click` calls
`event.prevent_default()` and returns early, which stops Textual's MRO dispatch
before `DataTable._on_click` runs (the base handler is what moves the cursor and
posts `RowSelected`).

## Closing a session

`k` on the selected row closes the session: it opens a `ConfirmKillScreen` modal
and, on confirm, sends `SIGTERM` to the `claude` PID (clean exit, transcript
flushed; never `SIGKILL`). The terminal itself stays open. Only **idle** rows are
killable — `k` on a working/waiting session just warns, to avoid interrupting a
turn in progress. The kill is gated by the same anti-PID-reuse guard used for
state: `kill_session` only fires if `get_session_registry(pid, starttime)` still
resolves (i.e. `procStart` matches), so a recycled PID is never signalled. The
row disappears on the next scan once the process is gone.

## Known limitations

- Terminal focus on Wayland is limited — same restrictions as the GTK widget.
- Whether `~/.claude/sessions/<pid>.json` is written depends on the Claude Code
  version; sessions without it use the coarser transcript-based state.
- Transcript state can't distinguish a tool that is *executing* from one
  *awaiting permission approval* — both end in an `assistant` `tool_use` and show
  as **working**. A permission-blocked session therefore won't light up
  **waiting**; the registry used to flag this distinctly.
- The registry format is first-party but undocumented — its `status` enum may
  change between Claude versions (the transcript fallback covers that case).
- JSONL slug resolution (transcript path): `cwd` → replace non-alphanum with
  `-` → match under `~/.claude/projects/`. The registry's `sessionId`, when a
  registry file exists, bypasses this guessing.
