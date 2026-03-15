import asyncio
import random
import string
import sqlite3
import time
from contextlib import contextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = "story_game.db"


# ── Database ───────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                num_rounds INTEGER NOT NULL,
                round_duration INTEGER NOT NULL,
                current_round INTEGER NOT NULL DEFAULT 0,
                round_start_time REAL
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL,
                name TEXT NOT NULL,
                seat INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL,
                part_number INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                text_received TEXT NOT NULL DEFAULT '',
                text_submitted TEXT NOT NULL DEFAULT ''
            );
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def make_code(length=6):
    return "".join(random.choices(string.ascii_uppercase, k=length))


def part_label(part_number: int, n: int) -> str:
    return f"Part {part_number + 1}/{n}"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Game Manager ───────────────────────────────────────────────────────────────

class GameManager:
    def __init__(self):
        self.connections: dict[str, dict[int, WebSocket]] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.drafts: dict[str, dict[int, str]] = {}

    async def connect(self, game_id: str, player_id: int, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(game_id, {})[player_id] = ws
        self.drafts.setdefault(game_id, {})
        log(f"WS connect: game={game_id} player={player_id} "
            f"(total connected: {len(self.connections.get(game_id, {}))})")

    def disconnect(self, game_id: str, player_id: int):
        self.connections.get(game_id, {}).pop(player_id, None)
        log(f"WS disconnect: game={game_id} player={player_id}")

    def set_draft(self, game_id: str, player_id: int, text: str):
        self.drafts.setdefault(game_id, {})[player_id] = text
        log(f"Draft saved: game={game_id} player={player_id} chars={len(text)}")

    def get_draft(self, game_id: str, player_id: int) -> str:
        return self.drafts.get(game_id, {}).get(player_id, "")

    async def broadcast(self, game_id: str, message: dict):
        dead = []
        for pid, ws in list(self.connections.get(game_id, {}).items()):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(pid)
        for pid in dead:
            self.connections.get(game_id, {}).pop(pid, None)

    async def send_to(self, game_id: str, player_id: int, message: dict):
        ws = self.connections.get(game_id, {}).get(player_id)
        if ws:
            try:
                await ws.send_json(message)
                log(f"WS sent {message['type']} → game={game_id} player={player_id}")
            except Exception as e:
                log(f"WS send failed: game={game_id} player={player_id}: {e}")
                self.connections.get(game_id, {}).pop(player_id, None)
        else:
            log(f"WS send_to: no connection for game={game_id} player={player_id}")

    async def start_round(self, game_id: str):
        start_time = time.time()
        with get_db() as db:
            game = dict(db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone())
            players = [
                dict(p) for p in db.execute(
                    "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
                ).fetchall()
            ]
            n = len(players)
            current_round = game["current_round"]

            player_payloads = []
            for player in players:
                part_number = (player["seat"] + current_round - 1) % n
                prev = db.execute(
                    """SELECT text_submitted FROM entries
                       WHERE game_id = ? AND part_number = ? AND round_num < ?
                       ORDER BY round_num DESC LIMIT 1""",
                    (game_id, part_number, current_round),
                ).fetchone()
                player_payloads.append({
                    "player_id": player["id"],
                    "part_number": part_number,
                    "text_to_edit": prev["text_submitted"] if prev else "",
                })

            db.execute(
                "UPDATE games SET round_start_time = ? WHERE id = ?",
                (start_time, game_id),
            )
        # DB closed. Safe for async work.

        self.drafts[game_id] = {}
        log(f"start_round: game={game_id} round={current_round} "
            f"players={len(players)} duration={game['round_duration']}s "
            f"connected={list(self.connections.get(game_id, {}).keys())}")

        # Do NOT cancel self.timers[game_id]: if this was called from within the
        # timer task itself (via end_round), self-cancel raises CancelledError at
        # the next await, silently killing execution.
        self.timers[game_id] = asyncio.create_task(
            self._round_timer(game_id, game["round_duration"])
        )
        log(f"Timer task created for game={game_id} ({game['round_duration']}s)")

        for payload in player_payloads:
            await self.send_to(game_id, payload["player_id"], {
                "type": "round_start",
                "round": current_round,
                "num_rounds": game["num_rounds"],
                "num_parts": n,
                "duration": game["round_duration"],
                "start_time": start_time,
                "part_number": payload["part_number"],
                "text_to_edit": payload["text_to_edit"],
            })

    async def _round_timer(self, game_id: str, duration: int):
        grace = 3  # seconds between warning and capture
        log(f"Timer sleeping {duration}s for game={game_id}")
        await asyncio.sleep(max(0, duration - grace))
        # Signal clients to flush their textarea immediately
        await self.broadcast(game_id, {"type": "prepare_submit"})
        log(f"prepare_submit sent for game={game_id}, waiting {grace}s")
        await asyncio.sleep(grace)
        log(f"Timer fired for game={game_id}")
        try:
            await self.end_round(game_id)
        except Exception:
            import traceback
            traceback.print_exc()

    async def end_round(self, game_id: str):
        log(f"end_round: starting for game={game_id}")
        with get_db() as db:
            game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if not game or game["status"] != "active":
                log(f"end_round: game={game_id} not active (status={game['status'] if game else 'missing'}), skipping")
                return
            game = dict(game)
            players = [
                dict(p) for p in db.execute(
                    "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
                ).fetchall()
            ]

            n = len(players)
            current_round = game["current_round"]
            log(f"end_round: game={game_id} round={current_round} players={len(players)}")
            log(f"end_round: current drafts={list(self.drafts.get(game_id, {}).keys())}")

            for player in players:
                seat = player["seat"]
                part_number = (seat + current_round - 1) % n

                prev = db.execute(
                    """SELECT text_submitted FROM entries
                       WHERE game_id = ? AND part_number = ? AND round_num < ?
                       ORDER BY round_num DESC LIMIT 1""",
                    (game_id, part_number, current_round),
                ).fetchone()
                text_received = prev["text_submitted"] if prev else ""
                text_submitted = self.get_draft(game_id, player["id"])
                log(f"  player={player['id']} part={part_number} "
                    f"submitted={len(text_submitted)} chars")

                db.execute(
                    """INSERT INTO entries
                       (game_id, part_number, round_num, player_id, text_received, text_submitted)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (game_id, part_number, current_round, player["id"],
                     text_received, text_submitted),
                )

            next_round = current_round + 1
            if next_round > game["num_rounds"]:
                db.execute("UPDATE games SET status = 'complete' WHERE id = ?", (game_id,))
                log(f"end_round: game={game_id} COMPLETE")
            else:
                db.execute(
                    "UPDATE games SET current_round = ? WHERE id = ?",
                    (next_round, game_id),
                )
                log(f"end_round: game={game_id} advancing to round {next_round}")
        # DB committed.

        if next_round > game["num_rounds"]:
            await self.broadcast(game_id, {"type": "game_complete"})
        else:
            await self.start_round(game_id)


manager = GameManager()


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    log("Server started")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = ""):
    return templates.TemplateResponse("index.html", {"request": request, "error": error})


@app.post("/games/create")
async def create_game(
    name: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    num_rounds: int = Form(...),
    round_duration: int = Form(120),
):
    game_id = make_code()
    with get_db() as db:
        db.execute(
            "INSERT INTO games (id, title, description, num_rounds, round_duration) VALUES (?, ?, ?, ?, ?)",
            (game_id, title, description, num_rounds, round_duration),
        )
        result = db.execute(
            "INSERT INTO players (game_id, name, seat) VALUES (?, ?, 0)",
            (game_id, name),
        )
        player_id = result.lastrowid
    log(f"Game created: {game_id} by player {player_id} ({name})")
    return RedirectResponse(f"/lobby/{game_id}?pid={player_id}", status_code=303)


@app.post("/games/join")
async def join_game(
    game_id: str = Form(...),
    name: str = Form(...),
):
    game_id = game_id.strip().upper()
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            return RedirectResponse(f"/?error=Game+{game_id}+not+found", status_code=303)
        if game["status"] != "waiting":
            return RedirectResponse(f"/?error=Game+{game_id}+has+already+started", status_code=303)
        count = db.execute(
            "SELECT COUNT(*) as c FROM players WHERE game_id = ?", (game_id,)
        ).fetchone()["c"]
        result = db.execute(
            "INSERT INTO players (game_id, name, seat) VALUES (?, ?, ?)",
            (game_id, name, count),
        )
        player_id = result.lastrowid
    log(f"Player joined: {name} (id={player_id}) game={game_id}")
    return RedirectResponse(f"/lobby/{game_id}?pid={player_id}", status_code=303)


@app.get("/lobby/{game_id}", response_class=HTMLResponse)
async def lobby(request: Request, game_id: str, pid: int):
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        if game["status"] == "active":
            return RedirectResponse(f"/game/{game_id}?pid={pid}")
        if game["status"] == "complete":
            return RedirectResponse(f"/story/{game_id}?pid={pid}")
        players = db.execute(
            "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
        ).fetchall()
        player = db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
    return templates.TemplateResponse(
        "lobby.html",
        {
            "request": request,
            "game": dict(game),
            "players": [dict(p) for p in players],
            "player": dict(player),
            "is_host": player["seat"] == 0,
        },
    )


@app.post("/games/{game_id}/start")
async def start_game(game_id: str, pid: int = Form(...)):
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        player = db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
        if not game or not player or player["seat"] != 0:
            raise HTTPException(status_code=403)
        if game["status"] != "waiting":
            raise HTTPException(status_code=400, detail="Game already started")
        n_players = db.execute(
            "SELECT COUNT(*) as c FROM players WHERE game_id = ?", (game_id,)
        ).fetchone()["c"]
        if n_players < 2:
            return RedirectResponse(
                f"/lobby/{game_id}?pid={pid}&error=Need+at+least+2+players",
                status_code=303,
            )
        # Randomise which part each player starts on
        all_players = db.execute(
            "SELECT id FROM players WHERE game_id = ? ORDER BY id", (game_id,)
        ).fetchall()
        new_seats = list(range(n_players))
        random.shuffle(new_seats)
        for player_row, seat in zip(all_players, new_seats):
            db.execute("UPDATE players SET seat = ? WHERE id = ?", (seat, player_row["id"]))

        db.execute(
            "UPDATE games SET status = 'active', current_round = 1 WHERE id = ?",
            (game_id,),
        )
    log(f"Game starting: {game_id} with {n_players} players")
    await manager.start_round(game_id)
    return RedirectResponse(f"/game/{game_id}?pid={pid}", status_code=303)


@app.post("/games/{game_id}/draft")
async def save_draft(game_id: str, pid: int = Form(...), text: str = Form(...)):
    """HTTP fallback for draft saving — called periodically from the game page."""
    manager.set_draft(game_id, pid, text)
    return JSONResponse({"ok": True})


@app.get("/api/game/{game_id}/state")
async def game_state(game_id: str):
    with get_db() as db:
        game = db.execute(
            "SELECT status, current_round, round_start_time, round_duration FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
    if not game:
        raise HTTPException(status_code=404)
    return JSONResponse({
        "status": game["status"],
        "current_round": game["current_round"],
        "round_start_time": game["round_start_time"],
        "round_duration": game["round_duration"],
    })


@app.get("/game/{game_id}", response_class=HTMLResponse)
async def game_page(request: Request, game_id: str, pid: int):
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(status_code=404)
        if game["status"] == "complete":
            return RedirectResponse(f"/story/{game_id}?pid={pid}")
        if game["status"] == "waiting":
            return RedirectResponse(f"/lobby/{game_id}?pid={pid}")

        players = db.execute(
            "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
        ).fetchall()
        player = db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
        if not player:
            raise HTTPException(status_code=404)

        n = len(players)
        current_round = game["current_round"]
        seat = player["seat"]
        current_part = (seat + current_round - 1) % n

        prev = db.execute(
            """SELECT text_submitted FROM entries
               WHERE game_id = ? AND part_number = ? AND round_num < ?
               ORDER BY round_num DESC LIMIT 1""",
            (game_id, current_part, current_round),
        ).fetchone()
        text_to_edit = prev["text_submitted"] if prev else ""

        raw_history = db.execute(
            """SELECT part_number, round_num, text_received, text_submitted
               FROM entries WHERE game_id = ? AND player_id = ?
               ORDER BY round_num""",
            (game_id, pid),
        ).fetchall()

    parts_seen: dict[int, dict] = {}
    for row in raw_history:
        parts_seen[row["part_number"]] = {
            "text_submitted": row["text_submitted"],
            "text_received": row["text_received"],
            "round_num": row["round_num"],
        }

    return templates.TemplateResponse(
        "game.html",
        {
            "request": request,
            "game": dict(game),
            "player": dict(player),
            "n": n,
            "current_part": current_part,
            "text_to_edit": text_to_edit,
            "parts_seen": parts_seen,
            "start_time": game["round_start_time"] or time.time(),
            "part_label": part_label,
        },
    )


@app.get("/story/{game_id}", response_class=HTMLResponse)
async def story_page(request: Request, game_id: str, pid: int):
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(status_code=404)
        n = db.execute(
            "SELECT COUNT(*) as c FROM players WHERE game_id = ?", (game_id,)
        ).fetchone()["c"]
        parts = []
        for part_num in range(n):
            entry = db.execute(
                """SELECT text_submitted FROM entries
                   WHERE game_id = ? AND part_number = ?
                   ORDER BY round_num DESC LIMIT 1""",
                (game_id, part_num),
            ).fetchone()
            parts.append(entry["text_submitted"] if entry else "")
    return templates.TemplateResponse(
        "story.html",
        {
            "request": request,
            "game": dict(game),
            "parts": parts,
            "n": n,
            "part_label": part_label,
            "pid": pid,
        },
    )


@app.get("/history/{game_id}", response_class=HTMLResponse)
async def history_page(request: Request, game_id: str, pid: int):
    with get_db() as db:
        game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(status_code=404)
        n = db.execute(
            "SELECT COUNT(*) as c FROM players WHERE game_id = ?", (game_id,)
        ).fetchone()["c"]
        rows = db.execute(
            """SELECT e.part_number, e.round_num, e.text_submitted, p.name
               FROM entries e
               JOIN players p ON e.player_id = p.id
               WHERE e.game_id = ?
               ORDER BY e.part_number, e.round_num""",
            (game_id,),
        ).fetchall()
        num_rounds = game["num_rounds"]

    # grid[part_number][round_num] = {text, name}
    grid: dict[int, dict[int, dict]] = {}
    for row in rows:
        grid.setdefault(row["part_number"], {})[row["round_num"]] = {
            "text": row["text_submitted"],
            "name": row["name"],
        }

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "game": dict(game),
            "n": n,
            "num_rounds": num_rounds,
            "grid": grid,
            "part_label": part_label,
            "pid": pid,
        },
    )


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws/{game_id}/{player_id}")
async def websocket_endpoint(ws: WebSocket, game_id: str, player_id: int):
    await manager.connect(game_id, player_id, ws)

    with get_db() as db:
        player_list = [
            {"name": p["name"], "seat": p["seat"]}
            for p in db.execute(
                "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
            ).fetchall()
        ]
    await manager.broadcast(game_id, {"type": "lobby_update", "players": player_list})

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")
            if msg_type == "draft":
                manager.set_draft(game_id, player_id, data.get("text", ""))
    except WebSocketDisconnect:
        manager.disconnect(game_id, player_id)
    except Exception as e:
        log(f"WS error game={game_id} player={player_id}: {e}")
        manager.disconnect(game_id, player_id)
