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
```

CLI flags (`--lang`, `--refresh-ms`, see the README) override these at launch.

## Session detection

Status comes from the session **transcript** (`~/.claude/projects/<slug>/<sessionId>.jsonl`).
A per-session registry (`~/.claude/sessions/<pid>.json`) is still preferred when
present, but **current Claude Code no longer writes it**, so in practice the
transcript path drives state for every session. No hook required.

1. The TUI enumerates sessions by scanning `/proc/<pid>/comm` for an exact match
   on `claude`; field 22 of `/proc/<pid>/stat` gives the process `starttime`
   (in ticks).
2. **State (registry, when present)** — `~/.claude/sessions/<pid>.json` carries a
   `status` field updated in real time:
   - `busy` / `shell` / `compacting` → **working**
   - `waiting` → **waiting** (Claude is blocked on a permission/notification)
   - `idle` → **idle**
   - `procStart` in the file must match the process `starttime` — a stale file
     from a recycled PID is ignored.
   Recent Claude Code releases stopped writing this file; the registry block is
   retained for older sessions but is otherwise dormant.
3. **State (transcript fallback — the live path today)** — derived from the most
   recent meaningful entry, bottom-up:
   - `assistant` → classified by `message.stop_reason`: `tool_use` / `pause_turn`
     / still-streaming (`null`) → **working**; a terminal reason (`end_turn`,
     `max_tokens`, `stop_sequence`, `refusal`) → **waiting**.
   - `user` → **working**
   - `system` → **idle**
   This is coarser than the registry: it cannot tell a tool that is *executing*
   (working) from one *awaiting permission approval* (which also ends in an
   `assistant` `tool_use` and genuinely needs the user) — both read as
   **working**.
4. **Context % + current tool** — parsed from the same transcript. Context % is
   input tokens / window size; the tool is the `name` of the most recent
   assistant `tool_use` block. With the registry gone, the transcript is located
   by slugifying `cwd` (see known limitations).
5. Walk the process tree to find the parent terminal window for click-to-focus.

### Why the registry instead of hooks

The earlier model installed Claude Code hooks. It couldn't track a genuine
`waiting` status: Claude fires no hook event when the user *approves* a
permission, so a long approved tool stayed stuck on `waiting` until
`PostToolUse`. The registry carried a real `waiting` status, needed no
`settings.json` changes, and worked under Wayland. Now that Claude Code no
longer writes the registry, the transcript fallback is the active source; it
recovers most of the signal (working vs waiting) but loses the registry's
ability to flag a permission wait distinctly from a running tool.

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

## Known limitations

- Terminal focus on Wayland is limited — same restrictions as the GTK widget.
- Current Claude Code no longer writes `~/.claude/sessions/<pid>.json`, so all
  sessions use the coarser transcript-based state. The registry path is kept for
  older sessions that still write it.
- Transcript state can't distinguish a tool that is *executing* from one
  *awaiting permission approval* — both end in an `assistant` `tool_use` and show
  as **working**. A permission-blocked session therefore won't light up
  **waiting**; the registry used to flag this distinctly.
- The registry format is first-party but undocumented — its `status` enum may
  change between Claude versions (the transcript fallback covers that case).
- JSONL slug resolution (transcript path): `cwd` → replace non-alphanum with
  `-` → match under `~/.claude/projects/`. The registry's `sessionId`, when a
  registry file exists, bypasses this guessing.
