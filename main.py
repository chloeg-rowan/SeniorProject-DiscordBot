import os
import json
import aiohttp
import chess
import discord
from dotenv import load_dotenv
from keep_alive import keep_alive
from pymongo import MongoClient
from datetime import datetime
from bson import ObjectId

intents = discord.Intents.all()
intents.message_content = True
client = discord.Client(intents=intents)

load_dotenv()

API_BASE = os.getenv("GAME_API_BASE", "http://localhost:3001")
CHESS_AI_API_URL = os.getenv("CHESS_AI_API_URL", os.getenv("GAME_API_BASE", "http://localhost:3001"))
AI_DIFFICULTY = os.getenv("AI_DIFFICULTY", "medium")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/chess")

# MongoDB connection
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["chess"]
users_collection = db["users"]
gamesessions_collection = db["gamesessions"]

games = {}
players = {}
difficulties = {}
discord_to_user_id = {}  # Map Discord ID to MongoDB User ID


def get_or_create_board(channel_id: int) -> chess.Board:
    if channel_id not in games:
        games[channel_id] = chess.Board()
    return games[channel_id]


def is_ai_game(channel_id: int) -> bool:
    return bool(players.get(channel_id, {}).get("ai"))


def is_training_game(channel_id: int) -> bool:
    return bool(players.get(channel_id, {}).get("training"))


def get_channel_difficulty(channel_id: int) -> str:
    return difficulties.get(channel_id, AI_DIFFICULTY)


async def get_or_create_discord_user(discord_id: int, username: str):
    """Get or create a Discord user in MongoDB."""
    cached_user_id = discord_to_user_id.get(discord_id)
    if cached_user_id:
        return users_collection.find_one({"_id": ObjectId(cached_user_id)})
    
    user = users_collection.find_one({"discordId": str(discord_id)})
    if not user:
        user_doc = {
            "discordId": str(discord_id),
            "email": None,
            "username": username or f"Discord User {discord_id}",
            "provider": "discord",
            "gameStats": {
                "classic": {"wins": 0, "losses": 0, "draws": 0, "rating": 0},
                "training": {"wins": 0, "losses": 0, "draws": 0, "rating": 0},
                "timed": {"wins": 0, "losses": 0, "draws": 0, "rating": 0},
            },
            "createdAt": datetime.utcnow(),
            "updatedAt": datetime.utcnow(),
        }
        result = users_collection.insert_one(user_doc)
        user = users_collection.find_one({"_id": result.inserted_id})
    
    discord_to_user_id[discord_id] = str(user["_id"])
    return user


async def save_game_session(discord_id: int, game_mode: str, result: str, resigned_by=None, winner=None, score=0, fen=None):
    """Save a game session to MongoDB."""
    user = await get_or_create_discord_user(discord_id, None)
    
    valid_mode = "classic" if game_mode not in ["classic", "training", "timed"] else game_mode
    
    game_session = {
        "userId": user["_id"],
        "gameMode": valid_mode,
        "result": result,
        "resignedBy": resigned_by,
        "winner": winner,
        "score": score,
        "fen": fen,
        "moveCount": int(fen.split()[5]) if fen else 0,
        "createdAt": datetime.utcnow(),
        "updatedAt": datetime.utcnow(),
    }
    
    gamesessions_collection.insert_one(game_session)
    
    # Update user stats
    if result == "white_wins":
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$inc": {f"gameStats.{valid_mode}.wins": 1, f"gameStats.{valid_mode}.rating": 16},
                "$set": {"updatedAt": datetime.utcnow()},
            }
        )
    elif result == "black_wins":
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$inc": {f"gameStats.{valid_mode}.losses": 1, f"gameStats.{valid_mode}.rating": -16},
                "$set": {"updatedAt": datetime.utcnow()},
            }
        )
    elif result == "draw":
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$inc": {f"gameStats.{valid_mode}.draws": 1},
                "$set": {"updatedAt": datetime.utcnow()},
            }
        )
    elif result == "white_resigned":
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$inc": {f"gameStats.{valid_mode}.losses": 1, f"gameStats.{valid_mode}.rating": -16},
                "$set": {"updatedAt": datetime.utcnow()},
            }
        )
    elif result == "black_resigned":
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$inc": {f"gameStats.{valid_mode}.wins": 1, f"gameStats.{valid_mode}.rating": 16},
                "$set": {"updatedAt": datetime.utcnow()},
            }
        )


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
        return None

    if board.turn == chess.WHITE:
        return pair.get("white")

    if pair.get("ai"):
        return None

    return pair.get("black")


def is_user_turn(channel_id: int, board: chess.Board, user_id: int) -> bool:
    pair = players.get(channel_id)
    if not pair:
        return False

    if board.turn == chess.WHITE:
        return pair.get("white") == user_id

    if pair.get("ai"):
        return False

    return pair.get("black") == user_id


