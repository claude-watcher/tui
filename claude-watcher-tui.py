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

The session-detection backend (ps / /proc / JSONL parsing / focus_terminal) is
derived from claude-watcher-gtk.py and follows it closely, but it is NOT a
verbatim copy: a handful of shared helpers have genuinely diverged, including
scan_local_sessions and focus_terminal. Exactly one block is held identical —
the remote-sessions core — and tests/test_core_parity.py is what holds it,
symbol by symbol, in both repositories.
"""

import argparse
import asyncio
import configparser
import ctypes
import ctypes.util
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

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

# ── Constantes des sessions distantes ─────────────────────────────────────────
# Déclarées ICI, avec les autres constantes de module : load_config() lit
# REMOTE_POLL_MS et vivait 1100 lignes AVANT sa définition.

REMOTE_POLL_MS       = 2000              # défaut de [remotes] poll_ms
REMOTE_POLL_MIN_MS   = 250               # plancher : en-dessous on martèle l'hôte
REMOTE_TIMEOUT_S     = 5                 # connexion ET lecture (urlopen)
# Budget TOTAL de lecture, en horloge monotone. REMOTE_TIMEOUT_S est un timeout
# PAR OPÉRATION socket : un pair qui livre un octet toutes les 4 s ne le déclenche
# jamais et parquerait le thread indéfiniment, ce qui défait aussi stop().
REMOTE_READ_BUDGET_S = 5
REMOTE_READ_CHUNK    = 64 * 1024
REMOTE_MAX_BYTES     = 4 * 1024 * 1024   # bombe mémoire sinon : read() non borné
REMOTE_MAX_ROWS      = 500
REMOTE_MAX_ELAPSED_S = 10 * 365 * 24 * 3600   # 10 ans : un elapsed importé non borné
                                              # (2**63) rendrait « 2562047788015215h30m »
REMOTE_STALE_X       = 3                 # périmé après 3 × l'intervalle de poll
REMOTE_LABEL_MAX     = 12
REMOTE_BACKOFF_MAX_S = 60
# STRICTEMENT supérieur au plafond de backoff, sinon la constante ne peut jamais
# changer le comportement : un token invalide ne se corrige pas en réessayant.
REMOTE_AUTH_RETRY_S  = 300
REMOTE_SCHEMES       = ('http', 'https')  # file:// serait lu par l'ouvreur par défaut


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    d = cfg['display']  if 'display'  in cfg else {}
    g = cfg['general']  if 'general'  in cfg else {}
    f = cfg['features'] if 'features' in cfg else {}
    r = cfg['remotes']  if 'remotes'  in cfg else {}
    idle_fmt = d.get('idle_format', 'none').lower()
    # Machines distantes : une section [remote:<nom>] par hôte (url, token,
    # enabled, label). Aucune section → dict vide → aucun thread, aucun HTTP.
    remote_sections = {
        name.split(':', 1)[1]: dict(cfg[name])
        for name in cfg.sections()
        if name.startswith('remote:') and name.split(':', 1)[1]
    }
    try:
        poll_ms = int(r.get('poll_ms', REMOTE_POLL_MS))
    except (TypeError, ValueError):
        poll_ms = REMOTE_POLL_MS
    return {
        'remote_poll_ms': max(REMOTE_POLL_MIN_MS, poll_ms),
        'remote_sections': remote_sections,
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

    Le fichier est forcé en 0600 INCONDITIONNELLEMENT : il peut contenir les
    tokens des remotes ([remote:<nom>] token=). Sans branche « si un token est
    présent » — une branche laisserait une fenêtre où le fichier est écrit
    lisible par tous juste avant que le token n'y atterrisse.

    Le chmod a lieu AVANT l'écriture, et la création passe par os.open(0600) :
    touch(mode=0600, exist_ok=True) NE re-chmode PAS un fichier existant, donc
    sur le chemin de mise à niveau (un config.ini 0644 écrit par une version
    d'avant les remotes — le cas courant) le token était écrit lisible par tous,
    et le chmod d'après-coup ne refermait la fenêtre qu'une fois le secret sur
    le disque.
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
        if CONFIG_PATH.exists():
            CONFIG_PATH.chmod(0o600)
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            cfg.write(fh)
    except OSError:
        pass


def parse_remote_flag(spec: str) -> tuple[str, str]:
    """`--remote NAME=URL` → (nom, url). Lève pour argparse si la forme est fausse."""
    name, sep, url = spec.partition('=')
    if not sep or not name.strip() or not url.strip():
        raise argparse.ArgumentTypeError(
            f"format attendu NAME=URL (reçu : {spec!r})")
    # « NAME=URL#TOKEN » vient d'un brouillon abandonné de la spec : le fragment
    # serait mangé par l'URL (/api/sessions jamais demandé), aucun en-tête d'auth
    # ne partirait, et le secret atterrirait NON RÉDIGÉ dans display_url. On le
    # refuse en nommant les formes réellement supportées plutôt que de l'accepter
    # silencieusement de travers.
    if '#' in url:
        raise argparse.ArgumentTypeError(
            f"'#' non supporté dans --remote (reçu : {spec!r}). Pour un token, utilisez "
            f"NAME=https://remote:TOKEN@hote/, la variable {remote_token_env('NAME')} "
            f"ou la clé token de la section [remote:NAME].")
    return name.strip(), url.strip()


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
    # metavar neutre : « NOM=URL » s'affichait même sous --lang en (l'aide
    # argparse n'est pas traduite, autant ne pas panacher les langues).
    p.add_argument('--remote', dest='remote', action='append', metavar='NAME=URL',
                   type=parse_remote_flag, default=[],
                   help="ajoute une machine distante servant claude-watcher-webui "
                        "(répétable ; webui parle HTTP en clair, cf. README). L'URL "
                        "peut porter le token : "
                        "http://remote:TOKEN@hote:8000/ — ATTENTION, un token en "
                        "ligne de commande est lisible par TOUS les utilisateurs de la "
                        "machine via /proc/<pid>/cmdline ; préférez "
                        "CW_REMOTE_TOKEN_<NOM> ou la section [remote:<nom>] du "
                        "config.ini (forcé en 0600). Jamais persisté.")
    p.add_argument('--no-local', dest='no_local', action='store_true',
                   help="n'analyse pas /proc : n'affiche que les sessions distantes.")
    return p.parse_args(argv)


# Global config — peuplé dans main() après merge config.ini + CLI
CFG: argparse.Namespace = argparse.Namespace(lang='en')

# ── i18n ──────────────────────────────────────────────────────────────────────

STRINGS = {
    'fr': {
        'title':      'CLAUDE CODE WATCHER',
        'waiting':    'attente',
        'working':    'travaille',
        'background': 'en fond',
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
        'count':      '{w} en attente · {p} en cours · ',
        'count_bg':   '{b} en fond · ',
        'count_total':'{t} total',
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
        'rm_label':         'Distants',
        'rm_ok':            'ok',
        'rm_stale':         'périmé',
        'rm_down':          'injoignable',
        'rm_auth':          'auth refusée',
        'rm_starting':      'démarrage',
        'rm_dead':          'thread arrêté',
        'rm_stale_row':     'périmé',
        'cfg_remotes':      'Machines distantes',
        'cfg_remotes_d':    'Lecture seule : déclarées dans config.ini ou par --remote.',
        'rm_no_config':     'aucune machine distante configurée',
        'tip_remote':       'Session distante ({label}) — lecture seule : ni focus, ni fermeture.',
        'rm_readonly':      'Session distante ({label}) : lecture seule.',
        'rm_none':          'aucune session distante',
    },
    'en': {
        'title':      'CLAUDE CODE WATCHER',
        'waiting':    'waiting',
        'working':    'working',
        'background': 'background',
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
        'count':      '{w} waiting · {p} working · ',
        'count_bg':   '{b} background · ',
        'count_total':'{t} total',
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
        'rm_label':         'Remotes',
        'rm_ok':            'ok',
        'rm_stale':         'stale',
        'rm_down':          'down',
        'rm_auth':          'auth failed',
        'rm_starting':      'starting',
        'rm_dead':          'poller stopped',
        'rm_stale_row':     'stale',
        'cfg_remotes':      'Remote machines',
        'cfg_remotes_d':    'Read-only: declared in config.ini or with --remote.',
        'rm_no_config':     'no remote machine configured',
        'tip_remote':       'Remote session ({label}) — read-only: no focus, no close.',
        'rm_readonly':      'Remote session ({label}): read-only.',
        'rm_none':          'no remote session',
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
COLOR_BACKGROUND = "#5c8a9e"   # muted teal — un shell/tâche de fond tourne, Claude a rendu la main
COLOR_CLAUDE  = "#cc785c"   # Claude brand orange — marque les instances CLAUDE_CONFIG_DIR custom
COLOR_REMOTE  = "#7a9ec2"   # bleu sourd — préfixe « <label>: » des lignes distantes
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
    # Racine VIDE ('/.claude/worktrees/wt') : le slug serait '' et `base / ''`
    # vaut `base`, donc on rendrait le DOSSIER DES PROJETS comme s'il était un
    # projet — et il existe toujours. Aucun repli ne convient ici (ni '' ni le
    # cwd du worktree, qui n'est pas l'endroit où Claude range le transcript) :
    # sans racine, il n'y a pas de projet à désigner.
    if not root:
        return None
    # Claude slugifie le cwd en remplaçant CHAQUE non-alphanumérique par '-'
    # (pas seulement '/'), donc 'geoffrey.laurent' → 'geoffrey-laurent'.
    slug = re.sub(r'[^a-zA-Z0-9]', '-', root)
    path = base / slug
    return path if path.exists() else None


DEFAULT_CONTEXT_WINDOW = 1_000_000

# Modèles qui démarrent à 200k — deux cas, même hypothèse de départ :
#   - 200k ferme : le modèle n'a pas de fenêtre 1M ;
#   - sous condition : Opus 4.6 / Sonnet 4.6 n'atteignent le 1M que via le
#     « extended context » de Claude Code, qui dépend du plan (Opus : inclus en
#     Max/Team/Enterprise, crédits en Pro ; Sonnet 4.6 : crédits sur tous les
#     plans) et se désactive avec CLAUDE_CODE_DISABLE_1M_CONTEXT=1.
# Comparés en sous-chaîne de `message.model`, qui peut être daté
# (`claude-opus-4-5-20251101`), préfixé plateforme (`anthropic.claude-opus-4-5…`)
# ou un alias nu (`opus`, `haiku`).
CONTEXT_200K = (
    'haiku',  # toutes les générations Haiku, y compris haiku-4-5
    'opus-4-5',
    'opus-4-1',
    'opus-4-2025',  # claude-opus-4-20250514
    'sonnet-4-5',
    'sonnet-4-2025',  # claude-sonnet-4-20250514
    'claude-3',
    'claude-2',
    'opus-4-6',  # sous condition
    'sonnet-4-6',  # sous condition
)


def context_window_for(model: str | None, observed_tokens: int = 0) -> int:
    """Fenêtre de contexte (tokens) déduite du modèle et de l'usage observé.

    Le JSONL ne trace pas la taille de fenêtre, et rien ne distingue un modèle
    « sous condition » resté à 200k du même modèle passé à 1M : le sélecteur
    `[1m]` de Claude Code n'arrive jamais dans `message.model` (une session
    `claude-opus-5[1m]` journalise `claude-opus-5`). D'où : 1M par défaut (tout
    modèle hors CONTEXT_200K est en 1M sur tous les plans), 200k pour
    CONTEXT_200K, puis promotion à 1M dès qu'un message dépasse les 200k — ce
    que seule une vraie fenêtre 1M permet.

    Partir sur 200k garde l'erreur du bon côté : un ctx% surévalué alerte tôt,
    un ctx% sous-évalué masque une session au bord de la compaction.
    """
    m = (model or '').lower()
    if any(tag in m for tag in CONTEXT_200K):
        return 1_000_000 if observed_tokens > 200_000 else 200_000
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
                        window = context_window_for(msg.get('model'), total)
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


def kill_session(s: dict) -> bool:
    """Ferme une session Claude via SIGTERM, avec garde anti-recyclage de PID.

    Prend le DICT de session, pas des primitives : la garde « session distante »
    doit vivre au point d'étranglement, pas dans chaque appelant. Le pid d'une
    ligne distante désigne un process LOCAL sans rapport — un kill qui fuit ne
    rate pas, il tue la mauvaise chose sur CETTE machine.

    Réutilise get_session_registry, qui ne renvoie le registre QUE si procStart
    == starttime : un None ici = process disparu ou PID recyclé entre le scan et
    la touche → on ne tire pas (pas d'innocent tué). SIGTERM laisse Claude flusher
    son transcript et sortir proprement (pas de SIGKILL). Retourne True si le
    signal est parti.
    """
    if s.get('remote'):
        return False
    pid = s['pid']
    if get_session_registry(pid, s.get('starttime', 0), s.get('config_dir')) is None:
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
    # Le slug du transcript se calcule sur le cwd de DÉMARRAGE de la session, que
    # le registre enregistre. Le cwd /proc dérive dès que le dossier est renommé
    # ou que l'utilisateur fait un `cd` en cours de session — le slugifier
    # désignerait un dossier projet inexistant et perdrait silencieusement
    # état/ctx/sujet/last_activity. On préfère donc le cwd du REGISTRE pour
    # résoudre le transcript ; le cwd vivant reste le libellé affiché (affaire de
    # l'appelant). Précédence identique côté serveur (webui/detect.py) : la même
    # session doit se lire pareil en local et via l'API.
    transcript_cwd = (reg.get('cwd') if reg else None) or cwd
    jsonl_state, context_pct, tool, topic, last_activity = get_session_info_from_jsonl(
        transcript_cwd, config_dir, session_id)
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
        # Un statut de registre qui mappe sur 'working' peut rester FIGÉ alors que
        # la session a en réalité rendu la main :
        #   - 'shell' : un shell de fond (`!cmd` interactif ou Bash
        #     run_in_background, dont un Monitor) persiste après la fin du tour ;
        #   - 'busy'  : des sous-agents interrompus (crash / ESC) laissent le
        #     statut bloqué sur 'busy' sans jamais repasser 'idle'.
        # On recoupe avec le JSONL — s'il indique que le tour est fini (dernier
        # assistant en stop_reason terminal, ou évènement système post-tour →
        # 'waiting'/'idle'), on dégrade vers l'état 'background' : un travail de
        # fond peut encore tourner, mais Claude ne calcule pas. On ne peut PAS
        # distinguer un !cmd utilisateur d'un shell/Monitor Claude, ni un 'busy'
        # résiduel — le registre est opaque là-dessus — d'où un état générique de
        # basse priorité (waiting > working > background > idle), signalé sans
        # voler la vedette à une session active/en attente. Une session vraiment
        # active — y compris en attente de sous-agents, où le dernier message
        # assistant porte les tool_use Task — donne jsonl_state='working' : la
        # condition est fausse, aucune réconciliation. 'compacting' est
        # volontairement EXCLU (vrai travail de fond, bref). jsonl_state vaut None
        # si le JSONL est introuvable : la condition est fausse, on garde 'working'.
        if status in ('shell', 'busy') and jsonl_state in ('waiting', 'idle'):
            state = 'background'
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


def focus_terminal(s: dict) -> bool:
    """Ramène au premier plan le terminal d'une session. Prend le DICT de session.

    Même raison que kill_session : la garde « session distante » est au point
    d'étranglement. Une session distante n'a pas de fenêtre ici — et son
    window_id/terminal_pid désigneraient une fenêtre locale sans rapport.
    """
    if s.get('remote'):
        return False
    window_id       = s.get('window_id')
    terminal_pid    = s.get('terminal_pid')
    kitty_socket    = s.get('kitty_socket')
    kitty_window_id = s.get('kitty_window_id')
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


def scan_local_sessions() -> list[dict]:
    """Scan /proc de CETTE machine → lignes de session, non triées."""
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
                'background':      False,
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
        window_id: str | None
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
            'background':      state == 'background',
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
    return sessions


def scan_sessions(remote_rows: list[dict] | None = None) -> list[dict]:
    """Sessions locales + distantes, triées. `remote_rows` vient du cache du
    poller (déjà adaptées) : AUCUN HTTP ici, la fonction tourne dans la boucle UI.
    """
    sessions: list[dict] = list(remote_rows or [])
    if not getattr(CFG, 'no_local', False):
        sessions.extend(scan_local_sessions())
    # --hide-daemons / --no-agents s'appliquent APRÈS la fusion : filtrés dans le
    # seul scan local, ils laissaient passer les lignes de démon distantes et les
    # compteurs d'agents distants — l'option ne faisait donc que la moitié de ce
    # qu'elle annonce. Les lignes distantes sont COPIÉES (dict(...)) : elles
    # appartiennent au cache du poller, les muter le corromprait durablement.
    if getattr(CFG, 'hide_daemons', False):
        sessions = [s for s in sessions if not s.get('daemon')]
    if not getattr(CFG, 'show_agents', True):
        sessions = [dict(s, agents=[]) if s.get('agents') else s for s in sessions]
    # Priorité d'état (attente > travaille > idle) dans tous les modes. En mode
    # 'idle', SEUL le groupe inactif est départagé par ancienneté d'inactivité
    # (plus récemment devenu inactif en tête) ; attente/travaille gardent le tri
    # alpha. Trier les sessions actives par mtime serait instable — leur JSONL
    # bouge en continu, l'ordre changerait à chaque scan. last_activity absent →
    # coule en bas du groupe inactif via +inf.
    if getattr(CFG, 'sort_mode', 'default') == 'idle':
        now = time.time()
        def _sort_key(s: dict) -> tuple:
            if s['waiting']:      bucket = 0
            elif s['working']:    bucket = 1
            elif s['background']: bucket = 2
            else:                 bucket = 3
            la = s.get('last_activity')
            idle = ((now - la) if la is not None else float('inf')) if bucket == 3 else 0.0
            return (bucket, idle, s['project'].lower())
        sessions.sort(key=_sort_key)
    else:
        sessions.sort(key=lambda s: (
            not s['waiting'], not s['working'], not s['background'], s['project'].lower()))
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
    if s['background']:
        return COLOR_BACKGROUND, tr('background')
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


def session_key(s: dict) -> str:
    """Clé de ligne du tableau. Le pid seul NE SUFFIT PAS dès qu'il y a des
    remotes : un pid 1234 local et un pid 1234 distant sont deux process
    différents et collisionneraient (clé dupliquée + curseur qui saute).

    On clé sur `remote_name` (le NOM de la section / du drapeau, unique par
    construction) et JAMAIS sur `remote` (le label, élidé à REMOTE_LABEL_MAX) :
    « build-server-01 » et « build-server-02 » donnent le même label élidé, donc
    la même clé — et Textual lève DuplicateKey dans add_row(), en plein tick de
    rafraîchissement.
    """
    r = s.get('remote_name') or s.get('remote')
    return f"{r}:{s['pid']}" if r else str(s['pid'])

# ── Sessions distantes ────────────────────────────────────────────────────────
# Cœur partagé (stdlib uniquement, aucune dépendance à Textual) : agrège les
# sessions d'autres machines servies par claude-watcher-webui (GET
# /api/sessions). Le widget GTK porte le même cœur + ses propres aides de
# présentation (les constantes REMOTE_* vivent en tête de fichier). La parité du
# cœur est vérifiée mécaniquement par tests/test_core_parity.py — présent dans
# CE dépôt comme dans celui du widget, sinon une modification livrée d'un seul
# côté passerait au vert ici et ferait rougir la CI de l'autre, sur le dos d'un
# contributeur sans rapport.

# Séquences ANSI (CSI/OSC/Fe) puis caractères de contrôle restants. Les chaînes
# du payload sont écrites par une AUTRE machine et atterrissent dans un terminal :
# sans ce nettoyage, un remote peut piloter l'écran local (\x1b[2J) ou usurper le
# label d'un autre remote. \n et \t sautent aussi — une ligne de tableau tient sur
# une ligne.
_ANSI_RE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]')
# En plus des contrôles C0/C1 : les contrôles de FORMAT Unicode. U+202E (RLO) et
# ses voisins réordonnent visuellement une chaîne — c'est la primitive d'usurpation
# de label que la spec nomme elle-même ; U+2028/2029 sont des sauts de ligne pour
# tout moteur de rendu et casseraient l'invariant « une ligne de tableau = une
# ligne » ; U+200B–200D et U+2060–2069 sont invisibles ou isolent la direction.
_CTRL_RE = re.compile(
    '[\x00-\x1f\x7f-\x9f'
    '\u061c'                    # ARABIC LETTER MARK
    '\u200b-\u200f'             # ZWSP/ZWNJ/ZWJ, LRM/RLM
    '\u2028-\u202e'             # LS/PS, LRE/RLE/PDF/LRO/RLO
    '\u2060-\u2069'             # word joiner, invisibles, LRI/RLI/FSI/PDI
    '\ufeff'                    # BOM (espace insécable de largeur nulle)
    ']')


def clean_remote_str(v: object, limit: int = 200) -> str | None:
    """Chaîne venue du réseau → sûre pour un terminal, ou None. FRONTIÈRE DE CONFIANCE."""
    if not isinstance(v, str):
        return None
    return _CTRL_RE.sub('', _ANSI_RE.sub('', v))[:limit] or None


def _as_int(v: object) -> int | None:
    # isinstance(True, int) est vrai en Python : un booléen n'est pas un pid.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _as_float(v: object) -> float | None:
    """Nombre fini, ou None. UNIQUE point d'entrée des flottants du réseau.

    isinstance(float('inf'), float) est VRAI : sans le test de finitude,
    `idle_seconds: Infinity` donnait last_activity = -inf, et format_idle levait
    OverflowError DANS LA BOUCLE DE RENDU. Et ce n'est pas réservé à un hôte
    hostile : json.dumps ÉMET `Infinity` par défaut et json.loads l'accepte, donc
    un webui simplement buggé suffit. Un entier gigantesque (10**400) fait lever
    float() lui-même — même traitement.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mask_query(query: str) -> str:
    """Valeurs de la query masquées : `?key=s3cr3t` → `?key=***`.

    Le webui n'accepte PLUS le token en query : il ne lit que les en-têtes, et
    son middleware d'accès journalise `query_params` à chaque requête — un token
    posé là serait à la fois refusé et écrit en clair dans le log du serveur.
    L'URL qu'on nous DONNE peut malgré tout en porter un, par habitude ou en
    suivant une doc plus ancienne, et sans masquage il ressortait en clair dans
    l'infobulle de ligne, la barre d'état et la sortie de --once. On masque TOUTE
    valeur : deviner laquelle est le secret est précisément ce qu'on ne veut pas
    parier, et le masquage ne coûte rien.
    """
    if not query:
        return ''
    return '&'.join(f'{k}=***' if sep else k
                    for k, sep, _v in (p.partition('=') for p in query.split('&')))


def redact_secrets(msg: str, url: str) -> str:
    """Masque dans un message d'erreur toute valeur de query de l'URL interrogée.

    `display_url` est rédigée, mais l'URL réellement passée à urllib garde sa
    query — il le FAUT : on ne réécrit pas l'URL qu'on nous a donnée (un reverse
    proxy devant le webui peut exiger ses propres paramètres). Or n'importe
    quelle exception qui cite l'URL (URL invalide, échec de connexion, timeout)
    recopie donc dans `st['error']` tout secret que cette query contiendrait,
    lequel est rendu dans l'infobulle de ligne, celle de la barre d'état, l'état
    vide et la sortie de `--once`. Un simple espace collé dans la valeur suffit à
    déclencher le cas.

    On masque la valeur, pas la clé : c'est la valeur qui est le secret, et la
    remplacer telle quelle couvre aussi bien la forme brute que celle réécrite
    par urllib.
    """
    query = urllib.parse.urlsplit(url).query
    if not query:
        return msg
    for part in query.split('&'):
        _k, sep, value = part.partition('=')
        # Seuil de longueur : le remplacement est une SOUS-CHAÎNE, donc une valeur
        # courte mutile le diagnostic sans rien protéger — `?key=e` transformait
        # « TimeoutError: timed out » en « Tim***outError: tim***d out ». En
        # dessous de 4 caractères, ce n'est pas un secret qu'on défend.
        if sep and len(value) >= 4:
            msg = msg.replace(value, '***').replace(urllib.parse.quote(value, safe=''), '***')
    return msg


def split_remote_url(url: str) -> tuple[str, str | None, str]:
    """URL avec userinfo → (url propre, token, url rédigée pour affichage).

    urllib NE SAIT PAS traiter le userinfo (vérifié) : laissé en place,
    Request.host devient « remote:tok@hote:8000 », aucun en-tête d'auth n'est
    ajouté et la connexion meurt sur une résolution DNS de cette chaîne. On
    découpe donc nous-mêmes, on garde le token de côté (envoyé en X-API-Key) et
    on reconstruit une URL propre.

    Le token est le MOT DE PASSE (« https://remote:tok@hote/ ») et, à défaut, le
    NOM D'UTILISATEUR (« https://tok@hote/ ») — même règle que le serveur, donc
    les deux bouts ne peuvent pas diverger. `(pwd or user)` et non
    `(pwd if has_pwd else user)` : sur « https://tok:@hote/ » (mot de passe vide),
    la seconde forme donnait None côté client là où le serveur retient « tok ».

    La rédaction se fait ICI, à l'unique point d'analyse : le token vit dans la
    chaîne d'URL, donc tout chemin qui affiche cette chaîne fuit tant qu'elle
    n'est pas rédigée en amont. La QUERY est masquée dans les deux branches :
    elle survit à l'absence de userinfo, et elle peut porter un secret que le
    webui n'accepte plus mais que l'utilisateur y a laissé quand même.
    On découpe la netloc à la main (et pas via u.hostname/u.port) pour préserver
    la casse et les crochets d'une adresse IPv6.
    """
    u = urllib.parse.urlsplit(url)
    shown_query = mask_query(u.query)
    userinfo, sep, hostport = u.netloc.rpartition('@')
    if not sep:
        return url, None, urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, shown_query, ''))
    user, has_pwd, pwd = userinfo.partition(':')
    token = (pwd or user) or None
    clean = urllib.parse.urlunsplit((u.scheme, hostport, u.path, u.query, ''))
    masked = f'{user}:***@{hostport}' if has_pwd else f'***@{hostport}'
    return clean, token, urllib.parse.urlunsplit((u.scheme, masked, u.path, shown_query, ''))


