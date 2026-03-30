import os
import aiohttp
import chess
import discord
from dotenv import load_dotenv
from keep_alive import keep_alive

intents = discord.Intents.all()
intents.message_content = True
client = discord.Client(intents=intents)

load_dotenv()

API_BASE = os.getenv("GAME_API_BASE", "http://localhost:3001")
AI_DIFFICULTY = os.getenv("AI_DIFFICULTY", "medium")

games = {}
players = {}

def get_or_create_board(channel_id: int) -> chess.Board:
    if channel_id not in games:
        games[channel_id] = chess.Board()
    return games[channel_id]

def is_move_like_text(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.startswith("!"):
        return False
    return len(t) <= 12

def parse_player_move(board: chess.Board, text: str) -> chess.Move:
    raw = text.strip()
    try:
        return board.parse_san(raw)
    except Exception:
        pass

    move = chess.Move.from_uci(raw.lower())
    if move in board.legal_moves:
        return move

    raise ValueError("invalid_or_illegal")

def current_turn_user_id(channel_id: int, board: chess.Board):
    pair = players.get(channel_id)
    if not pair:
        return None  # no player lock
    return pair["white"] if board.turn == chess.WHITE else pair["black"]

async def fetch_ai_move(fen: str, difficulty: str = "medium") -> dict:
    payload = {"fen": fen, "difficulty": difficulty}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_BASE}/api/chess/move", json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                err = data.get("error", f"HTTP {resp.status}")
                raise RuntimeError(err)
            return data

def parse_ai_move(board: chess.Board, ai_data: dict) -> chess.Move:
    raw = (
        ai_data.get("move")
        or ai_data.get("best_move")
        or ((ai_data.get("moves") or [{}])[0].get("move"))
    )
    if not raw:
        raise ValueError(f"AI response missing move: {ai_data}")

    try:
        mv = chess.Move.from_uci(raw.lower())
        if mv in board.legal_moves:
            return mv
    except Exception:
        pass

    try:
        return board.parse_san(raw)
    except Exception:
        raise ValueError(f"AI returned unplayable move '{raw}'")
    
def render_board_png(board, out_path="board.png", last_move=None, flipped=False):
    import chess.svg
    import cairosvg

    svg = chess.svg.board(board=board, lastmove=last_move, flipped=flipped, size=600)
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=out_path)
    return out_path

@client.event
async def on_ready():
    print(f"Signed on as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    text = message.content.strip()

    if text.lower().startswith("!help"):
        await message.channel.send(
            "Commands:"
            "\n\n**!Login** - Log in to your account."
            "\n**!startgame @opponent** - start a chess game."
            "\n**!board** - display current board state as PNG."
            "\n**!resign** - resign current game."
            "\n\nOr just type chess move notation directly (e.g., `e4`, `Nf3`, `Qxe7+`)."
        )
        return
    
    if text.lower().startswith("!login"):
        await message.channel.send("Login functionality is not implemented in this demo.")
        return

    if text.lower().startswith("!startgame"):
        mentions = message.mentions
        if len(mentions) != 1:
            await message.channel.send("Usage: `!startgame @opponent`")
            return

        white = message.author.id
        black = mentions[0].id
        channel_id = message.channel.id

        players[channel_id] = {"white": white, "black": black}
        games[channel_id] = chess.Board()

        await message.channel.send(
            f"New game started!\nWhite: <@{white}>\nBlack: <@{black}>\n"
            f"Turn: White\nType moves directly, e.g. `e4`."
        )
        return

    if text.lower().startswith("!resign"):
        channel_id = message.channel.id
        if channel_id in games:
            del games[channel_id]
        if channel_id in players:
            del players[channel_id]
        await message.channel.send(f"<@{message.author.id}> resigned. Game ended.")
        return
    
    if text.lower().startswith("!board"):
        channel_id = message.channel.id
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        png_path = render_board_png(board)
        await message.channel.send(file=discord.File(png_path))
        return

    if not is_move_like_text(text):
        return

    channel_id = message.channel.id
    board = games.get(channel_id)
    if board is None:
        return

    turn_uid = current_turn_user_id(channel_id, board)
    if turn_uid is not None and message.author.id != turn_uid:
        await message.channel.send("It is not your turn.")
        return

    try:
        player_move = parse_player_move(board, text)
    except Exception:
        await message.channel.send(f"❌ Invalid or illegal move: `{text}`")
        return

    board.push(player_move)

    if board.is_game_over():
        await message.channel.send(
            f"✅ You played `{text}`\nGame over: **{board.result()}**\n```{board}```"
        )
        return

    try:
        ai_data = await fetch_ai_move(board.fen(), AI_DIFFICULTY)
    except Exception as e:
        await message.channel.send(f"⚠️ AI request failed: {e}")
        return

    try:
        ai_move = parse_ai_move(board, ai_data)
        board.push(ai_move)
    except Exception as e:
        await message.channel.send(f"⚠️ AI move parse/apply failed: {e}")
        return

    out = [
        f"✅ {message.author.mention} played **{text}**",
        f"🤖 AI played **{ai_data.get('move', str(ai_move))}**",
        f"`{board.fen()}`",
        f"```{board}```"
    ]

    if "value" in ai_data:
        out.append(f"Eval: **{ai_data['value']}**")
    if "confidence" in ai_data:
        out.append(f"Confidence: **{ai_data['confidence']}**")
    if "think_time_ms" in ai_data:
        out.append(f"Think time: **{ai_data['think_time_ms']} ms**")

    await message.channel.send("\n".join(out))

keep_alive()
client.run(os.getenv("DISCORD_BOT_TOKEN"))