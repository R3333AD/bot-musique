import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

LAVALINK_URI = os.getenv("LAVALINK_URI")
if not LAVALINK_URI:
    raise RuntimeError("LAVALINK_URI environment variable is not set")

LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")
if not LAVALINK_PASSWORD:
    raise RuntimeError("LAVALINK_PASSWORD environment variable is not set")

# Non utilisés par le code actuel (résolution Spotify par scraping, pas API officielle).
# Régénère un nouveau secret sur https://developer.spotify.com/dashboard si tu veux
# les réactiver un jour, celui qu'il y avait ici a été vu en clair, à considérer grillé.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

dj_role_str = os.getenv("DJ_ROLE_ID")
DJ_ROLE_ID: int | None = int(dj_role_str) if dj_role_str else None

DATABASE_PATH = os.getenv("DATABASE_PATH", "playlists.db")
