"""Ambient barks: staleness bookkeeping, defensive parsing, and the cost guards.

Every test here stubs the model call — a bark is one background LLM call and
nothing in this module may ever reach the network.
"""

import asyncio

import pytest

from src.models import NPC, Location, LocationType, NPCTier, WorldClock, get_session
from src.services import barks
from src.services.barks import (
    bark_signature,
    barks_stale,
    cached_barks,
    generate_barks,
    parse_barks,
)


LOC_ID = "loc-square"


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    """The failure registry is module-level state — don't leak it between tests."""
    barks.reset_failure_cooldowns()
    yield
    barks.reset_failure_cooldowns()


def _scene(db, npcs=(), state="peaceful", day=1, hour=8) -> str:
    """A location with a cast standing in it, plus a clock."""
    with get_session() as session:
        session.add(Location(
            id=LOC_ID,
            name="Market Square",
            type=LocationType.DISTRICT,
            description="Fish stalls and wet stone.",
            atmosphere_tags=["salt-stained", "crowded"],
            current_state=state,
        ))
        for npc_id, name, tier in npcs:
            session.add(NPC(
                id=npc_id,
                name=name,
                tier=tier,
                profession="gull-catcher",
                current_location_id=LOC_ID,
                status="alive",
            ))
        session.add(WorldClock(day=day, hour=hour))
        session.commit()
    return LOC_ID


