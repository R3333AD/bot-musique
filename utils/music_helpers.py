import math

import discord
import wavelink


def format_duration(milliseconds: int) -> str:
    total_sec = int(milliseconds) // 1000
    minutes, secs = divmod(total_sec, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_progress(current: int, total: int, length: int = 12) -> str:
    if total <= 0:
        return "🔴 LIVE"
    ratio = current / total
    filled = round(ratio * length)
    bar = "▰" * filled + "▱" * (length - filled)
    return f"{bar} {format_duration(current)} / {format_duration(total)}"


ACCENT = 0x1DB954  # Spotify green


def create_now_playing_embed(player: wavelink.Player) -> discord.Embed | None:
    track = player.current
    if not track:
        return None

    duration_str = format_duration(track.length) if track.length else "LIVE"
    pos_str = format_duration(player.position) if player.position else "0:00"
    progress = format_progress(player.position, track.length) if track.length else "🔴 LIVE"

    loop_icon = {"track": "🔂", "queue": "🔁", None: ""}
    loop_text = {"track": " Track", "queue": " Queue", None: ""}
    loop_str = f" {loop_icon[player.loop_mode]}Loop{loop_text[player.loop_mode]}"

    embed = discord.Embed(
        title=track.title,
        url=track.uri,
        description=f"{progress}\n\n{loop_str}",
        color=ACCENT,
    )

    embed.add_field(name="Artist", value=track.author, inline=True)
    embed.add_field(name="Duration", value=duration_str, inline=True)
    embed.add_field(name="Volume", value=f"{player.volume // 10}%", inline=True)

    if track.artwork:
        embed.set_image(url=track.artwork)

    requester = getattr(track, "requester", None)
    if requester and player.guild:
        member = player.guild.get_member(requester)
        if member:
            embed.set_footer(
                text=f"Requested by {member.display_name}",
                icon_url=member.display_avatar.url,
            )

    return embed


def create_queue_embed(player: wavelink.Player, page: int = 1) -> discord.Embed | None:
    if player.queue.is_empty:
        return None

    tracks_per_page = 10
    total_pages = max(1, math.ceil(len(player.queue) / tracks_per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * tracks_per_page
    end = start + tracks_per_page

    embed = discord.Embed(
        title="📋 Queue",
        color=ACCENT,
    )

    lines = []
    queue_list = list(player.queue)
    for i, track in enumerate(queue_list[start:end], start=start + 1):
        duration = format_duration(track.length) if track.length else "LIVE"
        lines.append(f"`{i:02d}.` [{track.title}]({track.uri}) — `{duration}`")

    embed.description = "\n".join(lines)

    if total_pages > 1:
        embed.title += f" (p.{page}/{total_pages})"

    if player.current:
        current_dur = (
            format_duration(player.current.length) if player.current.length else "LIVE"
        )
        embed.add_field(
            name="▶️ Now Playing",
            value=f"[{player.current.title}]({player.current.uri}) — `{current_dur}`",
            inline=False,
        )

    embed.set_footer(text=f"{len(player.queue)} tracks  ·  {format_duration(sum(t.length for t in queue_list if t.length))} total")
    return embed
