# Claude Code Watcher — TUI

> [English version](README.md)

Une interface terminal (Textual) qui surveille toutes les sessions Claude Code actives sur la machine dans un tableau en temps réel — entièrement au clavier, fonctionne dans n'importe quel terminal.

<p align="center">
  <img src="doc/tui-fr.gif" alt="La TUI Claude Code Watcher suit plusieurs sessions dans un tableau en temps réel et bascule en mode cartes" width="800">
</p>

## Fonctionnalités

- Détecte automatiquement toutes les sessions Claude Code actives
- Affiche l'état de chaque session en **temps réel** :
  - **Attente** (orange) — Claude a répondu, attend votre saisie
  - **Travaille** (amber) — Claude traite votre message, avec le nom de l'outil
  - **Idle** (vert) — session en pause
- Utilisation du contexte (`ctx%`) affichée si disponible
- Nombre de **sous-agents** lancés par session (`N agents`), chacun détaillé dans l'infobulle de la ligne — désactivable dans les Réglages
- **Démon** de fond affiché en ligne `(D)` non-focusable (masquable dans les Réglages)
- **Tri par inactivité** optionnel (`s`) — sessions les plus récemment inactives en tête
- **Durée d'inactivité** optionnelle (`i`) sur les lignes idle — approx. (`02:24`, résolution minute) ou précise (`02:24:23`)
- Sessions en **worktree** Git rattachées à leur vrai projet, étiquetées `↳ WT: <nom>`
- `Entrée`/`Espace` ou clic sur une ligne pour focus le terminal de la session (le clic est désactivable dans les Réglages)
- Mode cartes (`c`) pour un affichage plus aéré
- En-tête affichant la version installée avec un indicateur de mise à jour (vert = à jour, rouge = une nouvelle version disponible)
- **Fenêtre de réglages** (`p`) — choix de la langue et de toutes les options d'affichage au même endroit (persistées)
- Langue auto-détectée depuis la locale système (`fr` / `en`), modifiable à tout moment dans les réglages
- **Machines distantes** — les sessions d'autres hôtes servant `claude-watcher-webui`, fusionnées dans la même liste et balisées `<nom>:<chemin>` (lecture seule ; voir [Sessions distantes](#sessions-distantes))

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
curl -fsSL https://github.com/claude-watcher/tui/releases/download/v1.3.1/install.sh | bash
```

Pour **monter de version**, relance simplement la commande `latest`.

L'installateur :
1. Installe `uv` si absent, vérifie `wmctrl`/`xdotool`
2. Télécharge le script dans `~/.local/bin/claude-watcher-tui`
3. Crée `~/.config/claude-watcher/config.ini` (config partagée, ignorée si déjà présente)

La langue est auto-détectée depuis la locale système, puis modifiable dans la fenêtre de réglages (`p`) ou dans `config.ini`.

<details>
<summary>Depuis un clone local (développement)</summary>

```bash
git clone https://github.com/claude-watcher/tui
cd tui
./install.sh          # installe le script du clone, sans téléchargement
```
</details>

> **Aucun hook à installer :** l'état provient des fichiers de session propres à
> Claude Code — rien n'est ajouté à `settings.json`.

## Utilisation

```bash
uv run ~/.local/bin/claude-watcher-tui
```

> **Pas dans votre `PATH` ?** `~/.local/bin` est dans le `PATH` par défaut sur la
> plupart des distributions, mais pas toutes. Si la commande est introuvable,
> ajoutez ceci à `~/.profile` (ou au rc de votre shell) puis reconnectez-vous :
> ```bash
> export PATH="$PATH:$HOME/.local/bin"
> ```

### Raccourcis clavier

| Touche | Action |
|--------|--------|
| `↑` / `↓` | Naviguer entre les sessions |
| `Entrée` / `Espace` / clic | Focus le terminal de la session (focus au clic désactivable dans les Réglages) |
| `p` | **Réglages** — langue + options d'affichage (appliqués et enregistrés aussitôt) |
| `k` | Fermer la session sélectionnée (inactive uniquement) — confirmation, puis `SIGTERM` |
| `a` | À propos / infos de mise à jour |
| `q` | Quitter |
| `c` `t` `h` `s` `i` | Bascules rapides (aussi dans les Réglages) : cartes · sujet · infobulle · tri · inactivité |

### Options CLI

```
--lang fr|en        forcer la langue (défaut : auto-détectée)
--refresh-ms MS     intervalle de rafraîchissement (défaut : 2000)
--once              afficher les sessions en texte brut et quitter (debug)
--cards             démarrer en mode cartes
--no-topic          masque le sujet de session sous chaque ligne (bascule avec « t »)
--no-agents         masque le compteur de sous-agents lancés par session
--hide-daemons      masque les lignes du démon Claude Code (balisées (D))
--no-hover          désactive l'infobulle de survol (bascule avec « h »)
--no-click-focus    le clic ne focalise plus le terminal (Entrée/Espace restent actifs)
--sort default|idle  ordre de tri (défaut : default ; bascule avec « s »)
--idle-format none|loose|precise  durée d'inactivité sur les lignes idle (défaut : none ; cycle avec « i »)
--remote NAME=URL   surveille une machine servant claude-watcher-webui (répétable)
--no-local          n'affiche que les sessions distantes (aucun scan /proc local)
```

## Sessions distantes

Pointez le watcher vers d'autres machines qui font tourner
[`claude-watcher-webui`](https://github.com/claude-watcher/webui) : leurs sessions
apparaissent dans la même liste, balisées `<nom>:<chemin>` (convention scp). Les lignes
distantes sont en **lecture seule** : ni focus, ni fermeture. Un remote qui ne répond plus
est marqué périmé avec l'âge de ses données, et chaque remote configuré figure dans la
ligne d'état sous les compteurs avec sa santé — `lab ok 3` (joignable) n'est jamais
confondu avec `lab injoignable`.

### D'abord, sur la machine distante

Il y a une moitié serveur, et elle n'est pas optionnelle :

1. Installez et **lancez**
   [`claude-watcher-webui`](https://github.com/claude-watcher/webui) sur cette machine —
   le watcher n'est qu'un consommateur de son `GET /api/sessions`.
2. webui écoute par défaut sur `APP_HOST=127.0.0.1` : tel quel, il n'est joignable **que
   depuis la machine elle-même**. Pour le regarder d'ailleurs, il faut soit élargir
   l'écoute, soit tunneliser (voir plus bas).
3. Une écoute non-loopback (`0.0.0.0` par exemple) **sans** `APP_AUTH_TOKEN` est
   **refusée au démarrage** — posez un token, ou acceptez explicitement le risque avec
   `APP_ALLOW_INSECURE_BIND=true`. Ce token est celui que vous donnerez au watcher.

> **webui parle HTTP en clair.** Il ne termine aucun TLS (il n'existe pas de réglage
> `ssl_certfile`), donc `https://box:8000/` ne marche **pas** contre lui : la connexion
> échoue sur `SSL: RECORD_LAYER_FAILURE`. Utilisez `http://`, ou placez un reverse proxy
> (nginx, Caddy, Traefik) devant et pointez le watcher vers l'URL `https://` du proxy.

