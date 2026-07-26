"""Cross-system tests: the seams between the five overhaul systems.

Each system has its own module pinning its own rules. What is pinned HERE is
what only breaks when two of them are wired together — a quest completion that
has to pay XP *and* reach the client as three ordered events, an engine-applied
fight that must not then be flagged as a lie, a boss entrance that must land
before the fight it introduces, and the one state payload every HUD reads.

Nothing here touches the network: every path exercised is code + SQLite.
"""

import asyncio
import json

import pytest

from src.models import (
    Campaign,
    DMState,
    Location,
    LocationType,
    NPC,
    Player,
    Quest,
    QuestStatus,
    WorldClock,
    get_session,
)
from src.services import appearance
from src.services import campaign as camp
from src.services import cinematics as cine
from src.services import progression
from src.services.asset_manager import AssetManager
from src.services.combat import build_combat_start
from src.tools.world_write.quests import _complete_quest, update_quest_status
from src.web import pending_actions, server
from src.web.streaming import ToolUsageTracker
from src.web.turn_audit import audit_turn


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------

_PLUMBING = {"tool_update", "tool_summary"}


def _tool_result_message(result: dict) -> dict:
    """A Strands toolResult message carrying a tool's return dict."""
    return {
        "message": {
            "content": [{
                "toolResult": {
                    "toolUseId": "t1",
                    "status": "success",
                    "content": [{"text": str(result)}],
                }
            }]
        }
    }


def _event_types(result: dict) -> list[str]:
    """The client-visible event types one tool result produces, IN ORDER."""
    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_tool_result_message(result))
    return [n["type"] for n in tracker.drain_notifications() if n["type"] not in _PLUMBING]


@pytest.fixture()
def world(db):
    """A minimal playable world: one outdoor room, one player, one clock."""
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT)
        session.add_all([square, WorldClock(day=1, hour=8, minute=0), DMState(tension="low")])
        session.flush()
        player = Player(name="Tester", currency=100, current_location_id=square.id)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id}


@pytest.fixture()
def api(world, tmp_path, monkeypatch):
    """The server module pinned to the test database, with every background
    generator stubbed out.

    Handlers call `init_db(get_active_db_path())`, which would otherwise
    switch the process onto the real world mid-test; `spawn_generation` is the
    one door from a request handler to an image/LLM API, so it is nailed shut.
    """
    monkeypatch.setattr(server, "get_active_db_path", lambda: str(tmp_path / "test.db"))
    monkeypatch.setattr(AssetManager, "spawn_generation", lambda self, key, factory: False)
    pending_actions.clear(world["player"])
    yield world
    pending_actions.clear(world["player"])


def _player_snapshot(player_id: str) -> dict:
    with get_session() as session:
        p = session.get(Player, player_id)
        return {
            "level": progression.player_level(p),
            "xp": progression.player_xp(p),
            "currency": p.currency,
            "inventory": [i.get("name") for i in (p.inventory or []) if isinstance(i, dict)],
            "active_quests": list(p.active_quests or []),
            "completed_quests": list(p.completed_quests or []),
        }


# ----------------------------------------------------------------------
# 1. Quest completion: rewards + XP + three ordered client events
# ----------------------------------------------------------------------

