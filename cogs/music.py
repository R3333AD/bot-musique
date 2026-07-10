import asyncio
import json
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
)
from utils.music_helpers import create_now_playing_embed, create_queue_embed, ACCENT

SPOTIFY_TRACK_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-\w+/)?track/([a-zA-Z0-9]+)"
)
SPOTIFY_COLLECTION_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-\w+/)?(playlist|album)/([a-zA-Z0-9]+)"
)

_EMBED_SCRIPT_RE = re.compile(r"<script[^>]*>({.*?})</script>", re.DOTALL)


async def _spotify_url_to_query(url: str) -> str | None:
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
                        return f"{author} - {title}" if author else title
    except Exception:
        pass
    return None


async def _spotify_collection_tracks(url: str) -> list[dict] | None:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            embed_url = re.sub(
                r"https?://open\.spotify\.com/(?:intl-\w+/)?",
                "https://open.spotify.com/embed/",
                url,
            )
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


def _score_track(t: wavelink.Playable, orig_title: str, orig_artist: str, orig_dur: int) -> float:
    st, sa = t.title.lower(), t.author.lower()
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


MIN_MATCH_SCORE = 6.0  # en dessous, on préfère prévenir plutôt que jouer n'importe quoi


async def _search_best(query: str, title: str, artist: str, duration: int, node) -> wavelink.Playable | None:
    try:
        result = await wavelink.Playable.search(query, node=node)
        candidates = result if isinstance(result, list) else (
            result.tracks if isinstance(result, wavelink.Playlist) else [result] if result else []
        )
        if not candidates:
            return None
        best = max(candidates, key=lambda t: _score_track(t, title, artist, duration))
        if _score_track(best, title, artist, duration) < MIN_MATCH_SCORE:
            return None  # aucun candidat assez fiable, mieux vaut prévenir que jouer le mauvais morceau
        return best
    except Exception:
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
        self._current_view: "NowPlayingView | None" = None


# ── Interactive Views ───────────────────────────────────────────────

