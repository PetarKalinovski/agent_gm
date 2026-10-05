"""A fight on offer: "Fight the wolves" when a beast or someone hostile is here.

fleet5-bram (a brawler who "picks fights he might not win") stood among the
Hollow's wolves and was offered talk and doors; a wolf raked him in the prose
and the fight was never his choice. The chip comes from the encounter
director's own roster (no party member, no story villain, no boss) and waits
while a parley is under way.
"""

import pytest

from src.models import Location, LocationType, NPC, NPCRelationship, Player, get_session
from src.services import encounters
from src.services.suggestions import MAX_CHIPS, suggest_actions


@pytest.fixture()
def den(db):
    encounters._parley.clear()
    encounters._talking.clear()
    with get_session() as session:
        place = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True,
                         objects=[{"kind": "take", "label": "Cracked bone whistle", "x": 40, "y": 60}])
        session.add(place)
        session.flush()
        people = {
            "gray": NPC(name="Gray-Flank Wolf", species="wolf", danger_tier="weak", current_location_id=place.id),
            "hollow": NPC(name="Hollow Wolf", species="wolf", danger_tier="weak", current_location_id=place.id),
            "vane": NPC(name="Aldric Vane", danger_tier="boss", current_location_id=place.id),
            "piper": NPC(name="Piper Quillow", danger_tier="harmless", current_location_id=place.id),
        }
        session.add_all(people.values())
        player = Player(name="Bram Holloway", current_location_id=place.id)
        session.add(player)
        session.flush()
        session.add(NPCRelationship(npc_id=people["piper"].id, player_id=player.id))
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, place=place.id)
        return ids


def _chips(w, lead=None):
    with get_session() as session:
        player = session.get(Player, w["player"])
        return suggest_actions(session, player, lead, player.current_location_id)


def test_a_pack_here_is_one_fight_chip_right_after_the_lead(den):
    chips = _chips(den)
    assert 2 <= len(chips) <= MAX_CHIPS
    assert chips[0] == {"kind": "fight", "npc_id": den["gray"], "label": "Fight the wolves",
                        "text": "I attack the wolves."}
    assert sum(1 for c in chips if c["kind"] == "fight") == 1
    assert not any("Vane" in c["label"] or "Aldric" in c["label"] for c in chips)   # the boss is the story's
    assert any(c["kind"] == "interact" for c in chips)                                # the thing keeps its slot


def test_one_beast_is_named_as_itself(den):
    with get_session() as session:
        session.get(NPC, den["hollow"]).status = "dead"
        session.commit()
    assert _chips(den)[0]["label"] == "Fight the Gray-Flank Wolf"


def test_a_hostile_stranger_is_fought_by_their_role_label(den):
    with get_session() as session:
        for key in ("gray", "hollow"):
            session.get(NPC, den[key]).status = "dead"
        session.add(NPC(name="Vex Marrow", profession="scout", danger_tier="dangerous",
                        current_mood="hostile", current_location_id=den["place"],
                        description_physical="a lean man in an ash-grey cloak"))
        session.commit()
    chip = _chips(den)[0]
    assert chip["kind"] == "fight" and chip["label"].startswith("Fight the ")
    assert "Vex" not in chip["label"] and "Marrow" not in chip["label"]


def test_no_fight_chip_mid_parley_or_with_nobody_to_fight(den):
    encounters._talking[den["player"]] = True
    assert not any(c["kind"] == "fight" for c in _chips(den))
    encounters._talking[den["player"]] = False
    with get_session() as session:
        for key in ("gray", "hollow"):
            session.get(NPC, den[key]).status = "dead"
        session.commit()
    assert not any(c["kind"] == "fight" for c in _chips(den))      # Piper is no enemy; Vane is the story's


def test_a_kill_lead_on_the_same_beast_is_not_offered_twice(den):
    lead = {"text": "Kill the Gray-Flank Wolf", "kind": "kill", "source": "quest", "quest_title": "Teeth",
            "target": {"type": "npc", "id": den["gray"], "name": "Gray-Flank Wolf", "here": True},
            "route_next_hop_location_id": None}
    chips = _chips(den, lead)
    assert chips[0]["kind"] == "lead" and chips[0]["label"].startswith("Attack")
    assert not any(c["kind"] == "fight" for c in chips)


def test_someone_met_is_fought_by_the_name_the_player_knows(den):
    with get_session() as session:
        for key in ("gray", "hollow"):
            session.get(NPC, den[key]).status = "dead"
        vex = NPC(name="Vex Marrow", profession="scout", danger_tier="dangerous",
                  current_mood="hostile", current_location_id=den["place"])
        session.add(vex)
        session.flush()
        session.add(NPCRelationship(npc_id=vex.id, player_id=den["player"]))
        session.commit()
    assert _chips(den)[0]["label"] == "Fight Vex Marrow"


def test_the_chips_under_a_talk_hold_the_fight_back(den, tmp_path, monkeypatch):
    """play_sse noted the parley AFTER it built the post-turn state, so the
    reply that WAS the talk still offered "Fight the wolves" beneath it (and
    the chip stayed hidden a turn after the talk ended)."""
    import asyncio
    import json

    from src.models import WorldClock
    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21))
        session.commit()

    class Agent:
        messages: list = []

        async def stream_async(self, context):
            yield {"data": 'Aldric Vane (low): "Ask your question, then. Quickly."\n'}

    class DM:
        agent = Agent()
        last_brief = {"present": {den["vane"]: "Aldric Vane", den["gray"]: "Gray-Flank Wolf",
                                  den["hollow"]: "Hollow Wolf"}}
        _player_name = "Bram Holloway"

        def _build_context(self, player_input, **_kwargs):
            return f"Player says: {player_input}"

    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": DM(), "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(den["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input="Who sent you?", player_id=den["player"]))
        return [chunk async for chunk in response.body_iterator]

    states = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frame = json.loads(line[6:])
                if frame.get("type") == "state":
                    states.append(frame["state"])
    assert encounters.in_parley(den["player"]) is True
    assert states and not any(c["kind"] == "fight" for c in states[-1]["suggestions"])


def test_a_kill_lead_on_any_of_the_pack_is_the_fight_already(den):
    """The roster is the pack: "Attack the Hollow Wolf" (the lead) and "Fight
    the wolves" are one choice in two chips."""
    lead = {"text": "Kill the Hollow Wolf", "kind": "kill", "source": "quest", "quest_title": "Teeth",
            "target": {"type": "npc", "id": den["hollow"], "name": "Hollow Wolf", "here": True},
            "route_next_hop_location_id": None}
    chips = _chips(den, lead)
    assert chips[0]["kind"] == "lead" and chips[0]["npc_id"] == den["hollow"]
    assert not any(c["kind"] == "fight" for c in chips)
