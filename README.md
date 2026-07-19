# Bot Musique Discord

Bot Discord multi-serveur avec support YouTube, SoundCloud et Spotify, propulsé par Lavalink.

## Fonctionnalités

- **Lecture audio** : YouTube, SoundCloud, Spotify (singles + playlists/albums)
- **File d'attente** : shuffle, remove, skipto, loop (track/queue)
- **Spotify** : résolution par scraping (pas d'API officielle) avec matching intelligent
- **Vote-skip** : si pas de rôle DJ, seuil à 50% des membres non-bot
- **Filtres audio** : bassboost, nightcore
- **Playlists** : sauvegarde/chargement par utilisateur (SQLite)
- **Persistance** : queue sauvegardée en SQLite, restaurée au reconnect
- **Déconnexion auto** : 5 min après le départ du dernier utilisateur
- **Boutons interactifs** : pause, skip, volume, loop, favori

## Prérequis

- **Python 3.10+** et `pip`
- **Java 17+** (pour Lavalink) — [Adoptium](https://adoptium.net/)
- **Lavalink** : télécharger `Lavalink.jar` dans `lavalink/`
  - https://github.com/lavalink-devs/Lavalink/releases
- **Plugins** (optionnels mais recommandés) dans `lavalink/plugins/` :
  - `youtube-plugin-*.jar` — https://github.com/lavalink-devs/youtube-source
  - `lavasrc-plugin-*.jar` — https://github.com/topi314/LavaSrc

## Installation

```bash
# Cloner / télécharger les fichiers
# Installer les dépendances Python
pip install -r requirements.txt

# Créer le fichier .env
echo DISCORD_TOKEN=ton_token_ici > .env
echo LAVALINK_URI=http://localhost:2333 >> .env
echo LAVALINK_PASSWORD=ton_mot_de_passe >> .env
```

## Configuration

### Fichier `.env`

| Variable | Obligatoire | Défaut | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Token du bot Discord |
| `LAVALINK_URI` | ✅ | `http://localhost:2333` | URL du serveur Lavalink |
| `LAVALINK_PASSWORD` | ✅ | `youshallnotpass` | Mot de passe Lavalink |
| `DJ_ROLE_ID` | ❌ | — | ID du rôle DJ (optionnel) |
| `DATABASE_PATH` | ❌ | `playlists.db` | Chemin de la base SQLite |

### Fichier `application.yml`

Le fichier `application.yml` est **généré automatiquement** par `generate_yml.py` à partir de `application.yml.example` et des variables du `.env`. Ne pas modifier `application.yml` directement (il est gitignoré).

## Lancement

```bash
# Tout-en-un (Lavalink + bot)
start.bat

# Ou manuellement :
# 1. Générer application.yml
python generate_yml.py
# 2. Lancer Lavalink
java -jar lavalink/Lavalink.jar
# 3. Lancer le bot (autre terminal)
python bot.py
```

## Commandes

| Commande | Description | DJ requis |
|---|---|---|
| `/play <query>` | Joue une musique ou l'ajoute à la file | ❌ |
| `/pause` / `/resume` | Pause / Reprendre | ❌ |
| `/skip` | Passe au morceau suivant (vote si pas DJ) | ❌ |
| `/skipto <pos>` | Saute à une position dans la file | ✅ |
| `/seek <temps>` | Avance dans le morceau | ❌ |
| `/stop` | Stop + vide la file | ✅ |
| `/volume <0-100>` | Volume | ✅ |
| `/nowplaying` | Morceau en cours | ❌ |
| `/queue` | File d'attente paginée | ❌ |
| `/shuffle` | Mélange la file | ✅ |
| `/remove <pos>` | Retire un morceau | ✅ |
| `/loop` | Cycle off → track → queue | ❌ |
| `/bassboost` / `/nightcore` | Filtres audio | ✅ |
| `/reset` | Reset des filtres | ✅ |
| `/save <nom>` | Sauvegarde la file en playlist | ❌ |
| `/playlist <nom>` | Charge une playlist | ❌ |
| `/pl_list` | Liste ses playlists | ❌ |
| `/pl_delete <nom>` | Supprime une playlist | ❌ |
| `/setchannel [#salon]` | Salon dédié au now-playing | ❌ |

## Architecture

```
bot.py                  # Point d'entrée, logging, connexion Lavalink
config.py               # Variables d'environnement
cogs/music.py           # Toute la logique du bot
utils/
  db.py                 # SQLite (playlists, settings, queue_state)
  embed_builder.py      # Embeds centralisés (couleurs, build_embed)
  music_helpers.py      # Embeds nowplaying + queue
generate_yml.py         # Génération de application.yml depuis .env
start.bat               # Lancement local
```

## Licence

MIT