def test_quest_completion_full_chain(world):
    """One tool call has to pay rewards, grant XP, level the character, and
    reach the client as quest_completed -> xp_gain -> level_up, in that order."""
    with get_session() as session:
        quest = Quest(
            title="The Long Haul",
            description="Walk it all the way",
            objectives=["Arrive"],
            rewards={"currency": 50, "items": ["Salt Ration"]},
            status=QuestStatus.ACTIVE,
            scale="saga",          # 400 XP — enough to cross two thresholds
        )
        session.add(quest)
        session.commit()
        quest_id = quest.id

    result = update_quest_status(quest_id, "completed")

    # The engine paid, in one transaction
    assert result["event"] == "quest_completed"
    assert result["rewards_applied"]
    progression_block = result["progression"]
    assert progression_block["xp_gained"] == progression.QUEST_XP["saga"]
    assert progression_block["source"] == "quest"
    assert progression_block["leveled_up"] is True

    after = _player_snapshot(world["player"])
    assert after["currency"] == 150
    assert "Salt Ration" in after["inventory"]
    assert after["xp"] == 400
    assert after["level"] == progression.level_for_xp(400) > 1

    # ...and the client hears about it in the order the UI needs: banner,
    # then the XP bar, then the level-up burst
    assert _event_types(result) == ["quest_completed", "xp_gain", "level_up"]


def test_quest_rewards_are_paid_once(world):
    """A retried completion must not pay twice — the idempotence guard is what
    lets the campaign engine and the DM tool share one completion path."""
    with get_session() as session:
        quest = Quest(title="Errand", description="Fetch", objectives=["Fetch"],
                      rewards={"currency": 30}, status=QuestStatus.ACTIVE, scale="errand")
        session.add(quest)
        session.commit()
        quest_id = quest.id

    update_quest_status(quest_id, "completed")
    first = _player_snapshot(world["player"])
    update_quest_status(quest_id, "completed")

    assert _player_snapshot(world["player"]) == first


# ----------------------------------------------------------------------
# 2. Campaign beat op and DM tool are the SAME completion
# ----------------------------------------------------------------------

def _make_quest(title: str) -> str:
    with get_session() as session:
        quest = Quest(
            title=title, description="Identical in every way",
            objectives=["Do it"], rewards={"currency": 40, "items": ["Sealed Writ"]},
            status=QuestStatus.ACTIVE, scale="mission",
        )
        session.add(quest)
        session.commit()
        return quest.id


def test_campaign_complete_quest_op_pays_xp(world):
    """A beat that completes a quest pays exactly what the DM's tool pays —
    same rewards, same XP, same lists. One completion path (R12), or engine-
    driven stories quietly become the cheap way to play."""
    baseline = _player_snapshot(world["player"])

    # Path A: the DM's tool
    quest_a = _make_quest("The Tool Path")
    update_quest_status(quest_a, "completed")
    via_tool = _player_snapshot(world["player"])

    # Rewind the character, keep the world
    with get_session() as session:
        p = session.get(Player, world["player"])
        p.currency = baseline["currency"]
        p.inventory = []
        p.xp = None
        p.level = None
        p.active_quests = list(baseline["active_quests"])
        p.completed_quests = list(baseline["completed_quests"])
        session.commit()

    # Path B: a campaign beat's consequence op
    quest_b = _make_quest("The Beat Path")
    events: list[dict] = []
    with get_session() as session:
        player = session.get(Player, world["player"])
        campaign = Campaign(player_id=world["player"], title="The Long Howl", status="active")
        session.add(campaign)
        session.flush()
        applied, exempt = camp.execute_ops(
            session, [{"op": "complete_quest", "quest_id": quest_b}],
            campaign, player, events=events,
        )
        session.commit()
    via_beat = _player_snapshot(world["player"])

    assert via_beat["xp"] == via_tool["xp"] == progression.QUEST_XP["mission"]
    assert via_beat["level"] == via_tool["level"]
    assert via_beat["currency"] == via_tool["currency"]
    assert via_beat["inventory"] == via_tool["inventory"]
    assert len(via_beat["completed_quests"]) == len(via_tool["completed_quests"])

    # The beat also tells the rest of the stack what it did
    assert any('Quest completed: "The Beat Path"' in line for line in applied)
    assert {"quest", "item"} <= exempt
    assert [e["type"] for e in events] == ["quest_completed"]


# ----------------------------------------------------------------------
# 3. The engine's own mutations must not be audited as lies
# ----------------------------------------------------------------------

