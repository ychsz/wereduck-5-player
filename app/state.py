from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from . import roles as R


class Phase(str, Enum):
    WAITING = "waiting"          # awaiting players to sit
    READY = "ready"              # between rounds, awaiting ready
    NIGHT = "night"              # night action FSM running
    DAY_SPEECH = "day_speech"    # round-robin speech
    DAY_VOTE = "day_vote"        # voting
    GAME_OVER = "game_over"


@dataclass
class Player:
    seat: int
    name: str = ""
    role: Optional[str] = None
    alive: bool = True
    ready: bool = False
    # night transient
    piped: bool = False              # in pipe this night
    in_belly_of: Optional[int] = None  # seat of pelican
    died_night: bool = False         # actually killed this night
    died_day: bool = False           # killed this day (vote/assassinate)
    # role-specific persistent
    vigilante_used: bool = False
    cupid_arrow_used: bool = False
    pelican_used: bool = False
    pelican_belly: list[int] = field(default_factory=list)  # seats swallowed (alive in belly)
    falcon_used: bool = False
    pigeon_piped: bool = False       # pigeon chose pipe this night
    pigeon_infected: list[int] = field(default_factory=list)  # seats infected
    lobbyist_charges: int = 0        # unused kill opportunities
    lobbyist_votes: int = 0          # accumulated votes received (for charge calc)
    lobbyist_votes_since_charge: int = 0
    lobbyist_killed_tonight: bool = False
    spy_intel: dict[int, str] = field(default_factory=dict)  # seat -> role_id learned
    priest_double_target: Optional[int] = None  # seat to double NEXT day's vote
    priest_double_consumed: bool = False
    vulture_count: int = 0
    raven_target_role: Optional[str] = None  # assigned each day
    raven_used_today: bool = False
    magpie_correct: list[int] = field(default_factory=list)  # seats correctly guessed
    assassin_used_today: bool = False
    # night-action state
    night_action_done: bool = False
    tracker_target: Optional[int] = None
    detective_target: Optional[int] = None
    # intel messages revealed to this player (night results, etc.)
    intel_msgs: list[str] = field(default_factory=list)
    # connection
    ws: Any = None
    online: bool = True
    secret: str = ""                 # reconnect token

    @property
    def is_duck(self) -> bool:
        return self.role is not None and R.faction_of(self.role) == "duck"

    @property
    def is_goose(self) -> bool:
        return self.role is not None and R.faction_of(self.role) == "goose"

    @property
    def is_neutral(self) -> bool:
        return self.role is not None and R.faction_of(self.role) == "neutral"

    def visible_dead(self) -> bool:
        """Whether OTHER players perceive this player as 'out' (dead)."""
        if not self.alive:
            return True
        return self.in_belly_of is not None

    def active_in_chain(self) -> bool:
        """Whether the player is active in the adjacency chain (alive, not piped, not in belly)."""
        if not self.alive:
            return False
        if self.in_belly_of is not None:
            return False
        return True


@dataclass
class ChatMsg:
    seat: Optional[int]   # None = system
    name: str
    text: str
    ts: float = field(default_factory=time.time)
    system: bool = False


class GameState:
    """Per-room game state. Single-threaded async access (one event loop)."""

    def __init__(self):
        self.players: dict[int, Player] = {}  # seat 1..5 -> Player
        self.phase: Phase = Phase.WAITING
        self.day_count: int = 0
        self.chat: list[ChatMsg] = []
        # night FSM
        self.night_steps: list[tuple[str, str]] = []
        self.night_cursor: int = 0
        self.night_current_seat: Optional[int] = None  # whose turn
        self.night_deadline: float = 0.0
        # day speech
        self.speech_index: int = 0          # index into speaking order list
        self.speech_order: list[int] = []   # seats
        self.speech_deadline: float = 0.0
        # voting
        self.votes: dict[int, int] = {}     # voter_seat -> target_seat (-1 = abstain)
        self.vote_deadline: float = 0.0
        # pending assassinations from day speech (assassin, raven)
        self.pending_assassin: list[tuple[int, int, str]] = []  # (assassin_seat, target_seat, guessed_role_id)
        self.pending_raven: list[tuple[int, int, str]] = []
        # magpie guesses (private, resolved immediately)
        # cupid lovers
        self.lover_pair: Optional[tuple[int, int]] = None
        # night prompt (computed by resolver, shown to active night player)
        self.night_prompt: Optional[dict] = None
        # snapshot of perceived-alive status at night start; closed-eye players
        # see this frozen view until day breaks or it becomes their turn
        self.night_snapshot: dict[int, bool] = {}
        # winners
        self.winners: list[int] = []        # seats who won
        self.win_reason: str = ""
        # day vote results stored for spy intel
        self.last_vote_counts: dict[int, int] = {}
        self.last_vote_who_voted: dict[int, list[int]] = {}  # target_seat -> list of voter seats

    def occupied_seats(self) -> list[int]:
        return sorted(s for s, p in self.players.items() if p.name)

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive and p.in_belly_of is None]

    def all_seated(self) -> bool:
        return len(self.occupied_seats()) == 5

    def get(self, seat: int) -> Optional[Player]:
        return self.players.get(seat)

    def reset_round(self) -> None:
        """Reset for a new round (new role assignment)."""
        self.day_count = 0
        self.chat.clear()
        self.phase = Phase.READY
        self.winners = []
        self.win_reason = ""
        self.lover_pair = None
        self.night_prompt = None
        self.night_snapshot = {}
        self.night_cursor = 0
        self.night_current_seat = None
        self.speech_index = 0
        self.speech_order = []
        self.votes = {}
        self.pending_assassin = []
        self.pending_raven = []
        self.last_vote_counts = {}
        self.last_vote_who_voted = {}
        for p in self.players.values():
            p.alive = True
            p.ready = False
            p.role = None
            p.piped = False
            p.in_belly_of = None
            p.died_night = False
            p.died_day = False
            p.vigilante_used = False
            p.cupid_arrow_used = False
            p.pelican_used = False
            p.pelican_belly = []
            p.falcon_used = False
            p.pigeon_piped = False
            p.pigeon_infected = []
            p.lobbyist_charges = 0
            p.lobbyist_votes = 0
            p.lobbyist_votes_since_charge = 0
            p.lobbyist_killed_tonight = False
            p.spy_intel = {}
            p.priest_double_target = None
            p.priest_double_consumed = False
            p.vulture_count = 0
            p.raven_target_role = None
            p.raven_used_today = False
            p.magpie_correct = []
            p.assassin_used_today = False
            p.night_action_done = False
            p.tracker_target = None
            p.detective_target = None
            p.intel_msgs = []

    def reset_night_transient(self) -> None:
        for p in self.players.values():
            p.piped = False
            p.died_night = False
            p.lobbyist_killed_tonight = False
            p.pigeon_piped = False
            p.night_action_done = False
            p.tracker_target = None
            p.detective_target = None
            for attr in ("_did_kill_tonight", "_killed_by_role"):
                if hasattr(p, attr):
                    delattr(p, attr)

    def reset_day_transient(self) -> None:
        for p in self.players.values():
            p.died_day = False
            p.assassin_used_today = False
            p.raven_used_today = False
            p.priest_double_consumed = False

    def system_chat(self, text: str) -> None:
        self.chat.append(ChatMsg(seat=None, name="系统", text=text, system=True))

    def add_chat(self, seat: int, text: str) -> None:
        p = self.players[seat]
        self.chat.append(ChatMsg(seat=seat, name=p.name, text=text))