async def fetch_chess_api(path: str, payload: dict) -> dict:
    url = f"{CHESS_AI_API_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except Exception:
                raise RuntimeError(text or f"HTTP {resp.status}")

            if resp.status != 200:
                raise RuntimeError(data.get("error", text) if isinstance(data, dict) else text)
            return data


async def fetch_health() -> dict:
    url = f"{CHESS_AI_API_URL}/api/health"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except Exception:
                raise RuntimeError(text or f"HTTP {resp.status}")

            if resp.status != 200:
                raise RuntimeError(data.get("error", text) if isinstance(data, dict) else text)
            return data


async def fetch_ai_move(fen: str, difficulty: str = "medium") -> dict:
    return await fetch_chess_api("/api/move", {"fen": fen, "difficulty": difficulty})


async def fetch_evaluation(fen: str) -> dict:
    return await fetch_chess_api("/api/evaluate", {"fen": fen})


async def fetch_analysis(fen: str, num_sims: int = 400) -> dict:
    return await fetch_chess_api("/api/analyze", {"fen": fen, "num_sims": num_sims})


async def fetch_training_analysis(fen: str, player_move: str) -> dict:
    return await fetch_chess_api("/api/training/analyze-move", {"fen": fen, "player_move": player_move})


async def fetch_suggestion(fen: str) -> dict:
    return await fetch_chess_api("/api/training/suggest", {"fen": fen})