def remote_token_env(name: str) -> str:
    """Nom de la variable d'environnement portant le token : `CW_REMOTE_TOKEN_<NOM>`."""
    return 'CW_REMOTE_TOKEN_' + re.sub(r'[^A-Za-z0-9]', '_', name).upper()


# Sémantique de configparser.getboolean, à la lettre. Seul « false » désactivait :
# « no », « 0 » et « off » laissaient le remote ACTIF, donc le token continuait de
# partir vers un hôte que l'utilisateur croyait éteint.
_BOOL_TRUE  = frozenset({'1', 'yes', 'true', 'on'})
_BOOL_FALSE = frozenset({'0', 'no', 'false', 'off'})


def remote_enabled(value: object, where: str) -> bool:
    """`enabled = …` → booléen. Lève ValueError sur une valeur ininterprétable.

    On REFUSE bruyamment plutôt que de retomber sur « activé » : le mode de panne
    d'une faute de frappe doit être « le watcher te le dit », pas « ton token
    part quand même ».
    """
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    raise ValueError(
        f"{where} enabled = {value!r} : valeur booléenne invalide "
        f"(attendu {'/'.join(sorted(_BOOL_TRUE))} ou {'/'.join(sorted(_BOOL_FALSE))})")


def enabled_remotes(remotes: list[dict]) -> list[dict]:
    """Les remotes actifs. UNIQUE application du filtre `enabled` : la liste
    complète (désactivés compris) reste disponible pour l'écran de paramètres,
    qui doit pouvoir montrer qu'un remote a bien été analysé mais est éteint."""
    return [r for r in remotes if r.get('enabled', True)]


