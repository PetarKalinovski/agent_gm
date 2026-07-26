"""What strangers see: the pure half of `src/services/perception.py`.

Every function here is code-owned and deterministic — no LLM, no query. The
contract this module defends: `style` is ALWAYS non-NULL (no armor reads as
`common`, untagged armor derives from its gear tier), and a garbled authored
label costs a clamp, never a turn.
"""

import time

import pytest
from sqlalchemy import event

from src.models import (
    Faction,
    Item,
    Location,
    LocationType,
    NPC,
    NPCTier,
    Player,
    get_engine,
    get_session,
)
from src.services import perception
from src.services import progression as prog

# The frozen seam lands on Item in the sibling chunk; the round-trip tests that
# need it skip until it does.
_SEAM_LANDED = {"visual_desc", "attire"} <= set(Item.model_fields)


def _armor(name="Coat", tier="common", **extra) -> dict:
    item = {"id": prog._slug(name), "name": name, "type": "armor",
            "slot": "armor", "tier": tier, "quantity": 1}
    item.update(extra)
    return item


def _wearer(armor: dict | None = None, level: int = 10, **extra) -> Player:
    """A player row (never persisted) with `armor` equipped."""
    inventory = [armor] if armor else []
    equipment = {"armor": prog.item_key(armor)} if armor else {}
    return Player(name="Rook Fennick", level=level, xp=0, traits=[],
                  inventory=inventory, equipment=equipment, **extra)


# ---------------------------------------------------------------------------
# Style derivation — always non-NULL, always explainable
# ---------------------------------------------------------------------------


def test_no_armor_reads_as_an_ordinary_townsperson():
    profile = perception.perception_profile(_wearer())
    assert profile["style"] == perception.DEFAULT_STYLE == "common"
    assert profile["source"] == "bare"
    assert profile["read_as"] == "an ordinary townsperson"
    assert profile["uniform_faction_id"] is None
    assert profile["concealing"] is False


@pytest.mark.parametrize("tier,style", [
    ("crude", "rough"), ("common", "common"), ("fine", "fine"),
    ("superior", "fine"), ("master", "noble"), ("legendary", "noble"),
])
def test_untagged_armor_derives_style_from_its_tier(tier, style):
    profile = perception.perception_profile(_wearer(_armor(tier=tier)))
    assert profile["style"] == style
    assert profile["source"] == "tier"
    assert profile["read_as"] == perception.STYLE_READ_AS[style]


def test_authored_attire_wins_over_the_tier_fallback():
    armor = _armor(tier="master", attire={"style": "filthy", "concealing": False})
    profile = perception.perception_profile(_wearer(armor))
    assert profile["style"] == "filthy"
    assert profile["source"] == "attire"


def test_a_legacy_type_armor_item_is_still_read():
    """No `slot`, just `type: "armor"` — progression's repair rule applies."""
    legacy = {"id": "harbor_mail", "name": "Harbor Mail", "type": "armor", "quantity": 1}
    profile = perception.perception_profile(_wearer(legacy))
    assert profile["source"] == "tier" and profile["style"] == "common"


def test_a_weapon_never_changes_how_they_read():
    sword = {"id": "iron_sword", "name": "Iron Sword", "type": "weapon",
             "slot": "weapon", "tier": "master", "quantity": 1,
             "attire": {"style": "noble"}}
    player = Player(name="Rook", level=10, inventory=[sword],
                    equipment={"weapon": "iron_sword"})
    assert perception.perception_profile(player)["style"] == "common"


# ---------------------------------------------------------------------------
# read_as assembly — four branches, fixed templates
# ---------------------------------------------------------------------------


def test_read_as_concealed_beats_everything():
    armor = _armor(attire={"style": "rough", "uniform_faction_id": "watch",
                           "concealing": True})
    profile = perception.perception_profile(_wearer(armor), faction_name="Lantern Watch")
    assert profile["read_as"] == perception.CONCEALED_READ_AS
    assert profile["concealing"] is True


def test_read_as_names_a_resolved_uniform():
    armor = _armor(attire={"style": "common", "uniform_faction_id": "watch-id"})
    profile = perception.perception_profile(_wearer(armor), faction_name="Lantern Watch")
    assert profile["read_as"] == "a member of the Lantern Watch"
    assert profile["uniform_faction_name"] == "Lantern Watch"


def test_read_as_falls_back_when_the_uniform_faction_is_unknown():
    armor = _armor(attire={"style": "common", "uniform_faction_id": "watch-id"})
    profile = perception.perception_profile(_wearer(armor))
    assert profile["read_as"] == perception.UNKNOWN_COLOURS_READ_AS
    assert profile["uniform_faction_name"] is None