La forme la plus sûre ne demande aucun proxy et garde le token hors du réseau — un tunnel
SSH vers une URL loopback :

```bash
ssh -N -L 8001:127.0.0.1:8000 box &          # webui reste sur 127.0.0.1 côté « box »
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://127.0.0.1:8001
```

### Déclarer des remotes

Les remotes permanents se déclarent dans `~/.config/claude-watcher/config.ini` (partagé
avec le widget GTK : une seule déclaration pour les deux) :

```ini
[remotes]
poll_ms = 2000              # intervalle d'interrogation, distinct de refresh_ms.
                            # Défaut 2000, plancher 250 — en dessous vous
                            # martelez l'hôte plus que vous ne le surveillez.

[remote:lab]
url = http://box:8000/      # SEULE clé obligatoire ; une section sans elle est ignorée
token = s3cr3t
enabled = true              # 1/yes/true/on · 0/no/false/off. Toute autre valeur
                            # est refusée au démarrage plutôt que prise pour « on »
label = lab                 # optionnel, défaut : le nom de la section
```

Le fichier est forcé en mode `0600` à chaque écriture du watcher, puisqu'il peut contenir
des tokens. Si vous le créez ou l'éditez à la main, faites
`chmod 600 ~/.config/claude-watcher/config.ini` vous-même — rien ne re-chmode un fichier
que le watcher n'a jamais écrit.

