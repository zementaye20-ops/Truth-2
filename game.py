"""
game.py — state machine and pure game logic for Two Truths and a Lie.

This module intentionally has ZERO telegram-specific code. It owns the data
model (GameState) and the rules (validation, scoring, turn order, awards).
bot.py drives the state machine by calling into this module and handling all
Telegram I/O (sending messages, inline keyboards, timers).

-----------------------------------------------------------------------------
STATE MACHINE
-----------------------------------------------------------------------------
LOBBY
  -> (host /begin, >=3 players) -> SUBMISSION (for turn_order[0])
SUBMISSION (current player DMs 2 truths + 1 lie, or times out)
  -> TAGGING once all 3 statements are in (player marks which one is the lie)
TAGGING
  -> VOTING (statements shuffled, posted to group)
VOTING (other players vote A/B/C, or timer expires)
  -> REVEAL (scores applied, recap line posted)
REVEAL
  -> if more players left in turn_order: SUBMISSION (next player)
  -> else: RECAP (final leaderboard + awards) -> ENDED

ENDED
  -> /rematch -> LOBBY (same players, "Ready" confirmation) -> ... 
-----------------------------------------------------------------------------
"""

import random
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    LOBBY = "LOBBY"
    SUBMISSION = "SUBMISSION"
    TAGGING = "TAGGING"   # all 3 statements in, waiting on the player to mark which is the lie
    VOTING = "VOTING"
    REVEAL = "REVEAL"
    RECAP = "RECAP"
    ENDED = "ENDED"


MAX_STATEMENT_LEN = 200
DEFAULT_SUBMISSION_TIMER = 90
DEFAULT_VOTING_TIMER = 45
LOBBY_AUTOCANCEL_SECONDS = 5 * 60


@dataclass
class RoundRecord:
    """Frozen record of one completed round, used for the final recap."""
    player_id: int
    username: str
    statements: list          # the 3 statements in their ORIGINAL submitted order [s0, s1] truths, [lie] at lie_index
    lie_index: int            # index into `statements` (0,1,2) of the lie, in ORIGINAL order
    fooled_voters: list       # user_ids who guessed wrong
    correct_voters: list      # user_ids who guessed right
    non_voters: list          # eligible voters who never voted
    skipped: bool = False     # True if the player timed out / was kicked and round was skipped
    high_stakes: bool = False         # storyteller went "double down" this round
    storyteller_points: int = 0       # points the storyteller actually earned (post-multiplier)
    wagers: dict = field(default_factory=dict)        # voter_id -> wager amount (1 or 2)
    point_deltas: dict = field(default_factory=dict)  # voter_id -> net points gained/lost this round
    streak_callouts: dict = field(default_factory=dict)  # voter_id -> streak length, for voters who hit a notable streak


