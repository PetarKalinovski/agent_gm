"""Who the world thinks the player is — identity, not clothes.

The structural claim this module defends: identity flows down the left column
(does this NPC have a relationship row?), appearance flows in from the profile
(what does the uniform claim?), and the two never touch. A Watch coat makes a
stranger treat you as Watch whether or not they know your name; an
acquaintance knows you whatever you wear.
"""

import pytest

from src.models import (
    Faction,
    NPC,
    NPCRelationship,
    NPCTier,
    Player,
    WorldClock,
    get_session,
)
from src.services import perception
from src.services import progression as prog


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


def _scene(session, *, armor=None, trust=50, moments=(), met=True,
           faction_name=None):
    """Maera in the tavern, optionally already acquainted with the player."""
    faction = None
    if faction_name:
        faction = Faction(name=faction_name)
        session.add(faction)
        session.flush()

    npc = NPC(name="Maera Duskwell", tier=NPCTier.MINOR)
    session.add(npc)
    player = Player(
        name="Rook Fennick", level=10,
        inventory=[armor] if armor else [],
        equipment={"armor": prog.item_key(armor)} if armor else {},
    )
    session.add(player)
    session.add(WorldClock(day=3, hour=12))
    session.flush()
    if met:
        session.add(NPCRelationship(npc_id=npc.id, player_id=player.id,
                                    trust_level=trust, key_moments=list(moments)))
    session.commit()
    return player, npc, (faction.id if faction else None)


# ---------------------------------------------------------------------------
# Familiarity — the relationship row is the only signal
# ---------------------------------------------------------------------------


def test_no_relationship_row_is_a_stranger(db):
    with get_session() as session:
        player, npc, _ = _scene(session, met=False)
        assert perception.familiarity(session, player.id, npc.id) == "stranger"


def test_a_plain_relationship_row_is_acquainted(db):
    with get_session() as session:
        player, npc, _ = _scene(session, trust=50)
        assert perception.familiarity(session, player.id, npc.id) == "acquainted"


def test_high_trust_reads_as_close(db):
    with get_session() as session:
        player, npc, _ = _scene(session, trust=perception.CLOSE_TRUST)
        assert perception.familiarity(session, player.id, npc.id) == "close"


def test_enough_key_moments_read_as_close(db):
    with get_session() as session:
        player, npc, _ = _scene(session, trust=10, moments=("a", "b", "c"))
        assert perception.familiarity(session, player.id, npc.id) == "close"


def test_familiarity_needs_both_ids(db):
    with get_session() as session:
        player, npc, _ = _scene(session)
        assert perception.familiarity(session, "", npc.id) == "stranger"
        assert perception.familiarity(session, player.id, "") == "stranger"


# ---------------------------------------------------------------------------
# The truth table — three familiarities x concealed/not
# ---------------------------------------------------------------------------

_HOOD = {"style": "rough", "concealing": True}


@pytest.mark.parametrize("met,trust,moments,concealed,verdict,known,difficulty", [
    (False, 50, (), False, "unknown", False, None),
    (False, 50, (), True, "unknown", False, None),
    (True, 50, (), False, "known", True, None),
    (True, 50, (), True, "contested", False, "tricky"),
    (True, 90, (), False, "known", True, None),
    (True, 90, (), True, "contested", False, "hard"),
])
def test_the_recognition_truth_table(db, met, trust, moments, concealed,
                                     verdict, known, difficulty):
    armor = _armor(attire=dict(_HOOD)) if concealed else _armor()
    with get_session() as session:
        player, npc, _ = _scene(session, armor=armor, trust=trust,
                                moments=moments, met=met)
        result = perception.recognition(session, player, npc)

    assert result["verdict"] == verdict
    assert result["identity_known"] is known
    assert result["reputation_visible"] is known
    if difficulty is None:
        assert result["check"] is None
    else:
        assert result["check"] == {"skill": "stealth", "difficulty": difficulty,
                                   "about_npc_id": npc.id}
        assert f'about_npc_id="{npc.id}"' in result["line"]


def test_a_stranger_is_never_rolled_for_even_hooded(db):
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor(attire=dict(_HOOD)), met=False)
        result = perception.recognition(session, player, npc)
    assert result["check"] is None, "no roll, ever — they have nothing to place"


def test_recognition_never_touches_the_rng(db):
    prog.set_seed(11)
    before = prog._RNG.getstate()
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor(attire=dict(_HOOD)))
        perception.recognition(session, player, npc)
    assert prog._RNG.getstate() == before, "code never rolls recognition"


# ---------------------------------------------------------------------------
# The structural separation: what the clothes claim vs who they are
# ---------------------------------------------------------------------------


def test_a_uniform_is_assumed_even_by_a_total_stranger(db):
    with get_session() as session:
        player, npc, faction_id = _scene(session, met=False,
                                         faction_name="The Lantern Watch")
        player.inventory = [_armor("Watch Coat", attire={
            "style": "common", "uniform_faction_id": faction_id})]
        player.equipment = {"armor": "watch_coat"}
        session.commit()
        profile = perception.profile_for(session, player)
        result = perception.recognition(session, player, npc, profile=profile)

    assert result["identity_known"] is False, "the coat is not a name"
    assert result["assumed_faction_id"] == faction_id
    assert result["assumed_faction_name"] == "The Lantern Watch"
    assert result["reputation_visible"] is False