@pytest.fixture()
def brawler(world):
    with get_session() as session:
        npc = NPC(name="Vex", current_location_id=world["square"],
                  description_physical="a hulking massive brawler")
        session.add(npc)
        session.commit()
        return npc.id


_WOUNDED_NARRATION = (
    "The blade opens a line across your ribs. You are bleeding, and Vex is not "
    "getting back up."
)


def test_combat_result_ledger_suppresses_audit(api, brawler):
    """A canvas fight commits health and deaths BEFORE the next turn's
    snapshots are taken, so `state_before == state_after`. Without the ledger
    the DM is flagged for narrating wounds the engine actually inflicted."""
    response = asyncio.run(server.combat_result(server.CombatResultRequest(
        player_id=api["player"], outcome="victory",
        hits_taken=1, hearts_lost=2, hearts_healed=0,
        defeated_npc_ids=[brawler],
    )))
    assert response["success"] is True
    changes = response["changes"]
    assert changes["health"] == "hurt"                    # 2 hearts lost, none healed
    assert changes["xp_awarded"] > 0
    assert changes["xp_breakdown"][0]["npc_name"] == "Vex"
    assert response["state"]["health"] == "hurt"

    # The next turn's snapshots are both taken after the fight
    state = server.build_player_state(api["player"])

    drained = pending_actions.drain(api["player"])
    code_actions = pending_actions.tool_equivalents(drained)
    assert {"update_player_health", "kill_npc"} <= code_actions

    clean = audit_turn(_WOUNDED_NARRATION, [], state, state, code_actions=code_actions)
    assert [w["check"] for w in clean] == []

    # ...and the suppression is not a blanket amnesty: the same narration with
    # nothing in the ledger still fires
    unbacked = audit_turn(_WOUNDED_NARRATION, [], state, state)
    assert "damage_without_tool" in [w["check"] for w in unbacked]


# ----------------------------------------------------------------------
# 4. A boss entrance lands BEFORE the fight it introduces
# ----------------------------------------------------------------------

def test_boss_fight_event_order(world):
    """`start_combat` carries the entrance on its own tool result, and the
    tracker emits it first — the client defers the spawn until the cinematic
    ends. A rematch gets no second entrance, ever."""
    with get_session() as session:
        warden = NPC(name="Coldiron", current_location_id=world["square"],
                     profession="warden", danger_tier="boss",
                     boss_title="Warden of the Ashen Gate")
        session.add(warden)
        session.commit()
        boss_id = warden.id

    first = build_combat_start([boss_id], world["player"])
    assert first["boss"] == {
        "npc_id": boss_id, "name": "Coldiron", "title": "Warden of the Ashen Gate",
    }
    assert first["enemies"][0]["tier"] == "boss"
    assert first["enemies"][0]["params"]["boss"] is True
    assert first["cinematic"]["kind"] == "boss_intro"
    assert _event_types(first) == ["cinematic", "combat_start"]

    # Rematch: same boss, no second entrance — the fight just starts
    rematch = build_combat_start([boss_id], world["player"])
    assert "cinematic" not in rematch
    assert rematch["boss"]["npc_id"] == boss_id
    assert _event_types(rematch) == ["combat_start"]

    # The refusal is recorded as identity, not as a cooldown
    rejected = [row for row in cine.recent(10) if row["status"] == "rejected"]
    assert rejected[0]["reject_reason"] == "boss_already_introduced"


def test_ordinary_fight_carries_no_cinematic(world):
    """Only bosses get an entrance; a standard guard is just a fight."""
    with get_session() as session:
        npc = NPC(name="Watchman", current_location_id=world["square"], profession="guard")
        session.add(npc)
        session.commit()
        npc_id = npc.id

    result = build_combat_start([npc_id], world["player"])
    assert result["boss"] is None
    assert "cinematic" not in result
    assert _event_types(result) == ["combat_start"]