def resolve_remotes(sections: dict[str, dict], flags: list[tuple[str, str]] | None,
                    env: Mapping[str, str] | None = None) -> list[dict]:
    """Sections [remote:<nom>] + drapeaux --remote → liste de remotes résolus.

    Le drapeau NOMME un remote, il ne l'efface pas : son URL gagne pour ce run,
    les autres clés de la section (token, label) survivent. Un remote déclaré en
    ligne de commande est forcément voulu maintenant → enabled. Rien n'est jamais
    réécrit dans le config.ini.

    Ordre de résolution du token : userinfo de l'URL → CW_REMOTE_TOKEN_<NOM> →
    section → aucun.

    Lève ValueError sur un `enabled` ininterprétable ou sur deux noms qui
    retombent sur la MÊME variable d'environnement (cf. plus bas).
    """
    env = os.environ if env is None else env
    merged: dict[str, dict] = {}
    for name, sec in sections.items():
        merged[name] = {
            'name':    name,
            'url':     (sec.get('url') or '').strip(),
            'token':   (sec.get('token') or '').strip() or None,
            'label':   (sec.get('label') or '').strip() or name,
            'enabled': remote_enabled(sec.get('enabled', 'true'), f'[remote:{name}]'),
        }
    for name, url in flags or []:
        r = merged.setdefault(name, {'name': name, 'token': None, 'label': name})
        r['url'], r['enabled'] = url, True

    # `a-b`, `a.b`, `a_b` (et `lab` / `LAB`) donnent tous CW_REMOTE_TOKEN_A_B :
    # le token d'un hôte de confiance partirait vers un hôte sans rapport. On
    # détecte la collision ICI, au moment de résoudre, plutôt que de choisir
    # arbitrairement un gagnant.
    by_env: dict[str, list[str]] = {}
    for name, r in merged.items():
        if r.get('url'):   # une section sans url est ignorée : ne pas la compter
            by_env.setdefault(remote_token_env(name), []).append(name)
    for var, names in by_env.items():
        # Seulement si la variable EXISTE réellement : sans elle, aucun secret ne
        # peut partir au mauvais endroit, et refuser de démarrer pour une
        # collision purement théorique bloque l'utilisateur sans rien protéger.
        if len(names) > 1 and var in env:
            raise ValueError(
                f"remotes {', '.join(sorted(names))} : mêmes variable d'environnement "
                f"de token ({var}). Renommez-en un — sinon le token de l'un partirait "
                f"vers l'autre.")

    remotes = []
    for r in merged.values():
        if not r['url']:
            continue
        r['url'], url_token, r['display_url'] = split_remote_url(r['url'])
        r['token'] = url_token or env.get(remote_token_env(r['name'])) or r['token']
        # Label trop bavard : élidé ici, une fois — sinon il mangerait le chemin.
        if len(r['label']) > REMOTE_LABEL_MAX:
            r['label'] = r['label'][:REMOTE_LABEL_MAX - 1] + '…'
        remotes.append(r)
    return remotes


