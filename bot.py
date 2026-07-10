import discord
from discord.ext import commands
import wavelink
from config import DISCORD_TOKEN, LAVALINK_URI, LAVALINK_PASSWORD
from utils.db import init_db


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )

    async def setup_hook(self):
        await init_db()
        await self.load_extension("cogs.music")
        await self.tree.sync()

        nodes = [
            wavelink.Node(
                uri=LAVALINK_URI,
                password=LAVALINK_PASSWORD,
            )
        ]
        await wavelink.Pool.connect(nodes=nodes, client=self)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        # Renommage retiré : Discord limite les changements de pseudo à 2/heure.
        # Si besoin de renommer le bot, fais-le une fois manuellement dans
        # le Developer Portal plutôt qu'à chaque démarrage.


bot = Bot()
bot.run(DISCORD_TOKEN)
