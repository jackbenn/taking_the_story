# Taking the Story

A web-paged multi-player storywriting experience

Live at <https://bennetto.com/story/>.

## Getting started

### Setup

Python 3.9 or later; the deployed image runs 3.11. `uvicorn==0.34.0` will not
install on 3.8, so if `python3 --version` is older, name a newer interpreter
(`python3.11`) in its place.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Run

```bash
.venv/bin/uvicorn main:app --reload
```

Then open <http://localhost:8000/story/>. Every route is under `/story`, so
`http://localhost:8000/` is a 404. To play locally with more than one player,
open the game in a second browser window and join with the code.

### Test

There are no tests yet (STORY-2 in `BACKLOG.md`).

### Not in this repo

Nothing is needed from outside. The SQLite database, `story_game.db`, is
created in the current directory on first start and is ignored by git; delete
it to start afresh.

The deployment lives in `bennetto-infra`, of which this repo is a submodule:
`docker/taking-the-story.Dockerfile` builds it, the database sits in the
`taking_the_story_data` volume, and nginx proxies `/story/` to it. Deploy
through that repo, not from here.

## How it works

### Stack

- **Backend:** FastAPI (`main.py`) and SQLite (`story_game.db`, its schema
  created on startup)
- **Templates:** Jinja2 (`templates/`), server-rendered HTML with a little
  inline JavaScript and no framework
- **Realtime:** a WebSocket at `/story/ws/{game_id}/{player_id}`, one per
  player, used only for pushes from the server, with polling of
  `/story/api/game/{game_id}/state` every 4 seconds as a fallback
- **Styles:** plain CSS (`static/style.css`)

### Game mechanics

N players each own a story **part** (0-indexed). Parts rotate each round: in
round R, the player in seat S edits part `(S + R − 1) mod N`, or
`(S − R + 1) mod N` when the host chose reverse order (`calc_part`).

Round 1 starts from a blank text box; each later round starts from the previous
submission for that part.

A server timer (`asyncio.create_task`) runs each round. Three seconds before
the end it broadcasts `prepare_submit`, so clients flush their drafts; then
`end_round()` saves every in-memory draft to `entries`, increments
`current_round`, and either calls `start_round()` for the next round or marks
the game complete and broadcasts `game_complete`.

### Messages

Server to client, over the WebSocket:

- `lobby_update`: the player list changed
- `round_start`: a new round has begun; the game page reloads
- `prepare_submit`: the round is about to end; send the draft now
- `game_complete`: the game is over; go to the final story

Client to server: drafts are POSTed to `/story/games/{game_id}/draft` (form
fields `pid`, `text`) two seconds after typing stops and every five seconds,
and held in `GameManager.drafts` until the round ends.

### Data

```
games(id, title, description, status, num_rounds, round_duration, current_round, round_start_time, reverse_order)
players(id, game_id, name, seat)
entries(id, game_id, part_number, round_num, player_id, text_received, text_submitted)
```

`seat` sets the rotation order; seat 0 is the host, and only the host can start
the game.

### Pages

```
/story/                      create or join a game
/story/how-to-play           the rules
/story/lobby/{code}?pid=N    waiting room
/story/game/{code}?pid=N     the current round (reloaded each round)
/story/final/{code}?pid=N    the finished story
/story/history/{code}?pid=N  every round's text, part by part
```

The game page is rendered by the server for each round. Its countdown runs in
the browser from the round's start time, injected into the template.
