"""
bot.py — entry point for the Two Truths and a Lie Telegram bot.

Run with:  python bot.py
Requires:  BOT_TOKEN in a .env file (see .env.example)

This file owns all Telegram I/O. Game rules/data live in game.py, persistence
lives in db.py. See the module docstring in game.py for the state machine.
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, BadRequest

import db
import game
from game import GameManager, GameState, Phase

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("two_truths_bot")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

manager = GameManager()

# Simple per-user rate limiting: user_id -> last command timestamp.
RATE_LIMIT_SECONDS = 2.0
_last_command_at: dict[int, float] = {}

# Submission phase: which player_id is allowed to DM right now, per chat — kept
# in GameState itself (submitting_player_id), but we also need a reverse map of
# user_id -> chat_id so an incoming DM knows which game it belongs to.
_dm_user_to_chat: dict[int, int] = {}

PING_TIMES_SUBMISSION = (30, 10)
PING_TIMES_VOTING = (15,)


# ============================================================ utilities ===

def username_of(user) -> str:
    return user.username and f"@{user.username}" or user.full_name


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    last = _last_command_at.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_command_at[user_id] = now
    return False


def persist(gs: GameState):
    db.save_snapshot(gs.chat_id, gs.to_dict())


def clear_persisted(chat_id: int):
    db.delete_snapshot(chat_id)


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Forbidden:
        logger.warning("Blocked / can't message chat %s", chat_id)
    except BadRequest as e:
        logger.warning("BadRequest sending to %s: %s", chat_id, e)


async def safe_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Forbidden:
        return None


def require_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def require_host(gs: GameState, user_id: int) -> bool:
    return gs.host_id == user_id


# ====================================================== /start (DM gate) ===

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        user = update.effective_user
        db.record_known_dm_user(user.id, username_of(user))
        bot_username = context.bot.username
        await update.message.reply_text(
            "✅ You're all set! I can now DM you when it's your turn.\n\n"
            "🎮 *What is Two Truths and a Lie?*\n"
            "In a group chat, each player privately sends me 2 true facts and 1 lie "
            "about themselves. I shuffle them and post them to the group, unlabeled — "
            "everyone else votes (and bets points) on which one they think is the lie. "
            "Fool people and you score; catch a lie and you score. After everyone's had "
            "a turn, there's a recap, a leaderboard, and some flavor awards.\n\n"
            "👉 Head back to your group chat and use /join when a lobby is open "
            "(the host starts one with /newgame).\n\n"
            f"💡 *Tip:* if the group has other bots too, Telegram lets you target mine "
            f"specifically by tagging it, e.g. `/join@{bot_username}` instead of just "
            f"`/join` — handy if commands aren't going through.\n\n"
            "Send /help any time for the full command list.",
            parse_mode=ParseMode.MARKDOWN,
        )


# =============================================================== /newgame ==

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    if not require_group(update):
        await update.message.reply_text("Start a game from a group chat, not a DM.")
        return

    chat_id = update.effective_chat.id
    existing = manager.get(chat_id)
    if existing and existing.phase != Phase.ENDED.value:
        await update.message.reply_text("A game is already in progress or a lobby is already open here.")
        return

    gs = manager.create(chat_id, host_id=user.id)
    gs.add_player(user.id, username_of(user))
    persist(gs)

    bot_username = context.bot.username
    await update.message.reply_text(
        f"🎉 New lobby opened by {username_of(user)}!\n\n"
        "Players: use /join to hop in (you must have DMed me first — tap my "
        "profile and hit Start if you haven't).\n"
        f"Host: use /settings to configure timers/min players (and /theme to set a theme), "
        f"then /begin once you have enough players.\n\n"
        "This lobby auto-cancels in 5 minutes if /begin isn't used.\n\n"
        f"💡 Lots of bots in this chat? Tag commands to me directly, e.g. "
        f"/join@{bot_username}, if a plain /join doesn't seem to register."
    )

    async def autocancel():
        await asyncio.sleep(game.LOBBY_AUTOCANCEL_SECONDS)
        cur = manager.get(chat_id)
        if cur and cur.phase == Phase.LOBBY.value and cur is gs:
            manager.remove(chat_id)
            clear_persisted(chat_id)
            await safe_send(context, chat_id, "⏱️ Lobby auto-cancelled — nobody started the game in time.")

    context.application.create_task(autocancel())


# ==================================================================== /join =

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    if not require_group(update):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase in (Phase.RECAP.value, Phase.ENDED.value):
        await update.message.reply_text("There's no open lobby or active game right now. Ask the host to run /newgame.")
        return
    if not db.has_dmed(user.id):
        await update.message.reply_text(
            f"{username_of(user)}, I need to be able to DM you first! "
            f"Open a private chat with me and send /start, then /join again."
        )
        return

    is_active_game = gs.phase != Phase.LOBBY.value
    if not gs.add_player(user.id, username_of(user), spectator=is_active_game):
        await update.message.reply_text(f"{username_of(user)} is already in the lobby.")
        return
    _dm_user_to_chat[user.id] = chat_id
    persist(gs)
    if is_active_game:
        await update.message.reply_text(
            f"👋 {username_of(user)} joined as a spectator — too late for their own turn this game, "
            f"but they can vote starting next round."
        )
    else:
        await update.message.reply_text(
            f"✅ {username_of(user)} joined! ({len(gs.players)} player{'s' if len(gs.players) != 1 else ''} so far)"
        )


# ================================================================ /settings =

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.LOBBY.value:
        await update.message.reply_text("Settings can only be changed before the game begins.")
        return
    if not require_host(gs, user.id):
        await update.message.reply_text("Only the host can change settings.")
        return

    keyboard = [
        [
            InlineKeyboardButton("Submission: 90s",  callback_data="set_sub_90"),
            InlineKeyboardButton("2 min",            callback_data="set_sub_120"),
            InlineKeyboardButton("3 min",            callback_data="set_sub_180"),
            InlineKeyboardButton("5 min",            callback_data="set_sub_300"),
        ],
        [
            InlineKeyboardButton("Voting: 45s",  callback_data="set_vote_45"),
            InlineKeyboardButton("90s",          callback_data="set_vote_90"),
            InlineKeyboardButton("2 min",        callback_data="set_vote_120"),
        ],
        [
            InlineKeyboardButton("Min players: 3", callback_data="set_min_3"),
            InlineKeyboardButton("4",              callback_data="set_min_4"),
            InlineKeyboardButton("5",              callback_data="set_min_5"),
        ],
        [
            InlineKeyboardButton("Rounds: 1", callback_data="set_rounds_1"),
            InlineKeyboardButton("2",         callback_data="set_rounds_2"),
            InlineKeyboardButton("3",         callback_data="set_rounds_3"),
            InlineKeyboardButton("5",         callback_data="set_rounds_5"),
        ],
    ]
    theme_line = f"Theme: {gs.settings['theme']}" if gs.settings.get("theme") else "Theme: none (use /theme <text> to set one)"
    rounds = gs.settings.get("rounds_per_player", 1)
    await update.message.reply_text(
        f"Current settings — submission: {gs.settings['submission_timer']}s, "
        f"voting: {gs.settings['voting_timer']}s, min players: {gs.settings['min_players']}, "
        f"rounds per player: {rounds}.\n"
        f"{theme_line}\nTap to change:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.LOBBY.value:
        await update.message.reply_text("Theme can only be set before the game begins.")
        return
    if not require_host(gs, user.id):
        await update.message.reply_text("Only the host can set the theme.")
        return
    text = " ".join(context.args).strip()
    if not text:
        gs.settings["theme"] = None
        await update.message.reply_text("Theme cleared — statements will be totally freeform again.")
    else:
        gs.settings["theme"] = text[:80]
        await update.message.reply_text(
            f"🎭 Theme set: \"{gs.settings['theme']}\". Submission prompts will mention it. "
            f"(/theme with no text clears it.)"
        )
    persist(gs)


async def cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or not require_host(gs, query.from_user.id):
        await query.answer("Only the host can do that.", show_alert=True)
        return
    data = query.data
    if data.startswith("set_sub_"):
        gs.settings["submission_timer"] = int(data.split("_")[-1])
    elif data.startswith("set_vote_"):
        gs.settings["voting_timer"] = int(data.split("_")[-1])
    elif data.startswith("set_min_"):
        gs.settings["min_players"] = int(data.split("_")[-1])
    elif data.startswith("set_rounds_"):
        gs.settings["rounds_per_player"] = int(data.split("_")[-1])
    persist(gs)
    await query.answer("Updated.")
    rounds = gs.settings.get("rounds_per_player", 1)
    await query.edit_message_text(
        f"Settings updated — submission: {gs.settings['submission_timer']}s, "
        f"voting: {gs.settings['voting_timer']}s, min players: {gs.settings['min_players']}, "
        f"rounds per player: {rounds}."
    )


# =================================================================== /begin =

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.LOBBY.value:
        await update.message.reply_text("No lobby to begin. Use /newgame first.")
        return
    if not require_host(gs, user.id):
        await update.message.reply_text("Only the host can start the game.")
        return
    min_players = gs.settings.get("min_players", 3)
    if len(gs.players) < min_players:
        await update.message.reply_text(f"Need at least {min_players} players to begin (currently {len(gs.players)}).")
        return

    gs.start_rounds()
    for uid in gs.players:
        _dm_user_to_chat[uid] = chat_id
    rounds = gs.settings.get("rounds_per_player", 1)
    names = ", ".join(gs.players[uid]["username"] for uid in gs.turn_order[:len(gs.players)])
    rounds_note = f" ({rounds} round{'s' if rounds > 1 else ''} each)" if rounds > 1 else ""
    await update.message.reply_text(f"🎲 Game starting{rounds_note}! Turn order: {names}")
    persist(gs)
    await start_next_round(update, context, chat_id)


# ============================================================= round flow ==

async def start_next_round(update_or_none, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    gs = manager.get(chat_id)
    if not gs:
        return
    if not gs.has_more_turns():
        await finish_game(context, chat_id)
        return

    player_id = gs.current_player_id()
    if player_id not in gs.players:
        # was kicked between turns
        gs.advance_turn()
        await start_next_round(None, context, chat_id)
        return

    username = gs.players[player_id]["username"]
    gs.begin_submission(player_id)
    persist(gs)

    await safe_send(
        context, chat_id,
        f"🫵 It's {username}'s turn! Sending them a DM to collect their 2 truths and 1 lie."
    )
    dm_ok = await safe_dm(
        context, player_id,
        "Your turn! Send me 2 TRUE statements and 1 LIE about yourself"
        + (f" — theme: \"{gs.settings['theme']}\"" if gs.settings.get("theme") else "")
        + " — as 3 separate messages, in any order. After all 3 are in, I'll ask you "
        f"to mark which one was the lie.\n\nYou have {gs.settings['submission_timer']} seconds.",
    )
    if dm_ok is None:
        # Can't DM them (blocked the bot) — skip immediately.
        await safe_send(context, chat_id, f"⚠️ I can't DM {username} — skipping their round.")
        gs.record_skipped_round(player_id)
        gs.advance_turn()
        persist(gs)
        await start_next_round(None, context, chat_id)
        return

    context.application.create_task(
        _timer_loop(
            context, chat_id, Phase.SUBMISSION.value, "submission_deadline",
            PING_TIMES_SUBMISSION,
            completion_fn=lambda g: g.submission_complete() and g.submitting_player_id == player_id,
            on_ping=lambda g, secs: safe_dm(context, player_id, f"⏱️ {secs}s left to finish your 3 statements!"),
            on_timeout=lambda g, completed: _submission_timeout(context, chat_id, player_id, completed),
        )
    )


async def _submission_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, player_id: int, completed: bool):
    gs = manager.get(chat_id)
    if not gs or gs.submitting_player_id != player_id:
        return
    # If phase already flipped to TAGGING, handle_dm_text already claimed this
    # — just let it go. We only act if still in SUBMISSION (timed out) or TAGGING
    # (timer fired after flip but before the callback, shouldn't happen but safe).
    if gs.phase == Phase.TAGGING.value:
        return
    if gs.phase != Phase.SUBMISSION.value:
        return
    if completed:
        await proceed_to_lie_tagging(context, chat_id)
        return
    username = gs.players.get(player_id, {}).get("username", "that player")
    await safe_send(context, chat_id, f"⌛ {username} didn't submit in time — skipping their round.")
    await safe_dm(context, player_id, "⌛ Time's up — your round was skipped.")
    gs.record_skipped_round(player_id)
    gs.advance_turn()
    persist(gs)
    await start_next_round(None, context, chat_id)


# --------------------------------------------- DM message handler (submit) -

async def handle_dm_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    chat_id = _dm_user_to_chat.get(user.id)
    if chat_id is None:
        return  # not part of any known game
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.SUBMISSION.value or gs.submitting_player_id != user.id:
        if gs and gs.phase == Phase.TAGGING.value and gs.submitting_player_id == user.id:
            await update.message.reply_text("Got all 3 already! Tap one of the buttons above to mark which was the lie.")
        return  # not their turn to submit, or wrong phase

    err = gs.add_submitted_statement(text)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    n = len(gs.submitted_statements)
    if n >= 3:
        # Flip the phase HERE, synchronously, before any `await`. This is the
        # one moment that matters: as long as this runs before we yield control
        # back to the event loop, the once-a-second timer task (which checks
        # `gs.phase != phase_name` first thing on every wake-up) is guaranteed
        # to see TAGGING and bail out instead of racing us to also fire the
        # lie-tagging prompt. Without this, both paths could detect "3rd
        # statement received" independently and send the prompt twice.
        gs.phase = Phase.TAGGING.value
    persist(gs)
    if n < 3:
        await update.message.reply_text(f"Got it ({n}/3). Send your next statement.")
    else:
        await proceed_to_lie_tagging(context, chat_id)


async def proceed_to_lie_tagging(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    gs = manager.get(chat_id)
    if not gs:
        return
    # Only proceed if the phase is TAGGING (set atomically in handle_dm_text).
    # Any other phase means someone else already handled this or the game moved on.
    if gs.phase != Phase.TAGGING.value:
        return
    player_id = gs.submitting_player_id
    keyboard = [
        [InlineKeyboardButton(f"#{i+1} was the lie", callback_data=f"tag_lie_{i}")]
        for i in range(3)
    ]
    lines = "\n".join(f"{i+1}. {s}" for i, s in enumerate(gs.submitted_statements))
    await safe_dm(
        context, player_id,
        f"Got all 3! Now tell me which one was the LIE:\n\n{lines}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_tag_lie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = _dm_user_to_chat.get(user_id)
    gs = manager.get(chat_id) if chat_id else None
    if not gs or gs.phase != Phase.TAGGING.value or gs.submitting_player_id != user_id or len(gs.submitted_statements) != 3:
        await query.answer("This isn't an active submission.", show_alert=True)
        return
    lie_idx = int(query.data.split("_")[-1])
    gs.set_shuffled_statements(gs.submitted_statements, lie_idx)
    persist(gs)
    await query.answer("Locked in!")

    if gs.can_double_down(user_id):
        keyboard = [
            [InlineKeyboardButton("🔥 Go high stakes (2x points, both ways)", callback_data="dd_yes")],
            [InlineKeyboardButton("Keep it normal", callback_data="dd_no")],
        ]
        await query.edit_message_text(
            "Want to go 🔥 **high stakes** on this round? You only get to use this once per "
            "game — it doubles the points you earn if you fool people, but does nothing to "
            "soften the blow if most people catch your lie.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text("✅ Got it — sending your statements to the group now...")
        await post_statements_for_voting(context, chat_id)


async def cb_double_down(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = _dm_user_to_chat.get(user_id)
    gs = manager.get(chat_id) if chat_id else None
    if not gs or gs.phase != Phase.TAGGING.value or gs.submitting_player_id != user_id:
        await query.answer("This isn't an active submission.", show_alert=True)
        return
    await query.answer()
    if query.data == "dd_yes" and gs.can_double_down(user_id):
        gs.set_high_stakes(user_id)
        await query.edit_message_text("🔥 High stakes locked in! Sending your statements to the group now...")
    else:
        await query.edit_message_text("✅ Got it — sending your statements to the group now...")
    persist(gs)
    await post_statements_for_voting(context, chat_id)


async def post_statements_for_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    gs = manager.get(chat_id)
    if not gs:
        return
    user_id = gs.submitting_player_id
    gs.begin_voting()
    persist(gs)

    username = gs.players[user_id]["username"]
    keyboard = [
        [
            InlineKeyboardButton(f"{letter} (bet 1)", callback_data=f"vote_{i}_1"),
            InlineKeyboardButton(f"{letter} (bet 2)", callback_data=f"vote_{i}_2"),
        ]
        for i, letter in enumerate(["A", "B", "C"])
    ]
    statement_lines = "\n".join(
        f"{letter}. {s['text']}" for letter, s in zip(["A", "B", "C"], gs.voting_statements)
    )
    stakes_note = "\n🔥 This round is HIGH STAKES — double points for the storyteller!" if gs.current_round_high_stakes else ""
    await safe_send(
        context, chat_id,
        f"🤔 {username}'s statements — which one is the LIE?\n\n{statement_lines}\n\n"
        f"Vote below! Bet 1 or 2 points on your confidence — win them if you're right, "
        f"lose them if you're wrong.{stakes_note}\n\nYou have {gs.settings['voting_timer']} seconds.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    context.application.create_task(
        _timer_loop(
            context, chat_id, Phase.VOTING.value, "voting_deadline",
            PING_TIMES_VOTING,
            completion_fn=lambda g: g.all_voted(),
            on_ping=lambda g, secs: safe_send(context, chat_id, f"⏰ {secs}s left to vote!"),
            on_timeout=lambda g, completed: _voting_timeout(context, chat_id, completed),
        )
    )


async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.VOTING.value:
        await query.answer("Voting isn't open right now.", show_alert=True)
        return
    voter_id = query.from_user.id
    if voter_id == gs.submitting_player_id:
        await query.answer("You can't vote on your own round!", show_alert=True)
        return
    if voter_id not in gs.eligible_voters:
        await query.answer("You're not part of this round's voting.", show_alert=True)
        return
    _, choice_str, wager_str = query.data.split("_")
    choice, wager = int(choice_str), int(wager_str)
    gs.record_vote(voter_id, choice, wager)
    persist(gs)
    await query.answer(f"Vote recorded: {['A','B','C'][choice]}, betting {wager} pt(s) (you can change it until voting closes)")


async def _voting_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, completed: bool):
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.VOTING.value:
        return
    await safe_send(context, chat_id, "🤔 Tallying votes...")
    await asyncio.sleep(2)

    record = gs.apply_round_scoring()
    lie_letter = ["A", "B", "C"][record.lie_index]
    storyteller = gs.players[record.player_id]["username"]

    def name_with_delta(uid):
        delta = record.point_deltas.get(uid, 0)
        sign = "+" if delta >= 0 else ""
        return f"{gs.players[uid]['username']} ({sign}{delta})"

    correct_names = ", ".join(name_with_delta(u) for u in record.correct_voters) or "nobody"
    fooled_names = ", ".join(name_with_delta(u) for u in record.fooled_voters) or "nobody"
    stakes_note = " 🔥(HIGH STAKES)" if record.high_stakes else ""

    msg = (
        f"🔎 Reveal!{stakes_note} The lie was {lie_letter}: \"{record.statements[record.lie_index]}\"\n\n"
        f"✅ Correctly caught it: {correct_names}\n"
        f"😵 Fooled: {fooled_names}\n"
    )
    if record.non_voters:
        msg += f"⌚ Didn't vote in time: {', '.join(gs.players[u]['username'] for u in record.non_voters)}\n"
    msg += f"\n{storyteller} earns {record.storyteller_points} point(s) for the bluff."
    for uid, streak in record.streak_callouts.items():
        msg += f"\n🔥 {gs.players[uid]['username']} is on a {streak}-round lie-catching streak!"
    await safe_send(context, chat_id, msg)

    gs.advance_turn()
    persist(gs)
    await start_next_round(None, context, chat_id)


# ============================================================== /pause etc =

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or not require_host(gs, user.id):
        await update.message.reply_text("Only the host can pause, and only during an active game.")
        return
    if gs.phase not in (Phase.SUBMISSION.value, Phase.TAGGING.value, Phase.VOTING.value):
        await update.message.reply_text("Nothing timed is currently running.")
        return
    if gs.paused:
        await update.message.reply_text("Already paused.")
        return
    deadline_attr = "voting_deadline" if gs.phase == Phase.VOTING.value else "submission_deadline"
    remaining = getattr(gs, deadline_attr) - time.time()
    gs.pause_remaining = max(remaining, 0)
    gs.paused = True
    persist(gs)
    await update.message.reply_text("⏸️ Timer paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or not require_host(gs, user.id):
        await update.message.reply_text("Only the host can resume.")
        return
    if not gs.paused:
        await update.message.reply_text("Nothing is paused.")
        return
    deadline_attr = "voting_deadline" if gs.phase == Phase.VOTING.value else "submission_deadline"
    setattr(gs, deadline_attr, time.time() + (gs.pause_remaining or 0))
    gs.paused = False
    persist(gs)
    await update.message.reply_text("▶️ Timer resumed.")


# ================================================================== /kick ==

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs:
        await update.message.reply_text("No active lobby or game here.")
        return
    if not require_host(gs, user.id):
        await update.message.reply_text("Only the host can kick players.")
        return

    target = None
    if update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset: ent.offset + ent.length].lstrip("@")
                target = next((uid for uid, p in gs.players.items() if p["username"].lstrip("@") == uname), None)
    if target is None and context.args:
        uname = context.args[0].lstrip("@")
        target = next((uid for uid, p in gs.players.items() if p["username"].lstrip("@") == uname), None)

    if target is None:
        await update.message.reply_text("Usage: /kick @username (they must be in the current lobby/game).")
        return
    if target == gs.host_id:
        await update.message.reply_text("The host can't kick themselves — leave the chat or /endgame instead.")
        return

    was_current_turn = gs.phase in (Phase.SUBMISSION.value, Phase.TAGGING.value, Phase.VOTING.value) and gs.current_player_id() == target
    name = gs.players[target]["username"]
    gs.remove_player(target)
    persist(gs)
    await update.message.reply_text(f"🚫 {name} was removed from the game.")

    if was_current_turn:
        if gs.phase in (Phase.SUBMISSION.value, Phase.TAGGING.value):
            gs.record_skipped_round(target)
        else:
            # Mid-voting kick of the storyteller: cancel this round with no scoring.
            await safe_send(context, chat_id, f"{name}'s round is cancelled since they were kicked mid-round.")
        gs.advance_turn()
        persist(gs)
        await start_next_round(None, context, chat_id)
    elif gs.phase == Phase.VOTING.value and target in gs.eligible_voters:
        gs.eligible_voters.remove(target)
        gs.votes.pop(target, None)
        persist(gs)


# ================================================================ /endgame =

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs:
        await update.message.reply_text("No active lobby or game here.")
        return
    if not require_host(gs, user.id):
        await update.message.reply_text("Only the host can force-end the game.")
        return
    await update.message.reply_text("🛑 Host force-ended the game.")
    await finish_game(context, chat_id)


async def finish_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    gs = manager.get(chat_id)
    if not gs:
        return
    gs.phase = Phase.RECAP.value

    lines = ["📋 **Final Recap**\n"]
    for rec in gs.round_history:
        if rec["skipped"]:
            lines.append(f"• {rec['username']}: round skipped (no submission).")
            continue
        lie = rec["statements"][rec["lie_index"]] if rec["lie_index"] >= 0 else "?"
        fooled_n = len(rec["fooled_voters"])
        stakes = " 🔥" if rec.get("high_stakes") else ""
        lines.append(
            f"• {rec['username']}'s lie was: \"{lie}\"{stakes} — fooled {fooled_n} player(s), "
            f"earned {rec.get('storyteller_points', fooled_n)} pt(s)."
        )
    lines.append("\n🏆 **Final Scores**")
    ranking = sorted(gs.scores.items(), key=lambda kv: kv[1], reverse=True)
    for i, (uid, pts) in enumerate(ranking, start=1):
        name = gs.players.get(uid, {}).get("username", "??")
        lines.append(f"{i}. {name} — {pts} pt{'s' if pts != 1 else ''}")

    awards = gs.compute_awards()
    lines.append("\n🎖️ **Awards**")
    for label, key in (("Best Bluffer", "best_bluffer"), ("Sharpest Eye", "sharpest_eye"), ("Easiest Read", "easiest_read")):
        ids = awards[key]
        names = ", ".join(gs.players.get(u, {}).get("username", "??") for u in ids) if ids else "—"
        lines.append(f"{label}: {names}")

    await safe_send(context, chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # Persist long-term leaderboard stats.
    deltas = {}
    for uid, p in gs.players.items():
        deltas[uid] = {
            "username": p["username"],
            "points": gs.scores.get(uid, 0),
            "bluffed_others": gs.fooled_count.get(uid, 0),
            "caught_lie": gs.caught_count.get(uid, 0),
            "best_bluffer": 1 if uid in awards["best_bluffer"] else 0,
            "sharpest_eye": 1 if uid in awards["sharpest_eye"] else 0,
            "easiest_read": 1 if uid in awards["easiest_read"] else 0,
            "played": 1,
        }
    db.apply_end_of_game_stats(chat_id, deltas)

    gs.last_game_player_ids = list(gs.players.keys())
    gs.phase = Phase.ENDED.value
    persist(gs)
    await safe_send(context, chat_id, "Game over! Use /rematch to play again with the same group, or /newgame to start fresh.")


# ================================================================ /rematch =

async def cmd_rematch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    if not gs or gs.phase != Phase.ENDED.value:
        await update.message.reply_text("There's no finished game to rematch here. Use /newgame instead.")
        return

    old_players = gs.last_game_player_ids
    new_gs = manager.create(chat_id, host_id=gs.host_id)
    for uid in old_players:
        uname = gs.players.get(uid, {}).get("username", "player")
        new_gs.add_player(uid, uname)
        new_gs.rematch_ready[uid] = False
        _dm_user_to_chat[uid] = chat_id
    persist(new_gs)

    keyboard = [[InlineKeyboardButton("✅ Ready", callback_data="rematch_ready")]]
    await update.message.reply_text(
        "🔁 Rematch lobby! Same players as last game — tap Ready below to confirm. "
        "Auto-cancels in 5 minutes if not everyone confirms.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    async def autocancel():
        await asyncio.sleep(game.LOBBY_AUTOCANCEL_SECONDS)
        cur = manager.get(chat_id)
        if cur is new_gs and cur.phase == Phase.LOBBY.value:
            manager.remove(chat_id)
            clear_persisted(chat_id)
            await safe_send(context, chat_id, "⏱️ Rematch lobby auto-cancelled — not everyone confirmed in time.")

    context.application.create_task(autocancel())


async def cb_rematch_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    gs = manager.get(chat_id)
    user_id = query.from_user.id
    if not gs or gs.phase != Phase.LOBBY.value or user_id not in gs.players:
        await query.answer("You're not part of this rematch.", show_alert=True)
        return
    gs.rematch_ready[user_id] = True
    persist(gs)
    ready_n = sum(1 for v in gs.rematch_ready.values() if v)
    total = len(gs.players)
    await query.answer("Marked ready!")
    if ready_n >= total:
        gs.start_rounds()
        await safe_send(context, chat_id, "✅ Everyone's ready! Starting the rematch.")
        persist(gs)
        await start_next_round(None, context, chat_id)
    else:
        await safe_send(context, chat_id, f"{ready_n}/{total} players ready.")


# ================================================================== /score =

async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    board = db.get_leaderboard(chat_id)
    if not board:
        await update.message.reply_text("No games have been played in this chat yet.")
        return
    lines = ["🏆 **Leaderboard** (this chat, all-time)\n"]
    for i, row in enumerate(board, start=1):
        lines.append(
            f"{i}. {row['username']} — {row['total_points']} pts "
            f"({row['games_played']} games, {row['best_bluffer_count']}🃏 {row['sharpest_eye_count']}👁️)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        return
    chat_id = update.effective_chat.id
    board = db.get_leaderboard(chat_id)
    row = next((r for r in board if r["user_id"] == user.id), None)
    if not row:
        await update.message.reply_text("You haven't played a finished game in this chat yet.")
        return
    games = row["games_played"] or 1
    fool_rate = row["times_bluffed_others"] / games
    catch_rate = row["times_caught_lie"] / games
    await update.message.reply_text(
        f"📊 **{row['username']}'s stats in this chat**\n\n"
        f"Games played: {row['games_played']}\n"
        f"Total points: {row['total_points']}\n"
        f"People fooled: {row['times_bluffed_others']} ({fool_rate:.1f}/game)\n"
        f"Lies caught: {row['times_caught_lie']} ({catch_rate:.1f}/game)\n"
        f"🃏 Best Bluffer awards: {row['best_bluffer_count']}\n"
        f"👁️ Sharpest Eye awards: {row['sharpest_eye_count']}\n"
        f"😅 Easiest Read awards: {row['easiest_read_count']}",
        parse_mode=ParseMode.MARKDOWN,
    )


# =================================================================== /help =

def build_help_text(bot_username: str) -> str:
    return f"""\