def adapt_remote_agents(raw: object) -> list[dict]:
    """Liste d'agents du payload → liste nettoyée (sortie de adapt_remote_row).

    Une entrée sans nom exploitable est jetée : elle n'aurait rien à afficher.
    """
    agents: list[dict] = []
    if not isinstance(raw, list):
        return agents
    for a in raw[:50]:
        if not isinstance(a, dict):
            continue
        name = clean_remote_str(a.get('name'), 60)
        if name:
            agents.append({'pid':   _as_int(a.get('pid')),
                           'name':  name,
                           'type':  clean_remote_str(a.get('type'), 60),
                           'model': clean_remote_str(a.get('model'), 60)})
    return agents


def remote_last_activity(idle: float | None, received_at: float,
                         age_seconds: float) -> float | None:
    """Instant de dernière activité, en horloge MURALE LOCALE.

    On n'importe qu'une DURÉE (inactivité mesurée là-bas + âge du snapshot) et on
    la soustrait à l'instant de réception LOCAL : immunisé contre une dérive
    d'horloge murale entre les deux machines. Le rendu compare ensuite
    last_activity à time.time() local, exactement comme pour une session locale
    — c'est pourquoi cette valeur reste en horloge murale alors que la
    péremption d'un remote, elle, est mesurée en monotone.
    """
    # Plafonné comme `elapsed` : `_as_float` rejette les non-finis, mais 1e308 est
    # fini et passait — format_idle en tirait une cellule de 311 caractères dans
    # une colonne dimensionnée sur son contenu. Même classe de défaut, un champ
    # plus loin.
    if idle is None:
        return None
    # Les DEUX termes sous le même plafond, pas seulement `idle` : `_as_float` ne
    # rejette que les non-finis, donc un `age_seconds` de 1e308 rouvrait la cellule
    # de 311 caractères que ce plafond ferme — le correctif avait borné un opérande
    # en laissant son voisin sur l'ancienne hypothèse.
    return received_at - min(float(REMOTE_MAX_ELAPSED_S),
                            max(0.0, idle) + max(0.0, age_seconds))