def test_visual_desc_is_appended_after_an_em_dash():
    armor = _armor(
        attire={"style": "common", "uniform_faction_id": "watch-id"},
        visual_desc="a boiled-leather Watch coat with the brass lantern badge",
    )
    profile = perception.perception_profile(_wearer(armor), faction_name="Lantern Watch")
    assert profile["read_as"] == (
        "a member of the Lantern Watch — a boiled-leather Watch coat with the "
        "brass lantern badge"
    )
    assert len(profile["read_as"]) <= perception.READ_AS_MAX
    # The door quotes only the base half, so the refusal line stays short
    assert perception.read_as_short(profile) == "a member of the Lantern Watch"


def test_read_as_truncates_on_a_word_boundary():
    armor = _armor(visual_desc="a " + "very " * 40 + "long coat")
    read_as = perception.perception_profile(_wearer(armor))["read_as"]
    assert len(read_as) <= perception.READ_AS_MAX
    assert read_as.endswith("…")
    assert not read_as[:-1].endswith(" "), "truncation must land on a word boundary"
    assert "very" in read_as and "ver…" not in read_as


def test_uniform_faction_name_is_none_without_a_uniform():
    profile = perception.perception_profile(_wearer(), faction_name="Lantern Watch")
    assert profile["uniform_faction_name"] is None
    assert profile["read_as"] == "an ordinary townsperson"


def test_profile_for_resolves_the_uniform_faction_name(db):
    with get_session() as session:
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        session.commit()
        armor = _armor(attire={"style": "common", "uniform_faction_id": watch.id})
        profile = perception.profile_for(session, _wearer(armor))
    assert profile["read_as"] == "a member of the The Lantern Watch"


def test_profile_for_survives_a_dangling_faction_id(db):
    with get_session() as session:
        armor = _armor(attire={"style": "common", "uniform_faction_id": "gone"})
        profile = perception.profile_for(session, _wearer(armor))
    assert profile["read_as"] == perception.UNKNOWN_COLOURS_READ_AS


# ---------------------------------------------------------------------------
# normalize_attire — the authoring boundary
# ---------------------------------------------------------------------------


def test_normalize_attire_clamps_an_unknown_style():
    assert perception.normalize_attire({"style": "resplendent"})["style"] == "common"


def test_normalize_attire_is_case_and_space_tolerant():
    assert perception.normalize_attire({"style": " NOBLE "})["style"] == "noble"


def test_normalize_attire_coerces_concealing_to_bool():
    assert perception.normalize_attire({"style": "fine", "concealing": "yes"})["concealing"] is True
    assert perception.normalize_attire({"style": "fine"})["concealing"] is False


def test_normalize_attire_blanks_an_empty_uniform_id():
    assert perception.normalize_attire({"style": "fine", "uniform_faction_id": "  "})[
        "uniform_faction_id"] is None
    assert perception.normalize_attire({"style": "fine", "uniform_faction_id": 7})[
        "uniform_faction_id"] is None


def test_normalize_attire_is_dropped_off_armor():
    assert perception.normalize_attire({"style": "noble"}, slot="weapon") is None
    assert perception.normalize_attire({"style": "noble"}, slot="trinket") is None
    assert perception.normalize_attire({"style": "noble"}, slot="armor")["style"] == "noble"


def test_normalize_attire_refuses_non_dicts():
    for raw in (None, "noble", 5, ["noble"]):
        assert perception.normalize_attire(raw) is None


def test_attire_of_only_reads_armor_entries():
    assert perception.attire_of(_armor(attire={"style": "noble"}))["style"] == "noble"
    sword = {"id": "s", "name": "S", "type": "weapon", "slot": "weapon",
             "tier": "fine", "attire": {"style": "noble"}}
    assert perception.attire_of(sword) is None
    assert perception.attire_of({"id": "x", "name": "Rock", "type": "misc"}) is None


# ---------------------------------------------------------------------------
# normalize_dress_code
# ---------------------------------------------------------------------------


def test_normalize_dress_code_drops_unknown_tokens():
    code = perception.normalize_dress_code({
        "min_style": "opulent",
        "banned_styles": ["filthy", "smelly", "CONCEALING", "filthy"],
        "reason": "  We keep standards.  ",
    })
    assert code["min_style"] is None
    assert code["banned_styles"] == ["filthy", "concealing"], "deduped, order-stable"
    assert code["reason"] == "We keep standards."