async def fetch_piece_info(fen: str, square: str) -> dict:
    return await fetch_chess_api("/api/training/piece-info", {"fen": fen, "square": square})


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
    lower = text.lower()
    channel_id = message.channel.id

    if lower.startswith("!help"):
        await message.channel.send(
            "Commands:"
            "\n\n**!startgame @opponent** - start a human chess game."
            "\n**!playai** - start a game against the AI."
            "\n**!training on/off** - enable or disable training mode against the AI."
            "\n**!difficulty [level]** - set or view AI difficulty."
            "\n**!board** - display current board state as PNG."
            "\n**!hint** - ask the AI for the best move in the current position."
            "\n**!suggest** - get a training suggestion for the current position."
            "\n**!analyze-move <move>** - get training feedback for a candidate move."
            "\n**!pieceinfo <square>** - get movement info for a piece."
            "\n**!evaluate** - evaluate the current board."
            "\n**!status** - show current game mode and difficulty."
            "\n**!resign** or **!reset** - end or restart the current channel game."
            "\n\nOr type chess move notation directly (e.g., `e4`, `Nf3`, `Qxe7+`)."
        )
        return

    if lower.startswith("!login"):
        await message.channel.send("Login happens through the web frontend; the bot does not manage account sign-in.")
        return

    if lower.startswith("!health"):
        try:
            data = await fetch_health()
            await message.channel.send(f"Chess API health: **{data.get('status', 'ok')}**")
        except Exception as e:
            await message.channel.send(f"⚠️ Health check failed: {e}")
        return

    if lower.startswith("!difficulty"):
        parts = lower.split()
        if len(parts) == 1:
            await message.channel.send(f"Current AI difficulty: **{get_channel_difficulty(channel_id)}**")
            return
        difficulty = parts[1]
        difficulties[channel_id] = difficulty
        await message.channel.send(f"AI difficulty set to **{difficulty}** for this channel.")
        return

    if lower.startswith("!startgame"):
        parts = lower.split()
        mentions = message.mentions
        if len(mentions) == 1:
            white = message.author.id
            black = mentions[0].id
            players[channel_id] = {"white": white, "black": black, "ai": False, "training": False}
            games[channel_id] = chess.Board()
            await message.channel.send(
                f"New human game started!\nWhite: <@{white}>\nBlack: <@{black}>\nTurn: White\nType moves directly, e.g. `e4`."
            )
            return

        if len(parts) == 1 or parts[1] in ("ai", "computer"):
            players[channel_id] = {"white": message.author.id, "black": None, "ai": True, "training": False}
            games[channel_id] = chess.Board()
            await message.channel.send(
                f"AI game started! White: <@{message.author.id}>\nDifficulty: **{get_channel_difficulty(channel_id)}**\nType moves directly, e.g. `e4`."
            )
            return

        await message.channel.send("Usage: `!startgame @opponent` or `!startgame ai`")
        return

    if lower.startswith("!playai"):
        players[channel_id] = {"white": message.author.id, "black": None, "ai": True, "training": False}
        games[channel_id] = chess.Board()
        await message.channel.send(
            f"AI game started! White: <@{message.author.id}>\nDifficulty: **{get_channel_difficulty(channel_id)}**\nType moves directly, e.g. `e4`."
        )
        return

    if lower.startswith("!training"):
        parts = lower.split()
        if "on" in parts:
            players[channel_id] = {"white": message.author.id, "black": None, "ai": True, "training": True}
            games[channel_id] = chess.Board()
            await message.channel.send(
                f"Training mode enabled! White: <@{message.author.id}>\nAI will analyze your moves and suggest improvements."
            )
            return
        if "off" in parts:
            if channel_id in players and players[channel_id].get("training"):
                players[channel_id]["training"] = False
                await message.channel.send("Training mode disabled for this channel.")
            else:
                await message.channel.send("Training mode is not enabled in this channel.")
            return
        await message.channel.send("Usage: `!training on` or `!training off`")
        return

    if lower.startswith("!status"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        mode = "AI game" if is_ai_game(channel_id) else "Human game"
        training = "Training" if is_training_game(channel_id) else "Standard"
        turn = "White" if board.turn == chess.WHITE else "Black"
        await message.channel.send(
            f"Game status: **{mode}** ({training})\nTurn: **{turn}**\nDifficulty: **{get_channel_difficulty(channel_id)}"
        )
        return

    if lower.startswith("!reset"):
        if channel_id in games:
            del games[channel_id]
        if channel_id in players:
            del players[channel_id]
        if channel_id in difficulties:
            del difficulties[channel_id]
        await message.channel.send("Game reset. Use `!startgame @opponent` or `!playai` to start a new game.")
        return

    if lower.startswith("!resign") or lower.startswith("!forfeit"):
        board = games.get(channel_id)
        game_mode = "training" if is_training_game(channel_id) else "classic"
        
        if board and channel_id in players:
            resigned_by_white = message.author.id == players[channel_id].get("white")
            result = "white_resigned" if resigned_by_white else "black_resigned"
            await save_game_session(message.author.id, game_mode, result, resigned_by="white" if resigned_by_white else "black", fen=board.fen())
        
        if channel_id in games:
            del games[channel_id]
        if channel_id in players:
            del players[channel_id]
        await message.channel.send(f"<@{message.author.id}> resigned. Game ended.")
        return

    if lower.startswith("!board"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        png_path = render_board_png(board)
        await message.channel.send(file=discord.File(png_path))
        return

    if lower.startswith("!hint"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        try:
            data = await fetch_analysis(board.fen())
            move = data.get("move") or (data.get("moves") or [{}])[0].get("move")
            response = [f"💡 Best move: **{move}**" if move else "💡 No move suggested."]
            if "value" in data:
                response.append(f"Eval: **{data['value']}**")
            if "confidence" in data:
                response.append(f"Confidence: **{data['confidence']}**")
            await message.channel.send("\n".join(response))
        except Exception as e:
            await message.channel.send(f"⚠️ Hint request failed: {e}")
        return

    if lower.startswith("!suggest"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        try:
            data = await fetch_suggestion(board.fen())
            response = [f"💡 Suggested move: **{data.get('suggested_move')}**"]
            if data.get("explanation"):
                response.append(data["explanation"])
            if data.get("confidence") is not None:
                response.append(f"Confidence: **{data['confidence']}**")
            await message.channel.send("\n".join(response))
        except Exception as e:
            await message.channel.send(f"⚠️ Suggestion request failed: {e}")
        return

    if lower.startswith("!evaluate"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        try:
            data = await fetch_evaluation(board.fen())
            response = [f"📊 Evaluation: **{data.get('value')}**"]
            if data.get("confidence") is not None:
                response.append(f"Confidence: **{data['confidence']}**")
            if data.get("think_time_ms") is not None:
                response.append(f"Think time: **{data['think_time_ms']} ms**")
            await message.channel.send("\n".join(response))
        except Exception as e:
            await message.channel.send(f"⚠️ Evaluation failed: {e}")
        return

    if lower.startswith("!pieceinfo"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        parts = text.split()
        if len(parts) < 2:
            await message.channel.send("Usage: `!pieceinfo e4`")
            return
        square = parts[1].lower()
        try:
            data = await fetch_piece_info(board.fen(), square)
            response = [
                f"📌 Piece: **{data.get('piece_name')}** on **{data.get('square')}**",
                f"Color: **{data.get('piece_color')}**",
                f"Legal destinations: **{len(data.get('legal_destinations', []))}**",
            ]
            if data.get("movement_rules"):
                response.append(f"Rules: {data.get('movement_rules')}")
            await message.channel.send("\n".join(response))
        except Exception as e:
            await message.channel.send(f"⚠️ Piece info failed: {e}")
        return

    if lower.startswith("!analyze-move") or lower.startswith("!review"):
        board = games.get(channel_id)
        if not board:
            await message.channel.send("No active game in this channel.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.channel.send("Usage: `!analyze-move e4`")
            return
        move_text = parts[1].strip()
        try:
            move = parse_player_move(board, move_text)
            uci_move = move.uci()
        except Exception:
            await message.channel.send(f"❌ Invalid or illegal move: `{move_text}`")
            return
        try:
            data = await fetch_training_analysis(board.fen(), uci_move)
            response = [f"🧠 Analysis for **{uci_move}**:"]
            if data.get("suggestion"):
                response.append(data["suggestion"])
            if data.get("best_move"):
                response.append(f"Best move: **{data['best_move']}**")
            if data.get("player_move_rank") is not None:
                response.append(f"Player move rank: **#{data['player_move_rank']}**")
            if data.get("confidence") is not None:
                response.append(f"Confidence: **{data['confidence']}**")
            await message.channel.send("\n".join(response))
        except Exception as e:
            await message.channel.send(f"⚠️ Analyze move failed: {e}")
        return

    if not is_move_like_text(text):
        return

    board = games.get(channel_id)
    if board is None:
        return

    if not is_user_turn(channel_id, board, message.author.id):
        if board.turn == chess.BLACK and is_ai_game(channel_id):
            await message.channel.send("The AI is thinking. Please wait for its move.")
            return
        await message.channel.send("It is not your turn.")
        return

    previous_fen = board.fen()
    try:
        player_move = parse_player_move(board, text)
    except Exception:
        await message.channel.send(f"❌ Invalid or illegal move: `{text}`")
        return

    board.push(player_move)

    if board.is_game_over():
        game_mode = "training" if is_training_game(channel_id) else "classic"
        if board.is_checkmate():
            result = "white_wins" if board.turn == chess.BLACK else "black_wins"
        else:
            result = "draw"
        
        await save_game_session(message.author.id, game_mode, result, fen=board.fen())
        
        png_path = render_board_png(board)
        await message.channel.send(
            f"✅ You played `{text}`\nGame over: **{board.result()}**",
            file=discord.File(png_path)
        )
        if channel_id in games:
            del games[channel_id]
        if channel_id in players:
            del players[channel_id]
        return

    if is_training_game(channel_id):
        uci_move = player_move.uci()
        try:
            analysis = await fetch_training_analysis(previous_fen, uci_move)
            analysis_lines = [f"🧠 Training analysis for **{uci_move}**:"]
            if analysis.get("suggestion"):
                analysis_lines.append(analysis["suggestion"])
            if analysis.get("best_move"):
                analysis_lines.append(f"Best move: **{analysis['best_move']}**")
            if analysis.get("player_move_rank") is not None:
                analysis_lines.append(f"Player move rank: **#{analysis['player_move_rank']}**")
            if analysis.get("confidence") is not None:
                analysis_lines.append(f"Confidence: **{analysis['confidence']}**")
            await message.channel.send("\n".join(analysis_lines))
        except Exception as e:
            await message.channel.send(f"⚠️ Training analysis failed: {e}")

    if is_ai_game(channel_id):
        try:
            ai_data = await fetch_ai_move(board.fen(), get_channel_difficulty(channel_id))
        except Exception as e:
            await message.channel.send(f"⚠️ AI request failed: {e}")
            return

        try:
            ai_move = parse_ai_move(board, ai_data)
            board.push(ai_move)
        except Exception as e:
            await message.channel.send(f"⚠️ AI move parse/apply failed: {e}")
            return

        if board.is_game_over():
            game_mode = "training" if is_training_game(channel_id) else "classic"
            if board.is_checkmate():
                result = "black_wins" if board.turn == chess.WHITE else "white_wins"
            else:
                result = "draw"
            
            await save_game_session(message.author.id, game_mode, result, fen=board.fen())
            
            out = [
                f"✅ {message.author.mention} played **{text}**",
                f"🤖 AI played **{ai_data.get('move', str(ai_move))}**",
                f"Game over: **{board.result()}**",
            ]
        else:
            out = [
                f"✅ {message.author.mention} played **{text}**",
                f"🤖 AI played **{ai_data.get('move', str(ai_move))}**",
            ]

        if "value" in ai_data:
            out.append(f"Eval: **{ai_data['value']}**")
        if "confidence" in ai_data:
            out.append(f"Confidence: **{ai_data['confidence']}**")
        if "think_time_ms" in ai_data:
            out.append(f"Think time: **{ai_data['think_time_ms']} ms**")

        png_path = render_board_png(board)
        await message.channel.send("\n".join(out), file=discord.File(png_path))
        
        if board.is_game_over():
            if channel_id in games:
                del games[channel_id]
            if channel_id in players:
                del players[channel_id]
        
        return

    png_path = render_board_png(board)
    await message.channel.send(
        f"✅ {message.author.mention} played **{text}**",
        file=discord.File(png_path)
    )

keep_alive()
client.run(os.getenv("DISCORD_BOT_TOKEN"))