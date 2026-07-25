"""Cinematic rarity is the feature — these tests are the anti-spam machinery.

A title card that fires every third turn makes the game feel worse than one
that never fires at all, so every limit is enforced against the `cinematics`
table (not an in-process counter that a page reload would clear). The tests
below drive both clocks by hand: the world clock for story-time cooldowns and
a fake wall clock for the real-time floor and the rolling session window.
"""

from datetime import datetime, timedelta

import pytest

from src.models import Cinematic, DMState, NPC, NPCTier, Player, WorldClock, get_session
from src.models.campaign import CINEMATIC_LABELS
from src.services import cinematics as cine
from src.services.cinematics import (
    CAMPAIGN_CINEMATIC_MAP,
    DM_CALLABLE,
    DURATIONS_MS,
    LIMITS,
    PAYLOAD_FIELDS,
    REJECT_REASONS,
    SESSION_WINDOW_MIN,
    VOCABULARY,
    WEATHER_LABELS,
    boss_intro_payload,
    drain_pending,
    recent,
    request_cinematic,
)


class FakeWallClock:
    """Controllable stand-in for real time (the service reads `_now()`)."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 7, 25, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float = 0.0, minutes: float = 0.0) -> None:
        self.t += timedelta(seconds=seconds, minutes=minutes)


@pytest.fixture()
def wall(monkeypatch):
    """Freeze the wall clock so the real-time limits are deterministic."""
    clock = FakeWallClock()
    monkeypatch.setattr(cine, "_now", clock)
    return clock


@pytest.fixture()
def world(db, wall):
    """A minimal initialized world: clock, DM state, one player."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=8, minute=0))
        session.add(DMState())
        p = Player(name="Tester")
        session.add(p)
        session.commit()
        return p.id


def advance_game(hours: float) -> None:
    """Move the story clock (minutes are tracked, so fractions accumulate)."""
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(hours)
        session.commit()


def weather_now() -> str | None:
    with get_session() as session:
        state = session.query(DMState).first()
        return state.weather


def ledger() -> list[Cinematic]:
    """Every row, oldest first."""
    with get_session() as session:
        return session.query(Cinematic).order_by(Cinematic.created_at.asc()).all()


def make_npc(**kwargs) -> str:
    kwargs.setdefault("name", "Coldiron")
    kwargs.setdefault("tier", NPCTier.MAJOR)
    with get_session() as session:
        npc = NPC(**kwargs)
        session.add(npc)
        session.commit()
        return npc.id


def get_npc(npc_id: str) -> NPC:
    with get_session() as session:
        return session.get(NPC, npc_id)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

def test_vocabulary_and_duration_tables_agree():
    """Every kind has a duration; every rate-limited kind is one the DM can ask for."""
    assert set(DURATIONS_MS) == set(VOCABULARY)
    assert set(DM_CALLABLE) <= set(VOCABULARY)
    assert set(LIMITS) == set(DM_CALLABLE)
    assert "kill_cam" not in DM_CALLABLE      # combat-internal, never DM-callable
    for kind, row in LIMITS.items():
        assert len(row) == 4, kind
        assert all(isinstance(n, int) and n >= 0 for n in row), kind


def test_campaign_cinematic_map_covers_every_label():
    """A campaign beat's label must always resolve — silently or to a kind."""
    assert set(CAMPAIGN_CINEMATIC_MAP) == set(CINEMATIC_LABELS)
    for label, kind in CAMPAIGN_CINEMATIC_MAP.items():
        assert kind is None or kind in DM_CALLABLE, label
    assert CAMPAIGN_CINEMATIC_MAP["act_open"] == "title_card"
    assert CAMPAIGN_CINEMATIC_MAP["finale"] == "title_card"
    assert CAMPAIGN_CINEMATIC_MAP["countdown_expired"] == "title_card"
    # Revelation/betrayal/arrival ride the campaign toast + prose in v1
    assert CAMPAIGN_CINEMATIC_MAP["revelation"] is None
    assert CAMPAIGN_CINEMATIC_MAP["betrayal"] is None
    assert CAMPAIGN_CINEMATIC_MAP["arrival"] is None
    assert CAMPAIGN_CINEMATIC_MAP["none"] is None