# ----------------------------------------------------------------------
# 5. One state payload, every key the HUD contracts for
# ----------------------------------------------------------------------

# Integration doc §2.2 — the frozen `state` event shape (+ `appearance`,
# the merged attire/appearance block: wanted signatures + the social reading)
_STATE_KEYS = {
    "location_id", "location", "day", "hour", "minute", "time_of_day",
    "health", "currency", "inventory", "quests", "tension",
    "level", "xp", "xp_into_level", "xp_for_level",
    "equipment", "combat", "appearance", "campaign", "weather",
}
_ITEM_KEYS = {"slot", "tier", "effect_tag", "equipped", "equippable", "price"}


def test_state_payload_superset(world):
    """A legacy row — level/xp/equipment all NULL — must produce the full
    payload with today's numbers: 5 hearts, 1 damage, nothing equipped."""
    with get_session() as session:
        p = session.get(Player, world["player"])
        # A row written before the overhaul: every new column NULL
        p.level = None
        p.xp = None
        p.equipment = None
        p.quick_slot_item_id = None
        p.inventory = [
            {"id": "iron_sword", "name": "Iron Sword", "type": "weapon", "quantity": 1},
            {"name": "Twine", "type": "misc", "quantity": 3},
        ]
        session.commit()

    state = server.build_player_state(world["player"])

    assert _STATE_KEYS <= set(state)
    assert state["level"] == 1
    assert state["xp"] == 0
    assert state["xp_into_level"] == 0
    assert state["xp_for_level"] == progression.XP_THRESHOLDS[2]
    assert state["equipment"] == {"weapon": None, "armor": None, "trinket": None}
    assert state["campaign"] is None
    assert state["weather"] == "clear"

    # A legacy DB yields the base appearance block: signatures a byte-old
    # world already has art for, and the code-owned default reading
    assert state["appearance"] == {
        "body_sig": "base",
        "weapon_sig": "unarmed",
        "action_sig": "base-unarmed",
        "version": appearance.APPEARANCE_VERSION,
        "read_as": "an ordinary townsperson",
    }

    combat = state["combat"]
    assert combat["max_hearts"] == 5 and combat["hearts"] == 5
    assert combat["light_damage"] == 1
    assert combat["heavy_damage"] == 3
    assert combat["parry_unlocked"] and combat["heavy_unlocked"]

    # Every inventory entry is annotated, legacy `type: weapon` included
    sword, twine = state["inventory"]
    assert _ITEM_KEYS <= set(sword)
    assert sword["slot"] == "weapon" and sword["tier"] == "common"
    assert sword["equippable"] is True and sword["equipped"] is False
    assert sword["price"] == progression.TIER_PRICE["weapon"][1]
    assert twine["slot"] is None and twine["equippable"] is False

    # The payload is JSON-serializable — it is an SSE frame, not a Python dict
    json.dumps(state)


def test_state_weather_is_suppressed_indoors(world):
    """Weather is world state, but it is invisible from inside a room — code
    decides, so the client never has to know what a building is."""
    with get_session() as session:
        session.query(DMState).first().weather = "rain"
        inn = Location(name="The Salt Tavern", type=LocationType.BUILDING)
        session.add(inn)
        session.flush()
        inn_id = inn.id
        session.commit()

    assert server.build_player_state(world["player"])["weather"] == "rain"

    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = inn_id
        session.commit()

    assert server.build_player_state(world["player"])["weather"] == "clear"


