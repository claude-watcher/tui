#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=0.71"]
# ///
"""
Claude Code Watcher — Textual TUI

Terminal counterpart to the GTK widget: monitors running Claude Code sessions
in a live table and lets you jump to the owning terminal window.

Run:    uv run ./claude-watcher-tui.py        # auto-installs textual
Config: ~/.config/claude-watcher/config.ini   # shared with the GTK widget (lang, refresh_ms)

Keys:   ↑/↓ navigate · enter/click focus terminal · r refresh now · q quit

The session-detection backend (ps / /proc / JSONL parsing / focus_terminal) is a
verbatim port of claude-watcher-gtk.py — only the frontend differs.
"""

import argparse
import asyncio
import configparser
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

_libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
_IN_CLOSE_WRITE = 0x00000008
_IN_CREATE      = 0x00000100
_IN_MOVED_TO    = 0x00000080

# ── Config ────────────────────────────────────────────────────────────────────

def _detect_lang() -> str:
    import locale
    lang = os.environ.get('LANG') or os.environ.get('LANGUAGE') or locale.getlocale()[0] or ''
    return 'fr' if lang.lower().startswith('fr') else 'en'

CONFIG_DIR  = Path.home() / '.config' / 'claude-watcher'
CONFIG_PATH = CONFIG_DIR / 'config.ini'

VERSION = "0.0.0"  # placeholder; release workflow stamps the git tag into this asset

# Update check — latest published release on GitHub
GITHUB_RELEASES_API = "https://api.github.com/repos/claude-watcher/tui/releases/latest"
RELEASES_URL        = "https://github.com/claude-watcher/tui/releases"
UPDATE_CMD = ("curl -fsSL "
              "https://github.com/claude-watcher/tui/releases/latest/download/install.sh | bash")
COLOR_VER_OK  = "#2e9e5b"   # dark green — installed version is the latest release
COLOR_VER_OLD = "#e0524f"   # red — a newer release is available

def _semver_tuple(s: str) -> tuple[int, ...]:
    """Loose semver → comparable int tuple. 'v1.2.3' → (1, 2, 3)."""
    parts = [int(n) for n in re.findall(r'\d+', s or '')][:3]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)

