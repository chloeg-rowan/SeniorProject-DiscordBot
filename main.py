import os
import aiohttp
import chess
import discord
from dotenv import load_dotenv, dotenv_values, set_key



intents = discord.Intents.all()
intents.message_content = True
client = discord.Client(intents = intents)

load_dotenv()




client.run(os.getenv('DISCORD_BOT_TOKEN'))