class NowPlayingView(discord.ui.View):
    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=300)
        self.player = player
        self._update_loop_button()

    def _update_loop_button(self):
        labels = {None: "Loop Off", "track": "Loop Track", "queue": "Loop Queue"}
        self.loop_button.label = labels.get(self.player.loop_mode, "Loop Off")
        styles = {
            None: discord.ButtonStyle.grey,
            "track": discord.ButtonStyle.success,
            "queue": discord.ButtonStyle.primary,
        }
        self.loop_button.style = styles.get(self.player.loop_mode, discord.ButtonStyle.grey)
        self._update_pause_button()

    def _update_pause_button(self):
        self.pause_resume.label = "Resume" if self.player.paused else "Pause"
        self.pause_resume.style = discord.ButtonStyle.success if self.player.paused else discord.ButtonStyle.secondary
        emoji = "▶️" if self.player.paused else "⏸️"
        self.pause_resume.emoji = emoji

    async def _check_voice(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or interaction.user.voice.channel != self.player.channel:
            await interaction.response.send_message("You must be in the same voice channel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        if self.player.paused:
            await self.player.resume()
            await interaction.response.send_message("▶️ Resumed", ephemeral=True)
        elif self.player.playing:
            await self.player.pause()
            await interaction.response.send_message("⏸️ Paused", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        self._update_pause_button()
        if self.player.now_playing_message:
            try:
                await self.player.now_playing_message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="⏭️", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        if not self.player.current:
            return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        await self.player.skip()
        await interaction.response.send_message("⏭️ Skipped", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        if not self.player.current:
            return await interaction.response.send_message("Nothing to stop.", ephemeral=True)
        self.player.queue.clear()
        await self.player.stop()
        if self.player.now_playing_message:
            try:
                await self.player.now_playing_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.player.now_playing_message = None
        await interaction.response.send_message("⏹️ Stopped", ephemeral=True)

    @discord.ui.button(label="Vol-", style=discord.ButtonStyle.grey, emoji="🔉", row=0)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        new_vol = max(10, self.player.volume - 10)
        await self.player.set_volume(new_vol)
        await interaction.response.send_message(f"🔉 Volume {new_vol // 10}%", ephemeral=True)

    @discord.ui.button(label="Vol+", style=discord.ButtonStyle.grey, emoji="🔊", row=0)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        new_vol = min(1000, self.player.volume + 10)
        await self.player.set_volume(new_vol)
        await interaction.response.send_message(f"🔊 Volume {new_vol // 10}%", ephemeral=True)

    @discord.ui.button(label="Loop Off", style=discord.ButtonStyle.grey, emoji="🔁", row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        order = [None, "track", "queue"]
        idx = (order.index(self.player.loop_mode) + 1) % len(order)
        self.player.loop_mode = order[idx]
        self._update_loop_button()
        labels = {None: "🔁 Loop off", "track": "🔂 Looping track", "queue": "🔁 Looping queue"}
        await interaction.response.send_message(labels[self.player.loop_mode], ephemeral=True)
        if self.player.now_playing_message:
            try:
                await self.player.now_playing_message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.grey, emoji="📋", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = create_queue_embed(self.player)
        if not embed:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.grey, emoji="🔀", row=1)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        if not self.player.queue or self.player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        self.player.queue.shuffle()
        await interaction.response.send_message("🔀 Queue shuffled", ephemeral=True)

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        if not self.player.queue or self.player.queue.is_empty:
            return await interaction.response.send_message("Queue is already empty.", ephemeral=True)
        self.player.queue.clear()
        await interaction.response.send_message("🗑️ Queue cleared", ephemeral=True)

    @discord.ui.button(label="Favorite", style=discord.ButtonStyle.success, emoji="❤️", row=1)
    async def favorite_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        embed = create_queue_embed(self.player, page=page)
        await interaction.response.edit_message(embed=embed, view=self)


# ── Cog ─────────────────────────────────────────────────────────────

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._skip_votes: dict[int, set[int]] = {}

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        print(f"Command error [{interaction.command.name if interaction.command else '?'}]: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"Error: {error}", ephemeral=True)

    # ── Permissions ───────────────────────────────────────────────

    async def _check_dj(self, interaction: discord.Interaction) -> bool:
        if DJ_ROLE_ID is None:
            return True
        if interaction.user.voice and interaction.user.voice.channel:
            non_bots = [m for m in interaction.user.voice.channel.members if not m.bot]
            if len(non_bots) <= 1:
                return True
        role = interaction.guild.get_role(DJ_ROLE_ID)
        return role is None or role in interaction.user.roles

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
            return player

        player = await channel.connect(cls=MusicPlayer)
        music_channel_id = await get_music_channel(interaction.guild_id)
        if music_channel_id:
            mc = interaction.guild.get_channel(music_channel_id)
            player.text_channel = mc if isinstance(mc, discord.TextChannel) else interaction.channel
        else:
            player.text_channel = interaction.channel
        return player

    # ── Disconnect timer ──────────────────────────────────────────

    async def _start_disconnect_timer(self, player: MusicPlayer):
        if player._disconnect_task and not player._disconnect_task.done():
            player._disconnect_task.cancel()

        async def _timer():
            try:
                await asyncio.sleep(300)
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

    # ── Event listeners ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        print(f"Lavalink node ready: {payload.node!r}")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

        embed = create_now_playing_embed(player)
        if not embed or not player.text_channel:
            return

        view = NowPlayingView(player)
        player._current_view = view

        if player.now_playing_message:
            try:
                await player.now_playing_message.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            player.now_playing_message = await player.text_channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

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
        elif not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)
        else:
            if player.now_playing_message:
                try:
                    await player.now_playing_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                player.now_playing_message = None

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: MusicPlayer | None = payload.player
        if not player:
            return

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
                    # DEBUG temporaire : compare ce qui a été lu sur Spotify vs trouvé sur YouTube
                    if best:
                        print(
                            f"[SPOTIFY MATCH] Spotify: '{artist} - {title}' ({duration}ms) "
                            f"-> YouTube: '{best.author} - {best.title}' ({best.length}ms) "
                            f"score={_score_track(best, title, artist, duration):.1f}"
                        )
                    else:
                        print(f"[SPOTIFY MATCH] Spotify: '{artist} - {title}' ({duration}ms) -> AUCUN candidat retenu")
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
            return

        # --- Direct URL auto-play (YouTube, SoundCloud) ---
        if query.startswith(("http://", "https://")):
            result = await wavelink.Playable.search(query, node=player.node)
            if not result:
                return await interaction.followup.send("No results found.", ephemeral=True)

            if isinstance(result, wavelink.Playlist):
                result.track_extras(requester=interaction.user.id)
                for track in result:
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
            return

        # --- Text query: show search select ---
        result = await wavelink.Playable.search(query, node=player.node)
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
            return

        view = SearchSelectView(tracks[:5], player, interaction.user.id)
        embed = discord.Embed(
            title="Search Results",
            description="Select a track to play:",
            color=ACCENT,
        )
        await interaction.followup.send(embed=embed, view=view)

    # ── Transport commands ────────────────────────────────────────

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        if player.paused:
            return await interaction.response.send_message("Already paused.", ephemeral=True)
        await player.pause()
        await interaction.response.send_message("Paused ⏸️")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.paused:
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        await player.resume()
        await interaction.response.send_message("Resumed ▶️")

    @app_commands.command(name="skip", description="Skip to the next track")
    async def skip(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)

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
        non_bots = [m for m in player.channel.members if not m.bot]
        required = math.ceil(len(non_bots) / 2)

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
    async def skipto(self, interaction: discord.Interaction, position: int):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
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
    async def seek(self, interaction: discord.Interaction, time: str):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
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
    async def stop(self, interaction: discord.Interaction):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        player.queue.clear()
        await player.stop()
        if player.now_playing_message:
            try:
                await player.now_playing_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            player.now_playing_message = None
        await interaction.response.send_message("Stopped ⏹️")

    @app_commands.command(name="volume", description="Set volume (0-100)")
    @app_commands.describe(volume="Volume level (0-100)")
    async def volume(self, interaction: discord.Interaction, volume: int):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        if volume < 0 or volume > 100:
            return await interaction.response.send_message("Must be 0-100.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        await player.set_volume(volume * 10)
        await interaction.response.send_message(f"Volume → {volume}%")

    @app_commands.command(name="nowplaying", description="Show current track")
    async def nowplaying(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        embed = create_now_playing_embed(player)
        if embed:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)

        total_pages = max(1, math.ceil(len(player.queue) / 10))
        embed = create_queue_embed(player, page=1)
        view = QueuePageView(player, total_pages) if total_pages > 1 else None
        await interaction.response.send_message(embed=embed, view=view)

    # ── Queue management ──────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        player.queue.shuffle()
        await interaction.response.send_message(f"🔀 Queue shuffled ({len(player.queue)} tracks)")

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Track number in the queue")
    async def remove(self, interaction: discord.Interaction, position: int):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        if position < 1 or position > len(player.queue):
            return await interaction.response.send_message(f"Position must be 1-{len(player.queue)}.", ephemeral=True)

        track = player.queue.get_at(position - 1)
        player.queue.remove(track)
        await interaction.response.send_message(f"Removed: **[{track.title}]({track.uri})**")

    @app_commands.command(name="loop", description="Toggle loop mode: off -> track -> queue")
    async def loop(self, interaction: discord.Interaction):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)

        order = [None, "track", "queue"]
        idx = (order.index(player.loop_mode) + 1) % len(order)
        player.loop_mode = order[idx]

        labels = {None: "🔁 Loop off", "track": "🔂 Looping track", "queue": "🔁 Looping queue"}
        await interaction.response.send_message(labels[player.loop_mode])

        if player._current_view:
            player._current_view._update_loop_button()
            if player.now_playing_message:
                try:
                    await player.now_playing_message.edit(view=player._current_view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    # ── Music channel ─────────────────────────────────────────────

    @app_commands.command(name="setchannel", description="Set the dedicated music text channel")
    @app_commands.describe(channel="Text channel for now-playing messages (leave empty to reset)")
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await set_music_channel(interaction.guild_id, channel.id if channel else None)
        if channel:
            await interaction.response.send_message(f"✅ Music channel set to {channel.mention}")
        else:
            await interaction.response.send_message("✅ Music channel reset (uses command channel)")

    # ── Audio filters ─────────────────────────────────────────────

    @app_commands.command(name="bassboost", description="Enable bass boost")
    async def bassboost(self, interaction: discord.Interaction):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)

        filters = wavelink.Filters()
        filters.equalizer = wavelink.Equalizer(payload=[(0, 0.2), (1, 0.15), (2, 0.1), (3, 0.05)])
        await player.set_filters(filters)
        await interaction.response.send_message("✅ Bass boost enabled")

    @app_commands.command(name="nightcore", description="Enable nightcore effect")
    async def nightcore(self, interaction: discord.Interaction):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)

        filters = wavelink.Filters()
        filters.timescale = wavelink.Timescale(payload={"pitch": 1.2, "speed": 1.2})
        await player.set_filters(filters)
        await interaction.response.send_message("✅ Nightcore enabled")

    @app_commands.command(name="reset", description="Reset all audio filters")
    async def reset(self, interaction: discord.Interaction):
        if not await self._check_dj(interaction):
            return await interaction.response.send_message("You need the DJ role for this.", ephemeral=True)
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("Not connected.", ephemeral=True)

        await player.set_filters(wavelink.Filters())
        await interaction.response.send_message("✅ Filters reset")

    # ── Playlists ─────────────────────────────────────────────────

    @app_commands.command(name="save", description="Save current queue as a playlist")
    @app_commands.describe(name="Playlist name")
    async def save(self, interaction: discord.Interaction, name: str):
        player: MusicPlayer | None = interaction.guild.voice_client
        if not player or player.queue.is_empty:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)

        track_data = [
            {"title": t.title, "uri": t.uri, "author": t.author, "duration": t.length}
            for t in player.queue.copy()
        ]

        try:
            await save_playlist(interaction.user.id, name, track_data)
            await interaction.response.send_message(f"✅ Saved **{len(track_data)} tracks** as `{name}`")
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="playlist", description="Load a saved playlist")
    @app_commands.describe(name="Playlist name")
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

    @app_commands.command(name="pl_list", description="List your saved playlists")
    async def pl_list(self, interaction: discord.Interaction):
        playlists_data = await list_playlists(interaction.user.id)
        if not playlists_data:
            return await interaction.response.send_message("No saved playlists.", ephemeral=True)

        lines = []
        for p in playlists_data:
            lines.append(f"**{p['name']}** — {p['track_count']} tracks ({p['created_at'][:10]})")

        embed = discord.Embed(
            title="Your playlists",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(playlists_data)} playlist(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pl_delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    async def pl_delete(self, interaction: discord.Interaction, name: str):
        deleted = await delete_playlist(interaction.user.id, name)
        if deleted:
            await interaction.response.send_message(f"Deleted playlist `{name}`")
        else:
            await interaction.response.send_message(f"No playlist `{name}` found.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