def test_endpoints_embed_state_and_tell_the_dm(api):
    """The canvas/HUD endpoints are the other half of the contract: each one
    resolves in code, embeds the same `state` payload the SSE event carries,
    and leaves a note so the next DM turn knows what already happened."""
    from src.models.location import Connection

    with get_session() as session:
        tavern = Location(name="The Salt Tavern", type=LocationType.BUILDING)
        session.add(tavern)
        session.flush()
        session.add(Connection(
            from_location_id=api["square"], to_location_id=tavern.id,
            travel_type="enter", travel_time_hours=0.1,
            bidirectional=True, discovered=True,
        ))
        tavern.objects = [{
            "kind": "take", "label": "Bundle of oilcloth", "x": 30, "y": 60,
            "payload": {"item": {"name": "Oilcloth", "type": "misc", "value": 3}},
        }]
        p = session.get(Player, api["player"])
        p.inventory = [
            {"id": "harbor_mail", "name": "Harbor Mail", "slot": "armor",
             "tier": "common", "quantity": 1},
            {"id": "draught", "name": "Healing Draught", "type": "consumable",
             "effect_tag": "heal_light", "quantity": 2},
        ]
        p.health_status = "hurt"
        session.commit()
        tavern_id = tavern.id

    # Gear
    equipped = asyncio.run(server.equip_item(api["player"], server.EquipRequest(item_id="harbor_mail")))
    assert equipped["slot"] == "armor"
    assert equipped["state"]["combat"]["max_hearts"] == 6

    # Quick slot + a potion drunk out of combat heals a health STEP
    slotted = asyncio.run(server.set_quick_slot(api["player"], server.QuickSlotRequest(item_id="draught")))
    assert slotted["quick_slot"]["effect_tag"] == "heal_light"
    drunk = asyncio.run(server.use_consumable(
        api["player"], server.UseConsumableRequest(item_id="draught", in_combat=False)))
    assert drunk["remaining"] == 1
    assert drunk["state"]["health"] == "winded"

    # Travel through an unlocked door, then touch the thing lying there
    travelled = asyncio.run(server.canvas_travel(server.TravelRequest(
        player_id=api["player"], destination_id=tavern_id)))
    assert travelled["success"] is True
    assert travelled["first_visit"] is True
    assert travelled["state"]["location_id"] == tavern_id

    scene = asyncio.run(server.get_location_assets(tavern_id, api["player"]))
    obj_id = scene["objects"][0]["id"]
    assert scene["objects"][0]["label"] == "Bundle of oilcloth"
    assert [e["id"] for e in scene["exits"]] == [api["square"]]
    assert scene["exits"][0]["locked"] is False
    assert scene["exits"][0]["door"]["slot"]

    taken = asyncio.run(server.canvas_interact(server.InteractRequest(
        player_id=api["player"], location_id=tavern_id, object_id=obj_id)))
    assert taken["result"]["items_gained"] == [{"name": "Oilcloth", "quantity": 1}]
    assert "Oilcloth" in [i["name"] for i in taken["state"]["inventory"]]

    # Everything above is now one briefing the DM must not re-apply
    drained = pending_actions.drain(api["player"])
    block = pending_actions.format_pending_actions(drained)
    assert "Market Square -> The Salt Tavern" in block
    assert "Picked up: Oilcloth" in block
    assert "Harbor Mail" in block and "Healing Draught" in block
    assert "never been described" in block          # first visit -> describe_location
    assert {"move_player", "advance_time", "add_to_inventory", "use_item"} <= (
        pending_actions.tool_equivalents(drained)
    )