def adapt_remote_row(row: object, remote: dict, received_at: float,
                     age_seconds: float = 0.0) -> dict | None:
    """Ligne d'API → dict de session locale, ou None si la ligne est inexploitable.

    Frontière de confiance : la FORME est validée autant que le contenu (chaque
    champ est converti, une ligne qui ne rentre pas est jetée, les autres
    passent). Un champ absent dégrade, il n'échoue pas — on met à jour un client
    avant d'avoir mis à jour tous les hôtes qu'il regarde.
    """
    if not isinstance(row, dict):
        return None
    pid = _as_int(row.get('pid'))
    if pid is None:
        return None
    state = row.get('state')
    if state not in ('waiting', 'working', 'background', 'idle', 'daemon'):
        state = 'idle'
    idle = _as_float(row.get('idle_seconds'))
    last_activity = remote_last_activity(idle, received_at, age_seconds)
    pct = _as_int(row.get('context_pct'))
    cwd = clean_remote_str(row.get('cwd'), 300) or '?'
    agents = adapt_remote_agents(row.get('agents'))
    return {
        'pid':             pid,
        'starttime':       0,
        'project':         clean_remote_str(row.get('project'), 80) or '?',
        'worktree':        clean_remote_str(row.get('worktree'), 80),
        'display_cwd':     clean_remote_str(row.get('display_cwd'), 300) or cwd,
        'last_activity':   last_activity,
        'topic':           clean_remote_str(row.get('topic'), 400),
        'cwd':             cwd,
        # Plafonné : un elapsed importé sans borne (2**63) s'affiche
        # « 2562047788015215h30m » et fait déborder la cellule.
        'elapsed':         min(REMOTE_MAX_ELAPSED_S, max(0, _as_int(row.get('elapsed')) or 0)),
        'waiting':         state == 'waiting',
        'working':         state == 'working',
        'background':      state == 'background',
        'context_pct':     min(100, max(0, pct)) if pct is not None else None,
        'tool':            clean_remote_str(row.get('tool'), 40),
        # Rien de local ne doit pouvoir être visé depuis une ligne distante.
        'terminal_pid':    None,
        'window_id':       None,
        'kitty_socket':    None,
        'kitty_window_id': None,
        # Affichage seulement : refresh_sessions ne DOIT PAS en faire un chemin
        # local (le ~/.claude d'un remote existe aussi ici).
        'config_dir':      clean_remote_str(row.get('config_dir'), 60),
        'agents':          agents,
        'daemon':          bool(row.get('daemon')) or state == 'daemon',
        'remote':          remote['label'],
        'remote_name':     remote['name'],
    }


