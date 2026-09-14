"""The overhaul schema delta must land on EXISTING databases, not just fresh ones.

Every new column is nullable and arrives as `ADD COLUMN ... DEFAULT NULL`, so a
legacy row reads `None` and the defaulting happens in code (progression's
`player_level`, `xp_for_quest`, `resolve_danger_tier`, ...). These tests pin
that contract: the columns appear, the tables appear, and nothing about a
pre-overhaul row changes except that it now answers `None` to the new questions.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from src.models import (
    NPC,
    BEAT_STATUSES,
    BEAT_TYPES,
    CAMPAIGN_STATUSES,
    CINEMATIC_LABELS,
    TRIGGER_TYPES,
    TRUTH_KINDS,
    Campaign,
    Cinematic,
    DMState,
    Item,
    Location,
    LocationType,
    Player,
    Quest,
    StoryBeat,
    get_session,
    init_db,
    reset_engine,
)

# Every column the overhaul adds to a table that already exists in the wild.
NEW_COLUMNS = {
    "players": ["level", "xp", "equipment", "quick_slot_item_id"],
    "quests": ["scale"],
    "npcs": ["danger_tier", "boss_title"],
    "locations": ["objects", "ambient_barks", "ambient_barks_meta"],
    "dm_state": ["weather"],
}

# Tables that `Base.metadata.create_all` must add to an existing database.
NEW_TABLES = ["campaigns", "story_beats", "cinematics"]

REAL_LEGACY_DB = Path(__file__).resolve().parents[1] / "data" / "emberfall.db"


def _table_columns(db_path, table: str) -> dict:
    """{column_name: (type, default)} straight from SQLite."""
    con = sqlite3.connect(str(db_path))
    try:
        return {r[1]: (r[2], r[4]) for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _table_names(db_path) -> set:
    con = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


@pytest.fixture()
def legacy_db(tmp_path):
    """A database in the pre-overhaul shape, with one populated row per table.

    Built by creating today's schema, seeding it, then surgically removing the
    new columns and tables — which is exactly what an on-disk `data/*.db` from
    before this build looks like. `init_db` is NOT re-run here; each test runs
    it so it can inspect the before/after.
    """
    db_path = tmp_path / "legacy.db"
    reset_engine()
    init_db(db_path)
    with get_session() as session:
        session.add(Player(id="p1", name="Legacy Hero", currency=10))
        session.add(Quest(id="q1", title="Legacy Errand"))
        session.add(NPC(id="n1", name="Legacy Guard"))
        session.add(Location(id="l1", name="Legacy Square", type=LocationType.DISTRICT))
        session.add(DMState(id="d1", tension="low"))
        session.commit()
    reset_engine()

    con = sqlite3.connect(str(db_path))
    try:
        for table, columns in NEW_COLUMNS.items():
            for column in columns:
                con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        for table in NEW_TABLES:
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.commit()
    finally:
        con.close()

    yield db_path
    reset_engine()


# --- Migration onto an existing database -----------------------------------


def test_legacy_db_starts_without_the_new_schema(legacy_db):
    """Guard on the fixture itself: it really is a pre-overhaul database."""
    assert not (set(NEW_TABLES) & _table_names(legacy_db))
    for table, columns in NEW_COLUMNS.items():
        existing = _table_columns(legacy_db, table)
        assert not (set(columns) & set(existing)), table


@pytest.mark.parametrize(
    "table,column",
    [(table, column) for table, columns in NEW_COLUMNS.items() for column in columns],
)
def test_init_db_adds_new_column(legacy_db, table, column):
    init_db(legacy_db)
    assert column in _table_columns(legacy_db, table)


@pytest.mark.parametrize(
    "table,column",
    [(table, column) for table, columns in NEW_COLUMNS.items() for column in columns],
)
def test_new_columns_are_added_with_default_null(legacy_db, table, column):
    """`_migrate_missing_columns` has an empty-string branch — none of ours may hit it."""
    init_db(legacy_db)
    _type, default = _table_columns(legacy_db, table)[column]
    assert default == "NULL", f"{table}.{column} migrated with DEFAULT {default}"


@pytest.mark.parametrize("table", NEW_TABLES)
def test_init_db_creates_new_table(legacy_db, table):
    init_db(legacy_db)
    assert table in _table_names(legacy_db)


def test_legacy_rows_read_new_columns_as_none(legacy_db):
    """The whole point: an old row survives and answers None, never garbage."""
    init_db(legacy_db)
    with get_session() as session:
        player = session.get(Player, "p1")
        assert player.name == "Legacy Hero" and player.currency == 10
        assert player.level is None
        assert player.xp is None
        assert player.equipment is None
        assert player.quick_slot_item_id is None

        assert session.get(Quest, "q1").scale is None

        npc = session.get(NPC, "n1")
        assert npc.danger_tier is None and npc.boss_title is None

        location = session.get(Location, "l1")
        assert location.objects is None
        assert location.ambient_barks is None
        assert location.ambient_barks_meta is None

        assert session.get(DMState, "d1").weather is None


def test_null_columns_default_the_way_code_reads_them(legacy_db):
    """NULL is not "broken" — it is the documented legacy reading."""
    init_db(legacy_db)
    with get_session() as session:
        player = session.get(Player, "p1")
        assert (player.level or 1) == 1
        assert (player.xp or 0) == 0
        assert (player.equipment or {}) == {}
        quest = session.get(Quest, "q1")
        assert (quest.scale or "job") == "job"
        location = session.get(Location, "l1")
        assert (location.objects or []) == []
        assert (session.get(DMState, "d1").weather or "clear") == "clear"


def test_migration_is_idempotent(legacy_db, capsys):
    """Second boot of the same DB must not try to re-add anything."""
    init_db(legacy_db)
    capsys.readouterr()
    reset_engine()
    init_db(legacy_db)
    assert "[migrate]" not in capsys.readouterr().out


@pytest.mark.skipif(not REAL_LEGACY_DB.exists(), reason="data/emberfall.db not present")
def test_real_emberfall_copy_migrates(tmp_path):
    """The actual world on disk — copied, never touched in place."""
    db_path = tmp_path / "emberfall.db"
    shutil.copy2(REAL_LEGACY_DB, db_path)
    reset_engine()
    init_db(db_path)
    try:
        tables = _table_names(db_path)
        for table in NEW_TABLES:
            assert table in tables
        for table, columns in NEW_COLUMNS.items():
            existing = _table_columns(db_path, table)
            for column in columns:
                assert column in existing, f"{table}.{column}"
        with get_session() as session:
            player = session.query(Player).first()
            if player is not None:
                assert player.level is None and player.xp is None
    finally:
        reset_engine()


# --- Fresh rows -------------------------------------------------------------


def test_fresh_player_gets_python_side_defaults(db):
    """New characters start at level 1 with an empty rack — no NULL bookkeeping."""
    with get_session() as session:
        player = Player(name="Newcomer")
        session.add(player)
        session.commit()
        session.refresh(player)
        assert player.level == 1
        assert player.xp == 0
        assert player.equipment == {}
        assert player.quick_slot_item_id is None


def test_fresh_quest_scale_is_null(db):
    """`scale` is authored, never assumed — a quest with no label reads as a job."""
    with get_session() as session:
        quest = Quest(title="Fetch the Ledger")
        session.add(quest)
        session.commit()
        session.refresh(quest)
        assert quest.scale is None


def test_authored_labels_round_trip(db):
    with get_session() as session:
        session.add(NPC(id="boss", name="The Warden", danger_tier="boss",
                        boss_title="Warden of the Ashen Gate", combat_archetype="brute"))
        session.add(Quest(id="saga", title="The Long Howl", scale="saga"))
        session.add(DMState(id="dm", weather="ash"))
        session.commit()
    with get_session() as session:
        npc = session.get(NPC, "boss")
        assert npc.danger_tier == "boss"
        assert npc.boss_title == "Warden of the Ashen Gate"
        assert session.get(Quest, "saga").scale == "saga"
        assert session.get(DMState, "dm").weather == "ash"


def test_location_canvas_columns_round_trip(db):
    objects = [{"id": "crate", "kind": "search", "label": "Crate", "x": 40.0, "y": 60.0,
                "payload": {}, "consumed": False}]
    barks = [{"npc_id": None, "text": "Ash on the wind again."}]
    meta = {"signature": "abc", "day": 3, "hour": 9}
    with get_session() as session:
        session.add(Location(id="sq", name="Square", type=LocationType.DISTRICT,
                             objects=objects, ambient_barks=barks, ambient_barks_meta=meta))
        session.commit()
    with get_session() as session:
        location = session.get(Location, "sq")
        assert location.objects == objects
        assert location.ambient_barks == barks
        assert location.ambient_barks_meta == meta


# --- New tables -------------------------------------------------------------


def test_campaign_and_beats_round_trip(db):
    with get_session() as session:
        session.add(Player(id="pc", name="Rook"))
        session.add(Campaign(
            id="c1", player_id="pc", title="The Long Howl",
            premise="Something is eating the nights.", stakes_personal="Your sister.",
            villain_name="Vask", ally_npc_ids=["n1", "n2"],
            acts=[{"act": 1, "title": "Scent", "premise": "Tracks in the ash."}],
            fixed_truths=[{"id": "t1", "kind": "villain_identity", "statement": "Vask is the howl.",
                           "spoiler_keywords": ["vask"], "revealed": False, "revealed_day": None}],
        ))
        session.add(StoryBeat(
            id="b1", campaign_id="c1", act=1, sequence=0, beat_type="deadline_event",
            title="The Howl Returns", directive="Tell them the horns started.",
            directive_missed="Tell them the horns stopped.",
            delivery_keywords=["horn", "ash"], trigger_type="time",
            trigger_data={"deadline": "short"},
            met_condition={"check": "quest_status", "quest_id": "q1", "status": "completed"},
            consequence_missed=[{"op": "tension_floor", "level": "high"}],
            clock_label="The Howl returns", cinematic="countdown_expired",
        ))
        session.commit()

    with get_session() as session:
        campaign = session.get(Campaign, "c1")
        assert campaign.status == "pending"        # generation not finished yet
        assert campaign.current_act == 1
        assert campaign.ally_npc_ids == ["n1", "n2"]
        assert campaign.fixed_truths[0]["kind"] == "villain_identity"
        assert campaign.last_beat_day is None and campaign.generated_day is None

        beat = session.query(StoryBeat).filter_by(campaign_id="c1").one()
        assert beat.status == "pending"
        assert beat.delivery_attempts == 0
        assert beat.variant is None
        assert beat.applied_effects == []
        assert beat.met_condition["check"] == "quest_status"
        assert beat.consequence_missed[0]["op"] == "tension_floor"
        assert beat.cinematic == "countdown_expired"


def test_cinematic_ledger_round_trip(db):
    with get_session() as session:
        session.add(Cinematic(id="cin1", player_id="pc", kind="boss_intro",
                              dedupe_key="npc-boss", title="The Warden",
                              subtitle="Warden of the Ashen Gate", npc_id="npc-boss",
                              mood="epic", source="auto", game_day=3, game_hour=21,
                              game_minute=30))
        session.add(Cinematic(id="cin2", kind="title_card", status="rejected",
                              reject_reason="cooldown"))
        session.commit()
    with get_session() as session:
        played = session.get(Cinematic, "cin1")
        assert played.status == "played"          # default
        assert played.source == "auto"
        assert played.reject_reason is None
        assert played.created_at is not None
        rejected = session.get(Cinematic, "cin2")
        assert rejected.source == "dm"            # default
        assert rejected.status == "rejected"
        assert rejected.dedupe_key is None


def test_campaign_menus_match_the_frozen_vocabulary():
    """Label menus are code-owned; nothing may be invented at runtime."""
    assert BEAT_TYPES == ("revelation", "confrontation", "deadline_event", "setpiece", "twist")
    assert TRUTH_KINDS == ("villain_identity", "the_secret", "the_betrayal",
                           "the_location", "the_cost")
    assert TRIGGER_TYPES == ("time", "location", "quest_status", "milestone", "after_beat")
    assert CINEMATIC_LABELS == ("none", "act_open", "revelation", "betrayal", "arrival",
                                "countdown_expired", "finale")
    # stalled: fired, never narrated in 3 tries; parked (not claimed told) until
    # its keywords or met_condition show up, or STALLED_SKIP_DAYS passes
    assert BEAT_STATUSES == ("pending", "armed", "fired", "delivered", "skipped", "stalled")
    assert CAMPAIGN_STATUSES == ("pending", "active", "won", "lost", "abandoned", "failed")


# --- Item pydantic fields ---------------------------------------------------


def test_legacy_item_dict_still_validates():
    item = Item.from_dict({"id": "iron_sword", "name": "Iron Sword", "type": "weapon",
                           "value": 40, "description": "Notched but honest."})
    assert item.slot is None and item.tier is None and item.effect_tag is None


def test_item_to_dict_carries_the_mechanical_keys():
    payload = Item(id="rope", name="Rope", type="misc").to_dict()
    assert payload["slot"] is None
    assert payload["tier"] is None
    assert payload["effect_tag"] is None


def test_mechanical_item_round_trips():
    item = Item.from_dict({"id": "ashblade", "name": "Ashblade", "type": "weapon",
                           "slot": "weapon", "tier": "superior"})
    assert item.slot == "weapon" and item.tier == "superior"
    potion = Item.from_dict({"id": "tonic", "name": "Tonic", "type": "consumable",
                             "effects": {"heal": 50}, "effect_tag": "heal_strong"})
    assert potion.effect_tag == "heal_strong"
    assert potion.to_dict()["effect_tag"] == "heal_strong"