def test_campaign_and_bark_endpoints(api, monkeypatch):
    """Read models only: the campaign view redacts, barks serve what is cached."""
    assert asyncio.run(server.get_campaign_view(api["player"])) == {"campaign": None}

    with get_session() as session:
        session.add(Campaign(
            player_id=api["player"], title="The Long Howl", status="active",
            acts=[{"act": 1, "title": "The Kennel Fire", "premise": "Feed."}],
            fixed_truths=[
                {"id": "t1", "statement": "Kestrel breeds them.", "revealed": False},
                {"id": "t2", "statement": "The fire was set.", "revealed": True},
            ],
        ))
        square = session.get(Location, api["square"])
        square.ambient_barks = [{"npc_id": None, "text": "Salt in the wind."}]
        session.commit()

    public = asyncio.run(server.get_campaign_view(api["player"]))
    assert public["campaign"]["title"] == "The Long Howl"
    assert public["sealed_truth_count"] == 1
    assert "Kestrel breeds them." not in json.dumps(public)
    assert public["revealed_truths"] == [{"kind": None, "statement": "The fire was set."}]

    spoiled = asyncio.run(server.get_campaign_view(api["player"], spoilers=1))
    assert "Kestrel breeds them." in json.dumps(spoiled)

    # A settled pool is served straight from the row — no generation, no model
    monkeypatch.setattr(server.barks_service, "barks_stale", lambda loc, clock=None: False)
    barks = asyncio.run(server.get_location_barks(api["square"]))
    assert barks["barks"] == [{"npc_id": None, "text": "Salt in the wind."}]
    assert barks["pending"] is False and barks["unavailable"] is False


def test_state_carries_equipped_gear_and_campaign(world):
    """The other half of the payload: once something IS equipped and a
    campaign IS live, both surface without a second request."""
    with get_session() as session:
        p = session.get(Player, world["player"])
        p.inventory = [{"id": "harbor_mail", "name": "Harbor Mail",
                        "slot": "armor", "tier": "common", "quantity": 1}]
        session.add(Campaign(
            player_id=world["player"], title="The Long Howl", status="active",
            current_act=1, acts=[{"act": 1, "title": "The Kennel Fire", "premise": "Feed."}],
            fixed_truths=[{"id": "t1", "statement": "Kestrel breeds them.", "revealed": False}],
        ))
        session.commit()
        progression.equip(session, p, "harbor_mail")
        session.commit()

    state = server.build_player_state(world["player"])

    assert state["equipment"]["armor"]["name"] == "Harbor Mail"
    assert state["inventory"][0]["equipped"] is True
    assert state["combat"]["max_hearts"] == 6      # base 5 + common armor
    assert state["campaign"]["title"] == "The Long Howl"
    assert state["campaign"]["act"] == 1
    # Spoiler-free by construction: a sealed truth never reaches the client
    assert state["campaign"]["revealed_truths"] == []


# ----------------------------------------------------------------------
# 6. Visible equipment + attire: state, doors, travel, pre-warm, ledger
# ----------------------------------------------------------------------

_HOOD = {
    "id": "ashfall_hood", "name": "Ashfall Hood", "type": "armor",
    "slot": "armor", "tier": "common", "quantity": 1,
    "visual_desc": "a soot-grey hood that swallows the face",
    "attire": {"style": "rough", "uniform_faction_id": None, "concealing": True},
}
_SWORD = {
    "id": "shortsword", "name": "Notched Shortsword", "type": "weapon",
    "slot": "weapon", "tier": "common", "quantity": 1,
    "visual_desc": "a notched iron shortsword",
}
_HOOD_READS_AS = ("someone who does not want to be seen — "
                  "a soot-grey hood that swallows the face")
_LANTERN_REASON = ("Maera keeps a warm room and a clean floor: no covered "
                   "faces under her roof.")


def test_state_carries_appearance_and_read_as(world):
    """Gear moves BOTH halves of the merged appearance block: the sprite
    signatures (what the renderer loads) and `read_as` (what strangers see).
    One state payload, no second request."""
    with get_session() as session:
        p = session.get(Player, world["player"])
        p.inventory = [dict(_HOOD), dict(_SWORD)]
        session.commit()
        progression.equip(session, p, "ashfall_hood")
        progression.equip(session, p, "shortsword")
        session.commit()
        expected = appearance.appearance_payload(p)

    state = server.build_player_state(world["player"])
    block = state["appearance"]

    assert block["body_sig"] == expected["body_sig"] != "base"
    assert block["weapon_sig"] == expected["weapon_sig"] != "unarmed"
    assert block["action_sig"] == f"{block['body_sig']}-{block['weapon_sig']}"
    assert block["version"] == appearance.APPEARANCE_VERSION
    assert block["read_as"] == _HOOD_READS_AS
    json.dumps(state)