def adapt_remote_payload(payload: object, remote: dict,
                         received_at: float) -> tuple[list[dict], int]:
    """Payload /api/sessions → (lignes de session, nombre de lignes ANNONCÉES).

    Les mauvaises lignes sont jetées. Le total annoncé est renvoyé à part pour
    que la zone d'état puisse dire « 500/612 » : tronquer à REMOTE_MAX_ROWS en
    silence donne un tableau qui a l'air complet.
    """
    if not isinstance(payload, dict):
        return [], 0
    rows = payload.get('sessions')
    if not isinstance(rows, list):
        return [], 0
    # age_seconds absent = webui antérieur à la mise en cache : 0.0, et le remote
    # marche. Coût maximal : un TTL de précision sur l'inactivité affichée.
    age = max(0.0, _as_float(payload.get('age_seconds')) or 0.0)
    adapted = (adapt_remote_row(r, remote, received_at, age) for r in rows[:REMOTE_MAX_ROWS])
    return [r for r in adapted if r is not None], len(rows)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ne suit AUCUNE redirection : la redirection devient une HTTPError 3xx.

    Mesuré : l'ouvreur par défaut d'urllib suit les 3xx en REJOUANT les en-têtes
    de la requête — donc notre X-API-Key — vers la cible, y compris sur un autre
    hôte et en dégradant https → http. Un remote mal saisi ou compromis
    exfiltrerait le token avec une seule 302. Aucun besoin légitime de
    redirection ici : /api/sessions est servi directement.
    """

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


_REMOTE_OPENER = urllib.request.build_opener(_NoRedirect)


def remote_endpoint(url: str) -> str:
    """URL de base → endpoint /api/sessions, en joignant sur le CHEMIN.

    `'https://box/?x=1'.rstrip('/') + '/api/sessions'` donnait
    « https://box/?x=1/api/sessions » : la query avalait le chemin et le endpoint
    n'était jamais demandé. La query reçue est préservée telle quelle — on ne
    réécrit pas l'URL qu'on nous a donnée (un reverse proxy devant le webui peut
    exiger ses propres paramètres). Le token, lui, n'y est JAMAIS ajouté par
    nous : il part en en-tête `X-API-Key` (cf. fetch_remote), seule forme que le
    webui accepte encore — et la query, elle, est journalisée côté serveur.
    """
    u = urllib.parse.urlsplit(url)
    if u.scheme not in REMOTE_SCHEMES:
        # file:// serait lu par l'ouvreur par défaut d'urllib : une faute de
        # frappe deviendrait une lecture de fichier local rendue comme des
        # sessions vivantes.
        raise ValueError(f"schéma non supporté : {u.scheme or '(aucun)'} "
                         f"(attendu {' ou '.join(REMOTE_SCHEMES)})")
    return urllib.parse.urlunsplit(
        (u.scheme, u.netloc, u.path.rstrip('/') + '/api/sessions', u.query, ''))


def read_capped(resp: Any, deadline: float) -> bytes:
    """Corps de réponse, au plus REMOTE_MAX_BYTES + 1 octets et avant `deadline`.

    Lecture par tranches, et pas un `read(MAX + 1)` unique : le timeout d'urlopen
    est PAR OPÉRATION socket, donc un pair qui livre un octet toutes les 4 s ne le
    déclenche jamais et parque le thread indéfiniment — ce qui défait aussi
    stop(). Le budget total est vérifié entre deux tranches (horloge monotone :
    un pas NTP ne doit pas rallonger ni écourter le budget).
    """
    chunks: list[bytes] = []
    total = 0
    while total <= REMOTE_MAX_BYTES:
        if time.monotonic() > deadline:
            raise TimeoutError('lecture trop lente')
        chunk = resp.read(min(REMOTE_READ_CHUNK, REMOTE_MAX_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b''.join(chunks)


def fetch_remote(remote: dict,
                 opener: Callable[..., Any] = _REMOTE_OPENER.open,
                 ) -> tuple[list[dict], str | None, int | None, int]:
    """Interroge un remote → (lignes, erreur, code HTTP, lignes annoncées).

    Ne lève JAMAIS : cette fonction est le corps d'un thread de poll, et une
    exception qui s'en échappe tue le thread pour de bon.

    La construction de la Request est DANS le try : `--remote lab=myhost` (sans
    schéma) lève ValueError, et hors du try elle tuait le thread au premier tour
    sans rien enregistrer — tooltip vide, état vide, « aucune session active »
    pour un remote mal configuré. La même levée faisait planter --once tout court,
    emportant les sessions locales.

    timeout couvre connexion ET chaque opération socket ; la lecture a en plus un
    budget total ; le corps est plafonné à 4 MiB (un read() non borné sur une
    socket distante est une bombe mémoire que l'UI ne survit pas) ; les
    redirections ne sont pas suivies (cf. _NoRedirect).
    """
    try:
        req = urllib.request.Request(remote_endpoint(remote['url']),
                                     headers={'User-Agent': 'claude-watcher-tui',
                                              'Accept': 'application/json'})
        if remote.get('token'):
            req.add_header('X-API-Key', remote['token'])
        with opener(req, timeout=REMOTE_TIMEOUT_S) as resp:
            raw = read_capped(resp, time.monotonic() + REMOTE_READ_BUDGET_S)
        if len(raw) > REMOTE_MAX_BYTES:
            return [], f'> {REMOTE_MAX_BYTES // (1024 * 1024)} MiB', None, 0
        payload = json.loads(raw.decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        return [], f'HTTP {e.code}', e.code, 0
    except Exception as e:
        # Tout le reste (URLError, timeout, URL invalide, JSON invalide,
        # décodage…) : le thread ne doit jamais mourir sur un hôte qui répond
        # n'importe quoi — ni sur une URL mal saisie.
        # Le texte d'erreur est SOUS CONTRÔLE DU SERVEUR (mesuré : une ligne de
        # statut bidon arrive telle quelle dans BadStatusLine, échappements ANSI
        # compris) et finit à l'écran → même nettoyage que le reste du payload.
        # Rédaction AVANT nettoyage : le message peut citer l'URL interrogée, donc
        # le token de la query. Même point d'étranglement que l'affichage.
        msg = redact_secrets(f'{type(e).__name__}: {e}', remote.get('url', ''))
        return [], clean_remote_str(msg, 120) or type(e).__name__, None, 0
    rows, total = adapt_remote_payload(payload, remote, time.time())
    return rows, None, None, total


def remote_health(st: dict, poll_s: float, now: float) -> tuple[str, float | None]:
    """(santé, âge de la donnée) — 'ok' | 'stale' | 'auth' | 'down' | 'starting' | 'dead'.

    `now` est une horloge MONOTONE, comme le `received_mono` qu'elle compare : la
    péremption est TOUT le contrat de cette fonctionnalité, et en horloge murale
    un pas NTP arrière ou un portable qui sort de veille rendait `max(0, now-ra)`
    positif et donc frais — un remote mort depuis une journée lisait « ok ».
    (`last_activity`, lui, reste en horloge murale : il est comparé à time.time()
    au rendu. Deux horloges, deux métiers.)

    « jamais répondu » (down) et « répondait, ne répond plus » (stale) sont
    distincts : un remote qui n'a JAMAIS répondu n'a aucune ligne à marquer
    périmée, il serait invisible sans la zone d'état. « starting » les distingue
    tous deux du cas normal « le premier poll n'est pas encore revenu », qui
    n'est pas une panne.

    'dead' : le thread de poll n'est plus là. Sans cet état, un thread mort après
    un premier succès lisait « ok » puis « périmé » POUR TOUJOURS — une donnée
    vieille d'un jour indiscernable d'un hôte ayant manqué deux polls.
    """
    ra = st.get('received_mono')
    age = None if ra is None else max(0.0, now - ra)
    if st.get('alive') is False:
        return 'dead', age
    if age is not None and age <= REMOTE_STALE_X * poll_s:
        return 'ok', age
    if st.get('status') in (401, 403):
        return 'auth', age
    if age is None:
        return ('down' if st.get('error') else 'starting'), None
    return 'stale', age


def remote_health_text(st: dict, poll_s: float, now: float) -> str:
    """Santé seule : « ok 3 » (joignable, 3 sessions) vs « injoignable » /
    « périmé 42s ». Confondre les deux premiers est exactement le mode de panne
    que cette fonctionnalité doit éviter : sans ça, les deux donnent la même
    liste vide.

    « ok 500/612 » quand le payload dépassait REMOTE_MAX_ROWS : tronquer en
    silence donne un tableau qui a l'air complet.
    """
    health, age = remote_health(st, poll_s, now)
    if health == 'ok':
        shown = len(st.get('rows') or [])
        total = st.get('total') or shown
        count = f'{shown}/{total}' if total > shown else str(shown)
        return f"{tr('rm_ok')} {count}"
    if health == 'auth':
        return tr('rm_auth')
    if health == 'starting':
        return tr('rm_starting')
    if health == 'dead':
        return tr('rm_dead')
    if health == 'stale' and age is not None:
        return f"{tr('rm_stale')} {format_elapsed(age)}"
    return tr('rm_down')


def remote_status_text(remote: dict, st: dict, poll_s: float, now: float) -> str:
    """Fragment de la zone d'état : « lab ok 0 » / « lab injoignable »."""
    return f"{remote['label']} {remote_health_text(st, poll_s, now)}"


def local_config_dirs(sessions: list[dict]) -> list[str]:
    """config_dir des lignes LOCALES uniquement.

    Le config_dir d'une ligne distante est un chemin de l'AUTRE machine, et le
    ~/.claude d'un remote existe aussi ici : la boucle naïve poserait un watch
    inotify LOCAL pour le compte d'un remote. Même classe de bug que la collision
    de pid, rayon d'action plus faible.
    """
    return [s['config_dir'] for s in sessions
            if s.get('config_dir') and not s.get('remote')]


def remotes_bar_text(remotes: list[dict], stat: dict[str, dict],
                     poll_s: float, now: float) -> str:
    """Contenu de la zone d'état : un fragment par remote CONFIGURÉ, session ou pas."""
    return f"{tr('rm_label')}: " + ' · '.join(
        remote_status_text(r, stat.get(r['name'], {}), poll_s, now) for r in remotes)


def remotes_bar_tooltip(remotes: list[dict], stat: dict[str, dict]) -> str:
    """Infobulle de la zone d'état : URL RÉDIGÉE + erreur courante par remote."""
    lines = []
    for r in remotes:
        st = stat.get(r['name'], {})
        lines.append(f"{r['label']} — {st.get('display_url', r.get('display_url', ''))}"
                     + (f"\n  {st['error']}" if st.get('error') else ''))
    return '\n'.join(lines)


def empty_state_text(remotes: list[dict], stat: dict[str, dict]) -> str:
    """« aucune session active » serait un mensonge quand des remotes ont été
    interrogés sans succès : on dit lesquels, et pourquoi."""
    failed = [f"{r['label']}: {stat[r['name']].get('error')}"
              for r in remotes
              if stat.get(r['name'], {}).get('error')]
    return '\n'.join([tr('rm_none'), *failed]) if failed else tr('no_session')


def remote_stale_text(rstate: dict | None) -> str | None:
    """« ⚠ périmé 42s » quand la donnée d'un remote dépasse 3 × l'intervalle de poll.

    La ligne est CONSERVÉE (la jeter ferait clignoter la liste au moindre poll
    manqué) mais elle affiche l'âge de la donnée.
    """
    if not rstate or rstate.get('health') in ('ok', 'starting'):
        return None
    age = rstate.get('age')
    return f"⚠ {tr('rm_stale_row')}" + (f' {format_elapsed(age)}' if age is not None else '')


def session_path_cell(s: dict, path_chars: int) -> tuple[str, str]:
    """(préfixe distant, chemin tronqué) de la cellule gauche d'une ligne.

    Ligne distante : « <label>: » (convention scp/rsync, pas besoin de légende) ;
    une ligne locale reste NUE — local est le cas courant, le baliser taxerait la
    majorité des lignes pour énoncer le défaut.

    Le préfixe est réservé HORS du budget de troncature : path_display tronque
    par la GAUCHE, donc préfixer APRÈS coup effacerait le marqueur — la seule
    chose qui distingue les deux lignes.
    """
    label = s.get('remote')
    budget = max(4, path_chars - len(label) - 1) if label else path_chars
    return (f'{label}:' if label else ''), path_display(s.get('display_cwd') or s['cwd'], budget)


def session_tooltip(s: dict, rstate: dict | None = None) -> str:
    """Infobulle de ligne : chemin complet + sujet complet (les cellules
    tronquent — chemin par la gauche, sujet ellipsé) + liste des sous-agents.

    Ligne distante : URL RÉDIGÉE (le token vit dans l'URL) + erreur courante —
    seul moyen de savoir quelle machine se cache derrière un label.
    """
    tip = s['cwd']
    label = s.get('remote')
    if label:
        tip = (f"{tip}\n\n{tr('tip_remote').format(label=label)}"
               f"\n{(rstate or {}).get('display_url', '')}")
        if rstate and rstate.get('error'):
            tip = f"{tip}\n{rstate['error']}"
        stale = remote_stale_text(rstate)
        if stale:
            tip = f"{tip}\n{stale}"
    if s.get('daemon'):
        return f"{tip}\n\n{tr('tip_daemon')}"
    topic = (s.get('topic') or '').strip()
    if topic:
        tip = f'{tip}\n\nTopic: {topic}'
    agents = s.get('agents') or []
    if agents:
        lines = []
        for a in agents:
            detail = ', '.join(x for x in (a.get('type'), a.get('model')) if x)
            lines.append(f" • {a['name']}" + (f' ({detail})' if detail else ''))
        tip = f"{tip}\n\n{tr('tip_agents')}\n" + '\n'.join(lines)
    return tip


def remote_rstate(s: dict, stat: dict[str, dict], poll_s: float,
                  now_mono: float) -> dict | None:
    """État du remote d'une ligne : santé, âge de la donnée, URL rédigée, erreur.
    None pour une ligne locale. `now_mono` est une horloge MONOTONE."""
    if not s.get('remote'):
        return None
    st = stat.get(s.get('remote_name') or '')
    if st is None:
        return None
    health, age = remote_health(st, poll_s, now_mono)
    return {'health': health, 'age': age, 'error': st.get('error'),
            'display_url': st.get('display_url', '')}


def remotes_panel_rows(remotes: list[dict], stat: dict[str, dict],
                       poll_s: float, now: float) -> list[tuple[str, str, str]]:
    """(nom, URL rédigée, santé) par remote configuré — panneau lecture seule des
    paramètres. Les remotes DÉSACTIVÉS y figurent aussi : sans eux, rien ne dit
    qu'un `[remote:*]` a bien été analysé mais est éteint."""
    rows = []
    for r in remotes:
        st = stat.get(r['name'], {})
        health = (remote_health_text(st, poll_s, now) if r.get('enabled', True)
                  else tr('off'))
        rows.append((r['name'], st.get('display_url', r.get('display_url', '')), health))
    return rows


class RemotePoller:
    """Un thread démon par remote actif ; cache {nom: état} sous verrou.

    Chaque thread boucle SÉQUENTIELLEMENT (requête, puis attente) : un hôte lent
    ne ralentit que lui-même, les requêtes ne s'empilent jamais, et un remote mort
    n'affecte pas les autres. `sessions()` et `snapshot()` ne font AUCUN HTTP —
    ils sont appelés depuis la boucle UI.

    Aucun widget n'est touché ici : le thread appelle `notify` (côté GTK :
    GLib.idle_add), qui replanifie le rafraîchissement dans la boucle principale.
    La TUI se rafraîchit sur son propre timer et ne passe pas de notify.

    La liste reçue est déjà filtrée sur `enabled` (cf. enabled_remotes, appliqué
    une seule fois à la résolution) : ce constructeur ne refiltre pas.
    """

    def __init__(self, remotes: list[dict], poll_ms: int = REMOTE_POLL_MS,
                 notify: Callable[[], None] | None = None) -> None:
        self.remotes = list(remotes)
        self.poll_s = max(REMOTE_POLL_MIN_MS / 1000, poll_ms / 1000)
        self._notify = notify
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # `received_mono` et non `received_at` : c'est une horloge MONOTONE, et
        # le nom doit l'annoncer — la confondre avec l'horloge murale est
        # exactement le bug que la péremption ne peut pas se permettre.
        self._state = {r['name']: {'rows': [], 'received_mono': None, 'error': None,
                                   'status': None, 'total': 0, 'alive': None,
                                   'display_url': r.get('display_url', '')}
                       for r in self.remotes}

    def start(self) -> None:
        for r in self.remotes:
            with self._lock:
                self._state[r['name']]['alive'] = True
            threading.Thread(target=self._loop, args=(r,), daemon=True,
                             name=f"remote-{r['name']}").start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def sessions(self) -> list[dict]:
        """Dernières lignes connues de tous les remotes (conservées en cas d'échec :
        les jeter ferait clignoter la liste au moindre poll manqué, et le marqueur
        « périmé » dit déjà qu'elles sont vieilles)."""
        with self._lock:
            return [row for st in self._state.values() for row in st['rows']]

    def _backoff(self, fails: int, status: int | None) -> float:
        delay = min(self.poll_s * 2 ** min(fails, 5), REMOTE_BACKOFF_MAX_S)
        if status in (401, 403):
            # Mauvais token : réessayer toutes les 2 s ne le corrigera pas.
            delay = max(delay, REMOTE_AUTH_RETRY_S)
        return delay

    def _loop(self, remote: dict) -> None:
        """Boucle de poll. Ne lève JAMAIS — et c'est GARANTI ici, pas promis.

        Rien ne gardait ce corps : une levée (depuis notify, depuis un callback,
        depuis n'importe quoi) terminait le thread. Et comme `received_mono`
        était déjà posé, le remote lisait « ok » pendant 3 × l'intervalle puis
        « périmé » POUR TOUJOURS — un instantané vieux d'un jour indiscernable
        d'un hôte ayant manqué deux polls. D'où : corps gardé, erreur
        enregistrée, et `alive=False` en sortie pour que le thread disparu ne
        puisse plus jamais lire « ok ».
        """
        fails = 0
        try:
            while not self._stop.is_set():
                try:
                    rows, error, status, total = fetch_remote(remote)
                    with self._lock:
                        st = self._state[remote['name']]
                        st['error'], st['status'] = error, status
                        if error is None:
                            st['rows'], st['total'] = rows, total
                            st['received_mono'] = time.monotonic()
                    if self._notify is not None and not self._stop.is_set():
                        self._notify()
                    if error is None:
                        fails, delay = 0, self.poll_s
                    else:
                        fails += 1
                        delay = self._backoff(fails, status)
                except Exception as e:
                    fails += 1
                    delay = self._backoff(fails, None)
                    # Rédaction avant nettoyage, comme dans fetch_remote : cette
                    # branche attrape ce qui a échappé à fetch_remote, donc aussi
                    # les messages citant l'URL et sa query.
                    msg = redact_secrets(f'{type(e).__name__}: {e}', remote.get('url', ''))
                    with self._lock:
                        self._state[remote['name']]['error'] = (
                            clean_remote_str(msg, 120) or type(e).__name__)
                self._stop.wait(delay)
        finally:
            with self._lock:
                self._state[remote['name']]['alive'] = False


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
            # Machines distantes : panneau LECTURE SEULE (nom / URL rédigée /
            # santé), équivalent de l'onglet du widget GTK — sans lui, la TUI
            # revendiquait une parité qu'elle n'avait pas. Les remotes sont lus
            # au démarrage : il n'y a rien à éditer ici.
            yield from self._compose_remotes()
            yield Static(f"[dim]{tr('config_hint')}[/dim]")

    def _compose_remotes(self) -> ComposeResult:
        remotes = getattr(CFG, 'remotes', None) or []
        if not remotes:
            return
        poller = getattr(self.app, '_poller', None)
        stat = poller.snapshot() if poller else {}
        poll_s = poller.poll_s if poller else REMOTE_POLL_MS / 1000
        with Vertical(classes="cfg-item"):
            yield Label(f"[b]{tr('cfg_remotes')}[/b]")
            yield Static(tr('cfg_remotes_d'), classes="cfg-desc")
            for name, url, health in remotes_panel_rows(remotes, stat, poll_s,
                                                        time.monotonic()):
                # Text() : l'URL rédigée et le texte de santé peuvent porter des
                # crochets venus du serveur (cf. l'infobulle de la barre d'état).
                yield Static(Text(f"{name:<14} {health:<14} {url}"), classes="cfg-desc")

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
    #remotes { color: #7a9ec2; padding: 0 1; }
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

    def __init__(self, refresh_ms: int, carded: bool = False,
                 remotes: list[dict] | None = None,
                 remote_poll_ms: int = REMOTE_POLL_MS) -> None:
        super().__init__()
        self._refresh_s = max(0.25, refresh_ms / 1000)
        self._carded = carded
        # Aucun remote déclaré → aucun poller, aucun thread, aucun HTTP.
        self._poller = RemotePoller(remotes, remote_poll_ms) if remotes else None
        self._sessions: list[dict] = []
        self._inotify_fd = -1
        self._watched_session_dirs: set[str] = set()
        self._last_sig: tuple | None = None  # structure du tableau au dernier rendu
        self._latest_version: str | None = None
        self._update_state = 'checking'  # checking | ok | old | unknown

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="counts")
        yield Static("", id="remotes")
        # Pas d'en-tête de colonnes (comme le widget GTK) ; colonnes (re)créées au refresh.
        yield SessionTable(id="sessions", cursor_type="row", zebra_stripes=False,
                           show_header=False)
        yield Center(Static(tr('no_session'), id="empty"))
        yield WatcherFooter()

    def on_mount(self) -> None:
        self.title = tr('title')
        self.sub_title = f"v{VERSION}"
        if self._poller:
            self._poller.start()
        self.refresh_sessions()
        self.set_interval(self._refresh_s, self.refresh_sessions)
        self.run_worker(self._watch_sessions_dir(), name="inotify")
        self.run_worker(self._check_version(), name="vercheck")
        self.set_interval(6 * 3600, lambda: self.run_worker(self._check_version(), exclusive=True))

    def on_unmount(self) -> None:
        # Les threads sont daemon (ils ne retiendraient pas le process), mais on
        # les arrête proprement : sans ça un poll en cours survit à l'UI jusqu'au
        # timeout — visible en test et en mode --frame.
        if self._poller:
            self._poller.stop()

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
        # Lecture du cache du poller uniquement : AUCUN HTTP ici (cette méthode
        # tourne dans la boucle UI, un hôte lent figerait l'écran).
        remote_rows = self._poller.sessions() if self._poller else None
        try:
            sessions = scan_sessions(remote_rows)
        except Exception:
            sessions = []
        self._sessions = sessions
        # Deux horloges, deux métiers : `now` (murale) sert aux durées affichées,
        # comparées à last_activity ; `now_mono` (monotone) sert à la péremption
        # des remotes, qu'un pas NTP arrière ne doit pas pouvoir rajeunir.
        now = time.time()
        now_mono = time.monotonic()
        rstat = self._poller.snapshot() if self._poller else {}

        # Surveille le sessions/ de chaque CLAUDE_CONFIG_DIR exposé (inotify
        # dynamique). Les lignes DISTANTES sont exclues (cf. local_config_dirs).
        for cfg_dir in local_config_dirs(sessions):
            self._add_session_watch(Path(cfg_dir) / 'sessions')

        table = self.query_one("#sessions", DataTable)
        empty = self.query_one("#empty", Static)

        # Préserve la session sous le curseur pour ne pas la perdre au repeuplement.
        # On la lit via la clé de ligne (cf. session_key), pas via une colonne cachée.
        prior_key = None
        if table.row_count and 0 <= table.cursor_row < table.row_count:
            try:
                prior_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            except Exception:
                prior_key = None
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
        waiting = working = background = 0
        target_row = 0
        row_tips: dict[str, Text] = {}  # str(pid) → infobulle (chemin + sujet complets)
        built: list[tuple[str, Text, Text, int]] = []  # (clé, cellule gauche, droite, hauteur)
        for i, s in enumerate(sessions):
            color, badge = session_state_label(s)
            if s['waiting']:
                waiting += 1
            elif s['working']:
                working += 1
            elif s['background']:
                background += 1

            daemon = s.get('daemon')
            # Cellule gauche : ● + chemin (ligne 1), pid · durée en sourdine (ligne 2).
            sess = Text(no_wrap=True, overflow="ellipsis")
            sess.append("● ", style=color)
            # Préfixe « (D) » en orange Claude pour repérer le démon d'un coup d'œil.
            if daemon:
                sess.append("(D) ", style=f"bold {COLOR_CLAUDE}")
            # Préfixe distant + chemin tronqué : cf. session_path_cell (la
            # réservation du budget y est expliquée et testée).
            prefix, path_txt = session_path_cell(s, path_chars)
            if prefix:
                sess.append(prefix, style=COLOR_REMOTE)
            sess.append(path_txt, style="#e2e2e2 bold")
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
            if idle_fmt != 'none' and la is not None \
                    and not s['working'] and not s['waiting'] and not s['background']:
                sess.append(f" · idle {format_idle(now - la, idle_fmt)}", style=TEXT_DIM2)
            cfg = display_config_dir(s.get('config_dir'))
            if cfg:
                sess.append(f" {CLAUDE_IDLE_GLYPH}{cfg}", style=COLOR_CLAUDE)
            # Donnée périmée : cf. remote_stale_text (formulation et couverture
            # alignées sur le widget GTK).
            rstate = remote_rstate(s, rstat, self._poller.poll_s, now_mono) \
                if self._poller else None
            stale = remote_stale_text(rstate)
            if stale:
                sess.append(f" {stale}", style=COLOR_WAITING)
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

            # Infobulle de survol : cf. session_tooltip.
            # Text() et NON str : l'infobulle Textual est un Static(markup=True),
            # un sujet retombé sur lastPrompt peut contenir des crochets ('[/]',
            # '[INST]'…) → MarkupError ou texte mangé. Text neutralise le markup.
            key = session_key(s)
            row_tips[key] = Text(session_tooltip(s, rstate))
            built.append((key, sess, st, row_h))
            if key == prior_key:
                target_row = i

        table._row_tips = row_tips
        has_rows = bool(sessions)
        table.display = has_rows
        empty.display = not has_rows
        if not has_rows:
            # Text() et NON str : Static interprète le markup Rich, et le texte
            # d'erreur vient du serveur — un « [/] » dans une ligne de statut
            # lèverait MarkupError EN PLEIN TICK de refresh (vérifié).
            empty.update(Text(empty_state_text(
                self._poller.remotes if self._poller else [], rstat)))

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

        # Ordre : waiting · working · [background ·] total — aligné sur le header GTK.
        # Le fragment background n'apparaît que s'il y a au moins une session en fond.
        bg_frag = tr('count_bg').format(b=background) if background else ''
        self.query_one("#counts", Static).update(
            tr('count').format(w=waiting, p=working)
            + bg_frag + tr('count_total').format(t=len(sessions))
        )

        # Zone d'état des remotes : TOUS les remotes configurés y figurent, même
        # sans session. Un remote qui n'a JAMAIS répondu (URL fausse, token
        # invalide, hôte éteint au démarrage) n'a aucune ligne à marquer périmée
        # — sans cette zone il serait purement invisible.
        remotes_bar = self.query_one("#remotes", Static)
        remotes_bar.display = bool(self._poller)
        if self._poller:
            # Text() des DEUX côtés : le texte d'erreur vient du serveur, et
            # l'infobulle Textual est elle aussi un Static(markup=True) — un
            # « [/] » dans une ligne de statut y levait MarkupError au survol,
            # donc un remote hostile faisait tomber la TUI (les deux sites
            # voisins, eux, enveloppaient déjà).
            remotes_bar.update(Text(remotes_bar_text(
                self._poller.remotes, rstat, self._poller.poll_s, now_mono)))
            remotes_bar.tooltip = Text(remotes_bar_tooltip(self._poller.remotes, rstat))

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
        # Session distante : rien à focus ici, et on le DIT (focus_terminal
        # refuserait de toute façon — la garde y est aussi, au point d'étranglement).
        if s.get('remote'):
            self.notify(tr('rm_readonly').format(label=s['remote']),
                        severity="information", timeout=2)
            return
        ok = focus_terminal(s)
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
        # Session distante : lecture seule. La garde vit AUSSI dans kill_session
        # — ici c'est le message, là-bas c'est la sécurité.
        if s.get('remote'):
            self.notify(tr('rm_readonly').format(label=s['remote']),
                        severity="warning", timeout=2)
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
            if kill_session(s):
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