# --------------------------------------------------------------------------
# Accept / reject shape
# --------------------------------------------------------------------------

def test_accepted_request_returns_payload(world):
    result = request_cinematic("title_card", player_id=world,
                               title="THE ROAD NORTH", subtitle="Three days from Emberfall")
    assert result["ok"] is True
    assert set(result["payload"]) == set(PAYLOAD_FIELDS)
    assert result["payload"]["kind"] == "title_card"
    assert result["payload"]["title"] == "THE ROAD NORTH"
    assert result["payload"]["subtitle"] == "Three days from Emberfall"
    assert result["payload"]["duration_ms"] == 4200
    assert "event" not in result       # the tool adds the event key, not the service


def test_rejected_result_has_no_event_key_and_says_nothing(world):
    """A rejection must be invisible: nothing for the client, silence from the DM."""
    request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")
    result = request_cinematic("title_card", player_id=world, title="THE ROAD SOUTH")

    assert result["ok"] is False
    assert result["reason"] in REJECT_REASONS
    assert "event" not in result
    assert "payload" not in result
    assert "prose" in result["instruction"]
    assert "Do NOT" in result["instruction"]


def test_unknown_kind_rejected(world):
    result = request_cinematic("explosion", player_id=world, title="BOOM")
    assert result["reason"] == "unknown_kind"


def test_kill_cam_is_not_dm_callable(world):
    """The DM cannot fire the kill cam; the combat loop can."""
    assert request_cinematic("kill_cam", player_id=world)["reason"] == "not_dm_callable"
    assert request_cinematic("kill_cam", player_id=world, dm_callable_only=False)["ok"] is True


def test_title_card_without_title_rejected(world):
    assert request_cinematic("title_card", player_id=world)["reason"] == "missing_title"
    assert request_cinematic("title_card", player_id=world, title="   ")["reason"] == "missing_title"


# --------------------------------------------------------------------------
# The four stacked limits
# --------------------------------------------------------------------------

def test_real_time_floor_blocks_two_in_one_turn(world, wall):
    """Two different kinds back to back — the second is refused on wall time."""
    assert request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")["ok"] is True
    assert request_cinematic("vision", player_id=world)["reason"] == "too_soon"

    wall.advance(seconds=LIMITS["vision"][2])
    assert request_cinematic("vision", player_id=world)["ok"] is True


def test_title_card_game_time_cooldown(world, wall):
    """Twelve story hours between chapter breaks, regardless of wall time."""
    advance_game(12)                     # day 1, 20:00
    assert request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")["ok"] is True

    wall.advance(minutes=1)
    advance_game(4)                      # day 2, 00:00 — new day, but only 4h later
    result = request_cinematic("title_card", player_id=world, title="THE LONG DARK")
    assert result["reason"] == "cooldown_game_time"

    wall.advance(minutes=1)
    advance_game(9)                      # day 2, 09:00 — 13h since the first card
    assert request_cinematic("title_card", player_id=world, title="THE LONG DARK")["ok"] is True


def test_daily_cap_allows_one_card_per_game_day(world, wall):
    """Cooldown satisfied, still refused: most days deserve no title card at all."""
    assert request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")["ok"] is True

    wall.advance(minutes=1)
    advance_game(12)                     # day 1, 20:00 — 720 story minutes later
    result = request_cinematic("title_card", player_id=world, title="THE LONG DARK")
    assert result["reason"] == "daily_cap"

    wall.advance(minutes=1)
    advance_game(13)                     # day 2, 09:00
    assert request_cinematic("title_card", player_id=world, title="THE LONG DARK")["ok"] is True


def test_session_cap_over_rolling_window(world, wall):
    """Four cards inside an hour of real time is already generous; the fifth waits."""
    titles = ["ONE", "TWO", "THREE", "FOUR", "FIVE"]
    for title in titles[:LIMITS["title_card"][3]]:
        assert request_cinematic("title_card", player_id=world, title=title)["ok"] is True
        wall.advance(minutes=1)
        advance_game(24)

    result = request_cinematic("title_card", player_id=world, title=titles[-1])
    assert result["reason"] == "session_cap"

    # The window rolls: once the earlier cards age out, the budget returns
    wall.advance(minutes=SESSION_WINDOW_MIN + 1)
    assert request_cinematic("title_card", player_id=world, title=titles[-1])["ok"] is True