def test_dress_locked_exit_and_travel_carry_the_reason(api):
    """A dress code authored on the destination locks the canvas door (the
    scene fetch renders `locked` with the house's own reason) and 409s
    `/api/travel` with `reason == "dress"` — one choke point, two surfaces."""
    from src.models.location import Connection

    with get_session() as session:
        tavern = Location(
            name="The Drowned Lantern", type=LocationType.BUILDING,
            dress_code={"min_style": None, "banned_styles": ["concealing"],
                        "reason": _LANTERN_REASON},
        )
        session.add(tavern)
        session.flush()
        session.add(Connection(
            from_location_id=api["square"], to_location_id=tavern.id,
            travel_type="enter", travel_time_hours=0.1,
            bidirectional=True, discovered=True,
        ))
        p = session.get(Player, api["player"])
        p.inventory = [dict(_HOOD)]
        session.commit()
        progression.equip(session, p, "ashfall_hood")
        session.commit()
        tavern_id = tavern.id

    scene = asyncio.run(server.get_location_assets(api["square"], api["player"]))
    door = next(e for e in scene["exits"] if e["id"] == tavern_id)
    assert door["locked"] is True
    assert any(_LANTERN_REASON in str(r) for r in door["requirements"])

    # The scene fetch also carries the appearance block: aim for the hood's
    # signature, serve base until every one of its files is on disk
    block = scene["appearance"]
    assert block["body_sig"] != "base"
    assert block["serving_body"] == "base" and block["body_ready"] is False
    assert block["serving_action"] == "base-unarmed"

    response = asyncio.run(server.canvas_travel(server.TravelRequest(
        player_id=api["player"], destination_id=tavern_id)))
    assert response.status_code == 409
    payload = json.loads(bytes(response.body))
    assert payload["blocked"] is True and payload["reason"] == "dress"
    assert _LANTERN_REASON in payload["requirements"][0]
    assert payload["chat_prompt"] == "I try to get through to The Drowned Lantern."

    # The refusal moved nobody
    with get_session() as session:
        assert session.get(Player, api["player"]).current_location_id == api["square"]


def test_equip_prewarms_both_axes_and_notes_the_reading(api, monkeypatch):
    """POST /equip fires background generation for the player's new look
    (dedup keys carry the signatures — one in-flight task per set) and the
    ledger note tells the DM how strangers now read them."""
    spawned: list[str] = []
    monkeypatch.setattr(
        AssetManager, "spawn_generation",
        lambda self, key, factory: spawned.append(key) or True,
    )

    with get_session() as session:
        p = session.get(Player, api["player"])
        p.inventory = [dict(_HOOD)]
        session.commit()

    equipped = asyncio.run(server.equip_item(
        api["player"], server.EquipRequest(item_id="ashfall_hood")))
    assert equipped["slot"] == "armor"

    block = equipped["state"]["appearance"]
    assert block["read_as"] == _HOOD_READS_AS
    assert spawned == [
        f"body:{api['player']}:{block['body_sig']}",
        f"action:{api['player']}:{block['action_sig']}",
    ]

    drained = pending_actions.drain(api["player"])
    assert [d["kind"] for d in drained] == ["equip"]
    assert drained[0]["summary"] == (
        "They now wear Ashfall Hood. "
        f"To strangers they now read as {_HOOD_READS_AS}."
    )

    # Taking it off notes the change back; base needs no generation at all
    spawned.clear()
    asyncio.run(server.unequip_item(api["player"], server.UnequipRequest(slot="armor")))
    drained = pending_actions.drain(api["player"])
    assert drained[0]["summary"] == (
        "They put away their Ashfall Hood. "
        "To strangers they now read as an ordinary townsperson."
    )
    assert spawned == []