def print_once(remotes: list[dict] | None = None) -> None:
    """Dump texte brut (non-TTY / debug) — pas de TUI.

    Un seul passage : les remotes sont interrogés SÉQUENTIELLEMENT ici (pas de
    thread pour un one-shot), chacun plafonné par le timeout de fetch_remote.
    """
    remote_rows: list[dict] = []
    stat: dict[str, dict] = {}
    for r in remotes or []:
        rows, error, status, total = fetch_remote(r)
        stat[r['name']] = {'rows': rows, 'error': error, 'status': status, 'total': total,
                           'display_url': r.get('display_url', '')}
        if error:
            print(f"[{'remote':>9}] {r['label']}: {error} ({r.get('display_url', '')})")
        remote_rows.extend(rows)
    sessions = scan_sessions(remote_rows)
    if not sessions:
        # « aucune session active » après avoir listé des remotes en échec est
        # exactement le mensonge que la spec interdit : même texte que la vue
        # live (cf. empty_state_text).
        print(empty_state_text(list(remotes or []), stat))
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
                and not s['working'] and not s['waiting'] and not s['background'] else "")
        topic = (s.get('topic') or '').strip().split('\n', 1)[0]
        top = f" · {topic}" if topic else ""
        agents = s.get('agents') or []
        ag = f" · {len(agents)} {tr('agents') if len(agents) > 1 else tr('agent')}" if agents else ""
        # Le label balise le CHEMIN, comme dans la vue live (il balisait le
        # projet ici, et les deux vues se contredisaient sur la convention scp).
        prefix, path_txt = session_path_cell(s, 40)
        cell = f"{prefix}{'(D) ' if s.get('daemon') else ''}{path_txt}"
        print(f"[{badge:>9}] {cell:<44} {tr('pid')} {s['pid']} · "
              f"{format_elapsed(s['elapsed'])}{ctx}{inst}{wt}{idle}{ag}{top}")