def test_duplicate_title_is_rejected(world, wall):
    """Same words, same card — a chapter only opens once."""
    assert request_cinematic("title_card", player_id=world, title="The Road North")["ok"] is True
    wall.advance(minutes=5)
    advance_game(48)
    result = request_cinematic("title_card", player_id=world, title="  THE ROAD NORTH  ")
    assert result["reason"] == "duplicate"


def test_rejections_do_not_consume_the_budget(world, wall):
    """A refused request must not count against the caps it just failed."""
    request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")
    for _ in range(3):
        request_cinematic("title_card", player_id=world, title="THE LONG DARK")

    wall.advance(minutes=1)
    advance_game(24)
    assert request_cinematic("title_card", player_id=world, title="THE LONG DARK")["ok"] is True


def test_world_not_initialized_is_refused(db, wall):
    """No clock, no cooldowns — refuse rather than play unthrottled."""
    with get_session() as session:
        player = Player(name="Tester")
        session.add(player)
        session.commit()
        player_id = player.id

    result = request_cinematic("title_card", player_id=player_id, title="THE ROAD NORTH")
    assert result["reason"] == "world_not_initialized"


# --------------------------------------------------------------------------
# Boss entrances
# --------------------------------------------------------------------------

def test_boss_intro_fires_once_per_boss(world, wall):
    """One entrance per character, ever — a rematch is just a fight."""
    boss = make_npc(name="Coldiron", danger_tier="boss")
    other = make_npc(name="Ashgrim", danger_tier="boss")

    assert request_cinematic("boss_intro", player_id=world, npc_id=boss)["ok"] is True

    wall.advance(minutes=30)
    advance_game(72)
    result = request_cinematic("boss_intro", player_id=world, npc_id=boss)
    assert result["reason"] == "boss_already_introduced"

    assert request_cinematic("boss_intro", player_id=world, npc_id=other)["ok"] is True


def test_boss_intro_payload_uses_danger_tier_and_epithet(world):
    """`danger_tier` is the flag; `boss_title` is only the words under the name."""
    boss_id = make_npc(name="Coldiron", danger_tier="boss",
                       boss_title="Warden of the Ashen Gate")
    payload = boss_intro_payload(get_npc(boss_id), world)

    assert payload["kind"] == "boss_intro"
    assert payload["title"] == "Coldiron"
    assert payload["subtitle"] == "Warden of the Ashen Gate"
    assert payload["npc_id"] == boss_id
    assert payload["duration_ms"] == DURATIONS_MS["boss_intro"]


def test_boss_intro_payload_falls_back_to_profession(world):
    boss_id = make_npc(name="Ashgrim", danger_tier="boss", profession="gate warden")
    assert boss_intro_payload(get_npc(boss_id), world)["subtitle"] == "gate warden"


def test_boss_intro_payload_none_for_ordinary_npc(world):
    """An epithet without the tier does nothing — no accidental entrances."""
    plain_id = make_npc(name="Mira", tier=NPCTier.MINOR, profession="baker")
    titled_id = make_npc(name="Pretender", tier=NPCTier.MINOR,
                         boss_title="Warden of Nothing")

    assert boss_intro_payload(get_npc(plain_id), world) is None
    assert boss_intro_payload(get_npc(titled_id), world) is None
    assert boss_intro_payload(None, world) is None


def test_boss_intro_payload_none_on_rematch(world, wall):
    boss_id = make_npc(name="Coldiron", danger_tier="boss")
    assert boss_intro_payload(get_npc(boss_id), world) is not None

    wall.advance(minutes=30)
    advance_game(48)
    assert boss_intro_payload(get_npc(boss_id), world) is None


# --------------------------------------------------------------------------
# Weather is world state
# --------------------------------------------------------------------------

def test_weather_label_menu_enforced(world):
    assert request_cinematic("weather", player_id=world, mood="apocalypse")["reason"] == "bad_weather_label"
    assert request_cinematic("weather", player_id=world)["reason"] == "bad_weather_label"
    assert request_cinematic("weather", player_id=world, mood="rain")["ok"] is True
    assert "rain" in WEATHER_LABELS


