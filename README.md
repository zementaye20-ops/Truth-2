# Two Truths and a Lie — Telegram Bot

A group-chat party game bot. Each round, one player privately sends the bot
two true statements and one lie about themselves; everyone else votes on
which statement is the lie; points are awarded for good bluffing and good
detection. Runs as a long-lived process from the command line.

## 1. Create a bot with @BotFather

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts (pick a name and a unique username
   ending in `bot`).
3. BotFather will reply with an API token that looks like
   `123456789:AAExampleTokenABCxyz...`. Keep this private.
4. Optional but recommended: send `/setprivacy` to BotFather, select your
   bot, and choose **Disable**. This isn't required for this bot (it only
   reads DMs and inline-button taps, not group messages), but disabling
   privacy mode is handy if you later add features that read group text.

## 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Requires Python 3.11+.

## 3. Configure the token

```bash
cp .env.example .env
```

Edit `.env` and paste your token:

```
BOT_TOKEN=123456789:AAExampleTokenABCxyz...
```

The token is loaded via `python-dotenv` and is never hardcoded or logged.

## 4. Run it

```bash
python bot.py
```

The bot starts long-polling. Add it to a group chat (and give it permission
to send messages), then:

1. Every player **must first DM the bot** at least once and send `/start` —
   this is how the bot gets permission to message them privately for their
   submissions. `/join` will tell you if you haven't done this yet.
2. In the group: `/newgame` to open a lobby.
3. Players use `/join` to hop in.
4. The host can run `/settings` to adjust timer lengths.

If your group has other bots in it and a command seems to be silently
ignored, tag this bot explicitly the same way Telegram supports for any
bot — append `@YourBotUsername` to the command, e.g. `/join@YourBotUsername`
instead of plain `/join`. This routes the command to this bot specifically
instead of letting Telegram (or another bot) try to claim it. `/start` and
`/help` both remind players of this and fill in the bot's actual username
automatically.
5. Once there are 3+ players, the host runs `/begin`.

## How a round works

1. The bot DMs the current player and asks for **2 true statements and 1
   lie**, sent as **three separate messages**, in any order. (We chose
   three separate messages over a single multi-line message because it's
   far more forgiving on mobile — no worrying about typos turning one
   statement into two lines — and it lets the bot validate and acknowledge
   each statement individually as it arrives.)
2. After all three are in, the bot asks the player (via inline buttons,
   still in DM) which one was the lie. This is necessary because the player
   submitted them "in any order they like" — the bot has no other way to
   know which is the lie.
3. The three statements are shuffled and posted to the group as A/B/C with
   no labels. Every player except the storyteller votes.
