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

Status comes from Claude Code's **own per-session registry** — a file Claude
maintains itself, keyed by PID and updated in real time. No hook required.

1. Claude writes `~/.claude/sessions/<pid>.json` on every state change, with a
   `status` field, the `sessionId`, and `cwd`.
2. The TUI enumerates sessions by scanning `/proc/<pid>/comm` for an exact match
   on `claude`; field 22 of `/proc/<pid>/stat` gives the process `starttime`
   (in ticks).
3. **State** — read from the registry file:
   - `busy` / `shell` / `compacting` → **working**
   - `waiting` → **waiting** (Claude is blocked on a permission/notification)
   - `idle` → **idle**
   - `procStart` in the file must match the process `starttime` — a stale file
     from a recycled PID is ignored.
4. **Context % + current tool** — parsed from the transcript, located exactly via
   `sessionId` → `~/.claude/projects/<slug>/<sessionId>.jsonl`. Context % is
   input tokens / window size; the tool is the `name` of the most recent
   assistant `tool_use` block.
5. **Fallback** — if a session's Claude predates the registry, state falls back
   to the transcript's last-entry type (`assistant` → waiting, `user` → working,
   `system` → idle). This is coarser: it cannot tell a permission `waiting` from
   a finished turn.
6. Walk the process tree to find the parent terminal window for click-to-focus.

### Why the registry instead of hooks

The earlier model installed Claude Code hooks. It couldn't track a genuine
`waiting` status: Claude fires no hook event when the user *approves* a
permission, so a long approved tool stayed stuck on `waiting` until
`PostToolUse`. The registry carries a real `waiting` status, needs no
`settings.json` changes, and works under Wayland.

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
- Sessions running an old Claude Code (no `~/.claude/sessions/<pid>.json`) fall
  back to coarser transcript-based state.
- The registry format is first-party but undocumented — its `status` enum may
  change between Claude versions (the transcript fallback covers that case).
- JSONL slug resolution (fallback path only): `cwd` → replace non-alphanum with
  `-` → match under `~/.claude/projects/`. The registry's `sessionId` bypasses
  this on the primary path.
