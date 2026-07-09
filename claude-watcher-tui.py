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

Keys:   ↑/↓ navigate · enter/space/click focus terminal · q quit

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
import signal
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
    d = cfg['display']  if 'display'  in cfg else {}
    g = cfg['general']  if 'general'  in cfg else {}
    f = cfg['features'] if 'features' in cfg else {}
    idle_fmt = d.get('idle_format', 'none').lower()
    return {
        'lang':       g.get('lang', _detect_lang()),
        'refresh_ms': int(d.get('refresh_ms', 2000)),
        'cards':      d.get('cards', 'false').lower() == 'true',
        'show_topic': f.get('show_topic', 'true').lower() == 'true',
        # Compteur/détail des subagents lancés : affiché par défaut.
        'show_agents': f.get('show_agents', 'true').lower() == 'true',
        # Démon Claude Code : affiché par défaut, balisé (D) ; masquable ici.
        'hide_daemons': f.get('hide_daemons', 'false').lower() == 'true',
        'hover':      f.get('hover', 'true').lower() == 'true',
        # Focus terminal au clic. Désactivable : cliquer le terminal pour le
        # remettre au premier plan ne doit pas voler le focus vers une autre
        # fenêtre. Entrée/Espace restent actifs.
        'click_focus': f.get('click_focus', 'true').lower() == 'true',
        # Tri : 'default' (alpha) ou 'idle' (par ancienneté d'inactivité). Format
        # de la durée d'inactivité affichée : 'none' (off), 'loose' (~Xm), 'precise'.
        'sort_mode':  'idle' if d.get('sort_mode', 'default').lower() == 'idle' else 'default',
        'idle_format': idle_fmt if idle_fmt in ('none', 'loose', 'precise') else 'none',
    }


def save_config(updates: dict[str, dict[str, str]]) -> None:
    """Persiste des clés dans config.ini : {section: {clé: valeur}}. Best-effort.

    Relit le fichier d'abord pour ne pas écraser les autres clés (config partagé
    avec le widget GTK). configparser ne conserve pas les commentaires en
    réécriture — comportement déjà admis côté GTK.
    """
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    for section, kv in updates.items():
        if section not in cfg:
            cfg[section] = {}
        for k, v in kv.items():
            cfg[section][k] = v
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open('w') as fh:
            cfg.write(fh)
    except OSError:
        pass


