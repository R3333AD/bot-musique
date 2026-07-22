import asyncio
import json
import logging
import math
import re
import ssl

import aiohttp
import certifi
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from config import DJ_ROLE_ID
from utils.db import (
    save_playlist, append_to_playlist, load_playlist, list_playlists,
    delete_playlist, get_music_channel, set_music_channel,
    save_queue_state, load_queue_state, delete_queue_state,
    spotify_cache_get, spotify_cache_set, spotify_cache_cleanup,
    get_guild_theme, set_guild_theme,
)
from utils.embed_builder import build_embed, COLOR_NOW_PLAYING
from utils.music_helpers import create_queue_embed

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 10.0

SPOTIFY_TRACK_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-\w+/)?track/([a-zA-Z0-9]+)"
)
SPOTIFY_COLLECTION_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-\w+/)?(playlist|album)/([a-zA-Z0-9]+)"
)

_EMBED_SCRIPT_RE = re.compile(r"<script[^>]*>({.*?})</script>", re.DOTALL)

_SPOTIFY_INTL_PREFIX_RE = re.compile(r"^(https?://open\.spotify\.com/)intl-\w+/")


def _normalize_spotify_url(url: str) -> str:
    url = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return _SPOTIFY_INTL_PREFIX_RE.sub(r"\1", url)


async def _search_with_timeout(query, node, source=None):
    kwargs = {"node": node}
    if source is not None:
        kwargs["source"] = source
    return await asyncio.wait_for(
        wavelink.Playable.search(query, **kwargs),
        timeout=SEARCH_TIMEOUT,
    )


async def _spotify_url_to_query(url: str) -> str | None:
    url = _normalize_spotify_url(url)
    cached = await spotify_cache_get(f"oembed:{url}")
    if cached is not None:
        return cached

    oembed_url = f"https://open.spotify.com/oembed?url={url}"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = (data.get("title") or "").strip()
                    author = (data.get("author_name") or "").strip()
                    if title:
                        result = f"{author} - {title}" if author else title
                        await spotify_cache_set(f"oembed:{url}", result)
                        return result
    except asyncio.TimeoutError:
        logger.warning("Spotify oembed timeout: %s", url)
    except aiohttp.ClientError as e:
        logger.warning("Spotify oembed error: %s", e)
    except Exception:
        logger.exception("Spotify oembed unexpected error")
    return None


async def _spotify_get_access_token() -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://open.spotify.com/get_access_token?reason=transport&productType=embed",
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("accessToken")
    except Exception as e:
        logger.warning("Failed to get Spotify access token: %s", e)
    return None


async def _spotify_collection_from_api(entity_type: str, entity_id: str, session: aiohttp.ClientSession, token: str) -> list[dict] | None:
    out = []
    offset = 0
    limit = 100
    try:
        while True:
            api_url = f"https://api.spotify.com/v1/{entity_type}s/{entity_id}/tracks?limit={limit}&offset={offset}&market=US"
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=10),
                headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                if not items:
                    break
                for item in items:
                    track = item.get("track") if entity_type == "playlist" else item
                    if not track:
                        continue
                    title = (track.get("name") or "").strip()
                    if not title:
                        continue
                    artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
                    out.append({
                        "title": title,
                        "artist": artists,
                        "duration_ms": track.get("duration_ms", 0),
                    })
                if len(items) < limit:
                    break
                offset += limit
        return out if out else None
    except Exception as e:
        logger.warning("Spotify API pagination error: %s", e)
        return None


