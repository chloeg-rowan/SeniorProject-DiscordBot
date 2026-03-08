import os
import aiohttp
import chess
import discord
from dotenv import load_dotenv, dotenv_values, set_key



intents = discord.Intents.all()
intents.message_content = True
client = discord.Client(intents = intents)

load_dotenv()

@client.event
async def on_ready():
    print(f'Signed on as {client.user}')


@client.event
async def on_message(message):
    
    if(message.content.startswith("!Help")) or (message.content.startswith("!help")):
        await message.channel.send("Commands: " 
                                   + "\n\n**!Login** - Log in to your account.")
        
    if(message.content.startswith("!Login")) or (message.content.startswith("!login")): #Once the database is functional, all methods will need to check if they logged in.
        await message.channel.send("Please enter your username. (Still in development, use \"admin\" as the username and \"password\" as the password.)")
        validEntry = False
        while not validEntry:
            mess = await client.wait_for("message", check=lambda msg: msg.author == message.author, timeout = 300.0)
            #Communicate with the database to check for login data. For now we will just check if the username is "admin" and the password is "password"
            if(mess.content == "admin"):
                validEntry = True
            else:
                await message.channel.send("Invalid username. Please enter your username.")
        await message.channel.send("Please enter your password.")
        validEntry = False
        while not validEntry:
            mess = await client.wait_for("message", check=lambda msg: msg.author == message.author, timeout = 300.0)
            if(mess.content == "password"):
                validEntry = True
            else:
                await message.channel.send("Invalid password. Please enter your password.")
        await message.channel.send("Login successful!")
        
    

client.run(os.getenv('DISCORD_BOT_TOKEN'))