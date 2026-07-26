"""ONE world-derived factor may ride a check — never two, never a number.

The legacy guarantee: `situation=None` rolls byte-identically to the
pre-attire engine under a seeded RNG. Everything else here is the four-row
table, its ordering, and what the DM is (and is not) told about it.
"""

import pytest

from src.models import (
    DMState,
    Faction,
    Location,
    LocationType,
    NPC,
    NPCTier,
    Player,
    WorldClock,
    get_session,
)
from src.services import perception
from src.services import progression as prog
from src.tools.world_write.progression import roll_check


@pytest.fixture(autouse=True)
def _clean_memo():
    perception.clear_recognition()
    yield
    perception.clear_recognition()


def _armor(name="Coat", **extra) -> dict:
    item = {"id": prog._slug(name), "name": name, "type": "armor",
            "slot": "armor", "tier": "common", "quantity": 1}
    item.update(extra)
    return item


def _scene(session, *, attire=None, hour=12, weather=None, tags=(),
           dress_code=None, npc_faction_id=None, with_npc=True,
           controlling_faction_id=None):
    location = Location(name="Counting Hall", type=LocationType.BUILDING,
                        atmosphere_tags=list(tags), dress_code=dress_code,
                        controlling_faction_id=controlling_faction_id)
    session.add(location)
    session.flush()
    if with_npc:
        session.add(NPC(name="Clerk", tier=NPCTier.AMBIENT,
                        current_location_id=location.id, faction_id=npc_faction_id))
    armor = _armor(attire=attire) if attire else None
    player = Player(name="Rook", level=10, current_location_id=location.id,
                    inventory=[armor] if armor else [],
                    equipment={"armor": prog.item_key(armor)} if armor else {})
    session.add(player)
    session.add(WorldClock(day=1, hour=hour))
    session.add(DMState(weather=weather))
    session.commit()
    return player, location


# ---------------------------------------------------------------------------
# The legacy guarantee
# ---------------------------------------------------------------------------


def _player_row(level=1, traits=None):
    return Player(name="Tester", level=level, xp=0, traits=traits or [], inventory=[])


def test_no_factor_matches_legacy_roll():
    """Same seed, same stream, same outcomes — `situation=None` is inert."""
    prog.set_seed(1234)
    legacy = [prog.roll_skill_check(_player_row(), "stealth", "tricky")["outcome"]
              for _ in range(50)]
    prog.set_seed(1234)
    with_kwarg = [
        prog.roll_skill_check(_player_row(), "stealth", "tricky", situation=None)["outcome"]
        for _ in range(50)
    ]
    assert legacy == with_kwarg


def test_no_situation_key_without_a_factor(monkeypatch):
    prog.set_seed(5)
    result = prog.roll_skill_check(_player_row(), "stealth", "tricky")
    assert "situation" not in result
    assert set(result) - {"_debug"} == {"outcome", "skill", "difficulty", "modifier"}


def test_the_effective_modifier_drives_the_dice_but_not_the_report():
    prog.set_seed(9)
    result = prog.roll_skill_check(
        _player_row(), "stealth", "tricky", "none",
        situation={"factor": "dark_or_weather", "effect": "advantage",
                   "because": "x", "effective": "advantage"},
    )
    assert result["modifier"] == "none", "the DM's own word is reported back"
    assert result["_debug"]["effective_modifier"] == "advantage"
    assert len(result["_debug"]["rolls"]) == 2, "advantage rolls twice"
    assert result["situation"]["factor"] == "dark_or_weather"


def test_a_garbled_effective_modifier_falls_back_to_the_dm_word():
    prog.set_seed(3)
    result = prog.roll_skill_check(
        _player_row(), "stealth", "tricky", "disadvantage",
        situation={"effect": "advantage", "effective": "blessed"},
    )
    assert result["_debug"]["effective_modifier"] == "disadvantage"


def test_invalid_words_still_error_with_a_situation():
    result = prog.roll_skill_check(_player_row(), "lockpicking", "tricky",
                                   situation={"effective": "advantage"})
    assert "error" in result and result["valid"] == list(prog.SKILLS)


# ---------------------------------------------------------------------------
# combine_modifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dm,factor,expected", [
    ("none", None, "none"),
    ("advantage", None, "advantage"),
    ("none", "advantage", "advantage"),
    ("none", "disadvantage", "disadvantage"),
    ("advantage", "advantage", "advantage"),
    ("disadvantage", "disadvantage", "disadvantage"),
    ("advantage", "disadvantage", "none"),
    ("disadvantage", "advantage", "none"),
])
def test_combine_modifier_never_stacks(dm, factor, expected):
    assert perception.combine_modifier(dm, factor) == expected


