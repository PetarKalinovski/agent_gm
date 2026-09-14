"""Standing has teeth: prices, doors, and conversations react in code."""

import pytest

from src.models import (
    Connection, Faction, Location, LocationType, NPC, NPCRelationship, Player, get_session,
)
from src.services import standing
from src.services.travel import check_route
from src.tools import agents_as_tools
from src.tools.world_write.npcs import reveal_secret
from src.tools.world_write.player import move_player


def _world(session, *, reputation=None, disposition=None, trust=None):
    watch = Faction(name="Lantern Watch", ideology="Order on the north road")
    session.add(watch)
    session.flush()
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    barracks = Location(name="Watch House", type=LocationType.BUILDING, depth=0, discovered=True,
                        controlling_faction_id=watch.id)
    session.add_all([square, barracks])
    session.flush()
    session.add(Connection(from_location_id=square.id, to_location_id=barracks.id, travel_type="walk",
                           travel_time_hours=0.1, bidirectional=True, discovered=True))
    brann = NPC(name="Captain Brann Coldiron", current_location_id=square.id, faction_id=watch.id,
                secrets=["The Watch pays Vex to keep the road quiet."],
                inventory_notable=[{"id": "watch_coat", "name": "Watch Coat", "type": "armor", "value": 40}])
    session.add(brann)
    player = Player(name="Rook", current_location_id=square.id, currency=100,
                    reputation={watch.id: reputation} if reputation is not None else {})
    session.add(player)
    session.flush()
    if disposition is not None or trust is not None:
        session.add(NPCRelationship(npc_id=brann.id, player_id=player.id,
                                    current_disposition=disposition or "neutral",
                                    trust_level=50 if trust is None else trust))
    session.commit()
    return {"player": player.id, "watch": watch.id, "square": square.id, "barracks": barracks.id, "brann": brann.id}


def test_bands_and_the_legacy_default():
    assert standing.standing_label(95) == "honoured"
    assert standing.standing_label(65) == "liked"
    assert standing.standing_label(50) == "neutral"
    assert standing.standing_label(25) == "disliked"
    assert standing.standing_label(5) == "hostile"
    player = Player(name="x", reputation=None)
    assert standing.standing_for(player, "f1") == "neutral"
    assert standing.reputation_of(player, None) == standing.REPUTATION_DEFAULT


def test_prices_follow_standing_and_disposition_and_clamp(db):
    with get_session() as session:
        world = _world(session)
        player = session.get(Player, world["player"])
        brann = session.get(NPC, world["brann"])
        assert standing.price_multiplier(session, player, brann) == (1.0, ""), "nothing known = legacy price"
        player.reputation = {world["watch"]: 90}
        mul, reason = standing.price_multiplier(session, player, brann)
        assert mul == 0.8 and "honoured by Lantern Watch" in reason
        player.reputation = {world["watch"]: 5}
        session.add(NPCRelationship(npc_id=brann.id, player_id=player.id, current_disposition="hostile", trust_level=10))
        session.flush()
        mul, reason = standing.price_multiplier(session, player, brann)
        assert mul == pytest.approx(min(standing.MUL_CEIL, 1.40 * 1.25))
        assert "hated by Lantern Watch" in reason and "dislikes them" in reason
    assert standing.buy_price(40, 1.0) == 40 and standing.buy_price(40, 1.15) == 46
    assert standing.buy_price(0, 1.4) == 0, "a free thing stays free"
    assert standing.sell_price(40) == 16 and standing.sell_price(0) == 0
    assert standing.sell_price(1) == 1, "never below one coin for a real thing"


def test_a_hostile_faction_closes_its_doors_but_a_pardon_is_not_a_pass(db):
    with get_session() as session:
        world = _world(session, reputation=5)
        player = session.get(Player, world["player"])
        route = check_route(session, world["square"], world["barracks"], player)
        assert route["blocked"] is True and route["reason"] == "standing"
        assert "Lantern Watch hold Watch House" in route["requirements"][0]
        # No player: a map question, not a person
        assert check_route(session, world["square"], world["barracks"], None)["blocked"] is False
    blocked = move_player(world["player"], world["barracks"])
    assert blocked.get("blocked") and "closed to them" in blocked["message"]
    assert move_player(world["player"], world["barracks"], requirements_met=True).get("success")
    with get_session() as session:
        conn = session.query(Connection).first()
        assert not (conn.cleared_for or []), "standing is never recorded as a cleared road"