async def _smoke_frame() -> None:
    """Monte l'app + laisse passer un refresh en headless, puis quitte.

    Toute exception du rendu (compose / on_mount / refresh_sessions) remonte ici.
    """
    # enabled_remotes ici aussi : le poller ne refiltre plus (le filtre vit à un
    # seul endroit), donc lui passer la liste complète ferait interroger un
    # remote explicitement désactivé.
    app = WatcherApp(refresh_ms=10_000, carded=getattr(CFG, 'cards', False),
                     remotes=enabled_remotes(getattr(CFG, 'remotes', None) or []),
                     remote_poll_ms=getattr(CFG, 'remote_poll_ms', REMOTE_POLL_MS))

    async def auto_pilot(pilot) -> None:  # noqa: ANN001
        await pilot.pause(0.4)  # laisse on_mount + 1er scan se terminer
        app.exit()

    await app.run_async(headless=True, auto_pilot=auto_pilot)


def main() -> None:
    global CFG
    conf = load_config()
    CFG = parse_args(conf)
    # Remotes : sections du config.ini + drapeaux --remote (les drapeaux gagnent,
    # rien n'est persisté). Liste vide = comportement d'avant, à l'octet près.
    CFG.remote_poll_ms = conf['remote_poll_ms']
    try:
        CFG.remotes = resolve_remotes(conf['remote_sections'], CFG.remote)
    except ValueError as e:
        # `enabled` ininterprétable, ou deux noms de remote sur la même variable
        # d'environnement de token : on refuse de démarrer PLUTÔT que d'envoyer
        # le token quelque part par défaut. Message, pas traceback.
        raise SystemExit(f"claude-watcher: {e}") from None
    enabled = enabled_remotes(CFG.remotes)
    if CFG.once:
        print_once(enabled)
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
    WatcherApp(refresh_ms=CFG.refresh_ms, carded=CFG.cards,
               remotes=enabled, remote_poll_ms=CFG.remote_poll_ms).run()


if __name__ == '__main__':
    main()