🎮 *Two Truths and a Lie*

Each round, one player privately submits 2 true statements and 1 lie about \
themselves. Everyone else votes on which one is the lie, betting 1 or 2 \
points on their confidence. The storyteller scores a point for every player \
they fool (doubled if they went 🔥 high stakes); each voter who catches the \
lie wins their bet, and loses it if they're fooled. Once everyone's had a \
turn, the game ends with a recap, final scores, and fun awards.

*Quickstart*
1. Everyone DMs me /start once (so I can message you privately later).
2. Host runs /newgame in the group.
3. Everyone runs /join.
4. Host runs /begin once there are enough players.

*Commands*
/newgame — open a lobby (host)
/join — join the lobby, or join an in-progress game as a spectator (DM me first!)
/settings — host: configure timers, min players
/theme <text> — host: set an optional theme for statements (no text clears it)
/begin — host: start the game
/kick @user — host: remove a player
/pause /resume — host: pause/resume the current timer
/endgame — host: force-end the game
/rematch — quick-restart with the same players
/score — this chat's all-time leaderboard
/stats — your personal lifetime stats in this chat
/help — this message

*Got other game bots in this chat?*
If your commands seem to go to the wrong bot (or nothing happens), tag me \
explicitly by adding my username after the command, e.g. \
`/join@{bot_username}` or `/begin@{bot_username}` instead of plain /join or \
/begin. Telegram routes tagged commands straight to the bot you named.