def test_weather_sets_dm_state_and_clear_nulls_it(world, wall):
    assert request_cinematic("weather", player_id=world, mood="RAIN")["ok"] is True
    assert weather_now() == "rain"

    wall.advance(minutes=1)
    advance_game(1)
    assert request_cinematic("weather", player_id=world, mood="clear")["ok"] is True
    assert weather_now() is None


def test_weather_same_label_twice_is_duplicate(world, wall):
    """Setting the weather that is already there is a no-op the DM tries often."""
    request_cinematic("weather", player_id=world, mood="rain")
    wall.advance(minutes=1)
    advance_game(2)

    result = request_cinematic("weather", player_id=world, mood="rain")
    assert result["reason"] == "duplicate"
    assert weather_now() == "rain"

    # ...and clearing weather that is already clear is equally a no-op
    wall.advance(minutes=1)
    advance_game(2)
    assert request_cinematic("weather", player_id=world, mood="clear")["ok"] is True
    wall.advance(minutes=1)
    advance_game(2)
    assert request_cinematic("weather", player_id=world, mood="clear")["reason"] == "duplicate"


# --------------------------------------------------------------------------
# The ledger + the deferred queue
# --------------------------------------------------------------------------

def test_every_call_writes_a_ledger_row(world, wall):
    """Accepted or refused, it is recorded — that is what survives a reload."""
    request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")
    wall.advance(seconds=1)
    request_cinematic("title_card", player_id=world, title="THE LONG DARK")
    wall.advance(seconds=1)
    request_cinematic("explosion", player_id=world)

    rows = ledger()
    assert len(rows) == 3

    assert rows[0].kind == "title_card"
    assert rows[0].status == "played"
    assert rows[0].reject_reason is None
    assert rows[0].dedupe_key == "the-road-north"
    assert rows[0].game_day == 1 and rows[0].game_hour == 8
    assert rows[0].source == "dm"
    assert rows[0].player_id == world

    assert rows[1].status == "rejected"
    assert rows[1].reject_reason in REJECT_REASONS

    assert rows[2].kind == "explosion"
    assert rows[2].reject_reason == "unknown_kind"


def test_pending_queue_drains_once(world):
    """A campaign beat queues it; /api/play delivers it exactly one time."""
    result = request_cinematic("title_card", player_id=world, title="ACT TWO",
                               subtitle="The bargain comes due",
                               source="campaign", defer=True)
    assert result["ok"] is True
    assert result["deferred"] is True
    assert ledger()[0].status == "pending"
    assert ledger()[0].source == "campaign"

    drained = drain_pending(world)
    assert len(drained) == 1
    assert drained[0]["kind"] == "title_card"
    assert drained[0]["title"] == "ACT TWO"
    assert drained[0]["subtitle"] == "The bargain comes due"
    assert drained[0]["duration_ms"] == 4200

    assert drain_pending(world) == []
    assert ledger()[0].status == "played"


def test_pending_queue_is_per_player(world):
    request_cinematic("title_card", player_id=world, title="ACT TWO",
                      source="campaign", defer=True)
    assert drain_pending("someone-else") == []
    assert len(drain_pending(world)) == 1


def test_queued_cinematic_consumes_the_budget(world, wall):
    """A queued card is a promise to play — it cannot be doubled up on."""
    request_cinematic("title_card", player_id=world, title="ACT TWO",
                      source="campaign", defer=True)
    wall.advance(minutes=1)
    advance_game(12)        # past the same-kind cooldown, still the same game day
    result = request_cinematic("title_card", player_id=world, title="ACT THREE")
    assert result["reason"] == "daily_cap"


def test_recent_returns_newest_first(world, wall):
    request_cinematic("title_card", player_id=world, title="THE ROAD NORTH")
    wall.advance(minutes=1)
    request_cinematic("weather", player_id=world, mood="rain")

    rows = recent(limit=5)
    assert [r["kind"] for r in rows] == ["weather", "title_card"]
    assert rows[0]["mood"] == "rain"
    assert rows[0]["status"] == "played"
    assert rows[0]["reject_reason"] is None
    assert rows[1]["title"] == "THE ROAD NORTH"
    assert rows[1]["created_at"]
    assert len(recent(limit=1)) == 1
