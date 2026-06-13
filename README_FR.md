# Claude Code Watcher — TUI

> [English version](README.md)

Une interface terminal (Textual) qui surveille toutes les sessions Claude Code actives sur la machine dans un tableau en temps réel — entièrement au clavier, fonctionne dans n'importe quel terminal.

## Fonctionnalités

- Détecte automatiquement toutes les sessions Claude Code actives
- Affiche l'état de chaque session en **temps réel** :
  - **Attente** (orange) — Claude a répondu, attend votre saisie
  - **Travaille** (amber) — Claude traite votre message, avec le nom de l'outil
  - **Idle** (vert) — session en pause
- Utilisation du contexte (`ctx%`) affichée si disponible
- `Entrée` ou clic sur une ligne pour focus le terminal de la session
- Mode cartes (`c`) pour un affichage plus aéré
- Langue auto-détectée depuis la locale système (`fr` / `en`)

## Prérequis

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (installé automatiquement si absent)
- `wmctrl` et `xdotool` pour le focus terminal

## Installation

```bash
curl -fsSL https://github.com/claude-watcher/tui/releases/latest/download/install.sh | bash
```

Épingler une version précise plutôt que la dernière :

```bash
curl -fsSL https://github.com/claude-watcher/tui/releases/download/v1.5.1/install.sh | bash
```

Pour **monter de version**, relance simplement la commande `latest`.

L'installateur :
1. Installe `uv` si absent, vérifie `wmctrl`/`xdotool`
2. Télécharge le script dans `~/bin/claude-watcher-tui`
3. Définit la langue (demandée dans un terminal ; sinon `CW_LANG=fr|en`)
4. Crée `~/.config/claude-watcher/config.ini` (config partagée, ignorée si déjà présente)

<details>
<summary>Depuis un clone local (développement)</summary>

```bash
git clone https://github.com/claude-watcher/tui
cd tui
./install.sh          # installe le script du clone, sans téléchargement
```
</details>

> **Aucun hook à installer :** l'état provient du registre de sessions propre à
> Claude Code — rien n'est ajouté à `settings.json`.

## Utilisation

```bash
uv run ~/bin/claude-watcher-tui
```

### Raccourcis clavier

| Touche | Action |
|--------|--------|
| `↑` / `↓` | Naviguer entre les sessions |
| `Entrée` / clic | Focus le terminal de la session |
| `r` | Rafraîchir maintenant |
| `c` | Basculer le mode cartes |
| `q` | Quitter |

### Options CLI

```
--lang fr|en        forcer la langue (défaut : auto-détectée)
--refresh-ms MS     intervalle de rafraîchissement (défaut : 2000)
--once              afficher les sessions en texte brut et quitter (debug)
--cards             démarrer en mode cartes
```

## Comment ça marche

Pour les détails techniques — détection des sessions, internals du focus au clic,
format du fichier de config et limitations connues — voir
[`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md) (en anglais).
