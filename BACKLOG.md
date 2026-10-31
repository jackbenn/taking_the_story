# Backlog — taking_the_story

Prefix: STORY · Next ID: 3 · Schema: v1

## [STORY-1] WebSocket URL is hardcoded as `ws://`, so other players stay in the lobby on https

- **Status:** done
- **Priority:** P1
- **Size:** quick
- **Depends on:** none
- **Tags:** bug, ui
- **Commits:** 360af96

`templates/lobby.html:44` and `templates/game.html:115` open
`` new WebSocket(`ws://${location.host}...`) ``. On https bennetto.com the
browser refuses a `ws://` connection from a secure page and the constructor
throws, which ends the script. In both templates the polling fallback
(`/api/game/{id}/state` every 4s) is set up after that line, so it never runs
either.

Seen live on 2026-10-31: after the host started the game it began for them (a
form POST and redirect), but the other player was left in the lobby. On the game
page, rounds probably do not advance for anyone; drafts still save, as their
`setInterval` comes before the socket.

Likely fix: choose the scheme from `location.protocol` (`wss` on https), and set
up the polling before opening the socket so a failed socket cannot disable it.
nginx already forwards the Upgrade headers for `/story/`.

### Acceptance criteria
- [x] On bennetto.com, a second player moves from the lobby to the game when the host starts
- [x] Rounds advance on every player's page
- [x] Polling still runs if the socket cannot connect

### Notes
- 2026-10-31: Fixed and deployed (infra a272692). Checked in a stub browser on https: the old lobby script threw before setting up any polling; the new one opens `wss://` with polling running, and keeps polling even with the socket forced to `ws://`. Live: `wss://bennetto.com/story/ws/...` answers 101 through nginx. Not yet seen: a two-player game in a real browser on bennetto.com.
- 2026-10-31: Jack confirmed live that the game starts and works for both players. The polling criterion rests on the stub-browser test above, not a live failure.

## [STORY-2] No tests

- **Status:** idea
- **Priority:** unknown
- **Size:** session
- **Depends on:** none
- **Tags:** test

There are no tests. The part rotation (`calc_part`, including `reverse_order`)
and the round lifecycle (`start_round`, `end_round`) are the obvious first
candidates.
