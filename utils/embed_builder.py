import discord

COLOR_NOW_PLAYING = 0x6C5CE7
COLOR_ERROR = 0xE63946
COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x4C6FFF
COLOR_QUEUE = 0x6C5CE7


def build_embed(
    *,
    type: str = "info",
    title: str | None = None,
    description: str | None = None,
    url: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    footer_text: str | None = None,
    footer_icon: str | None = None,
    thumbnail: str | None = None,
    image: str | None = None,
    color: int | None = None,
) -> discord.Embed:
    color_map = {
        "now_playing": COLOR_NOW_PLAYING,
        "error": COLOR_ERROR,
        "success": COLOR_SUCCESS,
        "info": COLOR_INFO,
        "queue": COLOR_QUEUE,
    }
    embed = discord.Embed(
        title=title,
        description=description,
        url=url,
        color=color if color else color_map.get(type, COLOR_INFO),
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer_text:
        embed.set_footer(text=footer_text, icon_url=footer_icon)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    return embed


def progress_bar(current_ms: int, total_ms: int, length: int = 12) -> str:
    if total_ms <= 0:
        return "🔴 LIVE"
    ratio = current_ms / total_ms
    filled = round(ratio * length)
    bar = "▬" * filled + "🔘" + "▬" * (length - filled) if filled < length else "▬" * (length - 1) + "🔘"
    current_str = _fmt_ts(current_ms)
    total_str = _fmt_ts(total_ms)
    return f"{bar} `[{current_str} / {total_str}]`"


def _fmt_ts(ms: int) -> str:
    total_sec = int(ms) // 1000
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