def test_standing_never_fires_without_a_controlling_faction_or_when_liked(db):
    with get_session() as session:
        world = _world(session, reputation=5)
        session.get(Location, world["barracks"]).controlling_faction_id = None
        session.commit()
        player = session.get(Player, world["player"])
        assert check_route(session, world["square"], world["barracks"], player)["blocked"] is False
    with get_session() as session:
        world = _world(session, reputation=70)
        player = session.get(Player, world["player"])
        assert check_route(session, world["square"], world["barracks"], player)["blocked"] is False


def test_dress_code_keeps_precedence_over_standing(db):
    from src.services.perception import normalize_dress_code
    with get_session() as session:
        world = _world(session, reputation=5)
        barracks = session.get(Location, world["barracks"])
        barracks.dress_code = normalize_dress_code({"min_style": "noble", "banned_styles": [], "reason": "Nobles only."})
        session.commit()
        player = session.get(Player, world["player"])
        route = check_route(session, world["square"], world["barracks"], player)
        assert route["reason"] == "dress" and len(route["requirements"]) == 2


def test_a_hostile_stranger_will_not_talk_and_no_model_is_built(db, monkeypatch):
    with get_session() as session:
        world = _world(session, disposition="hostile", trust=10)

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("an NPCAgent was constructed for a refused conversation")

    monkeypatch.setattr("src.agents.npc_agent.NPCAgent", Boom)
    monkeypatch.setattr(agents_as_tools, "get_recent_dm_context", lambda *a, **k: "", raising=False)
    result = agents_as_tools.prompt_npc_agent(world["player"], world["brann"], "Captain, a word.")
    assert result["refused"] is True and result["conversation_ended"] is True
    event = result["events"][0]
    assert event["event"] == "speech" and event["tone"] == "hostile"
    assert event["text"] == standing.refusal_line(world["brann"])
    assert "Do NOT put words in their mouth" in result["note"]
    with get_session() as session:
        rel = session.query(NPCRelationship).first()
        assert rel.recent_messages and rel.recent_messages[-1]["content"] == event["text"], "the cold shoulder is remembered"


def test_hostile_but_trusted_still_talks(db, monkeypatch):
    with get_session() as session:
        world = _world(session, disposition="hostile", trust=standing.REFUSAL_TRUST)
    seen = {}

    class Fake:
        def __init__(self, *a, **k):
            seen["built"] = True
            raise RuntimeError("stop here")

    monkeypatch.setattr("src.agents.npc_agent.NPCAgent", Fake)
    monkeypatch.setattr(agents_as_tools, "get_recent_dm_context", lambda *a, **k: "", raising=False)
    result = agents_as_tools.prompt_npc_agent(world["player"], world["brann"], "Captain.")
    assert seen.get("built") is True and "refused" not in result


def test_reveal_secret_is_trust_gated_and_registered(db):
    from src.agents.npc_agent import NPC_TOOLS
    assert any(getattr(t, "tool_name", getattr(t, "__name__", "")) == "reveal_secret" for t in NPC_TOOLS)
    with get_session() as session:
        world = _world(session, disposition="neutral", trust=standing.SECRET_TRUST - 1)
    refused = reveal_secret(world["brann"], world["player"], 0)
    assert refused["refused"] is True and refused["reason"] == "trust"
    with get_session() as session:
        assert not (session.query(NPCRelationship).first().revealed_secrets or [])
        session.query(NPCRelationship).first().trust_level = standing.SECRET_TRUST
        session.commit()
    told = reveal_secret(world["brann"], world["player"], 0)
    assert told["secret"].startswith("The Watch pays Vex")