def test_normalize_dress_code_returns_none_for_an_empty_gate():
    assert perception.normalize_dress_code({"reason": "We are picky."}) is None
    assert perception.normalize_dress_code({"banned_styles": ["nonsense"]}) is None
    assert perception.normalize_dress_code(None) is None
    assert perception.normalize_dress_code("no hoods") is None


def test_normalize_dress_code_caps_the_reason_and_keeps_digits():
    code = perception.normalize_dress_code({
        "min_style": "fine", "reason": "No entry after the 9th bell. " + "x" * 400,
    })
    assert len(code["reason"]) == perception.DRESS_REASON_MAX
    assert "9th bell" in code["reason"], "a house rule may legitimately use a number"


# ---------------------------------------------------------------------------
# dress_verdict — pure
# ---------------------------------------------------------------------------


def _profile(style="common", concealing=False, visual_desc=None):
    armor = _armor(attire={"style": style, "concealing": concealing},
                   **({"visual_desc": visual_desc} if visual_desc else {}))
    return perception.perception_profile(_wearer(armor))


def test_no_dress_code_admits_anyone():
    assert perception.dress_verdict(_profile("filthy"), None)["allowed"] is True
    assert perception.dress_verdict(_profile("filthy"), {})["allowed"] is True


def test_min_style_blocks_below_and_admits_at_equal_rank():
    code = {"min_style": "fine", "reason": "Members and their guests only."}
    assert perception.dress_verdict(_profile("common"), code)["allowed"] is False
    assert perception.dress_verdict(_profile("fine"), code)["allowed"] is True
    assert perception.dress_verdict(_profile("noble"), code)["allowed"] is True


def test_min_style_failure_names_the_rule_and_the_reading():
    code = {"min_style": "noble", "reason": "The box is the Baron's."}
    verdict = perception.dress_verdict(_profile("rough"), code)
    assert verdict["failed"] == "min_style" and verdict["token"] == "rough"
    assert verdict["requirement"] == (
        "The box is the Baron's. (the player currently reads as someone who "
        "works with their hands)"
    )


def test_a_banned_style_blocks_regardless_of_rank():
    code = {"min_style": None, "banned_styles": ["noble"], "reason": "No lords in here."}
    verdict = perception.dress_verdict(_profile("noble"), code)
    assert verdict["allowed"] is False and verdict["failed"] == "banned"
    assert verdict["token"] == "noble"


def test_concealing_is_banned_independently_of_style():
    code = {"banned_styles": ["concealing"], "reason": "No covered faces."}
    assert perception.dress_verdict(_profile("noble", concealing=True), code)["token"] == "concealing"
    assert perception.dress_verdict(_profile("noble"), code)["allowed"] is True


def test_banned_beats_min_style_when_both_would_fire():
    code = {"min_style": "fine", "banned_styles": ["filthy"], "reason": "Not here."}
    verdict = perception.dress_verdict(_profile("filthy"), code)
    assert verdict["failed"] == "banned", "the specific rule is the one the door quotes"


def test_a_reasonless_gate_still_says_something():
    verdict = perception.dress_verdict(_profile("filthy"), {"banned_styles": ["filthy"]})
    assert verdict["reason"] == perception.DEFAULT_DRESS_REASON
    assert verdict["requirement"].startswith(perception.DEFAULT_DRESS_REASON)


def test_the_seeded_lantern_refusal_reads_like_a_sentence():
    code = perception.normalize_dress_code({
        "banned_styles": ["concealing", "filthy"],
        "reason": ("Maera keeps a warm room and a clean floor: no covered faces "
                   "under her roof, and nobody who has been sleeping in the mudflats."),
    })
    verdict = perception.dress_verdict(_profile("rough", concealing=True), code)
    assert verdict["requirement"].endswith(
        "(the player currently reads as someone who does not want to be seen)"
    )


# ---------------------------------------------------------------------------
# appearance_lines — the DM context lines (and the per-turn budget)
# ---------------------------------------------------------------------------


def _seeded_scene(session, *, armor=None, npc_faction=None):
    """Square with Brann in it; the player wears `armor` if given."""
    watch = Faction(name="The Lantern Watch")
    session.add(watch)
    session.flush()
    square = Location(name="Market Square", type=LocationType.DISTRICT,
                      controlling_faction_id=watch.id)
    session.add(square)
    session.flush()
    brann = NPC(name="Captain Brann Coldiron", tier=NPCTier.MAJOR,
                current_location_id=square.id,
                faction_id=watch.id if npc_faction else None)
    session.add(brann)
    inventory = [armor] if armor else []
    player = Player(name="Rook Fennick", level=10, inventory=inventory,
                    equipment={"armor": prog.item_key(armor)} if armor else {},
                    current_location_id=square.id)
    session.add(player)
    session.commit()
    return {"player": player.id, "npc": brann.id, "watch": watch.id,
            "square": square.id}