def _fetch_latest_release() -> str | None:
    """Latest release tag (without leading 'v'), or None if unavailable."""
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={'User-Agent': 'claude-watcher-tui',
                     'Accept': 'application/vnd.github+json'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return (data.get('tag_name') or '').lstrip('v') or None
    except Exception:
        return None

# Glyphe titre terminal émis par Claude Code (séquence OSC)
CLAUDE_IDLE_GLYPH = '✳'   # prompt visible, attend l'utilisateur

_SESSIONS_DIR = Path.home() / '.claude' / 'sessions'

# status (champ du registre ~/.claude/sessions/<pid>.json) → état affiché.
# 'shell'/'compacting' = la session travaille ; 'waiting' = bloquée (permission).
_STATUS_MAP = {
    'busy':       'working',
    'shell':      'working',
    'compacting': 'working',
    'waiting':    'waiting',
    'idle':       'idle',
}


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    d = cfg['display'] if 'display' in cfg else {}
    g = cfg['general'] if 'general' in cfg else {}
    return {
        'lang':       g.get('lang', _detect_lang()),
        'refresh_ms': int(d.get('refresh_ms', 2000)),
    }


def parse_args(defaults: dict, argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Claude Code Watcher — TUI de suivi des sessions Claude.",
    )
    p.add_argument('--lang', default=defaults['lang'], choices=['fr', 'en'],
                   help="langue de l'interface (défaut: auto-détectée).")
    p.add_argument('--refresh-ms', type=int, default=defaults['refresh_ms'], dest='refresh_ms',
                   metavar='MS', help=f"intervalle de rafraîchissement (défaut {defaults['refresh_ms']}).")
    p.add_argument('--once', action='store_true',
                   help="affiche les sessions une fois en texte brut puis quitte (non-TTY / debug).")
    p.add_argument('--frame', action='store_true',
                   help="rend l'UI Textual une frame en headless puis quitte (rc=1 si le rendu "
                        "lève). Smoke-test du rendu sans ouvrir la TUI.")
    p.add_argument('--cards', action='store_true',
                   help="démarre en disposition « cartes » (ligne vide entre sessions). "
                        "Bascule à la volée avec la touche 'c'.")
    return p.parse_args(argv)


# Global config — peuplé dans main() après merge config.ini + CLI
CFG: argparse.Namespace = argparse.Namespace(lang='en')

# ── i18n ──────────────────────────────────────────────────────────────────────

STRINGS = {
    'fr': {
        'title':      'CLAUDE CODE WATCHER',
        'waiting':    'attente',
        'working':    'travaille',
        'idle':       'idle',
        'no_session': 'aucune session active',
        'attend':     'attend',
        'pid':        'pid',
        'col_state':  'état',
        'col_proj':   'projet',
        'col_meta':   'pid · durée',
        'col_ctx':    'ctx',
        'count':      '{w} en attente · {p} en cours · {t} total',
        'about':         'À propos',
        'close':         'Fermer',
        'copy':          'Copier la commande',
        'copied':        'Commande copiée',
        'ver_uptodate':  'À jour',
        'ver_outdated':  'Mise à jour disponible',
        'ver_checking':  'vérification…',
        'ver_unknown':   'statut inconnu',
        'ver_current':   'Version installée',
        'ver_latest':    'Dernière version',
        'ver_status':    'Statut',
        'authors':       'Auteurs',
        'update_cmd':    'Commande de mise à jour',
        'update_notif':  'Mise à jour disponible : v{v} — appuyez sur « a »',
    },
    'en': {
        'title':      'CLAUDE CODE WATCHER',
        'waiting':    'waiting',
        'working':    'working',
        'idle':       'idle',
        'no_session': 'no active session',
        'attend':     'waiting',
        'pid':        'pid',
        'col_state':  'state',
        'col_proj':   'project',
        'col_meta':   'pid · elapsed',
        'col_ctx':    'ctx',
        'count':      '{w} waiting · {p} working · {t} total',
        'about':         'About',
        'close':         'Close',
        'copy':          'Copy command',
        'copied':        'Command copied',
        'ver_uptodate':  'Up to date',
        'ver_outdated':  'Update available',
        'ver_checking':  'checking…',
        'ver_unknown':   'status unknown',
        'ver_current':   'Installed version',
        'ver_latest':    'Latest version',
        'ver_status':    'Status',
        'authors':       'Authors',
        'update_cmd':    'Update command',
        'update_notif':  'Update available: v{v} — press "a"',
    },
}

def tr(key: str) -> str:
    lang = getattr(CFG, 'lang', 'en')
    return STRINGS.get(lang, STRINGS['en']).get(key, key)

# ── Couleurs (réutilisées telles quelles depuis le widget GTK) ──────────────────

COLOR_TITLE   = "#cc8a2e"
COLOR_WAITING = "#e86c3a"
COLOR_WORKING = "#d4a052"
COLOR_IDLE    = "#4caf7d"
COLOR_CLAUDE  = "#cc785c"   # Claude brand orange — marque les instances CLAUDE_CONFIG_DIR custom
TEXT_DIM2     = "#888898"

# ── Détection process ────────────────────────────────────────────────────────────

TERMINAL_NAMES = [
    'gnome-terminal', 'xterm', 'konsole', 'tilix',
    'terminator', 'alacritty', 'kitty', 'xfce4-terminal',
    'mate-terminal', 'lxterminal', 'st', 'urxvt',
    'ghostty', 'wezterm', 'foot', 'rio', 'hyper', 'tabby',
]

CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'


_CLK_TCK = os.sysconf('SC_CLK_TCK')


def get_claude_processes() -> list[dict]:
    """Énumère les process 'claude' via /proc — pas de fork ps à chaque tick."""
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except Exception:
        return []
    procs = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'comm').read_text().strip() != 'claude':
                continue
            stat = (entry / 'stat').read_text()
            fields = stat[stat.rindex(')') + 2:].split()
            starttime = int(fields[19])
            elapsed = int(uptime - starttime / _CLK_TCK)
            start_unix = time.time() - elapsed
        except Exception:
            continue
        procs.append({'pid': int(entry.name), 'elapsed': elapsed,
                      'start_unix': start_unix, 'starttime': starttime})
    return procs


def get_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return None


def get_parent_terminal(pid: int, window_pids: set[int] | None = None) -> dict | None:
    """Remonte l'arbre de process pour trouver le terminal parent.

    Deux chemins :
    1. Nom connu dans TERMINAL_NAMES → match rapide explicite.
    2. Premier ancêtre qui possède une fenêtre X11 (window_pids) → universel.
    """
    current, visited = int(pid), set()
    while current > 1 and current not in visited:
        visited.add(current)
        try:
            with open(f'/proc/{current}/status') as f:
                content = f.read()
        except Exception:
            break
        name_m = re.search(r'Name:\s+(.+)', content)
        ppid_m = re.search(r'PPid:\s+(\d+)', content)
        name = name_m.group(1).strip() if name_m else ''
        for term_name in TERMINAL_NAMES:
            if term_name in name.lower():
                return {'pid': current, 'name': name}
        if window_pids and current in window_pids:
            return {'pid': current, 'name': name}
        current = int(ppid_m.group(1)) if ppid_m else 1
    return None


def get_env(pid: int) -> dict[str, str]:
    """Lit /proc/<pid>/environ → dict. Ne lève jamais d'exception."""
    try:
        return dict(
            kv.split('=', 1)
            for kv in Path(f'/proc/{pid}/environ').read_bytes().decode().split('\x00')
            if '=' in kv
        )
    except Exception:
        return {}


