import math

import discord
import wavelink

from utils.embed_builder import build_embed, progress_bar, _fmt_ts


def create_now_playing_embed(player: wavelink.Player) -> discord.Embed | None:

    track = player.current
    if not track:
        return None

    duration_str = _fmt_ts(track.length) if track.length else "LIVE"
    prog = progress_bar(player.position, track.length) if track.length else "🔴 LIVE"

    loop_icon = {"track": "🔂", "queue": "🔁", None: ""}
    loop_text = {"track": " Track", "queue": " Queue", None: ""}
    loop_str = f"{loop_icon[player.loop_mode]}Loop{loop_text[player.loop_mode]}"

    footer_parts = [f"Queue: {len(player.queue)} track{'s' if len(player.queue) != 1 else ''}"]
    requester = getattr(track, "requester", None)
    if requester and player.guild:
        member = player.guild.get_member(requester)
        if member:
            footer_parts.insert(0, f"Requested by {member.display_name}")

    embed = build_embed(
        type="now_playing",
        title=track.title,
        url=track.uri,
        description=f"{prog}\n\n{loop_str}",
        fields=[
            ("Artist", track.author, True),
            ("Duration", duration_str, True),
            ("Volume", f"{player.volume}%", True),
        ],
        footer_text="  ·  ".join(footer_parts),
        thumbnail=track.artwork if track.artwork else None,
    )
    return embed


def create_queue_embed(player: wavelink.Player, page: int = 1, color: int | None = None) -> discord.Embed | None:
    if player.queue.is_empty:
        return None

    tracks_per_page = 10
    total_pages = max(1, math.ceil(len(player.queue) / tracks_per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * tracks_per_page
    end = start + tracks_per_page

    lines = []
    queue_list = list(player.queue)
    for i, track in enumerate(queue_list[start:end], start=start + 1):
        duration = _fmt_ts(track.length) if track.length else "LIVE"
        lines.append(f"`{i:02d}.` [{track.title}]({track.uri}) — `{duration}`")

    title = "📋 Queue"
    if total_pages > 1:
        title += f" (p.{page}/{total_pages})"

    embed = build_embed(
        type="queue",
        title=title,
        description="\n".join(lines),
        fields=[],
        footer_text=f"{len(player.queue)} tracks  ·  {_fmt_ts(sum(t.length for t in queue_list if t.length))} total",
        color=color,
    )

    if player.current:
        current_dur = _fmt_ts(player.current.length) if player.current.length else "LIVE"
        embed.add_field(
            name="▶️ Now Playing",
            value=f"[{player.current.title}]({player.current.uri}) — `{current_dur}`",
            inline=False,
        )

    return embed