*Extras*
Each player can go 🔥 high stakes once per game on their own turn (double \
points, win or lose). Catch 3+ lies in a row and the bot will call out your \
streak. Players who join after the game has started come in as spectators — \
they can vote starting next round but don't get their own turn.
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_help_text(context.bot.username), parse_mode=ParseMode.MARKDOWN
    )


# ============================================================= timer loop ==

async def _timer_loop(context, chat_id, phase_name, deadline_attr, ping_seconds, completion_fn, on_ping, on_timeout):
    fired_pings = set()
    while True:
        await asyncio.sleep(1)
        gs = manager.get(chat_id)
        if gs is None or gs.phase != phase_name:
            return
        if gs.paused:
            continue
        if completion_fn(gs):
            await on_timeout(gs, True)
            return
        remaining = getattr(gs, deadline_attr) - time.time()
        if remaining <= 0:
            await on_timeout(gs, False)
            return
        for p in ping_seconds:
            if remaining <= p and p not in fired_pings:
                fired_pings.add(p)
                await on_ping(gs, p)


# ====================================================== crash recovery ====

async def register_commands(app: Application):
    """Register the command list with Telegram so the bot appears in the /
    autocomplete menu. Safe to skip if rate-limited — commands persist from
    the last successful registration."""
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
    from telegram.error import RetryAfter
    group_commands = [
        BotCommand("newgame",  "Open a new lobby"),
        BotCommand("join",     "Join the lobby or game as a spectator"),
        BotCommand("begin",    "Start the game (host only)"),
        BotCommand("settings", "Configure timers and min players (host only)"),
        BotCommand("theme",    "Set a theme for statements (host only)"),
        BotCommand("kick",     "Remove a player (host only)"),
        BotCommand("pause",    "Pause the active timer (host only)"),
        BotCommand("resume",   "Resume the active timer (host only)"),
        BotCommand("endgame",  "Force-end the game (host only)"),
        BotCommand("rematch",  "Quick-restart with the same players"),
        BotCommand("score",    "All-time leaderboard for this chat"),
        BotCommand("stats",    "Your personal lifetime stats"),
        BotCommand("help",     "Rules and command list"),
    ]
    private_commands = [
        BotCommand("start", "Register with the bot so it can DM you"),
        BotCommand("help",  "Rules and command list"),
        BotCommand("score", "All-time leaderboard"),
        BotCommand("stats", "Your personal lifetime stats"),
    ]
    try:
        await app.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("Registered bot commands with Telegram.")
    except RetryAfter as e:
        logger.warning(
            "Telegram rate-limited setMyCommands (retry in %ss) — skipping this startup. "
            "Commands from the last successful registration are still active.", e.retry_after
        )
    except Exception as e:
        logger.warning("Could not register commands: %s — continuing anyway.", e)