def get_all_windows() -> list[dict]:
    """Toutes les fenêtres X11 : [{wid, pid, title}] (une entrée par fenêtre/onglet)."""
    windows: list[dict] = []
    try:
        r = subprocess.run(['wmctrl', '-l', '-p'], capture_output=True, text=True, timeout=2)
    except Exception:
        return windows
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        windows.append({'wid': parts[0], 'pid': pid, 'title': parts[4]})
    return windows


def find_best_window(term_pid: int | None, cwd: str | None,
                     all_windows: list[dict]) -> str | None:
    """Parmi les fenêtres du terminal PID, choisit celle qui héberge la session."""
    if not term_pid:
        return None
    candidates = [w for w in all_windows if w['pid'] == term_pid]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]['wid']
    if cwd:
        proj = Path(cwd).name
        for w in candidates:
            if proj in w['title']:
                return w['wid']
    return candidates[0]['wid']


def cwd_to_project_dir(cwd: str | None, config_dir: str | None = None) -> Path | None:
    if not cwd:
        return None
    # Instance CLAUDE_CONFIG_DIR custom → ses JSONL vivent dans <config_dir>/projects,
    # pas dans ~/.claude/projects. Sinon état/contexte lus au mauvais endroit.
    base = Path(config_dir) / 'projects' if config_dir else CLAUDE_PROJECTS_DIR
    # Claude slugifie le cwd en remplaçant CHAQUE non-alphanumérique par '-'
    # (pas seulement '/'), donc 'geoffrey.laurent' → 'geoffrey-laurent'.
    slug = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    path = base / slug
    return path if path.exists() else None


DEFAULT_CONTEXT_WINDOW = 200_000


def context_window_for(model: str | None) -> int:
    """Fenêtre de contexte (tokens) déduite du nom du modèle.

    Le JSONL ne trace ni la taille de fenêtre ni le beta 1M d'Opus : on déduit
    donc depuis `message.model` (heuristique). Claude Code lance Opus/Sonnet 4.x
    avec la fenêtre 1M ; Haiku et les modèles inconnus retombent sur 200k.
    """
    m = (model or '').lower()
    if 'opus-4' in m or 'sonnet-4' in m or 'fable-5' in m or 'mythos-5' in m:
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW


# Cache {path: (mtime, résultat)} — évite de relire un JSONL inchangé d'un tick
# à l'autre. Taille du tail relu à chaud : l'état et le dernier usage assistant
# tiennent quasi toujours dans les derniers Ko (parse bottom-up + break précoce).
_JSONL_CACHE: dict[str, tuple[float, tuple[str | None, int | None, str | None]]] = {}
_JSONL_TAIL_BYTES = 65536


