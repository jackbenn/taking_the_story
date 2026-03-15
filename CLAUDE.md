# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# open http://localhost:8000
```

## Stack

- **Backend:** FastAPI (`main.py`) + SQLite (`story_game.db`, auto-created on startup)
- **Templates:** Jinja2 (`templates/`) — server-rendered HTML, no JS framework
- **Realtime:** WebSocket at `/ws/{game_id}/{player_id}` — one persistent connection per player
- **Styles:** Plain CSS (`static/style.css`)

## Game mechanics

Players (N) each own a story **part** (0-indexed). Parts rotate each round:

> In round R, player with seat S edits **part `(S + R − 1) mod N`**

Round 1 = blank text box. Subsequent rounds = pre-populated with the previous submission for that part.

The server timer (`asyncio.create_task`) fires `end_round()`, which saves all in-memory drafts to `entries`, increments `current_round`, then calls `start_round()` for the next round (or broadcasts `game_complete`).

## WebSocket message contract

Server → client:
- `lobby_update` — player list changed (broadcast)
- `round_start` — new round; includes `part_number`, `text_to_edit`, `start_time`, `duration` (per-player)
- `game_complete` — game over, redirect to story page

Client → server:
- `draft` — current textarea content (debounced ~1s + every 10s); server stores in `GameManager.drafts`

## Key data

```
games(id, title, description, status, num_rounds, round_duration, current_round, round_start_time)
players(id, game_id, name, seat)
entries(id, game_id, part_number, round_num, player_id, text_received, text_submitted)
```

`seat` determines rotation order. `seat == 0` means host (only the host can start the game).

## Page flow

```
/                       → create or join
/lobby/{code}?pid=N     → waiting room (WebSocket connected)
/game/{code}?pid=N      → active rounds (page reloads on round_start message)
/story/{code}?pid=N     → final assembled story
```

The game page is **server-rendered on each round** (client reloads on `round_start`). The countdown timer runs client-side using `server_start_time` injected into the template.