def _stub(reply: str, calls: list | None = None):
    """A run_agent override that records its prompt and returns a fixed reply."""
    def _run(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return reply
    return _run


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Signature + staleness
# ----------------------------------------------------------------------

def test_signature_changes_with_npc_set(db):
    _scene(db, npcs=[("npc-piper", "Piper", NPCTier.MINOR),
                     ("npc-hesk", "Hesk", NPCTier.MINOR)])
    before = bark_signature(LOC_ID)

    with get_session() as session:
        session.get(NPC, "npc-hesk").status = "dead"
        session.commit()

    assert bark_signature(LOC_ID) != before


def test_signature_changes_with_location_state(db):
    _scene(db)
    before = bark_signature(LOC_ID)

    with get_session() as session:
        session.get(Location, LOC_ID).current_state = "under_siege"
        session.commit()

    assert bark_signature(LOC_ID) != before


def test_signature_is_stable_across_calls(db):
    _scene(db, npcs=[("npc-piper", "Piper", NPCTier.MINOR)])
    assert bark_signature(LOC_ID) == bark_signature(LOC_ID)


def _write_pool(day=1, hour=8, signature=None):
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        loc.ambient_barks = [{"npc_id": None, "text": "Salt in the wind."}]
        loc.ambient_barks_meta = {
            "signature": signature if signature is not None else bark_signature(LOC_ID),
            "day": day,
            "hour": hour,
        }
        session.commit()


def test_missing_pool_is_stale(db):
    _scene(db)
    with get_session() as session:
        assert barks_stale(session.get(Location, LOC_ID)) is True


def test_fresh_pool_is_not_stale(db):
    _scene(db)
    _write_pool()
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        clock = session.query(WorldClock).first()
        assert barks_stale(loc, clock) is False


def test_stale_when_signature_changes(db):
    _scene(db, npcs=[("npc-piper", "Piper", NPCTier.MINOR)])
    _write_pool()

    with get_session() as session:
        session.get(NPC, "npc-piper").status = "dead"
        session.commit()

    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        clock = session.query(WorldClock).first()
        assert barks_stale(loc, clock) is True


def test_stale_after_refresh_hours(db):
    _scene(db)
    _write_pool(day=1, hour=8)

    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(11)  # inside the 12-hour window
        session.commit()
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        assert barks_stale(loc, session.query(WorldClock).first()) is False

    with get_session() as session:
        session.query(WorldClock).first().advance(2)  # now 13 hours on
        session.commit()
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        assert barks_stale(loc, session.query(WorldClock).first()) is True


def test_refresh_window_is_configurable(db, monkeypatch):
    _scene(db)
    _write_pool(day=1, hour=8)
    monkeypatch.setattr(
        barks, "_game_setting",
        lambda name, default: 2 if name == "bark_refresh_hours" else default,
    )

    with get_session() as session:
        session.query(WorldClock).first().advance(3)
        session.commit()
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        assert barks_stale(loc, session.query(WorldClock).first()) is True


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

def test_parse_handles_code_fences_and_prose():
    raw = (
        'Sure, here are the lines:\n'
        '```json\n'
        '[{"speaker": "ambient", "text": "Salt in the wind means salt in the bread."}]\n'
        '```'
    )
    assert parse_barks(raw, {}, 10) == [
        {"npc_id": None, "text": "Salt in the wind means salt in the bread."}
    ]


def test_parse_recovers_json_from_surrounding_prose():
    raw = 'Here you go: [{"speaker": "ambient", "text": "Third gull this week."}] hope it fits!'
    assert parse_barks(raw, {}, 10)[0]["text"] == "Third gull this week."


def test_parse_returns_empty_on_garbage():
    assert parse_barks("I'm sorry, I can't help with that.", {}, 10) == []
    assert parse_barks("", {}, 10) == []
    assert parse_barks('{"speaker": "ambient", "text": "not a list"}', {}, 10) == []


def test_parse_drops_entries_without_text():
    raw = '[{"speaker": "ambient"}, {"speaker": "ambient", "text": "   "}, {"speaker": "ambient", "text": "Kept."}]'
    assert parse_barks(raw, {}, 10) == [{"npc_id": None, "text": "Kept."}]


def test_speaker_resolution():
    speakers = {"piper": "npc-piper"}
    raw = (
        '[{"speaker": "Piper", "text": "a"},'
        ' {"speaker": "PIPER", "text": "b"},'
        ' {"speaker": "ambient", "text": "c"},'
        ' {"speaker": "Someone Else", "text": "d"}]'
    )
    assert [b["npc_id"] for b in parse_barks(raw, speakers, 10)] == [
        "npc-piper", "npc-piper", None, None
    ]


def test_text_truncated_and_capped():
    long_line = "x" * 400
    raw = "[" + ",".join(
        f'{{"speaker": "ambient", "text": "{long_line}"}}' for _ in range(20)
    ) + "]"
    parsed = parse_barks(raw, {}, 12)
    assert len(parsed) == 12
    assert all(len(b["text"]) == barks.TEXT_MAX for b in parsed)


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------

def test_generate_writes_pool_and_meta(db):
    _scene(db, npcs=[("npc-piper", "Piper", NPCTier.MINOR)], day=3, hour=14)
    reply = (
        '[{"speaker": "Piper", "text": "Third gull this week."},'
        ' {"speaker": "ambient", "text": "Salt in the wind."}]'
    )

    result = _run(generate_barks(LOC_ID, run_agent=_stub(reply)))
    assert result == [
        {"npc_id": "npc-piper", "text": "Third gull this week."},
        {"npc_id": None, "text": "Salt in the wind."},
    ]

    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        assert cached_barks(loc) == result
        assert loc.ambient_barks_meta["day"] == 3
        assert loc.ambient_barks_meta["hour"] == 14
        assert loc.ambient_barks_meta["signature"] == bark_signature(LOC_ID)
        # Freshly written pools are not stale
        assert barks_stale(loc, session.query(WorldClock).first()) is False


def test_generate_prompt_names_the_place_and_its_people(db):
    _scene(db, npcs=[("npc-piper", "Piper", NPCTier.MINOR)])
    calls: list[str] = []
    _run(generate_barks(LOC_ID, run_agent=_stub('[{"speaker":"ambient","text":"x"}]', calls)))

    assert len(calls) == 1
    assert "Market Square" in calls[0]
    assert "Piper" in calls[0]


def test_major_npcs_are_never_named_speakers(db):
    """R15: MAJOR NPCs don't mutter filler — their lines resolve to ambient."""
    _scene(db, npcs=[("npc-vell", "Vell", NPCTier.MAJOR),
                     ("npc-piper", "Piper", NPCTier.MINOR)])
    calls: list[str] = []
    reply = (
        '[{"speaker": "Vell", "text": "The tide is late."},'
        ' {"speaker": "Piper", "text": "Third gull this week."}]'
    )

    result = _run(generate_barks(LOC_ID, run_agent=_stub(reply, calls)))

    assert "Vell" not in calls[0]           # never offered as a speaker
    assert result[0]["npc_id"] is None      # and never attributed if named anyway
    assert result[1]["npc_id"] == "npc-piper"


def test_generate_truncates_and_caps_against_pool_size(db, monkeypatch):
    _scene(db)
    monkeypatch.setattr(
        barks, "_game_setting",
        lambda name, default: 3 if name == "bark_pool_size" else default,
    )
    long_line = "y" * 300
    reply = "[" + ",".join(
        f'{{"speaker": "ambient", "text": "{long_line}"}}' for _ in range(20)
    ) + "]"

    result = _run(generate_barks(LOC_ID, run_agent=_stub(reply)))
    assert len(result) == 3 + barks.POOL_SLACK
    assert all(len(b["text"]) == barks.TEXT_MAX for b in result)


def test_garbage_reply_leaves_the_pool_untouched(db):
    _scene(db)
    assert _run(generate_barks(LOC_ID, run_agent=_stub("no json here"))) == []
    with get_session() as session:
        assert session.get(Location, LOC_ID).ambient_barks is None


def test_model_exception_never_raises(db):
    _scene(db)

    def boom(prompt: str) -> str:
        raise RuntimeError("model exploded")

    assert _run(generate_barks(LOC_ID, run_agent=boom)) == []


def test_failed_generation_is_not_retried_immediately(db):
    _scene(db)
    assert _run(generate_barks(LOC_ID, run_agent=_stub("garbage"))) == []

    calls: list[str] = []
    good = '[{"speaker": "ambient", "text": "Salt in the wind."}]'
    assert _run(generate_barks(LOC_ID, run_agent=_stub(good, calls))) == []
    assert calls == []  # cooldown held the second call back

    barks.reset_failure_cooldowns()
    assert _run(generate_barks(LOC_ID, run_agent=_stub(good, calls))) != []
    assert len(calls) == 1


def test_disabled_barks_make_no_call(db, monkeypatch):
    _scene(db)
    monkeypatch.setattr(
        barks, "_game_setting",
        lambda name, default: False if name == "barks_enabled" else default,
    )
    calls: list[str] = []

    assert _run(generate_barks(LOC_ID, run_agent=_stub('[{"speaker":"ambient","text":"x"}]', calls))) == []
    assert calls == []


def test_missing_location_is_survivable(db):
    _scene(db)
    calls: list[str] = []
    assert _run(generate_barks("no-such-location", run_agent=_stub("[]", calls))) == []
    assert calls == []


def test_cached_barks_sanitizes_rows(db):
    _scene(db)
    with get_session() as session:
        loc = session.get(Location, LOC_ID)
        loc.ambient_barks = [
            {"npc_id": "npc-piper", "text": "kept"},
            {"npc_id": None, "text": "  "},
            "not a dict",
            {"text": "no npc key"},
        ]
        session.commit()
    with get_session() as session:
        assert cached_barks(session.get(Location, LOC_ID)) == [
            {"npc_id": "npc-piper", "text": "kept"},
            {"npc_id": None, "text": "no npc key"},
        ]


# ----------------------------------------------------------------------
# Music palette
# ----------------------------------------------------------------------

def test_epic_mood_is_in_the_palette():
    from src.services.music_generator import MOODS

    assert "epic" in MOODS
    assert MOODS["epic"]