def parse_args(defaults: dict, argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Claude Code Watcher — TUI de suivi des sessions Claude.",
    )
    p.add_argument('--lang', default=defaults['lang'], choices=['fr', 'en'],
                   help="langue de l'interface (défaut: auto-détectée).")
    p.add_argument('--refresh-ms', type=int, default=defaults['refresh_ms'], dest='refresh_ms',
                   metavar='MS', help=f"intervalle de rafraîchissement (défaut {defaults['refresh_ms']}).")
    p.add_argument('--no-topic', dest='show_topic', action='store_false',
                   default=defaults['show_topic'],
                   help="masque le sujet de session (titre IA) sous chaque ligne.")
    p.add_argument('--no-agents', dest='show_agents', action='store_false',
                   default=defaults['show_agents'],
                   help="masque le compteur de sous-agents lancés par session.")
    p.add_argument('--hide-daemons', dest='hide_daemons', action='store_true',
                   default=defaults['hide_daemons'],
                   help="masque les lignes du démon Claude Code (balisées (D)).")
    p.add_argument('--no-hover', dest='hover', action='store_false',
                   default=defaults['hover'],
                   help="désactive l'infobulle de survol. Bascule à la volée avec 'h'.")
    p.add_argument('--no-click-focus', dest='click_focus', action='store_false',
                   default=defaults['click_focus'],
                   help="le clic ne focalise plus le terminal (Entrée/Espace restent actifs).")
    p.add_argument('--sort', dest='sort_mode', default=defaults['sort_mode'],
                   choices=['default', 'idle'],
                   help="ordre de tri (défaut: default). Bascule à la volée avec 's'.")
    p.add_argument('--idle-format', dest='idle_format', default=defaults['idle_format'],
                   choices=['none', 'loose', 'precise'],
                   help="durée d'inactivité affichée sur les lignes idle (défaut: none). "
                        "Cycle à la volée avec 'i'.")
    p.add_argument('--once', action='store_true',
                   help="affiche les sessions une fois en texte brut puis quitte (non-TTY / debug).")
    p.add_argument('--frame', action='store_true',
                   help="rend l'UI Textual une frame en headless puis quitte (rc=1 si le rendu "
                        "lève). Smoke-test du rendu sans ouvrir la TUI.")
    p.add_argument('--cards', action='store_true', default=defaults['cards'],
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
        'agent':      'agent',
        'agents':     'agents',
        'tip_agents': 'Agents :',
        'daemon':     'démon',
        'tip_daemon': 'Démon Claude Code (pas une session).',
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
        'sort_label':    'Tri',
        'sort_default':  'par défaut',
        'sort_idle':     'par inactivité',
        'idle_label':    'Durée d’inactivité',
        'idle_none':     'masquée',
        'idle_loose':    'approx.',
        'idle_precise':  'précise',
        'hover_label':   'Infobulle',
        'on':            'activée',
        'off':           'désactivée',
        'kill_label':       'Fermer la session',
        'kill_confirm':     'Fermer « {proj} » (inactive depuis {idle}) ? Le terminal reste ouvert.',
        'kill_only_idle':   'Seules les sessions inactives peuvent être fermées.',
        'kill_ok':          'Session fermée : {proj} (pid {pid})',
        'kill_failed':      'Échec : process introuvable ou déjà terminé.',
        'confirm':          'Confirmer',
        'cancel':           'Annuler',
        'config_title':     'Paramètres',
        'config_hint':      'Modifs appliquées et enregistrées aussitôt · (esc) Fermer',
        'cfg_lang':         'Langue',
        'cfg_cards':        'Cartes',
        'cfg_topic':        'Sujet',
        'cfg_agents':       'Sous-agents',
        'cfg_daemons':      'Masquer les démons',
        'cfg_hover':        'Infobulle',
        'cfg_click':        'Focus au clic',
        'cfg_sort':         'Tri',
        'cfg_idle':         'Durée d’inactivité',
        'cfg_lang_d':       'Langue de l’interface.',
        'cfg_cards_d':      'Ligne vide entre les sessions (affichage plus aéré).',
        'cfg_topic_d':      'Affiche le sujet (titre IA) sous chaque session.',
        'cfg_agents_d':     'Compte les sous-agents lancés ; détail dans l’infobulle.',
        'cfg_daemons_d':    'Masque les lignes du démon Claude Code (balisées (D)).',
        'cfg_hover_d':      'Infobulle au survol : chemin et sujet complets.',
        'cfg_click_d':      'Un clic focalise le terminal. Désactivé : Entrée/Espace uniquement.',
        'cfg_sort_d':       'Ordre : par projet, ou par inactivité (récents en tête).',
        'cfg_idle_d':       'Durée d’inactivité affichée sur les lignes idle.',
    },
    'en': {
        'title':      'CLAUDE CODE WATCHER',
        'waiting':    'waiting',
        'working':    'working',
        'idle':       'idle',
        'no_session': 'no active session',
        'attend':     'waiting',
        'pid':        'pid',
        'agent':      'agent',
        'agents':     'agents',
        'tip_agents': 'Agents:',
        'daemon':     'daemon',
        'tip_daemon': 'Claude Code daemon (not a session).',
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
        'sort_label':    'Sort',
        'sort_default':  'default',
        'sort_idle':     'by idle time',
        'idle_label':    'Idle duration',
        'idle_none':     'hidden',
        'idle_loose':    'approx.',
        'idle_precise':  'precise',
        'hover_label':   'Tooltip',
        'on':            'on',
        'off':           'off',
        'kill_label':       'Close session',
        'kill_confirm':     'Close “{proj}” (idle for {idle})? The terminal stays open.',
        'kill_only_idle':   'Only idle sessions can be closed.',
        'kill_ok':          'Session closed: {proj} (pid {pid})',
        'kill_failed':      'Failed: process gone or already exited.',
        'confirm':          'Confirm',
        'cancel':           'Cancel',
        'config_title':     'Settings',
        'config_hint':      'Changes apply and save instantly · (esc) Close',
        'cfg_lang':         'Language',
        'cfg_cards':        'Cards',
        'cfg_topic':        'Topic',
        'cfg_agents':       'Subagents',
        'cfg_daemons':      'Hide daemons',
        'cfg_hover':        'Tooltip',
        'cfg_click':        'Click focus',
        'cfg_sort':         'Sort',
        'cfg_idle':         'Idle duration',
        'cfg_lang_d':       'Interface language.',
        'cfg_cards_d':      'Blank line between sessions (more spacing).',
        'cfg_topic_d':      'Show the topic (AI title) under each session.',
        'cfg_agents_d':     'Count spawned subagents; each detailed in the tooltip.',
        'cfg_daemons_d':    'Hide the Claude Code daemon rows (marked (D)).',
        'cfg_hover_d':      'Hover tooltip: full path and topic.',
        'cfg_click_d':      'Clicking a row focuses its terminal. Off: Enter/Space only.',
        'cfg_sort_d':       'Order: by project, or by idle time (recent first).',
        'cfg_idle_d':       'Idle duration shown on idle rows.',
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


def _argv_value(argv: list[str], flag: str) -> str | None:
    """Valeur suivant `flag` dans une argv (cmdline splitée sur NUL), sinon None.

    Vide → None (`or None`). Flag absent ou en dernière position → None.
    """
    try:
        return argv[argv.index(flag) + 1] or None
    except (ValueError, IndexError):
        return None


def scan_proc(collect_agents: bool = True) -> tuple[list[dict], dict[str, list[dict]]]:
    """Une seule passe /proc → (sessions/démons 'claude', subagents par parent).

    Une session interactive et le démon partagent comm=='claude' (le démon ne se
    distingue que par `claude daemon run …`) ; un subagent lancé (Task/essaim)
    tourne le binaire versionné (comm=version, donc invisible au filtre comm) et
    se repère à ses tokens argv exacts `--agent-id`/`--parent-session-id` — match
    sur token exact (argv NUL-splitée) pour éviter les faux positifs d'un
    substring noyé dans un plus gros argument.

    `collect_agents=False` (feature désactivée) saute entièrement la détection des
    subagents : aucun cmdline lu pour les process non-'claude' → zéro surcoût.

    comm est lu EN PREMIER : un échec de lecture cmdline ne fait jamais perdre une
    session claude (elle est juste traitée comme non-démon).
    """
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except Exception:
        return [], {}
    procs: list[dict] = []
    agents: dict[str, list[dict]] = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm est tronqué à 15 car (TASK_COMM_LEN) — 'claude' y tient.
            # read_bytes+decode(errors='ignore') : un comm non-UTF-8 (nom posé via
            # prctl par un process quelconque) lèverait UnicodeDecodeError avec
            # read_text() — PAS un OSError → crash du scan à chaque tick.
            comm = (entry / 'comm').read_bytes().decode(errors='ignore').strip()
        except OSError:
            continue
        if comm == 'claude':
            try:
                stat = (entry / 'stat').read_text()
                fields = stat[stat.rindex(')') + 2:].split()
                starttime = int(fields[19])
                elapsed = int(uptime - starttime / _CLK_TCK)
            except Exception:
                continue
            # cmdline seulement pour distinguer le démon ; illisible (course avec
            # un exec/exit) → non-démon, on ne perd pas la session pour autant.
            try:
                argv = (entry / 'cmdline').read_bytes().decode(errors='ignore').split('\0')
            except OSError:
                argv = []
            procs.append({'pid': int(entry.name), 'elapsed': elapsed,
                          'start_unix': time.time() - elapsed, 'starttime': starttime,
                          'is_daemon': len(argv) > 1 and argv[1] == 'daemon'})
            continue
        if not collect_agents:
            continue
        # Subagent : comm ≠ 'claude', on doit lire cmdline pour le repérer.
        try:
            argv = (entry / 'cmdline').read_bytes().decode(errors='ignore').split('\0')
        except OSError:
            continue
        if '--agent-id' not in argv:
            continue
        parent = _argv_value(argv, '--parent-session-id')
        if not parent:
            continue
        # --agent-name peut manquer (agents anonymes) : repli sur la partie locale
        # de l'id (<name>@<team>).
        name = _argv_value(argv, '--agent-name') or (_argv_value(argv, '--agent-id') or '?').split('@', 1)[0]
        model = (_argv_value(argv, '--model') or '').removeprefix('claude-')
        agents.setdefault(parent, []).append({
            'pid':   int(entry.name),
            'name':  name,
            'type':  _argv_value(argv, '--agent-type'),
            'model': model or None,
        })
    for lst in agents.values():
        lst.sort(key=lambda a: a['name'])
    return procs, agents


def resolve_config_dir(env: dict[str, str]) -> str | None:
    """CLAUDE_CONFIG_DIR d'un process, `~` résolu et validé absolu.

    CLAUDE_CONFIG_DIR hérité de l'env de la session : on résout `~` (quoté →
    non-expansé par le shell) et on rejette tout chemin relatif (sans cwd de la
    session, il pointerait sur le cwd du watcher → registre/JSONL/watch au mauvais
    endroit) → None. None aussi si la variable est absente.
    """
    config_dir = env.get('CLAUDE_CONFIG_DIR') or None
    if config_dir:
        config_dir = os.path.expanduser(config_dir)
        if not os.path.isabs(config_dir):
            return None
    return config_dir


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


_WORKTREE_MARKER = '/.claude/worktrees/'


def split_worktree(cwd: str | None) -> tuple[str | None, str | None]:
    """Sépare un cwd de worktree Claude en (racine projet, nom du worktree).

    <projet>/.claude/worktrees/<nom>[/sous-dossier] → (<projet>, <nom>).
    Hors worktree → (cwd, None). C'est la source unique du marqueur worktree.
    """
    if cwd and _WORKTREE_MARKER in cwd:
        root, _, rest = cwd.partition(_WORKTREE_MARKER)
        return root, rest.split('/', 1)[0]
    return cwd, None


def cwd_to_project_dir(cwd: str | None, config_dir: str | None = None) -> Path | None:
    if not cwd:
        return None
    # Instance CLAUDE_CONFIG_DIR custom → ses JSONL vivent dans <config_dir>/projects,
    # pas dans ~/.claude/projects. Sinon état/contexte lus au mauvais endroit.
    base = Path(config_dir) / 'projects' if config_dir else CLAUDE_PROJECTS_DIR
    # Worktree Claude : Claude range le transcript sous le slug du PROJET PARENT,
    # pas du cwd du worktree. On retombe sur la racine projet. Inoffensif hors
    # worktree ; au pire le dir n'existe pas → None.
    root, _ = split_worktree(cwd)
    # Claude slugifie le cwd en remplaçant CHAQUE non-alphanumérique par '-'
    # (pas seulement '/'), donc 'geoffrey.laurent' → 'geoffrey-laurent'.
    slug = re.sub(r'[^a-zA-Z0-9]', '-', root or cwd)
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


# Topic de session : `ai-title` (aiTitle, généré par Claude) écrit une fois tôt
# dans le JSONL puis rarement régénéré ; `last-prompt` (lastPrompt) est appendé à
# chaque tour. Le tail-read de l'état ne les voit pas (titre hors des derniers Ko).
# Cache dédié {path: (offset_dernière_ligne_complète, title, lastPrompt)} : scan
# complet au 1er passage, puis relecture du seul delta appendé. L'offset mémorisé
# tombe toujours sur une frontière de ligne → pas de 1re ligne à jeter.
_TOPIC_CACHE: dict[str, tuple[int, str | None, str | None]] = {}


def _read_topic(path: Path) -> tuple[str | None, str | None]:
    """(aiTitle, lastPrompt) du JSONL, en ne relisant que les octets ajoutés."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    title = last_prompt = None
    start = 0
    cached = _TOPIC_CACHE.get(str(path))
    if cached:
        prev, title, last_prompt = cached
        if size == prev:
            return title, last_prompt
        if size > prev:
            start = prev          # delta uniquement (start = frontière de ligne)
        else:
            # size < prev → fichier tronqué/rotaté → rescan complet depuis 0 ;
            # on repart de zéro (titre potentiellement disparu → pas de valeur périmée).
            title = last_prompt = None
    try:
        with path.open('rb') as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return title, last_prompt
    nl = data.rfind(b'\n')
    if nl == -1:                  # aucune ligne complète dans le delta
        return title, last_prompt
    for line in data[:nl + 1].decode(errors='ignore').split('\n'):
        if '"ai-title"' not in line and '"last-prompt"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('type') == 'ai-title' and ev.get('aiTitle'):
            title = ev['aiTitle']
        elif ev.get('type') == 'last-prompt' and ev.get('lastPrompt'):
            last_prompt = ev['lastPrompt']
    if len(_TOPIC_CACHE) > 200:
        _TOPIC_CACHE.clear()
    _TOPIC_CACHE[str(path)] = (start + nl + 1, title, last_prompt)
    return title, last_prompt


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
) -> tuple[str | None, int | None, str | None, str | None, float | None]:
    """État + % de contexte + outil courant + topic + mtime du JSONL.

    Retourne (state, context_pct, tool, topic, mtime). `state` ne sert qu'en
    fallback (registre absent) ; `topic` = titre IA, sinon dernier prompt ;
    `mtime` = dernière activité (proxy « inactif depuis »), None si introuvable.
    Si `session_id`
    est fourni, cible directement <session_id>.jsonl (chemin exact, aucun
    devinage) ; sinon le .jsonl le plus récent du projet. Court-circuit par mtime
    + lecture du seul tail (relecture complète si besoin).
    """
    project_dir = cwd_to_project_dir(cwd, config_dir)
    if not project_dir:
        return None, None, None, None, None
    latest = None
    if session_id:
        cand = project_dir / f'{session_id}.jsonl'
        if cand.is_file():
            latest = cand
    if latest is None:
        jsonl_files = [f for f in project_dir.glob('*.jsonl') if f.is_file()]
        if not jsonl_files:
            return None, None, None, None, None
        try:
            latest, _ = max(
                ((f, f.stat().st_mtime) for f in jsonl_files),
                key=lambda x: x[1],
            )
        except (OSError, ValueError):
            return None, None, None, None, None
    try:
        mtime = latest.stat().st_mtime
    except OSError:
        return None, None, None, None, None
    key = str(latest)
    cached = _JSONL_CACHE.get(key)
    if cached and cached[0] == mtime:
        result = cached[1]
    else:
        result = (None, None, None)
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
    # Topic désactivable (features.show_topic) : si off, on saute carrément la
    # lecture du JSONL pour le titre → aucun coût I/O quand la feature est éteinte.
    if getattr(CFG, 'show_topic', True):
        title, last_prompt = _read_topic(latest)
        topic = title or last_prompt
    else:
        topic = None
    return result[0], result[1], result[2], topic, mtime


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


def kill_session(pid: int, starttime: int, config_dir: str | None = None) -> bool:
    """Ferme une session Claude via SIGTERM, avec garde anti-recyclage de PID.

    Réutilise get_session_registry, qui ne renvoie le registre QUE si procStart
    == starttime : un None ici = process disparu ou PID recyclé entre le scan et
    la touche → on ne tire pas (pas d'innocent tué). SIGTERM laisse Claude flusher
    son transcript et sortir proprement (pas de SIGKILL). Retourne True si le
    signal est parti.
    """
    if get_session_registry(pid, starttime, config_dir) is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def get_session_state(pid: int, cwd: str | None,
                      starttime: int = 0,
                      config_dir: str | None = None,
                      ) -> tuple[str, int | None, str | None, str | None, float | None, str | None]:
    """État de la session. Retourne (state, context_pct, tool, topic,
    last_activity, session_id) — session_id sert à rattacher les subagents
    (--parent-session-id) à leur session ; None si le registre est absent.

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
    jsonl_state, context_pct, tool, topic, last_activity = get_session_info_from_jsonl(
        cwd, config_dir, session_id)
    if reg:
        # /rename : un nom choisi par l'utilisateur (champ `name` sans
        # nameSource='derived' — 'derived' = nom auto-généré, redondant avec le
        # cwd) prime sur le titre IA du JSONL comme sujet affiché. Même
        # interrupteur features.show_topic que le sujet classique.
        reg_name = reg.get('name')
        if reg_name and reg.get('nameSource') != 'derived' \
                and getattr(CFG, 'show_topic', True):
            topic = reg_name
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
        # Idle-since : instant EXACT du dernier changement d'état du registre
        # (ms epoch). Prioritaire sur le mtime du JSONL, qui bouge pour des
        # écritures de fond (résumés, todos) sans refléter l'inactivité réelle.
        # Fallback mtime si le champ est absent (version de Claude antérieure).
        ts = reg.get('statusUpdatedAt') or reg.get('updatedAt')
        if ts is not None:
            try:
                last_activity = float(ts) / 1000.0
            except (TypeError, ValueError):
                pass
    else:
        state = jsonl_state or 'idle'
    return state, context_pct, tool, topic, last_activity, session_id


def format_elapsed(s) -> str:
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60:02d}m"


def format_idle(secs, mode: str) -> str:
    """Durée d'inactivité formatée. mode='loose' (~Xm approx) ou 'precise' ([Nd ]HH:MM:SS)."""
    s = max(0, int(secs))
    if mode == 'precise':
        d, rem = divmod(s, 86400)
        h, rem = divmod(rem, 3600)
        m, sec = divmod(rem, 60)
        clock = f'{h:02d}:{m:02d}:{sec:02d}'
        return f'{d}d {clock}' if d else clock
    # loose : même découpage que precise mais SANS les secondes (résolution
    # minute) → ne change qu'une fois par minute, attire moins l'œil.
    d, rem = divmod(s, 86400)
    h, m = divmod(rem // 60, 60)
    clock = f'{h:02d}:{m:02d}'
    return f'{d}d {clock}' if d else clock


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

    procs, subagents = scan_proc(getattr(CFG, 'show_agents', True))

    sessions = []
    for p in procs:
        pid      = p['pid']
        # Démon : pas une session focusable (ni terminal, ni JSONL, ni registre
        # keyé par pid). On court-circuite tout le résolveur fenêtre/état et on
        # émet une ligne minimale balisée `daemon` — ou rien si masqué en conf.
        if p.get('is_daemon'):
            if getattr(CFG, 'hide_daemons', False):
                continue
            cwd = get_cwd(pid)
            sessions.append({
                'pid':             pid,
                'starttime':       p['starttime'],
                'project':         project_label(cwd),
                'worktree':        None,
                'display_cwd':     cwd or '?',
                'last_activity':   None,
                'topic':           None,
                'cwd':             cwd or '?',
                'elapsed':         p['elapsed'],
                'waiting':         False,
                'working':         False,
                'context_pct':     None,
                'tool':            None,
                'terminal_pid':    None,
                'window_id':       None,
                'kitty_socket':    None,
                'kitty_window_id': None,
                'config_dir':      resolve_config_dir(get_env(pid)),
                'agents':          [],
                'daemon':          True,
            })
            continue
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

        config_dir = resolve_config_dir(env)
        state, context_pct, tool, topic, last_activity, session_id = get_session_state(
            pid, cwd, p['starttime'], config_dir)
        # Worktree « confirmé » = marqueur détecté ET transcript résolu
        # (last_activity = mtime du JSONL trouvé). On affiche alors le VRAI projet
        # (racine parente) + une sous-ligne « ↳ WT: <nom> ». Non confirmé →
        # comportement inchangé (chemin brut, pas de sous-ligne).
        wt_root, wt_name = split_worktree(cwd)
        confirmed_wt = wt_name is not None and last_activity is not None
        sessions.append({
            'pid':             pid,
            'starttime':       p['starttime'],
            'project':         project_label(wt_root if confirmed_wt else cwd),
            'worktree':        wt_name if confirmed_wt else None,
            # Chemin affiché (racine projet si worktree) ; 'cwd' garde le chemin
            # complet pour l'infobulle.
            'display_cwd':     (wt_root if confirmed_wt else cwd) or '?',
            'last_activity':   last_activity,
            'topic':           topic,
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
            'agents':          subagents.get(session_id, []) if session_id else [],
            'daemon':          False,
        })
    # Priorité d'état (attente > travaille > idle) dans tous les modes. En mode
    # 'idle', SEUL le groupe inactif est départagé par ancienneté d'inactivité
    # (plus récemment devenu inactif en tête) ; attente/travaille gardent le tri
    # alpha. Trier les sessions actives par mtime serait instable — leur JSONL
    # bouge en continu, l'ordre changerait à chaque scan. last_activity absent →
    # coule en bas du groupe inactif via +inf.
    if getattr(CFG, 'sort_mode', 'default') == 'idle':
        now = time.time()
        def _sort_key(s: dict) -> tuple:
            if s['waiting']:   bucket = 0
            elif s['working']: bucket = 1
            else:              bucket = 2
            la = s.get('last_activity')
            idle = ((now - la) if la is not None else float('inf')) if bucket == 2 else 0.0
            return (bucket, idle, s['project'].lower())
        sessions.sort(key=_sort_key)
    else:
        sessions.sort(key=lambda s: (not s['waiting'], not s['working'], s['project'].lower()))
    return sessions


def session_state_label(s: dict) -> tuple[str, str]:
    """(couleur hex, libellé) pour l'état d'une session."""
    # Démon : ni actif ni inactif — point/badge neutres (gris).
    if s.get('daemon'):
        return TEXT_DIM2, tr('daemon')
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
from textual.binding import Binding  # noqa: E402
from textual.containers import Center, Horizontal, Vertical  # noqa: E402
from textual.content import Content  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Label, Select, Static, Switch  # noqa: E402


class SessionTable(DataTable):
    """DataTable où un simple clic sélectionne la ligne.

    Upstream n'émet RowSelected que si on clique la ligne déjà sous curseur :
    le 1er clic ne fait que déplacer le curseur → jamais de focus terminal au
    clic. Textual dispatche les handlers privés `_on_click` de toute la MRO,
    donc PAS de super() ici (la base tourne de toute façon) : on poste juste
    la sélection manquante du 1er clic (la base couvre le clic sur curseur).
    """

    async def _on_click(self, event) -> None:  # noqa: ANN001
        # Focus au clic désactivé (features.click_focus) : le clic est inerte —
        # ni focus terminal, ni déplacement du curseur. Cliquer le terminal pour
        # le ramener au premier plan ne doit avoir AUCUN effet de bord.
        # prevent_default() court-circuite le _on_click de DataTable : le
        # dispatch MRO de Textual s'arrête sur _no_default_action avant les
        # classes de base (vérifié dans message_pump, textual 8.2.7).
        if not getattr(CFG, 'click_focus', True):
            event.prevent_default()
            return
        meta = event.style.meta
        row, col = meta.get("row", -1), meta.get("column", -1)
        if 0 <= row < self.row_count and col >= 0 \
                and (row, col) != tuple(self.cursor_coordinate):
            self.post_message(DataTable.RowSelected(self, row, self.ordered_rows[row].key))

    def watch_hover_coordinate(self, old, value) -> None:  # noqa: ANN001
        # Survol souris : chemin + sujet complets de la ligne pointée. La base gère
        # le surlignage hover, on n'ajoute que l'infobulle (super() obligatoire).
        super().watch_hover_coordinate(old, value)
        # Infobulle désactivable (features.hover / --no-hover / touche 'h').
        if not getattr(CFG, 'hover', True):
            self.tooltip = None
            return
        tips = getattr(self, "_row_tips", None)
        if not tips:
            return
        try:
            key = self.coordinate_to_cell_key(value).row_key.value
        except Exception:
            self.tooltip = None
            return
        self.tooltip = tips.get(key)


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


class ConfirmKillScreen(ModalScreen[bool]):
    """Modale de confirmation avant de fermer une session. dismiss(True) = go."""

    CSS = """
    ConfirmKillScreen { align: center middle; }
    #kill-box {
        width: 64; max-width: 90%; height: auto;
        padding: 1 2; background: #1a1a22; border: round #d08770;
    }
    #kill-box > Static { margin-bottom: 1; }
    """

    BINDINGS = [
        ("escape,n", "cancel", "Cancel"),
        ("enter,y", "confirm", "Confirm"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="kill-box"):
            yield Static(f"[b]{tr('kill_label')} ?[/b]")
            yield Static(self._prompt)
            yield Static(f"[dim](y / ⏎) {tr('confirm')}  ·  (n / esc) {tr('cancel')}[/dim]")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class _NavSelect(Select):
    """Select qui n'ouvre QUE sur Entrée/Espace.

    Le Select natif lie aussi haut/bas à l'ouverture du menu (`show_overlay`), si
    bien qu'une flèche Bas changeait la valeur au lieu de naviguer. On retire
    haut/bas : elles remontent alors à ConfigScreen qui déplace le focus entre les
    réglages. Une fois le menu ouvert, c'est l'overlay (focalisé) qui reprend les
    flèches pour choisir la valeur, puis Entrée valide / Échap ferme.
    """

    # Textual FUSIONNE les BINDINGS de la hiérarchie : pour neutraliser le
    # haut/bas hérité (→ show_overlay), il faut les RÉASSIGNER ici. On les mappe
    # vers la navigation de focus (comme le fait ConfigScreen pour les Switch).
    BINDINGS = [
        Binding("enter,space", "show_overlay", "Show menu", show=False),
        Binding("down", "nav_next", show=False),
        Binding("up", "nav_prev", show=False),
    ]

    def action_nav_next(self) -> None:
        self.screen.focus_next()

    def action_nav_prev(self) -> None:
        self.screen.focus_previous()


class ConfigScreen(ModalScreen):
    """Fenêtre de réglages (langue + affichage). Pendant des touches de bascule
    et du dialogue Réglages du widget GTK. Chaque changement est appliqué et
    persisté DANS LA FOULÉE (config.ini, partagé avec le GTK) — pas de bouton OK.
    Les raccourcis c/t/h/s/i restent dispo en parallèle.

    Navigation : flèches haut/bas = passer d'un réglage à l'autre ; Entrée/Espace
    = activer (ouvrir un menu / basculer un switch). Tab fonctionne aussi.
    """

    # Panneau ancré EN BAS + fond transparent (pas de voile assombri) : le tableau
    # reste visible AU-DESSUS et se met à jour en direct quand on change un réglage
    # (refresh_sessions sur l'app de base) — on voit l'effet sans fermer la fenêtre.
    CSS = """
    ConfigScreen { align: center top; background: transparent; }
    #config-box {
        width: 70; max-width: 95%; height: auto; margin-top: 1;
        padding: 1 2; background: #1a1a22; border: round #3a3a4a;
        /* 7 réglages ne tiennent plus sur un terminal court : plafonne à
           l'écran et scrolle — la navigation par flèches ramène le réglage
           focalisé dans la zone visible. */
        max-height: 100%; overflow-y: auto;
    }
    #config-box > Static { margin-bottom: 1; }
    .cfg-item { height: auto; }
    .cfg-head { height: 3; }
    .cfg-head > Label { width: 1fr; content-align: left middle; height: 100%; }
    .cfg-head > Select { width: 24; }
    .cfg-desc { color: #888898; margin-bottom: 1; }
    """

    BINDINGS = [
        ("escape,p,q", "close", "Close"),
        Binding("down", "focus_next", "Next field", show=False),
        Binding("up", "focus_previous", "Previous field", show=False),
    ]

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def compose(self) -> ComposeResult:
        with Vertical(id="config-box"):
            yield Static(f"[b]{tr('config_title')}[/b]")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(tr('cfg_lang'))
                    yield _NavSelect([("Français", "fr"), ("English", "en")],
                                     value=getattr(CFG, 'lang', 'en'),
                                     allow_blank=False, id="cfg-lang")
                yield Static(tr('cfg_lang_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(f"{tr('cfg_cards')}  [dim](c)[/dim]")
                    yield Switch(value=self.app._carded, id="cfg-cards")
                yield Static(tr('cfg_cards_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(f"{tr('cfg_topic')}  [dim](t)[/dim]")
                    yield Switch(value=getattr(CFG, 'show_topic', True), id="cfg-topic")
                yield Static(tr('cfg_topic_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(tr('cfg_agents'))
                    yield Switch(value=getattr(CFG, 'show_agents', True), id="cfg-agents")
                yield Static(tr('cfg_agents_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(tr('cfg_daemons'))
                    yield Switch(value=getattr(CFG, 'hide_daemons', False), id="cfg-daemons")
                yield Static(tr('cfg_daemons_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(f"{tr('cfg_hover')}  [dim](h)[/dim]")
                    yield Switch(value=getattr(CFG, 'hover', True), id="cfg-hover")
                yield Static(tr('cfg_hover_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(tr('cfg_click'))
                    yield Switch(value=getattr(CFG, 'click_focus', True), id="cfg-click")
                yield Static(tr('cfg_click_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(f"{tr('cfg_sort')}  [dim](s)[/dim]")
                    yield _NavSelect([(tr('sort_default'), 'default'), (tr('sort_idle'), 'idle')],
                                     value=getattr(CFG, 'sort_mode', 'default'),
                                     allow_blank=False, id="cfg-sort")
                yield Static(tr('cfg_sort_d'), classes="cfg-desc")
            with Vertical(classes="cfg-item"):
                with Horizontal(classes="cfg-head"):
                    yield Label(f"{tr('cfg_idle')}  [dim](i)[/dim]")
                    yield _NavSelect([(tr('idle_none'), 'none'), (tr('idle_loose'), 'loose'),
                                      (tr('idle_precise'), 'precise')],
                                     value=getattr(CFG, 'idle_format', 'none'),
                                     allow_blank=False, id="cfg-idle")
                yield Static(tr('cfg_idle_d'), classes="cfg-desc")
            yield Static(f"[dim]{tr('config_hint')}[/dim]")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        val = event.value
        if event.switch.id == "cfg-cards":
            self.app._carded = val
            save_config({'display': {'cards': 'true' if val else 'false'}})
        elif event.switch.id == "cfg-topic":
            CFG.show_topic = val
            save_config({'features': {'show_topic': 'true' if val else 'false'}})
        elif event.switch.id == "cfg-agents":
            CFG.show_agents = val
            save_config({'features': {'show_agents': 'true' if val else 'false'}})
        elif event.switch.id == "cfg-daemons":
            CFG.hide_daemons = val
            save_config({'features': {'hide_daemons': 'true' if val else 'false'}})
        elif event.switch.id == "cfg-hover":
            CFG.hover = val
            if not val:
                self.app.query_one("#sessions", DataTable).tooltip = None
            save_config({'features': {'hover': 'true' if val else 'false'}})
        elif event.switch.id == "cfg-click":
            CFG.click_focus = val
            save_config({'features': {'click_focus': 'true' if val else 'false'}})
        self.app.refresh_sessions()

    def on_select_changed(self, event: Select.Changed) -> None:
        val = event.value
        if val is Select.BLANK:
            return
        if event.select.id == "cfg-lang":
            CFG.lang = val
            save_config({'general': {'lang': val}})
        elif event.select.id == "cfg-sort":
            CFG.sort_mode = val
            save_config({'display': {'sort_mode': val}})
        elif event.select.id == "cfg-idle":
            CFG.idle_format = val
            save_config({'display': {'idle_format': val}})
        self.app.refresh_sessions()

    def action_close(self) -> None:
        self.dismiss()


class WatcherFooter(Footer):
    """Footer qui pousse les touches « méta » (p Paramètres, a À propos) à droite.

    Textual n'a pas de marge auto ; on insère un spacer 1fr juste avant la touche
    'p', ce qui repousse 'p' et tout ce qui suit ('a') contre le bord droit, en
    laissant les actions de navigation (q/k/Focus) à gauche. Survit aux
    recompositions du footer (refait à chaque changement d'écran).
    """

    def compose(self) -> ComposeResult:
        injected = False
        for widget in super().compose():
            if not injected and getattr(widget, "key", None) == "p":
                yield Static("", classes="footer-spacer")
                injected = True
            yield widget


class WatcherApp(App):
    CSS = """
    Screen { background: #121214; }
    .footer-spacer { width: 1fr; height: 1; }
    #empty {
        color: #55556a;
        text-style: italic;
        padding: 2 0;
    }
    DataTable {
        background: #121214;
        /* 1fr (et non auto) : le tableau remplit l'espace restant et devient
           l'UNIQUE zone scrollable. En height:auto il débordait de l'écran, qui
           scrollait alors EN PLUS du tableau → double barre verticale.
           overflow-x:hidden : jamais de barre horizontale (les cellules sont
           déjà tronquées/ellipsées à la largeur des colonnes). */
        height: 1fr;
        overflow-x: hidden;
    }
    DataTable > .datatable--cursor { background: #2a2a33; }
    #counts { color: #888898; padding: 0 1; }
    """

    # Footer : actions principales visibles. Les bascules d'affichage (c/t/h/s/i)
    # restent ACTIVES mais masquées (show=False) — la fenêtre Paramètres ('p') est
    # désormais l'UI principale pour les régler (+ la langue). Pas de 'refresh' :
    # l'inotify + le polling rafraîchissent déjà en continu, un refresh manuel ne
    # servait à rien.
    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("c", "toggle_cards", "Cards", show=False),
        Binding("t", "toggle_topic", "Topic", show=False),
        Binding("h", "toggle_hover", "Hover", show=False),
        Binding("s", "toggle_sort", "Sort", show=False),
        Binding("i", "cycle_idle", "Idle", show=False),
        ("k", "kill_session", "Kill"),
        ("enter", "focus_session", "Focus terminal"),
        # Espace = même action, masquée du footer. Sert surtout quand le focus
        # au clic est désactivé (features.click_focus) : clavier uniquement.
        Binding("space", "focus_session", "Focus terminal", show=False),
        ("p", "config", "Parameters"),
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
        self._last_sig: tuple | None = None  # structure du tableau au dernier rendu
        self._latest_version: str | None = None
        self._update_state = 'checking'  # checking | ok | old | unknown

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="counts")
        # Pas d'en-tête de colonnes (comme le widget GTK) ; colonnes (re)créées au refresh.
        yield SessionTable(id="sessions", cursor_type="row", zebra_stripes=False,
                           show_header=False)
        yield Center(Static(tr('no_session'), id="empty"))
        yield WatcherFooter()

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
        # Préserve aussi l'OFFSET de scroll : table.clear() le remet à 0, et comme
        # le refresh tourne chaque seconde, scroller à la molette (sans bouger le
        # curseur) sautait en haut à chaque tick. On le restaure après repeuplement.
        prior_scroll_y = table.scroll_offset.y

        # Largeurs adaptatives : la colonne projet prend tout l'espace dispo → on
        # peut afficher un chemin plus long (tronqué par la gauche, fin prioritaire).
        avail = table.size.width or self.size.width or 80
        # -6 : gutter curseur + padding cellules + barre de défilement verticale.
        # Sans réserver la barre, proj_w + STATUS_W dépasse d'1-2 colonnes et la
        # colonne d'état (droite) se fait rogner (« travaill », « atten »).
        proj_w = max(20, avail - self.STATUS_W - 6)
        path_chars = max(8, proj_w - 2)              # -2 : préfixe "● " ligne 1
        # Hauteur calculée par ligne : base 2 (● chemin + pid·durée), +1 si un sujet
        # est affiché, +1 en mode cartes (ligne vide de séparation).

        # On construit toutes les lignes EN MÉMOIRE d'abord, pour décider ensuite
        # entre mise à jour en place et reconstruction (cf. signature plus bas).
        waiting = working = 0
        target_row = 0
        row_tips: dict[str, Text] = {}  # str(pid) → infobulle (chemin + sujet complets)
        built: list[tuple[str, Text, Text, int]] = []  # (clé, cellule gauche, droite, hauteur)
        for i, s in enumerate(sessions):
            color, badge = session_state_label(s)
            if s['waiting']:
                waiting += 1
            elif s['working']:
                working += 1

            daemon = s.get('daemon')
            # Cellule gauche : ● + chemin (ligne 1), pid · durée en sourdine (ligne 2).
            sess = Text(no_wrap=True, overflow="ellipsis")
            sess.append("● ", style=color)
            # Préfixe « (D) » en orange Claude pour repérer le démon d'un coup d'œil.
            if daemon:
                sess.append("(D) ", style=f"bold {COLOR_CLAUDE}")
            sess.append(path_display(s.get('display_cwd') or s['cwd'], path_chars),
                        style="#e2e2e2 bold")
            row_h = 2
            # Sous-ligne worktree « ↳ WT: <nom> » sous le chemin (worktree confirmé).
            worktree = s.get('worktree')
            if worktree:
                sess.append(f"\n  ↳ WT: {worktree}", style=COLOR_CLAUDE)
                row_h += 1
            sess.append(f"\n  {tr('pid')} {s['pid']} · {format_elapsed(s['elapsed'])}",
                        style=TEXT_DIM2)
            # Durée d'inactivité sur la ligne meta (cellule large) — PAS dans la
            # colonne d'état (largeur fixe STATUS_W) où le format precise serait
            # tronqué (« ctx 12% · 05 » au lieu de « 12:04:48 »).
            idle_fmt = getattr(CFG, 'idle_format', 'none')
            la = s.get('last_activity')
            if idle_fmt != 'none' and la is not None and not s['working'] and not s['waiting']:
                sess.append(f" · idle {format_idle(time.time() - la, idle_fmt)}", style=TEXT_DIM2)
            cfg = display_config_dir(s.get('config_dir'))
            if cfg:
                sess.append(f" {CLAUDE_IDLE_GLYPH}{cfg}", style=COLOR_CLAUDE)
            agents = s.get('agents') or []
            # Sujet IA (ligne 3) : distingue plusieurs sessions du même cwd.
            topic = (s.get('topic') or '').strip().split('\n', 1)[0]
            if topic:
                sess.append(f"\n  {topic}", style=f"italic {TEXT_DIM2}")
                row_h += 1
            if self._carded:
                sess.append("\n")
                row_h += 1

            # Cellule droite (alignée à droite, colonne d'état) : badge (ligne 1),
            # compteur de subagents sous le badge (ligne 2, comme le widget GTK),
            # ctx% + outil (ligne 3). Le compteur vit ICI et non à gauche : la ligne
            # meta gauche (no_wrap + overflow ellipsis) le tronquait dès que le
            # chemin remplissait la cellule. `right_lines` porte la hauteur réelle
            # de cette colonne dans row_h (sinon la 3e ligne serait rognée).
            st = Text(justify="right", no_wrap=True)
            st.append(badge, style=color)
            right_lines = 1
            if agents:
                n = len(agents)
                st.append(f"\n{n} {tr('agents') if n > 1 else tr('agent')}",
                          style=COLOR_CLAUDE)
                right_lines += 1
            pct  = s.get('context_pct')
            tool = s.get('tool')
            st.append("\n")
            right_lines += 1
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
                right_lines += 1
            # La colonne d'état peut dépasser la gauche (badge/agents/ctx) : la
            # hauteur de ligne doit couvrir la plus haute des deux cellules.
            row_h = max(row_h, right_lines)

            # Infobulle de survol : chemin complet + sujet complet (les cellules
            # tronquent — chemin par la gauche, sujet à la 1re ligne ellipsée).
            # Text() et NON str : l'infobulle Textual est un Static(markup=True),
            # un sujet retombé sur lastPrompt peut contenir des crochets ('[/]',
            # '[INST]'…) → MarkupError ou texte mangé. Text neutralise le markup.
            key = str(s['pid'])
            tip = s['cwd']
            if daemon:
                tip = f"{tip}\n\n{tr('tip_daemon')}"
            else:
                full_topic = (s.get('topic') or '').strip()
                if full_topic:
                    tip = f"{tip}\n\nTopic: {full_topic}"
                if agents:
                    lines = []
                    for a in agents:
                        detail = ', '.join(x for x in (a.get('type'), a.get('model')) if x)
                        lines.append(f" • {a['name']}" + (f" ({detail})" if detail else ""))
                    tip = f"{tip}\n\n{tr('tip_agents')}\n" + '\n'.join(lines)
            row_tips[key] = Text(tip)
            built.append((key, sess, st, row_h))
            if s['pid'] == prior_pid:
                target_row = i

        table._row_tips = row_tips
        has_rows = bool(sessions)
        table.display = has_rows
        empty.display = not has_rows

        # Signature de STRUCTURE : largeurs + clés ordonnées + hauteurs. Si elle est
        # inchangée (seul le texte des cellules bouge : durée, ctx%…), on met à jour
        # les cellules EN PLACE — pas de table.clear(), donc aucun clignotement ni
        # saut de scroll, curseur et offset intacts. Le clear()+repeuplement complet
        # (qui clignote à chaque tick) n'a lieu que sur un vrai changement de
        # structure : ajout/retrait/réordre de session, hauteur de ligne, largeur.
        sig = (proj_w, self.STATUS_W, tuple((k, h) for k, _g, _d, h in built))
        if sig == self._last_sig and table.row_count == len(built):
            for key, sess, st, _h in built:
                table.update_cell(key, "session", sess, update_width=False)
                table.update_cell(key, "status",  st,   update_width=False)
        else:
            self._last_sig = sig
            table.clear(columns=True)
            table.add_column("", width=proj_w, key="session")
            table.add_column("", width=self.STATUS_W, key="status")
            for key, sess, st, row_h in built:
                table.add_row(sess, st, height=row_h, key=key)
            if has_rows:
                # scroll=False : repositionner le curseur sans déplacer la vue
                # (un curseur resté en haut rescrollerait en haut) ; puis restaurer
                # l'offset après layout (virtual_size à jour → clamp correct).
                table.move_cursor(row=min(target_row, table.row_count - 1), scroll=False)
                self.call_after_refresh(table.scroll_to, None, prior_scroll_y, animate=False)

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
        # Le démon n'a pas de terminal : rien à focus. On le DIT (notif) plutôt
        # qu'un no-op muet — la TUI n'a pas de curseur « non-cliquable » comme le
        # GTK, sans retour l'utilisateur croit à un bug.
        if s.get('daemon'):
            self.notify(tr('tip_daemon'), severity="information", timeout=2)
            return
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
        # Entrée/Espace sont des bindings App, donc actifs même sous une modale
        # (About, confirmation de kill) qui ne consomme pas la touche : sans ce
        # garde, Espace y déclencherait un saut de focus fenêtre en plein dialogue.
        if len(self.screen_stack) > 1:
            return
        table = self.query_one("#sessions", DataTable)
        if table.row_count:
            self._focus_row(table.cursor_row)

    def action_kill_session(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if not table.row_count:
            return
        row = table.cursor_row
        if not (0 <= row < len(self._sessions)):
            return
        s = self._sessions[row]
        # Démon exclu du kill : pas une session (pas de registre keyé par pid),
        # le kill échouerait. Message dédié — « seules les sessions inactives »
        # serait trompeur pour une ligne qui s'affiche en gris/neutre.
        if s.get('daemon'):
            self.notify(tr('tip_daemon'), severity="warning", timeout=2)
            return
        # Kill réservé aux sessions inactives : on ne ferme pas une session qui
        # travaille ou attend une réponse (tour en cours).
        if s['waiting'] or s['working']:
            self.notify(tr('kill_only_idle'), severity="warning", timeout=2)
            return
        la = s.get('last_activity')
        idle_txt = format_idle(time.time() - la, 'precise') if la is not None else '?'
        prompt = tr('kill_confirm').format(proj=s['project'], idle=idle_txt)

        def _on_confirm(go: bool | None) -> None:
            if not go:
                return
            if kill_session(s['pid'], s.get('starttime', 0), s.get('config_dir')):
                self.notify(tr('kill_ok').format(proj=s['project'], pid=s['pid']),
                            severity="information", timeout=2)
                self.refresh_sessions()  # la ligne part dès le prochain scan
            else:
                self.notify(tr('kill_failed'), severity="error", timeout=3)

        self.push_screen(ConfirmKillScreen(prompt), _on_confirm)

    def action_config(self) -> None:
        self.push_screen(ConfigScreen())

    def action_toggle_cards(self) -> None:
        self._carded = not self._carded
        save_config({'display': {'cards': 'true' if self._carded else 'false'}})
        self.refresh_sessions()

    def action_toggle_topic(self) -> None:
        # Lue par get_session_info_from_jsonl qui (ré)active la lecture du titre.
        CFG.show_topic = not getattr(CFG, 'show_topic', True)
        save_config({'features': {'show_topic': 'true' if CFG.show_topic else 'false'}})
        self.refresh_sessions()

    def action_toggle_hover(self) -> None:
        # Efface l'infobulle courante quand on désactive.
        CFG.hover = not getattr(CFG, 'hover', True)
        if not CFG.hover:
            self.query_one("#sessions", DataTable).tooltip = None
        save_config({'features': {'hover': 'true' if CFG.hover else 'false'}})
        self.notify(f"{tr('hover_label')}: {tr('on' if CFG.hover else 'off')}", timeout=2)

    def action_toggle_sort(self) -> None:
        new = 'default' if getattr(CFG, 'sort_mode', 'default') == 'idle' else 'idle'
        CFG.sort_mode = new
        save_config({'display': {'sort_mode': new}})
        self.notify(f"{tr('sort_label')}: {tr('sort_' + new)}", timeout=2)
        self.refresh_sessions()

    def action_cycle_idle(self) -> None:
        # Cycle none → loose → precise → none (persistance via config.ini / --idle-format).
        order = ['none', 'loose', 'precise']
        cur = getattr(CFG, 'idle_format', 'none')
        new = order[(order.index(cur) + 1) % len(order)] if cur in order else 'none'
        CFG.idle_format = new
        save_config({'display': {'idle_format': new}})
        self.notify(f"{tr('idle_label')}: {tr('idle_' + new)}", timeout=2)
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
        wt = f" · WT:{s['worktree']}" if s.get('worktree') else ""
        # Durée d'inactivité : honore idle_format comme la vue live (none → masquée).
        idle_fmt = getattr(CFG, 'idle_format', 'none')
        la = s.get('last_activity')
        idle = (f" · idle {format_idle(time.time() - la, idle_fmt)}"
                if idle_fmt != 'none' and la is not None
                and not s['working'] and not s['waiting'] else "")
        topic = (s.get('topic') or '').strip().split('\n', 1)[0]
        top = f" · {topic}" if topic else ""
        agents = s.get('agents') or []
        ag = f" · {len(agents)} {tr('agents') if len(agents) > 1 else tr('agent')}" if agents else ""
        proj = f"(D) {s['project']}" if s.get('daemon') else s['project']
        print(f"[{badge:>9}] {proj:<30} {tr('pid')} {s['pid']} · "
              f"{format_elapsed(s['elapsed'])}{ctx}{inst}{wt}{idle}{ag}{top}")


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