@dataclass
class GameState:
    chat_id: int
    host_id: int
    phase: str = Phase.LOBBY.value

    # players: user_id -> {"username": str, "joined_order": int}
    players: dict = field(default_factory=dict)

    settings: dict = field(default_factory=lambda: {
        "submission_timer": DEFAULT_SUBMISSION_TIMER,
        "voting_timer": DEFAULT_VOTING_TIMER,
        "min_players": 3,
        "theme": None,          # e.g. "embarrassing moments" — included in the submission prompt
    })

    turn_order: list = field(default_factory=list)      # list of user_ids, consumed as rounds complete
    completed_turn_order: list = field(default_factory=list)  # for recap ordering
    current_turn_idx: int = 0

    # current submission in progress
    submitting_player_id: Optional[int] = None
    submitted_statements: list = field(default_factory=list)  # list of str, in submission order
    submission_deadline: Optional[float] = None

    # current voting round
    voting_statements: list = field(default_factory=list)   # shuffled [{"text":..., "is_lie":bool}]
    votes: dict = field(default_factory=dict)                # voter_id -> chosen index (0/1/2)
    voting_deadline: Optional[float] = None
    eligible_voters: list = field(default_factory=list)      # snapshot of who could vote this round

    scores: dict = field(default_factory=dict)               # user_id -> int, THIS game only
    fooled_count: dict = field(default_factory=dict)         # user_id -> # times they fooled someone (storyteller stat)
    caught_count: dict = field(default_factory=dict)         # user_id -> # times they caught a lie (voter stat)

    round_history: list = field(default_factory=list)        # list of RoundRecord (as dicts)

    current_streak: dict = field(default_factory=dict)       # user_id -> consecutive correct catches (this game)
    best_streak: dict = field(default_factory=dict)          # user_id -> best streak reached (this game)
    used_double_down: list = field(default_factory=list)     # user_ids who already used their one double-down
    current_round_high_stakes: bool = False                  # is the IN-PROGRESS round flagged double-down

    paused: bool = False
    pause_remaining: Optional[float] = None  # seconds left on whatever timer was running, set on pause

    lobby_created_at: float = field(default_factory=time.time)

    last_game_player_ids: list = field(default_factory=list)  # for /rematch
    rematch_ready: dict = field(default_factory=dict)          # user_id -> bool, used during rematch lobby

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "GameState":
        gs = GameState(chat_id=d["chat_id"], host_id=d["host_id"])
        for k, v in d.items():
            setattr(gs, k, v)
        return gs

    # ----------------------------------------------------------- lobby ----

    def add_player(self, user_id: int, username: str, spectator: bool = False) -> bool:
        if user_id in self.players:
            return False
        self.players[user_id] = {
            "username": username,
            "joined_order": len(self.players),
            "spectator": spectator,
        }
        self.scores.setdefault(user_id, 0)
        self.fooled_count.setdefault(user_id, 0)
        self.caught_count.setdefault(user_id, 0)
        self.current_streak.setdefault(user_id, 0)
        self.best_streak.setdefault(user_id, 0)
        return True

    def remove_player(self, user_id: int) -> bool:
        if user_id not in self.players:
            return False
        del self.players[user_id]
        if user_id in self.turn_order:
            self.turn_order.remove(user_id)
        return True

    def next_host(self) -> Optional[int]:
        """Earliest-joined remaining player becomes host. Returns new host_id or None."""
        remaining = sorted(self.players.items(), key=lambda kv: kv[1]["joined_order"])
        return remaining[0][0] if remaining else None

    # ------------------------------------------------------------ turns ---

    def start_rounds(self):
        ids = [uid for uid, p in self.players.items() if not p.get("spectator")]
        random.shuffle(ids)
        self.turn_order = ids
        self.current_turn_idx = 0
        self.completed_turn_order = []

    def current_player_id(self) -> Optional[int]:
        if 0 <= self.current_turn_idx < len(self.turn_order):
            return self.turn_order[self.current_turn_idx]
        return None

    def advance_turn(self):
        if self.current_player_id() is not None:
            self.completed_turn_order.append(self.current_player_id())
        self.current_turn_idx += 1

    def has_more_turns(self) -> bool:
        return self.current_turn_idx < len(self.turn_order)

    # ------------------------------------------------------- submission ---

    @staticmethod
    def validate_statement(text: str, existing: list) -> Optional[str]:
        """Return an error message string, or None if valid."""
        stripped = text.strip()
        if not stripped:
            return "Statement can't be empty."
        if len(stripped) > MAX_STATEMENT_LEN:
            return f"Statement is too long (max {MAX_STATEMENT_LEN} characters)."
        if any(stripped.lower() == s.strip().lower() for s in existing):
            return "That's a duplicate of one you already sent — statements must be distinct."
        return None

    def begin_submission(self, player_id: int):
        self.phase = Phase.SUBMISSION.value
        self.submitting_player_id = player_id
        self.submitted_statements = []
        self.submission_deadline = time.time() + self.settings["submission_timer"]
        self.current_round_high_stakes = False

    def can_double_down(self, player_id: int) -> bool:
        return player_id not in self.used_double_down

    def set_high_stakes(self, player_id: int):
        self.current_round_high_stakes = True
        self.used_double_down.append(player_id)

    def add_submitted_statement(self, text: str) -> Optional[str]:
        err = self.validate_statement(text, self.submitted_statements)
        if err:
            return err
        self.submitted_statements.append(text.strip())
        return None

    def submission_complete(self) -> bool:
        return len(self.submitted_statements) >= 3

    # ----------------------------------------------------------- voting ---

    def begin_voting(self):
        """Shuffle the 3 submitted statements (last one entered is treated as
        the lie ONLY if the caller marked it; in this game design the bot
        does not know which is the lie from raw submission order alone — see
        bot.py: the player explicitly tags the lie via inline buttons right
        after submitting all three, before this is called)."""
        self.phase = Phase.VOTING.value
        self.votes = {}
        self.eligible_voters = [
            uid for uid in self.players if uid != self.submitting_player_id
        ]
        self.voting_deadline = time.time() + self.settings["voting_timer"]

    def set_shuffled_statements(self, statements: list, lie_index: int):
        """statements: list of 3 strings in ORIGINAL submitted order.
        lie_index: index of the lie in that original order.
        Shuffles into voting_statements with is_lie flags, and labels A/B/C
        order randomized so the lie's position is unpredictable."""
        order = list(range(3))
        random.shuffle(order)
        self.voting_statements = [
            {"text": statements[i], "is_lie": (i == lie_index)} for i in order
        ]

    def lie_position(self) -> int:
        for i, s in enumerate(self.voting_statements):
            if s["is_lie"]:
                return i
        return -1

    def record_vote(self, voter_id: int, choice_idx: int, wager: int = 1) -> bool:
        if voter_id not in self.eligible_voters:
            return False
        wager = 2 if wager == 2 else 1
        self.votes[voter_id] = {"choice": choice_idx, "wager": wager}
        return True

    def all_voted(self) -> bool:
        return set(self.eligible_voters) <= set(self.votes.keys())

    # ----------------------------------------------------------- scoring --

    def apply_round_scoring(self) -> RoundRecord:
        lie_idx = self.lie_position()
        multiplier = 2 if self.current_round_high_stakes else 1
        fooled, correct, non_voters = [], [], []
        wagers, point_deltas, streak_callouts = {}, {}, {}

        for voter_id in self.eligible_voters:
            if voter_id not in self.votes:
                non_voters.append(voter_id)
                self.current_streak[voter_id] = 0
                continue
            vote = self.votes[voter_id]
            wager = vote["wager"]
            wagers[voter_id] = wager
            if vote["choice"] == lie_idx:
                correct.append(voter_id)
                delta = wager
                self.scores[voter_id] = self.scores.get(voter_id, 0) + delta
                self.caught_count[voter_id] = self.caught_count.get(voter_id, 0) + 1
                self.current_streak[voter_id] = self.current_streak.get(voter_id, 0) + 1
                self.best_streak[voter_id] = max(
                    self.best_streak.get(voter_id, 0), self.current_streak[voter_id]
                )
                if self.current_streak[voter_id] >= 3:
                    streak_callouts[voter_id] = self.current_streak[voter_id]
            else:
                fooled.append(voter_id)
                delta = -wager
                self.scores[voter_id] = self.scores.get(voter_id, 0) + delta
                self.current_streak[voter_id] = 0
            point_deltas[voter_id] = delta

        storyteller_id = self.submitting_player_id
        storyteller_points = len(fooled) * multiplier
        self.scores[storyteller_id] = self.scores.get(storyteller_id, 0) + storyteller_points
        self.fooled_count[storyteller_id] = self.fooled_count.get(storyteller_id, 0) + len(fooled)

        original_statements = [s["text"] for s in self.voting_statements]
        record = RoundRecord(
            player_id=storyteller_id,
            username=self.players[storyteller_id]["username"],
            statements=original_statements,
            lie_index=lie_idx,
            fooled_voters=fooled,
            correct_voters=correct,
            non_voters=non_voters,
            high_stakes=self.current_round_high_stakes,
            storyteller_points=storyteller_points,
            wagers=wagers,
            point_deltas=point_deltas,
            streak_callouts=streak_callouts,
        )
        self.round_history.append(asdict(record))
        return record

    def record_skipped_round(self, player_id: int):
        record = RoundRecord(
            player_id=player_id,
            username=self.players[player_id]["username"],
            statements=[],
            lie_index=-1,
            fooled_voters=[],
            correct_voters=[],
            non_voters=[],
            skipped=True,
        )
        self.round_history.append(asdict(record))

    # ------------------------------------------------------------ awards --

    def compute_awards(self) -> dict:
        """Returns {"best_bluffer": [user_ids], "sharpest_eye": [...], "easiest_read": [...]}
        Ties are allowed (multiple winners)."""
        if not self.players:
            return {"best_bluffer": [], "sharpest_eye": [], "easiest_read": []}

        def top_ids(d: dict, reverse: bool):
            real = {uid: v for uid, v in d.items() if uid in self.players}
            if not real:
                return []
            best = max(real.values()) if reverse else min(real.values())
            return [uid for uid, v in real.items() if v == best]

        # Easiest Read: fewest people fooled BUT only among players who actually had a turn (fooled_count present
        # and they had a non-skipped round). We approximate using fooled_count where a turn happened.
        had_turn = {r["player_id"] for r in self.round_history if not r.get("skipped")}
        easiest_pool = {uid: v for uid, v in self.fooled_count.items() if uid in had_turn}

        return {
            "best_bluffer": top_ids(self.fooled_count, reverse=True),
            "sharpest_eye": top_ids(self.caught_count, reverse=True),
            "easiest_read": (
                [uid for uid, v in easiest_pool.items() if v == min(easiest_pool.values())]
                if easiest_pool else []
            ),
        }


class GameManager:
    """In-memory registry of active GameStates, one per chat_id."""

    def __init__(self):
        self.games: dict[int, GameState] = {}

    def get(self, chat_id: int) -> Optional[GameState]:
        return self.games.get(chat_id)

    def create(self, chat_id: int, host_id: int) -> GameState:
        gs = GameState(chat_id=chat_id, host_id=host_id)
        self.games[chat_id] = gs
        return gs

    def remove(self, chat_id: int):
        self.games.pop(chat_id, None)