# ---------------------------------------------------------------------------
# The four rows
# ---------------------------------------------------------------------------


def test_night_covers_a_stealth_attempt(db):
    with get_session() as session:
        player, _ = _scene(session, hour=23)
        factor = perception.situational_factor(session, player, "stealth")
    assert factor == {"factor": "dark_or_weather", "effect": "advantage",
                      "because": "the dark and the weather are covering them"}


@pytest.mark.parametrize("weather", sorted(perception.COVERING_WEATHER))
def test_covering_weather_covers_a_stealth_attempt(db, weather):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather=weather)
        factor = perception.situational_factor(session, player, "stealth")
    assert factor["factor"] == "dark_or_weather"


def test_clear_daylight_gives_stealth_nothing(db):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather="clear")
        assert perception.situational_factor(session, player, "stealth") is None


def test_dark_or_weather_is_stealth_only(db):
    with get_session() as session:
        player, _ = _scene(session, hour=23)
        for skill in ("persuasion", "athletics", "perception", "cunning"):
            factor = perception.situational_factor(session, player, skill)
            assert factor is None or factor["factor"] != "dark_or_weather"


def test_filthy_in_a_room_that_notices(db):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "filthy"}, tags=("wealthy",))
        factor = perception.situational_factor(session, player, "persuasion")
    assert factor["factor"] == "filthy_in_fine_company"
    assert factor["effect"] == "disadvantage"


def test_filthy_in_a_room_whose_door_demands_money(db):
    code = {"min_style": "fine", "banned_styles": [], "reason": "Members only."}
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "rough"}, dress_code=code)
        factor = perception.situational_factor(session, player, "persuasion")
    assert factor["factor"] == "filthy_in_fine_company"


def test_a_plain_room_does_not_notice(db):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "filthy"}, tags=("smoky",))
        assert perception.situational_factor(session, player, "persuasion") is None


def test_a_hood_in_company_costs_them(db):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "common", "concealing": True})
        factor = perception.situational_factor(session, player, "persuasion")
    assert factor["factor"] == "hooded_and_watched"
    assert factor["because"] == "nobody trusts a face they cannot see"


def test_a_hood_in_an_empty_room_costs_nothing(db):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "common", "concealing": True},
                           with_npc=False)
        assert perception.situational_factor(session, player, "persuasion") is None


def test_their_colours_help_against_their_own_people(db):
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.flush()
        player, _ = _scene(session, attire={"style": "common",
                                            "uniform_faction_id": watch.id},
                           npc_faction_id=watch.id)
        for skill in ("persuasion", "cunning"):
            factor = perception.situational_factor(session, player, skill)
            assert factor["factor"] == "wearing_their_colours"
            assert factor["effect"] == "advantage"


def test_their_colours_help_on_ground_they_control(db):
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.flush()
        player, _ = _scene(session, attire={"style": "common",
                                            "uniform_faction_id": watch.id},
                           controlling_faction_id=watch.id, with_npc=False)
        assert perception.situational_factor(
            session, player, "persuasion")["factor"] == "wearing_their_colours"


def test_somebody_elses_colours_do_nothing(db):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "common",
                                            "uniform_faction_id": "fangs"})
        assert perception.situational_factor(session, player, "persuasion") is None


def test_colours_do_not_apply_to_athletics_or_perception(db):
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.flush()
        player, _ = _scene(session, attire={"style": "common",
                                            "uniform_faction_id": watch.id},
                           npc_faction_id=watch.id)
        assert perception.situational_factor(session, player, "athletics") is None
        assert perception.situational_factor(session, player, "perception") is None


def test_only_one_factor_ever_applies_and_the_penalty_wins(db):
    """A filthy, hooded impostor in their own colours gets no free help."""
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.flush()
        player, _ = _scene(
            session,
            attire={"style": "filthy", "uniform_faction_id": watch.id,
                    "concealing": True},
            tags=("opulent",), npc_faction_id=watch.id,
        )
        factor = perception.situational_factor(session, player, "persuasion")

    assert factor["factor"] == "filthy_in_fine_company"
    assert set(factor) == {"factor", "effect", "because"}


def test_hooded_beats_the_uniform_when_the_room_is_plain(db):
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.flush()
        player, _ = _scene(session, attire={"style": "common", "concealing": True,
                                            "uniform_faction_id": watch.id},
                           npc_faction_id=watch.id)
        assert perception.situational_factor(
            session, player, "persuasion")["factor"] == "hooded_and_watched"