4. When voting closes (everyone's voted, or the timer runs out), the bot
   reveals the lie and the scores for that round.

## Scoring

- The storyteller scores **1 point for every player who picked wrong**
  (i.e., who they fooled) — **doubled** if they went 🔥 high stakes (see
  below).
- Each voter bets **1 or 2 points** on their guess when they vote: correct
  guesses **win** the bet, wrong guesses **lose** it.
- Scores accumulate across all rounds of a single game and are shown on the
  final leaderboard. Yes, this means scores can go negative — that's part
  of the risk of confidence betting.

At the end of the game (after everyone's had a turn) the bot posts a recap
of every round, the final score ranking, and three flavor awards with no
gameplay effect:

- **Best Bluffer** — fooled the most people in total across the game.
- **Sharpest Eye** — correctly caught the most lies as a voter.
- **Easiest Read** — among players who actually had a turn, fooled the
  *fewest* people with their lie.

Ties produce multiple winners for an award.

## Extra mechanics

- **Confidence betting** — when voting, each player picks a statement
  *and* a wager (1 or 2 points). It's a small risk/reward layer on top of
  the base 1-point-per-correct-guess rule.
- **High stakes (double down)** — right after submitting their statements,
  each player gets a one-time-per-game option to flag their round as
  🔥 high stakes, doubling the points they earn from fooling people. There's
  no downside multiplier — if their lie gets caught by everyone, they just
  earn 0 either way — so it's a pure "I'm confident in this bluff" play.
- **Streaks** — the bot tracks each player's consecutive correct lie-catches
  within a game and calls out a streak of 3+ in the reveal message. Streaks
  reset on a miss or a non-vote.
- **Themed rounds** — the host can run `/theme <text>` (e.g.
  `/theme embarrassing moments`) before `/begin` to nudge what kind of
  statements players submit. The theme shows up in each player's submission
  DM. `/theme` with no text clears it.
- **Spectator mode** — `/join` still works after a game has started. Late
  joiners are added as spectators: they can't have a storytelling turn
  this game (turn order is locked in at `/begin`), but they vote and score
  normally starting with the next round.
- **Configurable minimum players** — `/settings` includes a "min players"
  option (3/4/5) in addition to the submission/voting timers.

## Admin / host controls

- `/settings` — timer lengths (lobby only).
- `/kick @username` — remove a player. If it's currently their turn, the
  round is skipped/cancelled with no scoring; otherwise they're just
  dropped from the remaining turn order.
- `/pause` / `/resume` — freeze and unfreeze whatever timer (submission or
  voting) is currently running.
- `/endgame` — force-end the game immediately and post the recap built from
  whatever rounds have happened so far.
- If the host leaves the chat or is kicked, host privileges automatically
  transfer to whichever remaining player joined earliest, and the bot
  announces the handoff.

## Persistence & crash recovery

All data lives in a local SQLite database (`two_truths.db`, created
automatically on first run):

- **Long-term leaderboard** — per chat, per user: games played, total
  points, times you fooled someone, times you caught a lie, and counts of
  each award. Survives restarts and is shown with `/score` or
  `/leaderboard`; `/stats` shows just your own numbers (plus per-game
  averages) instead of the whole chat.
- **Live game snapshots** — after every meaningful state change (someone
  joins, a statement is submitted, a vote is cast, a round ends, etc.) the
  full in-progress game state for that chat is written to SQLite. On
  startup, the bot reloads any in-progress games and resumes them.

### Recovery limits

- Timers are **not** preserved across a restart — they restart fresh at
  their configured full length (e.g., a voting round that had 10 seconds
  left will get a brand-new full-length window after recovery). Already
  cast votes / already submitted statements are kept.
- If the bot crashes **mid-submission** (player has sent some but not all
  of their 2 truths + 1 lie, or has submitted all 3 but not yet tagged the
  lie), recovery keeps whatever statements were already received and gives
  the player a fresh submission timer to finish up. If they had already
  tagged the lie and voting had started, that part is unaffected.
- Lobbies that haven't started a game yet (`/newgame` was run but `/begin`
  wasn't) and rematch "Ready" lobbies are **not** persisted across a
  restart — if the bot restarts while a lobby is open, players need to run
  `/newgame` again. (Only games that have actually begun rounds are
  snapshotted for recovery purposes; this keeps the recovery logic simple
  and avoids resurrecting stale, half-formed lobbies from days earlier.)

## Rate limiting

Each user is limited to roughly one command every 2 seconds. Excess
commands are silently ignored to prevent spam from corrupting lobby,
submission, or voting state.

## Project structure

```
bot.py            entry point — all Telegram I/O, handlers, timers
game.py           state machine, validation, scoring, awards (no Telegram code)
db.py             SQLite persistence: leaderboard + crash-recovery snapshots
requirements.txt  dependencies
.env.example      template for your bot token
```

## Commands

| Command | Who | Description |
|---|---|---|
| `/newgame` | anyone | Open a lobby (auto-cancels in 5 min if unused) |
| `/join` | anyone | Join the open lobby (must have DMed the bot first) |
| `/settings` | host | Configure submission/voting timer lengths, min players |
| `/theme <text>` | host | Set (or clear) an optional theme for statements |
| `/begin` | host | Start the game (needs the configured minimum players) |
| `/kick @user` | host | Remove a player |
| `/pause` / `/resume` | host | Pause/resume the active timer |
| `/endgame` | host | Force-end the game and post the recap so far |
| `/rematch` | anyone | Quick-restart with the same players |
| `/score` / `/leaderboard` | anyone | This chat's all-time leaderboard |
| `/stats` | anyone | Your own lifetime stats in this chat |
| `/help` | anyone | Rules and command list |