def test_appearance_lines_are_one_line_for_a_plain_player(db):
    with get_session() as session:
        ids = _seeded_scene(session)
    lines = perception.appearance_lines(ids["player"])
    assert lines == ["- Player reads as: an ordinary townsperson"]


def test_appearance_lines_warn_about_a_uniform_in_the_room(db):
    with get_session() as session:
        ids = _seeded_scene(session, npc_faction=True)
    with get_session() as session:
        player = session.get(Player, ids["player"])
        player.inventory = [_armor(
            "Lantern Watch Coat",
            attire={"style": "common", "uniform_faction_id": ids["watch"]},
            visual_desc="a boiled-leather Watch coat with the brass lantern badge",
        )]
        player.equipment = {"armor": "lantern_watch_coat"}
        session.commit()

    lines = perception.appearance_lines(ids["player"])
    assert lines[0].startswith("- Player reads as: a member of the The Lantern Watch")
    assert len(lines) == 2
    assert "Captain Brann Coldiron" in lines[1]
    assert "that is a crime, not a misunderstanding" in lines[1]


def test_appearance_lines_use_the_lite_uniform_warning_alone(db):
    with get_session() as session:
        ids = _seeded_scene(session, npc_faction=False)
    with get_session() as session:
        player = session.get(Player, ids["player"])
        player.inventory = [_armor(
            "Lantern Watch Coat",
            attire={"style": "common", "uniform_faction_id": ids["watch"]},
        )]
        player.equipment = {"armor": "lantern_watch_coat"}
        session.commit()

    lines = perception.appearance_lines(ids["player"])
    assert len(lines) == 2
    assert lines[1].endswith("will assume they are The Lantern Watch, and act accordingly.")


def test_appearance_lines_name_the_recognition_check_for_a_hood(db):
    from src.models import NPCRelationship, WorldClock

    with get_session() as session:
        ids = _seeded_scene(session, armor=_armor(
            "Ashfall Hood", attire={"style": "rough", "concealing": True}))
        session.add(WorldClock(day=1, hour=8))
        session.add(NPCRelationship(npc_id=ids["npc"], player_id=ids["player"],
                                    trust_level=50))
        session.commit()

    perception.clear_recognition()
    lines = perception.appearance_lines(ids["player"])
    assert len(lines) == 2
    assert "Their face is covered." in lines[1]
    assert 'roll_check(player_id, "stealth", "tricky"' in lines[1]
    assert f'about_npc_id="{ids["npc"]}"' in lines[1]

    # Once the roll has landed, the line stops asking and starts telling
    perception.remember_recognition(ids["player"], ids["npc"], recognized=True, day=1)
    try:
        placed = perception.appearance_lines(ids["player"])
        assert "has already placed them today" in placed[1]
        assert "roll_check" not in placed[1]
    finally:
        perception.clear_recognition()


def test_appearance_lines_ignore_strangers_under_the_hood(db):
    from src.models import WorldClock

    with get_session() as session:
        ids = _seeded_scene(session, armor=_armor(
            "Ashfall Hood", attire={"style": "rough", "concealing": True}))
        session.add(WorldClock(day=1, hour=8))
        session.commit()

    lines = perception.appearance_lines(ids["player"])
    assert lines == ["- Player reads as: someone who does not want to be seen"]


def test_appearance_lines_never_raise_on_a_missing_player(db):
    assert perception.appearance_lines("nobody") == []
    assert perception.appearance_lines("") == []


def test_appearance_lines_query_budget(db):
    """It sits on the per-turn hot path — count the reads, not the vibes."""
    from src.models import NPCRelationship, WorldClock

    with get_session() as session:
        ids = _seeded_scene(session, npc_faction=True, armor=_armor(
            "Ashfall Hood", attire={"style": "rough", "uniform_faction_id": "w",
                                    "concealing": True}))
        session.add(WorldClock(day=1, hour=8))
        session.add(NPCRelationship(npc_id=ids["npc"], player_id=ids["player"]))
        session.commit()

    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        started = time.perf_counter()
        perception.appearance_lines(ids["player"])
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # player + faction + npcs + relationships + clock — the worst case
    assert len(selects) <= 5, selects
    assert elapsed_ms < 20, f"{elapsed_ms:.1f}ms on the per-turn path"


# ---------------------------------------------------------------------------
# The frozen seam: both authoring tools accept the two fields
# ---------------------------------------------------------------------------