async def _spotify_collection_from_embed(url: str, session: aiohttp.ClientSession) -> list[dict] | None:
    embed_url = re.sub(
        r"https?://open\.spotify\.com/(?:intl-\w+/)?",
        "https://open.spotify.com/embed/",
        url,
    )
    try:
        async with session.get(embed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text(encoding="utf-8")
            for match in _EMBED_SCRIPT_RE.finditer(html):
                try:
                    data = json.loads(match.group(1))
                    entity = (
                        data.get("props", {})
                        .get("pageProps", {})
                        .get("state", {})
                        .get("data", {})
                        .get("entity", {})
                    )
                    if entity.get("type") in ("playlist", "album"):
                        out = []
                        for t in entity.get("trackList", []):
                            title = (t.get("title") or "").strip()
                            if not title:
                                continue
                            artist = (t.get("subtitle") or "").strip()
                            artist_clean = re.sub(r"[,\s]+", " ", artist).strip()
                            title_clean = re.sub(
                                r"\s*[([]([^)\]]*feat\.[^)\]]*|[^)\]]*remastered[^)\]]*|[^)\]]*remix[^)\]]*|[^)\]]*live[^)\]]*)[)\]]\s*",
                                "",
                                title,
                                flags=re.IGNORECASE,
                            ).strip()
                            out.append({
                                "title": title_clean,
                                "artist": artist_clean,
                                "duration_ms": t.get("duration", 0),
                            })
                        return out
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return None


async def _spotify_collection_tracks(url: str) -> list[dict] | None:
    url = _normalize_spotify_url(url)
    cached = await spotify_cache_get(f"collection:{url}")
    if cached is not None:
        return json.loads(cached)

    m = SPOTIFY_COLLECTION_RE.match(url)
    if not m:
        return None
    entity_type = m.group(1)
    entity_id = m.group(2)

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as session:
        # Try API first (paginated = all tracks)
        token = await _spotify_get_access_token()
        if token:
            out = await _spotify_collection_from_api(entity_type, entity_id, session, token)
            if out:
                logger.info("Spotify API fetched %d tracks for %s", len(out), url)
                await spotify_cache_set(f"collection:{url}", json.dumps(out))
                return out

        # Fallback: embed page (limited to ~20 tracks)
        out = await _spotify_collection_from_embed(url, session)
        if out:
            logger.info("Spotify embed fetched %d tracks for %s (API unavailable)", len(out), url)
            await spotify_cache_set(f"collection:{url}", json.dumps(out))
            return out

    return None


_JUNK_TITLE_RE = re.compile(
    r"\b("
    r"official\s*(music\s*)?(video|audio)|"
    r"lyrics?(\s*video)?|"
    r"visualizer|"
    r"audio\s*only|"
    r"full\s*song|"
    r"hq|hd|4k"
    r")\b",
    re.IGNORECASE,
)


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", _JUNK_TITLE_RE.sub("", text)).strip()


def _score_track(t: wavelink.Playable, orig_title: str, orig_artist: str, orig_dur: int) -> float:
    st, sa = _clean_title(t.title).lower(), t.author.lower()
    ot, oa = orig_title.lower(), orig_artist.lower()
    score = 0.0
    for word in ot.split():
        if word in st and len(word) > 1:
            score += 1.5
    for word in oa.split():
        if word in sa and len(word) > 1:
            score += 1.5
    if ot in st or st in ot:
        score += 3
    if oa in sa or sa in oa:
        score += 3
    if t.length and t.length > 30000:
        score += 1
    if orig_dur and t.length:
        diff = abs(t.length - orig_dur) / 1000
        if diff < 5:
            score += 5
        elif diff < 15:
            score += 3
        elif diff < 30:
            score += 1
    return score


MIN_MATCH_SCORE = 6.0


def _best_candidate(candidates: list, title: str, artist: str, duration: int):
    if not candidates:
        return None, 0.0
    best = max(candidates, key=lambda t: _score_track(t, title, artist, duration))
    return best, _score_track(best, title, artist, duration)


async def _search_best(query: str, title: str, artist: str, duration: int, node) -> wavelink.Playable | None:
    try:
        result = await _search_with_timeout(query, node)
        candidates = result if isinstance(result, list) else (
            result.tracks if isinstance(result, wavelink.Playlist) else [result] if result else []
        )
        best, best_score = _best_candidate(candidates, title, artist, duration)
        source = "YouTube"

        if best is None or best_score < MIN_MATCH_SCORE:
            try:
                sc_result = await _search_with_timeout(
                    query, node, source=wavelink.TrackSource.SoundCloud
                )
                sc_candidates = sc_result if isinstance(sc_result, list) else (
                    sc_result.tracks if isinstance(sc_result, wavelink.Playlist) else [sc_result] if sc_result else []
                )
                sc_best, sc_score = _best_candidate(sc_candidates, title, artist, duration)
                if sc_best is not None and sc_score > best_score:
                    best, best_score, source = sc_best, sc_score, "SoundCloud"
            except asyncio.TimeoutError:
                logger.warning("SoundCloud fallback timeout for query: %s", query)
            except Exception:
                logger.exception("SoundCloud fallback error for query: %s", query)

        if best is None:
            return None

        logger.info(
            "Spotify match [%s]: '%s - %s' (%dms) -> '%s - %s' (%dms) score=%.1f",
            source, artist, title, duration,
            best.author, best.title, best.length, best_score,
        )
        if best_score < MIN_MATCH_SCORE:
            logger.info("Rejected (score %.1f < %.1f)", best_score, MIN_MATCH_SCORE)
            return None
        return best
    except asyncio.TimeoutError:
        logger.warning("Search timeout for query: %s", query)
        return None
    except Exception:
        logger.exception("Search error for query: %s", query)
        return None


def _format_duration(milliseconds: int) -> str:
    total_sec = int(milliseconds) // 1000
    minutes, secs = divmod(total_sec, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ── Player ──────────────────────────────────────────────────────────

class MusicPlayer(wavelink.Player):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = wavelink.Queue()
        self.loop_mode: str | None = None
        self.now_playing_message: discord.Message | None = None
        self.text_channel: discord.TextChannel | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._current_view: "NowPlayingLayout | None" = None
        self._np_refresh_task: asyncio.Task | None = None
        self._crossfade_task: asyncio.Task | None = None
        self.theme_color: int = COLOR_NOW_PLAYING
        self.crossfade_duration: int = 0
        self._base_volume: int = 100


# ── Interactive Views ───────────────────────────────────────────────

TITLE_ID, PROGRESS_ID, LOOP_ID, META_ID, FOOTER_ID = 101, 102, 103, 104, 105


def _progress_bar(current_ms: int, total_ms: int, length: int = 14) -> str:
    if total_ms <= 0:
        return "`🔴 LIVE`"
    ratio = min(current_ms / total_ms, 1.0)
    filled = min(round(ratio * length), length - 1)
    bar = "▬" * filled + "🔘" + "▬" * (length - filled - 1)
    return f"{bar}\n`[{_format_duration(current_ms)} / {_format_duration(total_ms)}]`"


class NowPlayingLayout(discord.ui.LayoutView):
    def __init__(self, player: "MusicPlayer"):
        super().__init__(timeout=300)
        self.player = player

        self.pause_resume = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="⏸️", label="Pause", row=0)
        self.pause_resume.callback = self._on_pause_resume
        self.skip_button = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⏭️", label="Skip", row=0)
        self.skip_button.callback = self._on_skip
        self.stop_button = discord.ui.Button(style=discord.ButtonStyle.danger, emoji="⏹️", label="Stop", row=0)
        self.stop_button.callback = self._on_stop
        self.vol_down = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🔉", label="Vol-", row=0)
        self.vol_down.callback = self._on_vol_down
        self.vol_up = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🔊", label="Vol+", row=0)
        self.vol_up.callback = self._on_vol_up

        self.loop_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🔁", label="Loop Off", row=1)
        self.loop_button.callback = self._on_loop
        self.queue_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="📋", label="Queue", row=1)
        self.queue_button.callback = self._on_queue
        self.shuffle_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🔀", label="Shuffle", row=1)
        self.shuffle_button.callback = self._on_shuffle
        self.clear_button = discord.ui.Button(style=discord.ButtonStyle.danger, emoji="🗑️", label="Clear", row=1)
        self.clear_button.callback = self._on_clear
        self.favorite_button = discord.ui.Button(style=discord.ButtonStyle.success, emoji="❤️", label="Favorite", row=1)
        self.favorite_button.callback = self._on_favorite

        self.restart_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🔄", label="Restart", row=2)
        self.restart_button.callback = self._on_restart
        self.theme_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🎨", label="Theme", row=2)
        self.theme_button.callback = self._on_theme
        self.clean_button = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="🧹", label="Clean", row=2)
        self.clean_button.callback = self._on_clean

        row0 = discord.ui.ActionRow(self.pause_resume, self.skip_button, self.stop_button, self.vol_down, self.vol_up)
        row1 = discord.ui.ActionRow(self.loop_button, self.queue_button, self.shuffle_button, self.clear_button, self.favorite_button)
        row2 = discord.ui.ActionRow(self.restart_button, self.theme_button, self.clean_button)

        track = player.current
        thumb = discord.ui.Thumbnail(track.artwork) if track.artwork else discord.ui.Thumbnail("https://i.imgur.com/AfFp7pu.png")

        self.title_display = discord.ui.TextDisplay(f"### [{track.title}]({track.uri})", id=TITLE_ID)
        self.progress_display = discord.ui.TextDisplay(_progress_bar(player.position, track.length), id=PROGRESS_ID)
        self.loop_display = discord.ui.TextDisplay(self._loop_line(), id=LOOP_ID)
        self.meta_display = discord.ui.TextDisplay(self._meta_line(), id=META_ID)
        self.footer_display = discord.ui.TextDisplay(self._footer_line(), id=FOOTER_ID)

        section = discord.ui.Section(self.title_display, self.progress_display, self.loop_display, accessory=thumb)
        self.container = discord.ui.Container(
            section, discord.ui.Separator(), self.meta_display, discord.ui.Separator(),
            self.footer_display, discord.ui.Separator(), row0, row1, row2,
            accent_colour=self._paused_color() if player.paused else player.theme_color,
        )
        self.add_item(self.container)
        self._update_disable_states()

    def _loop_line(self) -> str:
        return {"track": "🔂 Looping track", "queue": "🔁 Looping queue"}.get(self.player.loop_mode, "🔁 Loop off")

    def _meta_line(self) -> str:
        track = self.player.current
        duration = _format_duration(track.length) if track.length else "LIVE"
        return f"🎤 **{track.author}**  ·  ⏱️ `{duration}`  ·  🔊 `{self.player.volume}%`"

    def _footer_line(self) -> str:
        return f"-# Requested by {self._requester_name()}  ·  Queue: {len(self.player.queue)} track{'s' if len(self.player.queue) != 1 else ''}"

    def _requester_name(self) -> str:
        track = self.player.current
        if not track:
            return "quelqu'un"
        requester = getattr(track, "requester", None)
        if requester and self.player.guild:
            member = self.player.guild.get_member(requester)
            if member:
                return member.display_name
        return "quelqu'un"

    def _paused_color(self) -> int:
        c = self.player.theme_color
        return ((c >> 1) & 0x7F7F7F) | 0x404040

    def _update_disable_states(self):
        has_current = self.player.current is not None
        has_queue = not (not self.player.queue or self.player.queue.is_empty)
        self.pause_resume.disabled = not has_current
        self.skip_button.disabled = not has_current
        self.stop_button.disabled = not has_current
        self.shuffle_button.disabled = not has_queue
        self.clear_button.disabled = not has_queue
        self.favorite_button.disabled = not has_current
        self.restart_button.disabled = not has_current
        self.clean_button.disabled = not has_queue

    def refresh(self):
        self.progress_display.content = _progress_bar(self.player.position, self.player.current.length)
        self.loop_display.content = self._loop_line()
        self.meta_display.content = self._meta_line()
        self.footer_display.content = self._footer_line()
        self.container.accent_colour = self._paused_color() if self.player.paused else self.player.theme_color

    async def _check_voice(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or interaction.user.voice.channel != self.player.channel:
            await interaction.response.send_message("You must be in the same voice channel.", ephemeral=True)
            return False
        return True

    async def _on_pause_resume(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if self.player.paused:
            await self.player.pause(False)
            self.refresh()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("▶️ Resumed", ephemeral=True)
        elif self.player.playing:
            await self.player.pause(True)
            self.refresh()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("⏸️ Paused", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    async def _on_skip(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if not self.player.current:
            return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        await self.player.skip()
        await interaction.response.send_message("⏭️ Skipped", ephemeral=True)

    async def _on_stop(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if not self.player.current:
            return await interaction.response.send_message("Nothing to stop.", ephemeral=True)
        if self.player._crossfade_task and not self.player._crossfade_task.done():
            self.player._crossfade_task.cancel()
        if self.player._np_refresh_task and not self.player._np_refresh_task.done():
            self.player._np_refresh_task.cancel()
        self.player.queue.clear()
        await self.player.stop()
        await interaction.response.send_message("⏹️ Stopped", ephemeral=True)

    async def _on_vol_down(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        new_vol = max(10, self.player.volume - 10)
        self.player._base_volume = new_vol
        await self.player.set_volume(new_vol)
        self.refresh()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🔉 Volume {new_vol}%", ephemeral=True)

    async def _on_vol_up(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        new_vol = min(100, self.player.volume + 10)
        self.player._base_volume = new_vol
        await self.player.set_volume(new_vol)
        self.refresh()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🔊 Volume {new_vol}%", ephemeral=True)

    async def _on_loop(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        order = [None, "track", "queue"]
        idx = (order.index(self.player.loop_mode) + 1) % len(order)
        self.player.loop_mode = order[idx]
        self.refresh()
        labels = {None: "🔁 Loop off", "track": "🔂 Looping track", "queue": "🔁 Looping queue"}
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(labels[self.player.loop_mode], ephemeral=True)

    async def _on_queue(self, interaction: discord.Interaction):
        embed = create_queue_embed(self.player, color=self.player.theme_color)
        if not embed:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_shuffle(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if not self.player.queue or self.player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        self.player.queue.shuffle()
        await interaction.response.send_message("🔀 Queue shuffled", ephemeral=True)

    async def _on_clear(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if not self.player.queue or self.player.queue.is_empty:
            return await interaction.response.send_message("Queue is already empty.", ephemeral=True)
        self.player.queue.clear()
        self.refresh()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("🗑️ Queue cleared", ephemeral=True)

    async def _on_favorite(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        track = self.player.current
        if not track:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        track_data = [{"title": track.title, "uri": track.uri, "author": track.author, "duration": track.length}]
        try:
            await append_to_playlist(interaction.user.id, "favorites", track_data)
            await interaction.response.send_message(f"❤️ Saved **{track.title}** to favorites", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    async def _on_restart(self, interaction: discord.Interaction):
        if not await self._check_voice(interaction):
            return
        if not self.player.current or not self.player.current.length:
            return await interaction.response.send_message("Nothing to restart.", ephemeral=True)
        await self.player.seek(0)
        await interaction.response.send_message("🔄 Restarted", ephemeral=True)

    async def _on_theme(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("MusicCog")
        if cog and not await cog._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        presets = ["#6C5CE7", "#FF5733", "#33FF57", "#3357FF", "#FF33F5", "#FFD733"]
        current_hex = "#{:06x}".format(self.player.theme_color)
        idx = presets.index(current_hex) + 1 if current_hex in presets else 0
        if idx >= len(presets):
            idx = 0
        new_hex = presets[idx]
        await set_guild_theme(interaction.guild_id, new_hex)
        self.player.theme_color = int(new_hex.lstrip("#"), 16)
        self.refresh()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🎨 Theme → {new_hex}", ephemeral=True)

    async def _on_clean(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("MusicCog")
        if cog and not await cog._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        if not self.player.queue or self.player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        await interaction.response.defer()

        all_tracks = list(self.player.queue)
        self.player.queue.clear()
        sem = asyncio.Semaphore(5)
        removed = 0

        async def _check(t: wavelink.Playable) -> wavelink.Playable | None:
            nonlocal removed
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        wavelink.Playable.search(t.uri, node=self.player.node),
                        timeout=5.0,
                    )
                    if result:
                        return t
                except Exception:
                    pass
                removed += 1
                return None

        results = await asyncio.gather(*[_check(t) for t in all_tracks], return_exceptions=True)
        for r in results:
            if isinstance(r, wavelink.Playable):
                self.player.queue.put(r)

        if removed and self.player.guild:
            cog = interaction.client.get_cog("MusicCog")
            if cog:
                await cog._persist_queue(self.player, self.player.guild.id)

        msg = f"🧹 Removed **{removed}** invalid track{'s' if removed != 1 else ''}" if removed else "✅ All tracks are valid"
        await interaction.followup.send(msg, ephemeral=True)


class SearchSelectView(discord.ui.View):
    def __init__(self, tracks: list[wavelink.Playable], player: MusicPlayer, user_id: int):
        super().__init__(timeout=30)
        self.tracks = tracks
        self.player = player
        self.user_id = user_id

        options = [
            discord.SelectOption(
                label=f"{i+1}. {t.title[:80]}",
                description=re.sub(r"\s+", " ", (t.author or ""))[:100] if t.author else None,
                value=str(i),
            )
            for i, t in enumerate(tracks)
        ]
        self.select = discord.ui.Select(placeholder="Choose a track...", options=options)
        self.select.callback = self._select_callback
        self.add_item(self.select)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your search.", ephemeral=True)
            return

        idx = int(self.select.values[0])
        track = self.tracks[idx]
        track.requester = interaction.user.id

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"▶️ Playing: **{track.title}**",
            embed=None,
            view=self,
        )
        self.stop()

        if self.player.current:
            self.player.queue.put(track)
        else:
            await self.player.play(track)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        self.stop()


class QueuePageView(discord.ui.View):
    def __init__(self, player: MusicPlayer, total_pages: int):
        super().__init__(timeout=60)
        self.player = player
        self.total_pages = total_pages
        self.current_page = 1

        options = [
            discord.SelectOption(label=f"Page {i}/{total_pages}", value=str(i))
            for i in range(1, min(total_pages, 25) + 1)
        ]
        self.page_select = discord.ui.Select(placeholder="Go to page...", options=options[:25])
        self.page_select.callback = self._page_callback
        self.add_item(self.page_select)

    async def _page_callback(self, interaction: discord.Interaction):
        page = int(self.page_select.values[0])
        self.current_page = page
        embed = create_queue_embed(self.player, page=page, color=self.player.theme_color)
        await interaction.response.edit_message(embed=embed, view=self)


# ── Cog ─────────────────────────────────────────────────────────────

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._skip_votes: dict[int, set[int]] = {}
        self._reconnect_task: asyncio.Task | None = None
        self._cache_cleanup_task: asyncio.Task | None = None
        self._node: wavelink.Node | None = None

    async def cog_load(self):
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())

    async def cog_unload(self):
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._cache_cleanup_task and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()

    async def _reconnect_loop(self):
        while True:
            try:
                await asyncio.sleep(30)
                pool = wavelink.Pool
                if not pool.nodes:
                    continue
                for node in pool.nodes.values():
                    if node.status != wavelink.NodeStatus.CONNECTED:
                        logger.warning("Node %s disconnected, reconnecting...", node.uri)
                        try:
                            await asyncio.wait_for(node.connect(), timeout=10.0)
                            logger.info("Node %s reconnected", node.uri)
                        except Exception as e:
                            logger.error("Failed to reconnect node %s: %s", node.uri, e)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Reconnect loop error")

    async def _cache_cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(3600)
                await spotify_cache_cleanup()
                logger.info("Spotify cache cleaned")
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _persist_queue(self, player: MusicPlayer, guild_id: int):
        if not player.queue or player.queue.is_empty:
            await delete_queue_state(guild_id)
            return
        tracks = [
            {"title": t.title, "uri": t.uri, "author": t.author, "duration": t.length, "requester": getattr(t, "requester", None)}
            for t in list(player.queue)
        ]
        await save_queue_state(guild_id, tracks, 0, player.loop_mode)

    async def _restore_queue(self, player: MusicPlayer, guild_id: int):
        state = await load_queue_state(guild_id)
        if not state or not state["tracks"]:
            return
        await delete_queue_state(guild_id)
        tracks_data = state["tracks"]
        player.loop_mode = state.get("loop_mode")

        async def _resolve(t: dict) -> wavelink.Playable | None:
            try:
                result = await _search_with_timeout(t["uri"], player.node)
                if isinstance(result, wavelink.Playlist):
                    loaded = result.tracks
                elif isinstance(result, list):
                    loaded = result
                elif result:
                    loaded = [result]
                else:
                    return None
                if loaded:
                    track = loaded[0]
                    if "requester" in t:
                        track.requester = t["requester"]
                    return track
            except Exception:
                pass
            return None

        resolved = await asyncio.gather(*[_resolve(t) for t in tracks_data], return_exceptions=True)
        restored = 0
        for r in resolved:
            if isinstance(r, wavelink.Playable):
                player.queue.put(r)
                restored += 1

        if restored and player.text_channel:
            try:
                await player.text_channel.send(f"🔄 Restored **{restored} tracks** from saved queue.")
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        logger.error("Command error [%s]: %s", interaction.command.name if interaction.command else "?", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"Error: {error}", ephemeral=True)

    # ── Permissions ───────────────────────────────────────────────

    async def _check_dj(self, interaction: discord.Interaction) -> bool:
        if DJ_ROLE_ID is None:
            player = interaction.guild.voice_client
            if player and player.current:
                if not interaction.user.voice or not player.channel or interaction.user.voice.channel != player.channel:
                    return False
            return True
        if interaction.user.voice:
            player = interaction.guild.voice_client
            if player and player.channel and interaction.user.voice.channel == player.channel:
                listeners = [m for m in player.channel.members if not m.bot and not (m.voice and (m.voice.self_deaf or m.voice.deaf))]
                if len(listeners) <= 1:
                    return True
        role = interaction.guild.get_role(DJ_ROLE_ID)
        if role is None:
            return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
        return role in interaction.user.roles

    async def _require_player(self, interaction: discord.Interaction) -> MusicPlayer | None:
        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return None
        return player

    async def _require_same_channel(self, interaction: discord.Interaction) -> MusicPlayer | None:
        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return None
        if not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return None
        if not interaction.user.voice or interaction.user.voice.channel != player.channel:
            await interaction.response.send_message("You must be in the same voice channel.", ephemeral=True)
            return None
        return player

    async def _ensure_voice(self, interaction: discord.Interaction) -> MusicPlayer | None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You must be in a voice channel.", ephemeral=True)
            return None

        channel = interaction.user.voice.channel
        player: MusicPlayer | None = interaction.guild.voice_client

        if player:
            if player.channel != channel:
                await player.move_to(channel)
            music_channel_id = await get_music_channel(interaction.guild_id)
            if music_channel_id:
                mc = interaction.guild.get_channel(music_channel_id)
                player.text_channel = mc if isinstance(mc, discord.TextChannel) else interaction.channel
            else:
                player.text_channel = interaction.channel
            theme_hex = await get_guild_theme(interaction.guild_id)
            player.theme_color = int(theme_hex.lstrip("#"), 16)
            return player

        player = await channel.connect(cls=MusicPlayer)
        music_channel_id = await get_music_channel(interaction.guild_id)
        if music_channel_id:
            mc = interaction.guild.get_channel(music_channel_id)
            player.text_channel = mc if isinstance(mc, discord.TextChannel) else interaction.channel
        else:
            player.text_channel = interaction.channel

        theme_hex = await get_guild_theme(interaction.guild_id)
        player.theme_color = int(theme_hex.lstrip("#"), 16)
        asyncio.create_task(self._restore_queue(player, interaction.guild_id))
        return player

    # ── Disconnect timer ──────────────────────────────────────────

    async def _start_disconnect_timer(self, player: MusicPlayer):
        if player._disconnect_task and not player._disconnect_task.done():
            player._disconnect_task.cancel()

        async def _timer():
            try:
                await asyncio.sleep(300)
                self._stop_np_refresh(player)
                self._stop_crossfade(player)
                if player.guild:
                    await delete_queue_state(player.guild.id)
                player.queue.clear()
                if player.now_playing_message:
                    try:
                        await player.now_playing_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    player.now_playing_message = None
                await player.disconnect()
            except asyncio.CancelledError:
                pass

        player._disconnect_task = asyncio.create_task(_timer())

    def _cancel_disconnect_timer(self, player: MusicPlayer):
        if player._disconnect_task and not player._disconnect_task.done():
            player._disconnect_task.cancel()
            player._disconnect_task = None

    # ── Now-playing auto-refresh ────────────────────────────────

    def _start_np_refresh(self, player: MusicPlayer):
        self._stop_np_refresh(player)
        player._np_refresh_task = asyncio.create_task(self._np_refresh_loop(player))

    def _stop_np_refresh(self, player: MusicPlayer):
        if player._np_refresh_task and not player._np_refresh_task.done():
            player._np_refresh_task.cancel()
            player._np_refresh_task = None

    async def _np_refresh_loop(self, player: MusicPlayer):
        try:
            while True:
                await asyncio.sleep(15)
                if not player.current or not player.now_playing_message:
                    break
                if not isinstance(player._current_view, NowPlayingLayout):
                    break
                player._current_view.refresh()
                try:
                    await player.now_playing_message.edit(view=player._current_view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    break
        except asyncio.CancelledError:
            pass

    # ── Crossfade ─────────────────────────────────────────────────

    def _start_crossfade(self, player: MusicPlayer):
        self._stop_crossfade(player)
        if player.crossfade_duration > 0:
            player._crossfade_task = asyncio.create_task(self._crossfade_loop(player))

    def _stop_crossfade(self, player: MusicPlayer):
        if player._crossfade_task and not player._crossfade_task.done():
            player._crossfade_task.cancel()
            player._crossfade_task = None

    async def _crossfade_loop(self, player: MusicPlayer):
        try:
            while True:
                await asyncio.sleep(1)
                if not player.current:
                    break
                if not player.current.length:
                    continue
                remaining = (player.current.length - player.position) / 1000
                cf = player.crossfade_duration
                if remaining <= cf and not player.paused:
                    ratio = max(remaining / cf, 0.0)
                    fade_vol = max(int(player._base_volume * ratio), 0)
                    await player.set_volume(fade_vol)
                elif player.volume != player._base_volume and not (remaining <= cf):
                    await player.set_volume(player._base_volume)
        except asyncio.CancelledError:
            pass

    # ── Event listeners ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        self._node = payload.node
        logger.info("Lavalink node ready: %s (session %s)", payload.node.uri, payload.session_id)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

        if not player.text_channel:
            return

        if player.crossfade_duration > 0:
            await player.set_volume(player._base_volume)

        view = NowPlayingLayout(player)
        player._current_view = view

        if player.now_playing_message:
            try:
                await player.now_playing_message.edit(view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            player.now_playing_message = await player.text_channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException):
            pass

        self._start_np_refresh(player)
        self._start_crossfade(player)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

        self._stop_np_refresh(player)
        self._stop_crossfade(player)
        self._skip_votes.pop(player.guild.id, None)

        reason = str(payload.reason)
        if reason == "replaced":
            return

        track = payload.track

        if player.loop_mode == "track":
            await player.play(track)
        elif player.loop_mode == "queue":
            player.queue.put(track)
            if not player.queue.is_empty:
                next_track = player.queue.get()
                await player.play(next_track)
            if player.guild:
                await self._persist_queue(player, player.guild.id)
        elif not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
            if player.guild:
                await self._persist_queue(player, player.guild.id)
        else:
            if player.now_playing_message:
                try:
                    await player.now_playing_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                player.now_playing_message = None
            if player.guild:
                await delete_queue_state(player.guild.id)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

        self._stop_np_refresh(player)
        self._stop_crossfade(player)

        if player.text_channel:
            try:
                err_msg = str(payload.exception) if isinstance(payload.exception, str) else payload.exception.get("message", str(payload.exception))
                await player.text_channel.send(f"Error: {err_msg}")
            except (discord.Forbidden, discord.HTTPException):
                pass

        if not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        player: MusicPlayer | None = member.guild.voice_client
        if not player or not player.channel:
            return

        non_bots = [m for m in player.channel.members if not m.bot]
        if not non_bots:
            await self._start_disconnect_timer(player)
        else:
            self._cancel_disconnect_timer(player)

    # ── Play command ──────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name or URL (YouTube, SoundCloud, Spotify)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        player = await self._ensure_voice(interaction)
        if not player:
            return

        # --- Spotify collection (playlist/album) ---
        if m := SPOTIFY_COLLECTION_RE.match(query):
            tracks = await _spotify_collection_tracks(query)
            if not tracks:
                return await interaction.followup.send(
                    "Could not load that Spotify playlist/album.",
                    ephemeral=True,
                )
            await interaction.followup.send(
                f"📂 Loading **{len(tracks)} tracks** from *{m.group(1)}*..."
            )

            sem = asyncio.Semaphore(5)

            async def _resolve(item: dict) -> wavelink.Playable | None:
                async with sem:
                    title = item["title"]
                    artist = item["artist"]
                    duration = item["duration_ms"]
                    search_query = f"{artist} - {title}"
                    best = await _search_best(search_query, title, artist, duration, player.node)
                    if best:
                        best.requester = interaction.user.id
                    return best

            results = await asyncio.gather(*[_resolve(t) for t in tracks])
            added = 0
            for t in results:
                if t:
                    player.queue.put(t)
                    added += 1
            if added == 0:
                return await interaction.followup.send(
                    "Could not find any tracks on YouTube.", ephemeral=True
                )
            if not player.current:
                first = player.queue.get()
                await player.play(first)
                await interaction.followup.send(
                    f"▶️ Added **{added}** tracks — now playing"
                )
            else:
                await interaction.followup.send(
                    f"✅ Added **{added}** tracks to the queue."
                )
            await self._persist_queue(player, interaction.guild_id)
            return

        # --- Spotify single track ---
        if SPOTIFY_TRACK_RE.match(query):
            converted = await _spotify_url_to_query(query)
            if not converted:
                return await interaction.followup.send(
                    "Could not resolve that Spotify link. Try searching by name instead.",
                    ephemeral=True,
                )
            parts = converted.split(" - ", 1)
            search_title = parts[-1].strip() if len(parts) > 1 else converted
            search_artist = parts[0].strip() if len(parts) > 1 else ""
            track = await _search_best(converted, search_title, search_artist, 0, player.node)
            if not track:
                return await interaction.followup.send("No results found.", ephemeral=True)
            track.requester = interaction.user.id
            if player.current:
                player.queue.put(track)
                total_sec = track.length // 1000 if track.length else 0
                duration = f" ({total_sec // 60}:{total_sec % 60:02d})" if total_sec else ""
                await interaction.followup.send(
                    f"Added to queue: **[{track.title}]({track.uri})**{duration}"
                )
            else:
                await player.play(track)
                await interaction.followup.send(
                    f"▶️ Now playing: **[{track.title}]({track.uri})**"
                )
            await self._persist_queue(player, interaction.guild_id)
            return

        # --- Direct URL auto-play (YouTube, SoundCloud) ---
        if query.startswith(("http://", "https://")):
            result = await _search_with_timeout(query, node=player.node)
            if not result:
                return await interaction.followup.send("No results found.", ephemeral=True)

            if isinstance(result, wavelink.Playlist):
                for track in result:
                    track.requester = interaction.user.id
                    player.queue.put(track)
                if not player.current:
                    first = player.queue.get()
                    await player.play(first)
                    await interaction.followup.send(
                        f"▶️ Loaded playlist **{result.name}** ({len(result)}) — now playing"
                    )
                else:
                    await interaction.followup.send(
                        f"Added **{len(result)} tracks** from playlist to queue."
                    )
                await self._persist_queue(player, interaction.guild_id)
                return

            track = result[0] if isinstance(result, list) else result
            track.requester = interaction.user.id
            if player.current:
                player.queue.put(track)
                total_sec = track.length // 1000 if track.length else 0
                duration = f" ({total_sec // 60}:{total_sec % 60:02d})" if total_sec else ""
                await interaction.followup.send(
                    f"Added to queue: **[{track.title}]({track.uri})**{duration}"
                )
            else:
                await player.play(track)
                await interaction.followup.send(
                    f"▶️ Now playing: **[{track.title}]({track.uri})**"
                )
            await self._persist_queue(player, interaction.guild_id)
            return

        # --- Text query: show search select ---
        result = await _search_with_timeout(query, node=player.node)
        if not result:
            return await interaction.followup.send("No results found.", ephemeral=True)

        tracks = list(result) if isinstance(result, list) else [result]
        if not tracks:
            return await interaction.followup.send("No results found.", ephemeral=True)

        # Single result: auto-play
        if len(tracks) == 1:
            track = tracks[0]
            track.requester = interaction.user.id
            if player.current:
                player.queue.put(track)
                total_sec = track.length // 1000 if track.length else 0
                duration = f" ({total_sec // 60}:{total_sec % 60:02d})" if total_sec else ""
                await interaction.followup.send(
                    f"Added to queue: **[{track.title}]({track.uri})**{duration}"
                )
            else:
                await player.play(track)
                await interaction.followup.send(
                    f"▶️ Now playing: **[{track.title}]({track.uri})**"
                )
            await self._persist_queue(player, interaction.guild_id)
            return

        view = SearchSelectView(tracks[:5], player, interaction.user.id)
        embed = build_embed(
            title="Search Results",
            description="Select a track to play:",
        )
        await interaction.followup.send(embed=embed, view=view)

    # ── Transport commands ────────────────────────────────────────

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client
        if player.paused:
            return await interaction.response.send_message("Already paused.", ephemeral=True)
        await player.pause(True)
        await interaction.response.send_message("Paused ⏸️")

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client
        if not player.paused:
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        await player.pause(False)
        await interaction.response.send_message("Resumed ▶️")

    @app_commands.command(name="skip", description="Skip to the next track")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client

        # DJ bypass
        if await self._check_dj(interaction):
            self._skip_votes.pop(interaction.guild_id, None)
            await player.skip()
            await interaction.response.send_message("Skipped ⏭️")
            return

        # Vote skip
        guild_votes = self._skip_votes.setdefault(interaction.guild_id, set())
        if interaction.user.id in guild_votes:
            return await interaction.response.send_message("You already voted to skip.", ephemeral=True)

        guild_votes.add(interaction.user.id)
        listeners = [m for m in player.channel.members if not m.bot and not (m.voice and (m.voice.self_deaf or m.voice.deaf))]
        required = math.ceil(len(listeners) / 2)

        if len(guild_votes) >= required:
            self._skip_votes.pop(interaction.guild_id, None)
            await player.skip()
            await interaction.response.send_message("⏭️ Vote skip passed!")
        else:
            await interaction.response.send_message(
                f"🗳️ Vote skip: {len(guild_votes)}/{required} votes"
            )

    @app_commands.command(name="skipto", description="Skip to a specific position in the queue")
    @app_commands.describe(position="Track number to skip to")
    @app_commands.guild_only()
    async def skipto(self, interaction: discord.Interaction, position: int):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        if position < 1 or position > len(player.queue):
            return await interaction.response.send_message(f"Position must be 1-{len(player.queue)}.", ephemeral=True)

        all_tracks = list(player.queue)
        player.queue.clear()
        for t in all_tracks[position - 1:]:
            player.queue.put(t)
        await player.skip()
        await interaction.response.send_message(f"⏭️ Skipped to position {position}")

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(time="Position (e.g. 1:30 or 90 for seconds)")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, time: str):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client
        if not player.current.length:
            return await interaction.response.send_message("Cannot seek on a live stream.", ephemeral=True)

        seconds = 0
        parts = time.split(":")
        try:
            if len(parts) == 2:
                seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                seconds = int(time)
        except ValueError:
            return await interaction.response.send_message("Invalid time format. Use seconds or mm:ss.", ephemeral=True)

        max_pos = player.current.length // 1000
        seconds = max(0, min(seconds, max_pos))
        await player.seek(seconds * 1000)
        await interaction.response.send_message(f"⏩ Seeking to {_format_duration(seconds * 1000)}")

    @app_commands.command(name="stop", description="Stop playback and clear queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        self._stop_crossfade(player)
        self._stop_np_refresh(player)
        player.queue.clear()
        await player.stop()
        if player.now_playing_message:
            try:
                await player.now_playing_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            player.now_playing_message = None
        await delete_queue_state(interaction.guild_id)
        await interaction.response.send_message("Stopped ⏹️")

    @app_commands.command(name="volume", description="Set volume (0-100)")
    @app_commands.describe(volume="Volume level (0-100)")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, volume: int):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        if volume < 0 or volume > 100:
            return await interaction.response.send_message("Must be 0-100.", ephemeral=True)
        player = interaction.guild.voice_client
        player._base_volume = volume
        await player.set_volume(volume)
        await interaction.response.send_message(f"Volume → {volume}%")

    @app_commands.command(name="crossfade", description="Set crossfade duration between tracks")
    @app_commands.describe(seconds="Crossfade duration in seconds (0 to disable, max 10)")
    @app_commands.guild_only()
    async def crossfade(self, interaction: discord.Interaction, seconds: int):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        if seconds < 0 or seconds > 10:
            return await interaction.response.send_message("Must be 0-10 seconds.", ephemeral=True)
        player = interaction.guild.voice_client
        player.crossfade_duration = seconds
        if seconds > 0:
            player._base_volume = player.volume
            self._start_crossfade(player)
            await interaction.response.send_message(f"🔀 Crossfade → {seconds}s")
        else:
            self._stop_crossfade(player)
            await player.set_volume(player._base_volume)
            await interaction.response.send_message("🔀 Crossfade disabled")

    @app_commands.command(name="nowplaying", description="Show current track")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client
        view = NowPlayingLayout(player)
        await interaction.response.send_message(view=view)

    @app_commands.command(name="restart", description="Restart the current track from the beginning")
    @app_commands.guild_only()
    async def restart(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client
        if not player.current.length:
            return await interaction.response.send_message("Cannot restart a live stream.", ephemeral=True)
        await player.seek(0)
        await interaction.response.send_message("🔄 Restarted from the beginning")

    @app_commands.command(name="queue", description="Show the queue")
    @app_commands.guild_only()
    async def queue_cmd(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)

        total_pages = max(1, math.ceil(len(player.queue) / 10))
        embed = create_queue_embed(player, page=1, color=player.theme_color)
        view = QueuePageView(player, total_pages) if total_pages > 1 else None
        await interaction.response.send_message(embed=embed, view=view)

    # ── Queue management ──────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        player.queue.shuffle()
        await interaction.response.send_message(f"🔀 Queue shuffled ({len(player.queue)} tracks)")

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Track number in the queue")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: int):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        if position < 1 or position > len(player.queue):
            return await interaction.response.send_message(f"Position must be 1-{len(player.queue)}.", ephemeral=True)

        track = player.queue.get_at(position - 1)
        player.queue.remove(track)
        await interaction.response.send_message(f"Removed: **[{track.title}]({track.uri})**")

    @app_commands.command(name="dedup", description="Remove duplicate tracks from the queue")
    @app_commands.guild_only()
    async def dedup(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)

        seen = set()
        kept = []
        removed = 0
        for track in list(player.queue):
            if track.uri in seen:
                removed += 1
                continue
            seen.add(track.uri)
            kept.append(track)

        player.queue.clear()
        for t in kept:
            player.queue.put(t)

        if removed:
            await self._persist_queue(player, interaction.guild_id)

        await interaction.response.send_message(f"🧹 Removed **{removed}** duplicate{'s' if removed != 1 else ''} ({len(kept)} unique tracks remaining)")

    @app_commands.command(name="loop", description="Toggle loop mode: off -> track -> queue")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction):
        if not await self._require_same_channel(interaction):
            return
        player = interaction.guild.voice_client

        order = [None, "track", "queue"]
        idx = (order.index(player.loop_mode) + 1) % len(order)
        player.loop_mode = order[idx]

        labels = {None: "🔁 Loop off", "track": "🔂 Looping track", "queue": "🔁 Looping queue"}
        await interaction.response.send_message(labels[player.loop_mode])

        if isinstance(player._current_view, NowPlayingLayout):
            player._current_view.refresh()
            if player.now_playing_message:
                try:
                    await player.now_playing_message.edit(view=player._current_view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    @app_commands.command(name="queueclean", description="Remove invalid tracks from the queue")
    @app_commands.guild_only()
    async def queueclean(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        await interaction.response.defer()

        all_tracks = list(player.queue)
        player.queue.clear()
        sem = asyncio.Semaphore(5)
        removed = 0

        async def _check(t: wavelink.Playable) -> wavelink.Playable | None:
            nonlocal removed
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        wavelink.Playable.search(t.uri, node=player.node),
                        timeout=5.0,
                    )
                    if result:
                        return t
                except Exception:
                    pass
                removed += 1
                return None

        results = await asyncio.gather(*[_check(t) for t in all_tracks], return_exceptions=True)
        for r in results:
            if isinstance(r, wavelink.Playable):
                player.queue.put(r)

        if removed:
            await self._persist_queue(player, interaction.guild_id)

        msg = f"🧹 Removed **{removed}** invalid track{'s' if removed != 1 else ''}" if removed else "✅ All tracks are valid"
        await interaction.followup.send(msg)

    # ── Music channel ─────────────────────────────────────────────

    @app_commands.command(name="setchannel", description="Set the dedicated music text channel")
    @app_commands.describe(channel="Text channel for now-playing messages (leave empty to reset)")
    @app_commands.guild_only()
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await set_music_channel(interaction.guild_id, channel.id if channel else None)
        if channel:
            await interaction.response.send_message(f"✅ Music channel set to {channel.mention}")
        else:
            await interaction.response.send_message("✅ Music channel reset (uses command channel)")

    @app_commands.command(name="theme", description="Change the accent color")
    @app_commands.describe(color="Hex color (e.g. #FF5733) or reset to default")
    @app_commands.guild_only()
    async def theme(self, interaction: discord.Interaction, color: str | None = None):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)

        if color is None or color.lower() in ("reset", "default"):
            color_hex = "#6C5CE7"
            await set_guild_theme(interaction.guild_id, color_hex)
            player: MusicPlayer | None = interaction.guild.voice_client
            if player:
                player.theme_color = int(color_hex.lstrip("#"), 16)
                if isinstance(player._current_view, NowPlayingLayout):
                    player._current_view.refresh()
                    if player.now_playing_message:
                        try:
                            await player.now_playing_message.edit(view=player._current_view)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
            return await interaction.response.send_message("✅ Theme reset to default")

        color_hex = color.strip()
        if not color_hex.startswith("#") or len(color_hex) != 7:
            return await interaction.response.send_message("Invalid format. Use `#RRGGBB` (e.g. `#FF5733`)", ephemeral=True)
        try:
            int(color_hex[1:], 16)
        except ValueError:
            return await interaction.response.send_message("Invalid hex color.", ephemeral=True)

        await set_guild_theme(interaction.guild_id, color_hex)
        player: MusicPlayer | None = interaction.guild.voice_client
        if player:
            player.theme_color = int(color_hex.lstrip("#"), 16)
            if isinstance(player._current_view, NowPlayingLayout):
                player._current_view.refresh()
                if player.now_playing_message:
                    try:
                        await player.now_playing_message.edit(view=player._current_view)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
        await interaction.response.send_message(f"✅ Theme changed to {color_hex}")

    # ── Audio filters ─────────────────────────────────────────────

    @app_commands.command(name="bassboost", description="Enable bass boost")
    @app_commands.guild_only()
    async def bassboost(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client

        filters = wavelink.Filters()
        filters.equalizer = wavelink.Equalizer(payload=[(0, 0.2), (1, 0.15), (2, 0.1), (3, 0.05)])
        await player.set_filters(filters)
        await interaction.response.send_message("✅ Bass boost enabled")

    @app_commands.command(name="nightcore", description="Enable nightcore effect")
    @app_commands.guild_only()
    async def nightcore(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client

        filters = wavelink.Filters()
        filters.timescale = wavelink.Timescale(payload={"pitch": 1.2, "speed": 1.2})
        await player.set_filters(filters)
        await interaction.response.send_message("✅ Nightcore enabled")

    @app_commands.command(name="reset", description="Reset all audio filters")
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction):
        if not await self._require_player(interaction):
            return
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player = interaction.guild.voice_client

        await player.set_filters(wavelink.Filters())
        await interaction.response.send_message("✅ Filters reset")

    # ── Playlists ─────────────────────────────────────────────────

    @app_commands.command(name="save", description="Save current queue as a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def save(self, interaction: discord.Interaction, name: str):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)

        track_data = []
        if player.current:
            track_data.append({
                "title": player.current.title, "uri": player.current.uri,
                "author": player.current.author, "duration": player.current.length,
                "requester": getattr(player.current, "requester", None),
            })
        track_data += [
            {"title": t.title, "uri": t.uri, "author": t.author, "duration": t.length, "requester": getattr(t, "requester", None)}
            for t in player.queue.copy()
        ]

        try:
            await save_playlist(interaction.user.id, name, track_data)
            await interaction.response.send_message(f"✅ Saved **{len(track_data)} tracks** as `{name}`")
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="playlist", description="Load a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def playlist(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        player = await self._ensure_voice(interaction)
        if not player:
            return

        tracks_data = await load_playlist(interaction.user.id, name)
        if not tracks_data:
            return await interaction.followup.send(f"No playlist `{name}` found.", ephemeral=True)

        for t in tracks_data:
            result = await wavelink.Playable.search(t["uri"], node=player.node)
            if isinstance(result, wavelink.Playlist):
                loaded = result.tracks
            elif isinstance(result, list):
                loaded = result
            else:
                continue

            if loaded:
                track = loaded[0]
                track.requester = interaction.user.id
                player.queue.put(track)

        if not player.current and not player.queue.is_empty:
            first = player.queue.get()
            await player.play(first)
            await interaction.followup.send(f"▶️ Loaded playlist `{name}` — now playing")
        elif not player.queue.is_empty:
            await interaction.followup.send(f"✅ Added tracks from `{name}` to queue")
        else:
            await interaction.followup.send("Could not load any tracks from the playlist.", ephemeral=True)

        await self._persist_queue(player, interaction.guild_id)

    @app_commands.command(name="pl_list", description="List your saved playlists")
    @app_commands.guild_only()
    async def pl_list(self, interaction: discord.Interaction):
        playlists_data = await list_playlists(interaction.user.id)
        if not playlists_data:
            return await interaction.response.send_message("No saved playlists.", ephemeral=True)

        lines = []
        for p in playlists_data:
            lines.append(f"**{p['name']}** — {p['track_count']} tracks ({p['created_at'][:10]})")

        embed = build_embed(
            type="info",
            title="Your playlists",
            description="\n".join(lines),
            footer_text=f"{len(playlists_data)} playlist(s)",
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pl_delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def pl_delete(self, interaction: discord.Interaction, name: str):
        deleted = await delete_playlist(interaction.user.id, name)
        if deleted:
            await interaction.response.send_message(f"Deleted playlist `{name}`")
        else:
            await interaction.response.send_message(f"No playlist `{name}` found.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