Pour jeter un œil ponctuel à une machine, utilisez le drapeau — il n'est jamais écrit dans
le fichier de config :

```bash
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://box:8000
uv run ~/.local/bin/claude-watcher-tui --remote lab=http://remote:s3cr3t@box:8000/
CW_REMOTE_TOKEN_LAB=s3cr3t uv run ~/.local/bin/claude-watcher-tui --remote lab=http://box:8000
```

Ordre de résolution du token, premier trouvé gagne :

1. le userinfo de l'URL — `https://remote:<token>@hote/` (le token est le **mot de
   passe** ; `https://<token>@hote/` sans deux-points marche aussi)
2. `CW_REMOTE_TOKEN_<NOM>` — le nom en majuscules, non-alphanumériques remplacés par `_`
   (`--remote my-lab=…` → `CW_REMOTE_TOKEN_MY_LAB`)
3. la clé `token` de la section `[remote:<nom>]` correspondante
4. aucun — le remote est interrogé sans authentification

Quelle que soit sa provenance, le token part dans un **en-tête** `X-API-Key`, jamais dans
un paramètre de query — webui n'accepte le token qu'en en-tête (`X-API-Key`,
`Authorization: Bearer`, `Authorization: Basic`), et il journalise `query_params` à chaque
requête : un token dans l'URL serait à la fois refusé et écrit en clair dans le log du
serveur. Une query présente dans l'URL du remote est tout de même transmise telle quelle —
le watcher ne réécrit pas votre URL, et un reverse proxy peut avoir besoin de ses propres
paramètres — mais elle ne vous authentifiera pas, et elle est masquée partout où le
watcher l'affiche.

> **Le token doit être en ASCII.** Les valeurs d'en-tête HTTP sont en latin-1 : un token
> hors de cette plage s'authentifierait comme une autre chaîne ; webui refuse un tel token
> au démarrage plutôt que de servir des 401 inexplicables.

> **Un token passé dans `--remote` est visible par tous les utilisateurs de la machine**
> via `/proc/<pid>/cmdline`, lisible par tous (`-r--r--r--`), alors que
> `/proc/<pid>/environ` n'est lisible que par son propriétaire (`-r--------`). Sur une
> machine partagée, préférez `CW_REMOTE_TOKEN_<NOM>` ou le fichier de config (`0600`).

> **Un token envoyé à un remote `http://` circule en clair**, et le watcher ne vous en
> empêchera pas. Utilisez un tunnel SSH vers une URL loopback, ou un reverse proxy qui
> termine le `https://` (les certificats sont alors vérifiés, sans option pour le
> désactiver).

Seules les URL `http` et `https` sont interrogées : un `--remote lab=box` sans schéma ou
une coquille `file://` sont signalés comme une erreur sur ce remote, pas exécutés.

### Modes de panne, et ce que le watcher en fait

| Situation | Comportement |
|---|---|
| Hôte lent ou figé | timeout de 5 s (connexion et lecture) **et** budget total de lecture de 5 s ; un thread par remote, donc seul cet hôte est ralenti |
| Réponse énorme | lecture plafonnée à 4 Mio, poll compté en échec |
| Échecs répétés | backoff exponentiel, plafonné à 60 s |
| HTTP 401 / 403 | affiché comme une erreur d'auth, réessayé au plus toutes les 5 min |
| Redirections | **non suivies** — une 302 rejouerait votre token vers la cible |
| Plus de 500 sessions | tronqué, et la ligne d'état affiche `lab ok 500/612` |
| Premier poll en cours | `lab démarrage`, pas `lab injoignable` |
| Thread de poll disparu | `lab thread arrêté` — jamais un `ok` trompeur |

Les remotes sont lus au démarrage : en ajouter ou en retirer demande un redémarrage
(l'écran de paramètres les liste en lecture seule, avec leur URL rédigée et leur santé).
Pointer un remote vers votre propre machine avec le scan local actif liste chaque session
deux fois — une fois nue, une fois préfixée ; c'est un choix de configuration, pas un bug.

## Comment ça marche

Pour les détails techniques — détection des sessions, internals du focus au clic,
format du fichier de config et limitations connues — voir
[`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md) (en anglais).