def test_authoring_tools_accept_the_seam_params_today(player):
    """Whatever the merge order, passing the two fields must never raise."""
    from src.tools.world_write.items import create_item_template
    from src.tools.world_write.player import add_to_inventory

    template = create_item_template(
        "ashfall_hood", "Ashfall Hood", "armor", 0, "A hood.",
        slot="armor", tier="common",
        visual_desc="a soot-grey hood that swallows the face",
        attire={"style": "rough", "concealing": True},
    )
    assert template["success"] is True

    added = add_to_inventory(
        player, "Ashfall Hood", "armor", "A hood.", slot="armor", tier="common",
        visual_desc="a soot-grey hood that swallows the face",
        attire={"style": "rough", "concealing": True},
    )
    assert added["success"] is True


@pytest.mark.skipif(not _SEAM_LANDED,
                    reason="Item.visual_desc / Item.attire land in the sibling chunk")
def test_add_to_inventory_persists_normalized_attire(player):
    from src.tools.world_write.player import add_to_inventory

    add_to_inventory(player, "Ashfall Hood", "armor", "A hood.",
                     slot="armor", tier="common",
                     visual_desc="a soot-grey hood that swallows the face",
                     attire={"style": "obscure", "concealing": 1})
    # A sword is not a garment: the attire is dropped, not refused
    add_to_inventory(player, "Iron Sword", "weapon", "A sword.",
                     slot="weapon", tier="common",
                     attire={"style": "noble"})

    with get_session() as session:
        row = session.get(Player, player)
        entries = {e["name"]: e for e in row.inventory}

    assert entries["Ashfall Hood"]["attire"] == {
        "style": "common", "uniform_faction_id": None, "concealing": True,
    }
    assert entries["Ashfall Hood"]["visual_desc"] == "a soot-grey hood that swallows the face"
    assert entries["Iron Sword"]["attire"] is None

    with get_session() as session:
        row = session.get(Player, player)
        profile = perception.perception_profile(row)
    assert profile["concealing"] is True, "the hood auto-equipped and now reads"


@pytest.mark.skipif(not _SEAM_LANDED,
                    reason="Item.visual_desc / Item.attire land in the sibling chunk")
def test_buying_a_garment_carries_its_attire_to_the_buyer(db):
    """The day-one loop: a hood bought off a merchant must still conceal."""
    from src.tools.world_write.items import transfer_item

    hood = {
        "id": "ashfall_hood", "name": "Ashfall Hood", "type": "armor",
        "value": 50, "description": "Fangs work.", "stackable": False,
        "quantity": 1, "slot": "armor", "tier": "common",
        "visual_desc": "a soot-grey hood that swallows the face",
        "attire": {"style": "rough", "uniform_faction_id": None, "concealing": True},
    }
    with get_session() as session:
        vex = NPC(name="Vex Marrow", tier=NPCTier.MINOR, inventory_notable=[hood])
        buyer = Player(name="Rook Fennick", level=10, currency=500)
        session.add_all([vex, buyer])
        session.commit()
        vex_id, buyer_id = vex.id, buyer.id

    result = transfer_item(vex_id, buyer_id, "ashfall_hood", from_type="npc",
                           to_type="player", is_purchase=True)
    assert result["success"] is True

    with get_session() as session:
        row = session.get(Player, buyer_id)
        entry = row.inventory[0]
        profile = perception.perception_profile(row)

    assert entry["attire"]["concealing"] is True
    assert entry["visual_desc"] == "a soot-grey hood that swallows the face"
    assert profile["concealing"] is True, "it auto-equipped and the world now reads it"
    assert profile["read_as"] == (
        "someone who does not want to be seen — a soot-grey hood that swallows the face"
    )


@pytest.mark.skipif(not _SEAM_LANDED,
                    reason="Item.visual_desc / Item.attire land in the sibling chunk")
def test_stock_merchant_carries_attire_onto_the_shelf(db):
    from src.tools.world_write.progression import stock_merchant

    with get_session() as session:
        npc = NPC(name="Vex Marrow", tier=NPCTier.MINOR)
        session.add(npc)
        session.commit()
        npc_id = npc.id

    stock_merchant(npc_id, [{
        "kind": "armor", "tier": "common", "name": "Ashfall Hood",
        "visual_desc": "a soot-grey hood that swallows the face",
        "attire": {"style": "rough", "concealing": True},
    }])

    with get_session() as session:
        entry = session.get(NPC, npc_id).inventory_notable[0]
    assert entry["attire"]["concealing"] is True
    assert entry["attire"]["style"] == "rough"
    assert entry["visual_desc"] == "a soot-grey hood that swallows the face"