def test_an_unknown_skill_never_produces_a_factor(db):
    with get_session() as session:
        player, _ = _scene(session, hour=23)
        assert perception.situational_factor(session, player, "lockpicking") is None
        assert perception.situational_factor(session, player, "") is None


# ---------------------------------------------------------------------------
# The tool: what actually reaches the DM
# ---------------------------------------------------------------------------


def _forced(monkeypatch, *values):
    class _RNG:
        def __init__(self):
            self._values = list(values)

        def randint(self, a, b):
            return self._values.pop(0)

    monkeypatch.setattr(prog, "_RNG", _RNG())


def test_tool_result_is_unchanged_when_nothing_applies(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather="clear")
        player_id = player.id

    _forced(monkeypatch, 11)
    result = roll_check(player_id, "stealth", "tricky")
    assert set(result) == {"success", "outcome", "skill", "difficulty",
                           "event", "instruction"}


def test_tool_reports_the_factor_as_a_phrase_never_a_mechanic(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, hour=23)
        player_id = player.id

    _forced(monkeypatch, 3, 17)
    result = roll_check(player_id, "stealth", "tricky", reason="over the wall")

    assert result["situation"] == {
        "factor": "dark_or_weather",
        "because": "the dark and the weather are covering them",
    }
    assert result["instruction"].endswith(
        "The world was with them: the dark and the weather are covering them — "
        "let that show in how it goes."
    )
    blob = " ".join(str(v) for v in result.values()).lower()
    assert "advantage" not in blob and "disadvantage" not in blob
    assert not any(ch.isdigit() for ch in blob)
    assert result["outcome"] == "success", "the world's advantage kept the 17"


def test_tool_reports_a_penalty_without_naming_it(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "common", "concealing": True})
        player_id = player.id

    _forced(monkeypatch, 17, 3)
    result = roll_check(player_id, "persuasion", "desperate")
    assert result["situation"]["factor"] == "hooded_and_watched"
    assert "The world was against them: nobody trusts a face they cannot see" \
        in result["instruction"]
    # 3 + level 10 = 13 against DC 20; the 17 would have cleared it
    assert result["outcome"] == "failure", "the world's disadvantage kept the 3"


def test_the_dm_word_and_the_world_word_cancel(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, attire={"style": "common", "concealing": True})
        player_id = player.id

    _forced(monkeypatch, 11)
    result = roll_check(player_id, "persuasion", "tricky", modifier="advantage")
    assert result["situation"]["factor"] == "hooded_and_watched"
    assert result["outcome"] == "success", "one die only — the two cancelled to none"


# ---------------------------------------------------------------------------
# about_npc_id — the recognition binding
# ---------------------------------------------------------------------------


def test_about_npc_id_records_a_failed_hide_as_recognised(db, monkeypatch):
    with get_session() as session:
        player, location = _scene(session, hour=12, weather="clear")
        player_id, npc_id = player.id, session.query(NPC).first().id

    _forced(monkeypatch, 2)   # total 12 vs DC 16 -> failure
    result = roll_check(player_id, "stealth", "hard", about_npc_id=npc_id)
    assert result["outcome"] == "failure"
    assert perception.recalled_recognition(player_id, npc_id, 1) is True


def test_about_npc_id_records_a_successful_hide_as_unrecognised(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather="clear")
        player_id, npc_id = player.id, session.query(NPC).first().id

    _forced(monkeypatch, 18)
    result = roll_check(player_id, "stealth", "tricky", about_npc_id=npc_id)
    assert result["outcome"] == "success"
    assert perception.recalled_recognition(player_id, npc_id, 1) is False


def test_about_npc_id_is_ignored_on_any_other_skill(db, monkeypatch):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather="clear")
        player_id, npc_id = player.id, session.query(NPC).first().id

    _forced(monkeypatch, 2)
    roll_check(player_id, "persuasion", "hard", about_npc_id=npc_id)
    assert perception.recalled_recognition(player_id, npc_id, 1) is None


def test_a_refused_word_writes_no_memo(db):
    with get_session() as session:
        player, _ = _scene(session, hour=12, weather="clear")
        player_id, npc_id = player.id, session.query(NPC).first().id

    assert "error" in roll_check(player_id, "lockpicking", "hard", about_npc_id=npc_id)
    assert perception.recalled_recognition(player_id, npc_id, 1) is None