def _read_tail_lines(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """Derniers `max_bytes` du fichier, en lignes. Le bool indique si tout le
    fichier a été lu (tail complet → pas de fallback nécessaire)."""
    with path.open('rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode(errors='ignore').split('\n')
    if start > 0 and len(lines) > 1:
        lines = lines[1:]  # 1re ligne potentiellement tronquée → jetée
    return lines, start == 0


def _parse_session_lines(lines: list[str]) -> tuple[str | None, int | None, str | None]:
    """Parse bottom-up : (state, context_pct, tool).

    `tool` = nom du dernier tool_use du message assistant le plus récent (l'outil
    courant) ; `state` n'est utilisé qu'en fallback (registre absent).
    """
    state = None
    context_pct = None
    tool = None
    seen_assistant = False
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('isSidechain'):
            continue
        kind = ev.get('type', '')
        if state is None:
            if kind == 'assistant':
                # stop_reason discriminates "working" from "waiting": 'tool_use'
                # (a tool was dispatched, result pending) or a still-streaming
                # message (None) means Claude is busy; only a terminal end-of-turn
                # reason means it handed control back and is waiting on the user.
                sr = (ev.get('message') or {}).get('stop_reason')
                state = 'working' if sr in (None, 'tool_use', 'pause_turn') else 'waiting'
            elif kind == 'user':
                state = 'working'
            elif kind == 'system':
                state = 'idle'
        if kind == 'assistant':
            msg = ev.get('message', {})
            if not seen_assistant:
                seen_assistant = True
                content = msg.get('content')
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'tool_use':
                            tool = block.get('name')
                            break
            if context_pct is None:
                usage = msg.get('usage', {})
                if usage:
                    total = (usage.get('input_tokens', 0)
                             + usage.get('cache_creation_input_tokens', 0)
                             + usage.get('cache_read_input_tokens', 0))
                    if total > 0:
                        window = context_window_for(msg.get('model'))
                        context_pct = min(100, round(total * 100 / window))
        if state is not None and context_pct is not None:
            break
    return state, context_pct, tool


def get_session_info_from_jsonl(
    cwd: str | None,
    config_dir: str | None = None,
    session_id: str | None = None,
) -> tuple[str | None, int | None, str | None]:
    """État + % de contexte + outil courant depuis le JSONL de la session.

    Retourne (state, context_pct, tool). `state` ne sert qu'en fallback (registre
    absent). Si `session_id` est fourni, cible directement <session_id>.jsonl
    (chemin exact, aucun devinage) ; sinon le .jsonl le plus récent du projet.
    Court-circuit par mtime + lecture du seul tail (relecture complète si besoin).
    """
    project_dir = cwd_to_project_dir(cwd, config_dir)
    if not project_dir:
        return None, None, None
    latest = None
    if session_id:
        cand = project_dir / f'{session_id}.jsonl'
        if cand.is_file():
            latest = cand
    if latest is None:
        jsonl_files = [f for f in project_dir.glob('*.jsonl') if f.is_file()]
        if not jsonl_files:
            return None, None, None
        try:
            latest, _ = max(
                ((f, f.stat().st_mtime) for f in jsonl_files),
                key=lambda x: x[1],
            )
        except (OSError, ValueError):
            return None, None, None
    try:
        mtime = latest.stat().st_mtime
    except OSError:
        return None, None, None
    key = str(latest)
    cached = _JSONL_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    result: tuple[str | None, int | None, str | None] = (None, None, None)
    try:
        lines, complete = _read_tail_lines(latest, _JSONL_TAIL_BYTES)
        result = _parse_session_lines(lines)
        # Tail tronqué et incomplet (état ou pct manquant) → relecture complète.
        if not complete and (result[0] is None or result[1] is None):
            result = _parse_session_lines(latest.read_text(errors='ignore').split('\n'))
    except Exception:
        pass
    if len(_JSONL_CACHE) > 200:
        _JSONL_CACHE.clear()
    _JSONL_CACHE[key] = (mtime, result)
    return result


def get_session_registry(pid: int, starttime: int,
                         config_dir: str | None = None) -> dict | None:
    """Registre de session première-partie écrit par Claude : <config>/sessions/<pid>.json.

    Source d'état primaire (champ `status` temps réel) + `sessionId`/`cwd`.
    Le registre vit sous le CLAUDE_CONFIG_DIR de l'instance : une session lancée
    avec un config dir custom écrit dans <config_dir>/sessions/, PAS dans
    ~/.claude/sessions/. Le chercher au mauvais endroit le rend introuvable et
    fait retomber (à tort) sur le fallback JSONL.
    Garde anti-recyclage de PID : `procStart` doit correspondre au `starttime`
    (champ 22 de /proc/<pid>/stat) du process courant, sinon fichier périmé →
    ignoré. Retourne le dict, ou None si absent/illisible/périmé.
    """
    sessions_dir = (Path(config_dir) / 'sessions') if config_dir else _SESSIONS_DIR
    try:
        data = json.loads((sessions_dir / f'{pid}.json').read_text())
    except (OSError, ValueError):
        return None
    ps = data.get('procStart')
    if ps is not None:
        try:
            if int(ps) != starttime:
                return None
        except (TypeError, ValueError):
            pass
    return data


def get_session_state(pid: int, cwd: str | None,
                      starttime: int = 0,
                      config_dir: str | None = None) -> tuple[str, int | None, str | None]:
    """État de la session. Retourne (state, context_pct, tool).

    Le registre ~/.claude/sessions/<pid>.json (champ `status`) est prioritaire
    quand il existe ; selon la version de Claude Code il peut être absent,
    auquel cas l'état est déduit du JSONL. Le JSONL fournit dans tous les cas le
    % de contexte et le nom de l'outil courant. `sessionId` du registre, quand
    il existe, donne le chemin exact du JSONL ; sinon on devine par slug du cwd.
    """
    reg = get_session_registry(pid, starttime, config_dir)
    session_id = reg.get('sessionId') if reg else None
    if reg and not cwd:
        cwd = reg.get('cwd')
    jsonl_state, context_pct, tool = get_session_info_from_jsonl(cwd, config_dir, session_id)
    if reg:
        status = reg.get('status', '')
        state = _STATUS_MAP.get(status, 'idle')
        # 'shell' persiste tant qu'un shell de fond tourne (un `!cmd` interactif
        # ou un Bash run_in_background), MÊME après que Claude a rendu la main :
        # le statut reste figé sur 'shell' alors que la session attend en réalité
        # l'utilisateur. On recoupe avec le JSONL — s'il indique que le tour est
        # terminé (dernier assistant en stop_reason terminal → 'waiting'/'idle'),
        # le shell n'est qu'un résidu de fond et l'état réel est celui du JSONL,
        # pas 'working'. jsonl_state vaut None si le JSONL est introuvable : la
        # condition est alors fausse et on garde l'ancien comportement.
        if status == 'shell' and jsonl_state in ('waiting', 'idle'):
            state = jsonl_state
    else:
        state = jsonl_state or 'idle'
    return state, context_pct, tool


def format_elapsed(s) -> str:
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60:02d}m"


def project_label(cwd: str | None) -> str:
    if not cwd:
        return '?'
    parts = Path(cwd).parts
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else '?'


def display_config_dir(path: str | None) -> str | None:
    """Nom d'instance depuis CLAUDE_CONFIG_DIR.

    Cas courant ~/.claude-<name> → juste <name>. Sinon chemin avec $HOME → ~.
    """
    if not path:
        return None
    home = str(Path.home())
    collapsed = '~' + path[len(home):] if path == home or path.startswith(home + '/') else path
    prefix = '~/.claude-'
    if collapsed.startswith(prefix) and len(collapsed) > len(prefix):
        return collapsed[len(prefix):]
    return collapsed


def focus_terminal(window_id: str | None, terminal_pid: int | None,
                   kitty_socket: str | None = None,
                   kitty_window_id: str | None = None) -> bool:
    # Kitty remote control : désambiguïse quand plusieurs onglets partagent un wid.
    if kitty_socket and kitty_window_id:
        try:
            r = subprocess.run(
                ['kitty', '@', '--to', kitty_socket,
                 'focus-window', '--match', f'id:{kitty_window_id}'],
                capture_output=True, timeout=2,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    # Fenêtre X11 exacte (WINDOWID depuis l'env, ou meilleure fenêtre par titre)
    if window_id:
        try:
            subprocess.run(['wmctrl', '-ia', window_id], timeout=2)
            return True
        except Exception:
            pass
    # Fallback xdotool sur le PID du terminal
    if terminal_pid:
        try:
            subprocess.run(
                ['xdotool', 'search', '--pid', str(terminal_pid), 'windowfocus', '--sync'],
                timeout=2,
            )
            return True
        except Exception:
            pass
    return False


def scan_sessions() -> list[dict]:
    all_windows = get_all_windows()
    window_pids = {w['pid'] for w in all_windows}

    procs = get_claude_processes()

    sessions = []
    for p in procs:
        pid      = p['pid']
        cwd      = get_cwd(pid)
        term     = get_parent_terminal(pid, window_pids)
        term_pid = term['pid'] if term else None
        env      = get_env(pid)

        kitty_socket    = env.get('KITTY_LISTEN_ON') or None
        kitty_window_id = env.get('KITTY_WINDOW_ID') or None
        raw_wid         = env.get('WINDOWID')
        if raw_wid:
            try:
                window_id = hex(int(raw_wid))
            except ValueError:
                window_id = raw_wid
        else:
            window_id = find_best_window(term_pid, cwd, all_windows)

        config_dir = env.get('CLAUDE_CONFIG_DIR') or None
        if config_dir:
            # CLAUDE_CONFIG_DIR hérité de l'env de la session : on résout `~`
            # (quoté → non-expansé par le shell) et on rejette tout chemin
            # relatif (sans cwd de la session, il pointerait sur le cwd du
            # watcher → registre/JSONL/watch au mauvais endroit). → défaut.
            config_dir = os.path.expanduser(config_dir)
            if not os.path.isabs(config_dir):
                config_dir = None
        state, context_pct, tool = get_session_state(pid, cwd, p['starttime'], config_dir)
        sessions.append({
            'pid':             pid,
            'project':         project_label(cwd),
            'cwd':             cwd or '?',
            'elapsed':         p['elapsed'],
            'waiting':         state == 'waiting',
            'working':         state == 'working',
            'context_pct':     context_pct,
            'tool':            tool,
            'terminal_pid':    term_pid,
            'window_id':       window_id,
            'kitty_socket':    kitty_socket,
            'kitty_window_id': kitty_window_id,
            'config_dir':      config_dir,
        })
    sessions.sort(key=lambda s: (not s['waiting'], not s['working'], s['project'].lower()))
    return sessions


def session_state_label(s: dict) -> tuple[str, str]:
    """(couleur hex, libellé) pour l'état d'une session."""
    if s['waiting']:
        return COLOR_WAITING, tr('attend')
    if s['working']:
        return COLOR_WORKING, tr('working')
    return COLOR_IDLE, tr('idle')


def ctx_color(pct: int) -> str:
    if pct >= 80:
        return COLOR_WAITING
    if pct >= 60:
        return COLOR_WORKING
    return TEXT_DIM2


def path_display(cwd: str | None, max_chars: int) -> str:
    """Chemin du projet, $HOME → ~, tronqué par la GAUCHE (on garde la fin du path).

    L'utilisateur veut voir la fin du chemin (le projet) en priorité : si ça
    déborde, on coupe le début avec '…' plutôt que la fin.
    """
    if not cwd or cwd == '?':
        return '?'
    home = str(Path.home())
    p = '~' + cwd[len(home):] if cwd == home or cwd.startswith(home + '/') else cwd
    if max_chars >= 2 and len(p) > max_chars:
        p = '…' + p[-(max_chars - 1):]
    return p

# ── TUI (Textual) ───────────────────────────────────────────────────────────────

from rich.text import Text  # noqa: E402

from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Center, Vertical  # noqa: E402
from textual.content import Content  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Static  # noqa: E402


class SessionTable(DataTable):
    """DataTable où un simple clic sélectionne la ligne.

    Upstream n'émet RowSelected que si on clique la ligne déjà sous curseur :
    le 1er clic ne fait que déplacer le curseur → jamais de focus terminal au
    clic. Textual dispatche les handlers privés `_on_click` de toute la MRO,
    donc PAS de super() ici (la base tourne de toute façon) : on poste juste
    la sélection manquante du 1er clic (la base couvre le clic sur curseur).
    """

    async def _on_click(self, event) -> None:  # noqa: ANN001
        meta = event.style.meta
        row, col = meta.get("row", -1), meta.get("column", -1)
        if 0 <= row < self.row_count and col >= 0 \
                and (row, col) != tuple(self.cursor_coordinate):
            self.post_message(DataTable.RowSelected(self, row, self.ordered_rows[row].key))


class AboutScreen(ModalScreen):
    """Centered modal: about info, version/update status, credits, update command."""

    CSS = """
    AboutScreen { align: center middle; }
    #about-box {
        width: 72; max-width: 90%; height: auto;
        padding: 1 2; background: #1a1a22; border: round #3a3a4a;
    }
    #about-box > Static { margin-bottom: 1; }
    #about-cmd {
        background: #15151c; border: round #3a3a4a;
        padding: 0 1; color: #c8c8d0;
    }
    """

    BINDINGS = [
        ("escape,a,q", "close", "Close"),
        ("c", "copy_cmd", "Copy"),
    ]

    def __init__(self, state: str, latest: str | None) -> None:
        super().__init__()
        self._state = state
        self._latest = latest

    def compose(self) -> ComposeResult:
        with Vertical(id="about-box"):
            yield Static("[b]Claude Code Watcher[/b]\n"
                         "[dim]Textual TUI — monitors running Claude Code sessions.[/dim]")
            yield Static(self._version_block())
            yield Static(f"[dim]{tr('authors')} :[/dim]\n"
                         "  kardagan\n"
                         "  [link='https://github.com/babs']babs[/link] [dim](Damien Degois)[/dim]")
            if self._state == 'old':
                yield Static(f"[dim]{tr('update_cmd')} :[/dim]")
                yield Static(UPDATE_CMD, id="about-cmd")
                yield Static(f"[dim](c) {tr('copy')}  ·  (esc) {tr('close')}[/dim]")
            else:
                yield Static(f"[dim](esc) {tr('close')}[/dim]")

    def _version_block(self) -> str:
        if self._state == 'ok':
            status = f"[{COLOR_VER_OK}]✓ {tr('ver_uptodate')}[/]"
        elif self._state == 'old':
            status = f"[{COLOR_VER_OLD}]⚠ {tr('ver_outdated')}[/]"
        elif self._state == 'unknown':
            status = f"[dim]{tr('ver_unknown')}[/dim]"
        else:
            status = f"[dim]{tr('ver_checking')}[/dim]"
        latest = f"v{self._latest}" if self._latest else "—"
        return (f"{tr('ver_current')} : [b]v{VERSION}[/b]\n"
                f"{tr('ver_latest')} : {latest}\n"
                f"{tr('ver_status')} : {status}")

    def action_close(self) -> None:
        self.dismiss()

    def action_copy_cmd(self) -> None:
        if self._state != 'old':
            return
        self.app.copy_to_clipboard(UPDATE_CMD)
        self.app.notify(tr('copied'), severity="information", timeout=2)


class WatcherApp(App):
    CSS = """
    Screen { background: #121214; }
    #empty {
        color: #55556a;
        text-style: italic;
        padding: 2 0;
    }
    DataTable {
        background: #121214;
        height: auto;
    }
    DataTable > .datatable--cursor { background: #2a2a33; }
    #counts { color: #888898; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("c", "toggle_cards", "Cards"),
        ("enter", "focus_session", "Focus terminal"),
        ("a", "about", "About"),
    ]

    # Largeur fixe de la colonne d'état (droite) : "● travaille" = 11 + un peu d'air.
    STATUS_W = 12

    def __init__(self, refresh_ms: int, carded: bool = False) -> None:
        super().__init__()
        self._refresh_s = max(0.25, refresh_ms / 1000)
        self._carded = carded
        self._sessions: list[dict] = []
        self._inotify_fd = -1
        self._watched_session_dirs: set[str] = set()
        self._latest_version: str | None = None
        self._update_state = 'checking'  # checking | ok | old | unknown

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="counts")
        # Pas d'en-tête de colonnes (comme le widget GTK) ; colonnes (re)créées au refresh.
        yield SessionTable(id="sessions", cursor_type="row", zebra_stripes=False,
                           show_header=False)
        yield Center(Static(tr('no_session'), id="empty"))
        yield Footer()

    def on_mount(self) -> None:
        self.title = tr('title')
        self.sub_title = f"v{VERSION}"
        self.refresh_sessions()
        self.set_interval(self._refresh_s, self.refresh_sessions)
        self.run_worker(self._watch_sessions_dir(), name="inotify")
        self.run_worker(self._check_version(), name="vercheck")
        self.set_interval(6 * 3600, lambda: self.run_worker(self._check_version(), exclusive=True))

    async def _watch_sessions_dir(self) -> None:
        """Instant refresh via inotify on Claude's session registry directories.

        Claude réécrit <config>/sessions/<pid>.json à chaque changement d'état :
        on rafraîchit dès qu'un de ces fichiers bouge, sans attendre le polling.
        Le dossier par défaut est surveillé d'emblée ; les CLAUDE_CONFIG_DIR
        custom sont ajoutés dynamiquement (_add_session_watch) à mesure que le
        scan les expose — plusieurs watches sur un seul fd inotify.
        """
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._inotify_fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._inotify_fd < 0:
            self._inotify_fd = -1
            return
        ifd = self._inotify_fd
        try:
            self._add_session_watch(_SESSIONS_DIR)
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            loop.add_reader(ifd, ready.set)
            try:
                while True:
                    await ready.wait()
                    ready.clear()
                    try:
                        os.read(ifd, 4096)  # drain pending events
                    except OSError:
                        pass
                    self.call_later(self.refresh_sessions)
            finally:
                loop.remove_reader(ifd)
        finally:
            os.close(ifd)
            self._inotify_fd = -1

    def _add_session_watch(self, path: Path) -> None:
        """Watch inotify sur un dossier sessions/ (idempotent ; skip si absent)."""
        if self._inotify_fd < 0:
            return
        key = str(path)
        if key in self._watched_session_dirs or not path.is_dir():
            return
        if _libc.inotify_add_watch(
            self._inotify_fd, key.encode(),
            _IN_CLOSE_WRITE | _IN_CREATE | _IN_MOVED_TO,
        ) < 0:
            return
        self._watched_session_dirs.add(key)

    # ── Refresh ─────────────────────────────────────────────────────────────
    def refresh_sessions(self) -> None:
        try:
            sessions = scan_sessions()
        except Exception:
            sessions = []
        self._sessions = sessions

        # Surveille le sessions/ de chaque CLAUDE_CONFIG_DIR exposé (inotify dynamique).
        for s in sessions:
            cfg = s.get('config_dir')
            if cfg:
                self._add_session_watch(Path(cfg) / 'sessions')

        table = self.query_one("#sessions", DataTable)
        empty = self.query_one("#empty", Static)

        # Préserve le PID sous le curseur pour ne pas le perdre au repeuplement.
        # On le lit via la clé de ligne (= str(pid)), pas via une colonne cachée.
        prior_pid = None
        if table.row_count and 0 <= table.cursor_row < table.row_count:
            try:
                rk = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
                prior_pid = int(rk.value) if rk.value else None
            except Exception:
                prior_pid = None

        # Largeurs adaptatives : la colonne projet prend tout l'espace dispo → on
        # peut afficher un chemin plus long (tronqué par la gauche, fin prioritaire).
        avail = table.size.width or self.size.width or 80
        proj_w = max(20, avail - self.STATUS_W - 4)  # -4 : gutter curseur + padding cellules
        path_chars = max(8, proj_w - 2)              # -2 : préfixe "● " ligne 1
        row_h = 3 if self._carded else 2             # carded = 1 ligne vide en plus

        table.clear(columns=True)
        table.add_column("", width=proj_w, key="session")
        table.add_column("", width=self.STATUS_W, key="status")

        waiting = working = 0
        target_row = 0
        for i, s in enumerate(sessions):
            color, badge = session_state_label(s)
            if s['waiting']:
                waiting += 1
            elif s['working']:
                working += 1

            # Cellule gauche : ● + chemin (ligne 1), pid · durée en sourdine (ligne 2).
            sess = Text(no_wrap=True, overflow="ellipsis")
            sess.append("● ", style=color)
            sess.append(path_display(s['cwd'], path_chars), style="#e2e2e2 bold")
            sess.append(f"\n  {tr('pid')} {s['pid']} · {format_elapsed(s['elapsed'])}",
                        style=TEXT_DIM2)
            cfg = display_config_dir(s.get('config_dir'))
            if cfg:
                sess.append(f" {CLAUDE_IDLE_GLYPH}{cfg}", style=COLOR_CLAUDE)
            if self._carded:
                sess.append("\n")

            # Cellule droite (alignée à droite) : badge (ligne 1), ctx% + tool (ligne 2).
            st = Text(justify="right", no_wrap=True)
            st.append(badge, style=color)
            pct  = s.get('context_pct')
            tool = s.get('tool')
            st.append("\n")
            meta2_parts = []
            if pct is not None:
                meta2_parts.append((f"ctx {pct}%", ctx_color(pct)))
            if tool and (s['working'] or s['waiting']):
                meta2_parts.append((tool, TEXT_DIM2))
            for idx, (txt, sty) in enumerate(meta2_parts):
                if idx:
                    st.append(" · ", style=TEXT_DIM2)
                st.append(txt, style=sty)
            if self._carded:
                st.append("\n")

            # key=str(pid) : retrouve la session au clic et restaure le curseur au refresh.
            table.add_row(sess, st, height=row_h, key=str(s['pid']))
            if s['pid'] == prior_pid:
                target_row = i

        has_rows = bool(sessions)
        table.display = has_rows
        empty.display = not has_rows
        if has_rows:
            table.move_cursor(row=min(target_row, table.row_count - 1))

        self.query_one("#counts", Static).update(
            tr('count').format(w=waiting, p=working, t=len(sessions))
        )

    # ── Version / update check ────────────────────────────────────────────────
    def format_title(self, title: str, sub_title: str) -> Content:
        """Color the header sub-title (version) by update state."""
        if not sub_title:
            return Content(title)
        if self._update_state == 'ok':
            ver, style = sub_title, COLOR_VER_OK
        elif self._update_state == 'old':
            ver, style = f"{sub_title} ⚠", COLOR_VER_OLD
        else:
            ver, style = sub_title, "dim"
        return Content.assemble(Content(title), (" — ", "dim"), Content(ver).stylize(style))

    async def _check_version(self) -> None:
        loop = asyncio.get_running_loop()
        latest = await loop.run_in_executor(None, _fetch_latest_release)
        self._apply_version_check(latest)

    def _apply_version_check(self, latest: str | None) -> None:
        if latest is None:
            self._update_state, self._latest_version = 'unknown', None
        else:
            self._latest_version = latest
            self._update_state = 'old' if _semver_tuple(latest) > _semver_tuple(VERSION) else 'ok'
        # Force the header to re-render format_title with the new state (the
        # empty assignment guarantees a value change so the watcher fires).
        self.sub_title = ""
        self.sub_title = f"v{VERSION}"
        if self._update_state == 'old':
            self.notify(tr('update_notif').format(v=self._latest_version),
                        severity="warning", timeout=8)

    def action_about(self) -> None:
        self.push_screen(AboutScreen(self._update_state, self._latest_version))

    # ── Actions ─────────────────────────────────────────────────────────────
    def _focus_row(self, row: int) -> None:
        if not (0 <= row < len(self._sessions)):
            return
        s = self._sessions[row]
        ok = focus_terminal(
            s.get('window_id'), s.get('terminal_pid'),
            s.get('kitty_socket'), s.get('kitty_window_id'),
        )
        self.notify(
            f"{'→ ' if ok else '✗ '}{s['project']} (pid {s['pid']})",
            severity="information" if ok else "warning",
            timeout=2,
        )

    def action_focus_session(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if table.row_count:
            self._focus_row(table.cursor_row)

    def action_refresh(self) -> None:
        self.refresh_sessions()

    def action_toggle_cards(self) -> None:
        self._carded = not self._carded
        self.refresh_sessions()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._focus_row(event.cursor_row)


def print_once() -> None:
    """Dump texte brut (non-TTY / debug) — pas de TUI."""
    sessions = scan_sessions()
    if not sessions:
        print(tr('no_session'))
        return
    for s in sessions:
        _, badge = session_state_label(s)
        pct = s.get('context_pct')
        ctx = f" · ctx {pct}%" if pct is not None else ""
        cfg = display_config_dir(s.get('config_dir'))
        inst = f" · {CLAUDE_IDLE_GLYPH}{cfg}" if cfg else ""
        print(f"[{badge:>9}] {s['project']:<30} {tr('pid')} {s['pid']} · "
              f"{format_elapsed(s['elapsed'])}{ctx}{inst}")


async def _smoke_frame() -> None:
    """Monte l'app + laisse passer un refresh en headless, puis quitte.

    Toute exception du rendu (compose / on_mount / refresh_sessions) remonte ici.
    """
    app = WatcherApp(refresh_ms=10_000, carded=getattr(CFG, 'cards', False))

    async def auto_pilot(pilot) -> None:  # noqa: ANN001
        await pilot.pause(0.4)  # laisse on_mount + 1er scan se terminer
        app.exit()

    await app.run_async(headless=True, auto_pilot=auto_pilot)


def main() -> None:
    global CFG
    CFG = parse_args(load_config())
    if CFG.once:
        print_once()
        return
    if CFG.frame:
        import asyncio
        import sys
        import traceback
        try:
            asyncio.run(_smoke_frame())
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        print("frame ok")
        return
    WatcherApp(refresh_ms=CFG.refresh_ms, carded=CFG.cards).run()


if __name__ == '__main__':
    main()