async def on_startup(app: Application):
    await register_commands(app)
    # Reload any in-progress games that were running before a restart.
    for chat_id in db.list_snapshot_chat_ids():
        data = db.load_snapshot(chat_id)
        if not data:
            continue
        gs = GameState.from_dict(data)
        if gs.phase == Phase.LOBBY.value:
            db.delete_snapshot(chat_id)
            continue
        manager.games[chat_id] = gs
        for uid in gs.players:
            _dm_user_to_chat[uid] = chat_id
        if gs.phase == Phase.SUBMISSION.value:
            player_id = gs.submitting_player_id
            gs.submission_deadline = time.time() + gs.settings["submission_timer"]
            persist(gs)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔄 I restarted after a hiccup — resuming "
                    f"{gs.players.get(player_id, {}).get('username', 'the current player')}'s submission "
                    f"with a fresh {gs.settings['submission_timer']}s timer. Anything they already sent is still saved."
                ),
            )
            app.create_task(
                _timer_loop(
                    _ctx_shim(app), chat_id, Phase.SUBMISSION.value, "submission_deadline",
                    PING_TIMES_SUBMISSION,
                    completion_fn=lambda g: g.submission_complete() and g.submitting_player_id == player_id,
                    on_ping=lambda g, secs: safe_dm(_ctx_shim(app), player_id, f"⏱️ {secs}s left to finish your 3 statements!"),
                    on_timeout=lambda g, completed: _submission_timeout(_ctx_shim(app), chat_id, player_id, completed),
                )
            )
        elif gs.phase == Phase.TAGGING.value:
            player_id = gs.submitting_player_id
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔄 I restarted — {gs.players.get(player_id, {}).get('username', 'the current player')}, "
                    f"check your DMs! I've re-sent you the prompt to pick which statement was your lie."
                ),
            )
            await proceed_to_lie_tagging(_ctx_shim(app), chat_id)
        elif gs.phase == Phase.VOTING.value:
            gs.voting_deadline = time.time() + gs.settings["voting_timer"]
            persist(gs)
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🔄 I restarted after a hiccup — resuming voting with a fresh {gs.settings['voting_timer']}s timer. Votes already cast are still counted."
            )
            app.create_task(
                _timer_loop(
                    _ctx_shim(app), chat_id, Phase.VOTING.value, "voting_deadline",
                    PING_TIMES_VOTING,
                    completion_fn=lambda g: g.all_voted(),
                    on_ping=lambda g, secs: app.bot.send_message(chat_id, f"⏰ {secs}s left to vote!"),
                    on_timeout=lambda g, completed: _voting_timeout(_ctx_shim(app), chat_id, completed),
                )
            )
        else:
            await app.bot.send_message(chat_id, "🔄 Bot restarted — game state restored, continuing where we left off.")
        logger.info("Recovered game for chat %s in phase %s", chat_id, gs.phase)
    for chat_id in db.list_snapshot_chat_ids():
        data = db.load_snapshot(chat_id)
        if not data:
            continue
        gs = GameState.from_dict(data)

        if gs.phase == Phase.LOBBY.value:
            # Un-started lobbies (incl. rematch "Ready" lobbies) aren't resumed —
            # see README "Recovery limits". Just drop the stale snapshot.
            db.delete_snapshot(chat_id)
            continue

        manager.games[chat_id] = gs
        for uid in gs.players:
            _dm_user_to_chat[uid] = chat_id

        if gs.phase in (Phase.SUBMISSION.value,):
            # Mid-submission: restart timer, keep any statements already collected.
            player_id = gs.submitting_player_id
            gs.submission_deadline = time.time() + gs.settings["submission_timer"]
            persist(gs)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔄 I restarted after a hiccup — resuming "
                    f"{gs.players.get(player_id, {}).get('username', 'the current player')}'s submission "
                    f"with a fresh {gs.settings['submission_timer']}s timer. Anything they already sent is still saved."
                ),
            )
            app.create_task(
                _timer_loop(
                    _ctx_shim(app), chat_id, Phase.SUBMISSION.value, "submission_deadline",
                    PING_TIMES_SUBMISSION,
                    completion_fn=lambda g: g.submission_complete() and g.submitting_player_id == player_id,
                    on_ping=_submission_ping,
                    on_timeout=lambda g, completed: _submission_timeout(_ctx_shim(app), chat_id, player_id, completed),
                )
            )
        elif gs.phase == Phase.TAGGING.value:
            # All 3 statements were received but the player hadn't tagged the lie yet
            # when the bot crashed. Re-send the tagging DM so they can continue.
            player_id = gs.submitting_player_id
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔄 I restarted — {gs.players.get(player_id, {}).get('username', 'the current player')}, "
                    f"check your DMs! I've re-sent you the prompt to pick which statement was your lie."
                ),
            )
            await proceed_to_lie_tagging(_ctx_shim(app), chat_id)
        elif gs.phase == Phase.VOTING.value:
            gs.voting_deadline = time.time() + gs.settings["voting_timer"]
            persist(gs)
            await app.bot.send_message(
                chat_id,
                f"🔄 I restarted after a hiccup — resuming voting with a fresh {gs.settings['voting_timer']}s timer. "
                f"Votes already cast are still counted."
            )
            app.create_task(
                _timer_loop(
                    _ctx_shim(app), chat_id, Phase.VOTING.value, "voting_deadline",
                    PING_TIMES_VOTING,
                    completion_fn=lambda g: g.all_voted(),
                    on_ping=lambda g, secs: app.bot.send_message(chat_id, f"⏰ {secs}s left to vote!"),
                    on_timeout=lambda g, completed: _voting_timeout(_ctx_shim(app), chat_id, completed),
                )
            )
        else:
            await app.bot.send_message(chat_id, "🔄 Bot restarted — game state restored, continuing where we left off.")
        logger.info("Recovered game for chat %s in phase %s", chat_id, gs.phase)


class _ctx_shim:
    """Minimal shim so timer/helper functions written for `ContextTypes.DEFAULT_TYPE`
    (which only ever use `context.bot` and `context.application.create_task`) also
    work when called from recover_games(), where we only have the Application."""
    def __init__(self, app: Application):
        self.bot = app.bot
        self.application = app


# =================================================================== main ==

def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set. Copy .env.example to .env and fill in your token.")

    db.init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("theme", cmd_theme))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("endgame", cmd_endgame))
    app.add_handler(CommandHandler("rematch", cmd_rematch))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("leaderboard", cmd_score))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(CallbackQueryHandler(cb_settings, pattern=r"^set_(sub|vote|min|rounds)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tag_lie, pattern=r"^tag_lie_\d$"))
    app.add_handler(CallbackQueryHandler(cb_double_down, pattern=r"^dd_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(cb_vote, pattern=r"^vote_\d_\d$"))
    app.add_handler(CallbackQueryHandler(cb_rematch_ready, pattern=r"^rematch_ready$"))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_dm_text))

    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Starting Two Truths and a Lie bot...")
    application.run_polling()