def test_an_acquaintance_knows_them_and_the_coat_is_still_assumed(db):
    with get_session() as session:
        player, npc, faction_id = _scene(session, faction_name="The Lantern Watch")
        player.inventory = [_armor("Watch Coat", attire={
            "style": "common", "uniform_faction_id": faction_id})]
        player.equipment = {"armor": "watch_coat"}
        session.commit()
        profile = perception.profile_for(session, player)
        result = perception.recognition(session, player, npc, profile=profile)

    assert result["identity_known"] is True
    assert result["assumed_faction_id"] == faction_id, "the two facts are independent"


# ---------------------------------------------------------------------------
# The memo — a contested answer sticks for the game-day
# ---------------------------------------------------------------------------


def test_memo_write_read_and_day_staleness():
    perception.remember_recognition("p1", "n1", recognized=True, day=3)
    assert perception.recalled_recognition("p1", "n1", 3) is True
    assert perception.recalled_recognition("p1", "n1", 4) is None, "a new day, a new hood"
    assert perception.recalled_recognition("p1", "other", 3) is None
    assert perception.recalled_recognition("other", "n1", 3) is None


def test_clear_recognition_is_scoped_then_global():
    perception.remember_recognition("p1", "n1", recognized=True, day=1)
    perception.remember_recognition("p2", "n1", recognized=False, day=1)
    perception.clear_recognition("p1")
    assert perception.recalled_recognition("p1", "n1", 1) is None
    assert perception.recalled_recognition("p2", "n1", 1) is False
    perception.clear_recognition()
    assert perception.recalled_recognition("p2", "n1", 1) is None


def test_a_live_memo_overrides_contested(db):
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor(attire=dict(_HOOD)))
        assert perception.recognition(session, player, npc)["verdict"] == "contested"

        perception.remember_recognition(player.id, npc.id, recognized=True, day=3)
        made = perception.recognition(session, player, npc)
        assert made["verdict"] == "known" and made["identity_known"] is True
        assert made["check"] is None, "never ask twice in one scene"

        perception.remember_recognition(player.id, npc.id, recognized=False, day=3)
        hidden = perception.recognition(session, player, npc)
        assert hidden["verdict"] == "unknown" and hidden["identity_known"] is False


def test_a_stale_memo_asks_again(db):
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor(attire=dict(_HOOD)))
        perception.remember_recognition(player.id, npc.id, recognized=True, day=1)
        # The seeded clock says day 3
        assert perception.recognition(session, player, npc)["verdict"] == "contested"


# ---------------------------------------------------------------------------
# The NPC-facing block — four variants, code-owned
# ---------------------------------------------------------------------------


def test_block_for_a_plain_stranger(db):
    with get_session() as session:
        player, npc, _ = _scene(session, met=False)
        block = perception.npc_recognition_block(session, player, npc)
    assert block.startswith("**Who you see (the engine decided this")
    assert "You do not know this person. What you see: an ordinary townsperson." in block
    assert "Do not use a name you were never told" in block
    assert "Rook Fennick" not in block


def test_block_for_a_stranger_in_uniform_names_the_faction(db):
    with get_session() as session:
        player, npc, faction_id = _scene(session, met=False,
                                         faction_name="The Lantern Watch")
        player.inventory = [_armor("Watch Coat", attire={
            "style": "common", "uniform_faction_id": faction_id})]
        player.equipment = {"armor": "watch_coat"}
        session.commit()
        block = perception.npc_recognition_block(session, player, npc)

    assert "treat them as The Lantern Watch" in block
    assert "You do not know their name." in block
    assert "Rook Fennick" not in block


def test_block_for_an_acquaintance_names_them_and_the_costume(db):
    with get_session() as session:
        player, npc, faction_id = _scene(session, faction_name="The Lantern Watch")
        player.inventory = [_armor("Watch Coat", attire={
            "style": "common", "uniform_faction_id": faction_id})]
        player.equipment = {"armor": "watch_coat"}
        session.commit()
        block = perception.npc_recognition_block(session, player, npc)

    assert "You know this person: Rook Fennick." in block
    assert "You recognise them on sight, whatever they are wearing." in block
    assert "What they have on: a member of the The Lantern Watch" in block
    assert "You may absolutely have an opinion about that." in block


def test_block_for_a_contested_hood_forbids_the_name(db):
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor("Ashfall Hood", attire=dict(_HOOD)))
        block = perception.npc_recognition_block(session, player, npc)

    assert "This person's face is covered" in block
    assert "you have NOT placed them" in block
    assert "do not use their name" in block
    assert "Rook Fennick" not in block


def test_a_resolved_hood_flips_the_block_to_known(db):
    with get_session() as session:
        player, npc, _ = _scene(session, armor=_armor("Ashfall Hood", attire=dict(_HOOD)))
        perception.remember_recognition(player.id, npc.id, recognized=True, day=3)
        block = perception.npc_recognition_block(session, player, npc)
    assert "You know this person: Rook Fennick." in block
