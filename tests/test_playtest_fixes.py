"""Pins for what the 2026-10-04 playtest (custom apothecary, fresh Emberfall)
found broken in the first half hour of play."""

from src.models import (
    Campaign, DMState, Location, LocationType, NPC, NPCRelationship, Player, WorldClock, get_session,
)
from src.services import party, standing
from src.services.commands import CommandRunner
from src.services.starting_kit import KIT_DRAUGHT, KIT_WEAPON, grant_starting_kit
from src.services.suggestions import suggest_actions
from src.services.world_tick import format_world_tick_context


def _place(session):
    session.add(WorldClock(day=1, hour=19, minute=0))
    session.add(DMState(tension="low"))
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True, visited=True)
    session.add(hollow)
    session.flush()
    player = Player(name="Ilse Varrow", current_location_id=hollow.id, currency=25)
    villain = NPC(name="Aldric Vane", profession="wolf-caller", current_location_id=hollow.id,
                  danger_tier="boss", combat_archetype="stalker")
    wolf = NPC(name="Hollow Wolf", current_location_id=hollow.id, danger_tier="weak",
               description_physical="a lean grey wolf")
    session.add_all([player, villain, wolf])
    session.commit()
    return player.id, villain.id, wolf.id, hollow.id


def test_new_characters_start_armed_with_a_draught_on_q(db):
    with get_session() as session:
        player_id, *_ = _place(session)
    assert grant_starting_kit(player_id) == [KIT_WEAPON, KIT_DRAUGHT]
    with get_session() as session:
        player = session.get(Player, player_id)
        names = [i["name"] for i in player.inventory]
        assert KIT_WEAPON in names and KIT_DRAUGHT in names
        assert (player.equipment or {}).get("weapon")          # auto-equipped
        assert player.quick_slot_item_id == KIT_DRAUGHT.lower().replace(" ", "_")
    assert grant_starting_kit(player_id) == []                # idempotent


def test_one_conversation_does_not_recruit_anyone():
    # A relationship row is born at the stranger default; it must not clear the bar
    assert standing.join_refusal(NPC(name="X"), NPCRelationship(trust_level=50))["error"] == "trust"


def test_the_campaign_villain_never_joins(db):
    with get_session() as session:
        player_id, villain_id, _, _ = _place(session)
        session.add(Campaign(player_id=player_id, title="The Drilled Pack", status="active",
                             villain_npc_id=villain_id))
        session.add(NPCRelationship(npc_id=villain_id, player_id=player_id, trust_level=90,
                                    current_disposition="friendly"))
        session.commit()
        refusal = party.can_join(session, session.get(Player, player_id), session.get(NPC, villain_id))
    assert refusal and refusal["error"] == "enemy"


def test_a_boss_entrance_waits_for_the_fight(db, monkeypatch):
    played = []
    import src.services.commands as commands

    monkeypatch.setattr(commands, "play_cinematic",
                        lambda **kw: played.append(kw) or {"success": True, "event": "cinematic", **kw})
    monkeypatch.setattr(commands, "start_combat",
                        lambda player_id, npc_ids: {"event": "combat_start", "enemies": npc_ids})
    with get_session() as session:
        player_id, villain_id, _, _ = _place(session)
    runner = CommandRunner(player_id, present_ids=[villain_id])
    assert runner.run("@boss Aldric Vane").ok
    assert played == []                                       # an arrival is not a fight
    fight = runner.run("@fight Aldric Vane")
    assert fight.ok and fight.result["cinematic"]["kind"] == "boss_intro"
    assert [p["kind"] for p in played] == ["boss_intro"]


def test_an_unspent_boss_entrance_is_reported_not_lost(db, monkeypatch):
    import src.services.commands as commands

    monkeypatch.setattr(commands, "play_cinematic", lambda **kw: {"success": True})
    with get_session() as session:
        player_id, villain_id, _, _ = _place(session)
    runner = CommandRunner(player_id, present_ids=[villain_id])
    runner.run("@boss Aldric Vane")
    assert any("saved" in line for line in runner.ledger())


def test_nobody_is_invited_to_talk_to_a_wolf(db):
    with get_session() as session:
        player_id, _, wolf_id, hollow_id = _place(session)
        player = session.get(Player, player_id)
        chips = suggest_actions(session, player, None, hollow_id)
    assert not any(c.get("kind") == "talk" and c.get("npc_id") == wolf_id for c in chips)


def test_world_global_planned_beats_stay_out_of_the_briefing():
    text = format_world_tick_context({"narrative_state": {
        "current_arc": "Someone is training the wolves.",
        "planned_beats": ["The letter's recipient is dead"],
        "tension": "low",
    }})
    assert "Someone is training the wolves." in text
    assert "letter" not in text


def test_a_quest_reward_is_never_paid_twice_in_one_reply(db):
    from src.models import Quest, QuestStatus

    with get_session() as session:
        player_id, *_ = _place(session)
        session.add(Quest(title="Teeth on the North Road", description="Find out.", status=QuestStatus.ACTIVE,
                          objectives=[{"text": "Report back", "kind": "custom", "done": True}],
                          rewards={"currency": 60}))
        session.commit()
    runner = CommandRunner(player_id)
    assert runner.run("@quest-done Teeth on the North Road").ok
    twice = runner.run("@coin +60")
    assert not twice.ok and "paid twice" in twice.message
    assert runner.run("@coin +5").ok                          # a tip is still a tip
    with get_session() as session:
        assert session.get(Player, player_id).currency == 25 + 60 + 5


def test_the_villain_stays_out_of_an_early_group_fight(db, monkeypatch):
    import src.services.commands as commands

    started = {}
    monkeypatch.setattr(commands, "start_combat",
                        lambda player_id, npc_ids: started.setdefault("ids", npc_ids) and
                        {"event": "combat_start", "enemies": npc_ids})
    with get_session() as session:
        player_id, villain_id, wolf_id, _ = _place(session)
        session.add(Campaign(player_id=player_id, title="The Drilled Pack", status="active",
                             current_act=1, villain_npc_id=villain_id))
        session.commit()
    runner = CommandRunner(player_id, present_ids=[villain_id, wolf_id])
    fight = runner.run("@fight Aldric Vane, Hollow Wolf")
    assert fight.ok and started["ids"] == [wolf_id]
    assert "did not join" in fight.message
    alone = CommandRunner(player_id, present_ids=[villain_id])
    started.clear()
    assert alone.run("@fight Aldric Vane").ok and started["ids"] == [villain_id]   # chosen, allowed


def test_moving_to_someone_here_is_no_move_and_to_someone_away_is(db):
    with get_session() as session:
        player_id, villain_id, _, hollow_id = _place(session)
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add(square)
        session.flush()
        session.add(NPC(name="Captain Brann Coldiron", current_location_id=square.id))
        session.commit()
        square_id = square.id
    runner = CommandRunner(player_id, present_ids=[villain_id])
    here = runner.run("@move Aldric Vane")
    assert here.ok and "right here" in here.message
    away = runner.run("@move Captain Brann Coldiron")
    assert away.ok
    with get_session() as session:
        assert session.get(Player, player_id).current_location_id == square_id


def test_a_tier_on_bread_is_ignored_not_fatal(db):
    with get_session() as session:
        player_id, *_ = _place(session)
    assert CommandRunner(player_id).run("@give Traveler's Bread | kind: misc | tier: crude").ok


def test_a_tier_on_a_named_blade_makes_it_a_weapon(db):
    with get_session() as session:
        player_id, *_ = _place(session)
    assert CommandRunner(player_id).run("@give Iron Sword | tier: fine").ok
    with get_session() as session:
        sword = next(i for i in session.get(Player, player_id).inventory if i["name"] == "Iron Sword")
    assert sword["slot"] == "weapon" and sword["tier"] == "fine"


def _inn(session):
    from src.services.scene_objects import normalize_objects

    session.add(WorldClock(day=1, hour=21, minute=0))
    session.add(DMState(tension="low"))
    inn = Location(name="The Crooked Mug", type=LocationType.BUILDING, depth=0, discovered=True,
                   objects=normalize_objects([{"kind": "rest", "label": "Bed", "x": 50, "y": 50}]),
                   rest={"kind": "inn", "price": 8, "quality": "good"})
    session.add(inn)
    session.flush()
    player = Player(name="Bram", current_location_id=inn.id, currency=25, health_status="hurt")
    session.add(player)
    session.commit()
    return player.id


def test_paying_for_a_room_is_sleeping_in_it(db):
    with get_session() as session:
        player_id = _inn(session)
    runner = CommandRunner(player_id)
    paid = runner.run("@coin -8 | a night's room")
    assert paid.ok and "@rest" in paid.message
    with get_session() as session:
        player = session.get(Player, player_id)
        clock = session.query(WorldClock).first()
        assert player.currency == 25 - 8 and clock.day == 2       # charged once, morning came
    again = runner.run("@rest")
    assert again.ok and "already taken" in again.message
    with get_session() as session:
        assert session.query(WorldClock).first().day == 2          # not a second whole day


def test_a_secret_told_in_dialogue_is_recorded_and_journaled(db, tmp_path, monkeypatch):
    from src.services import dialogue, memory_files

    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path)
    with get_session() as session:
        player_id, villain_id, _, _ = _place(session)
        npc = session.get(NPC, villain_id)
        npc.secrets = ["He was drummed out of the Watch for selling patrol routes to smugglers"]
        session.add(NPCRelationship(npc_id=villain_id, player_id=player_id, trust_level=80,
                                    current_disposition="friendly"))
        session.commit()
    said = [{"npc_id": villain_id, "text": "They drummed me out of the Watch. Said I sold patrol "
                                           "routes to smugglers. Maybe I did."}]
    assert dialogue.note_confessions(player_id, said)
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=villain_id).first()
        assert rel.revealed_secrets == [0]
    assert any("smugglers" in e for e in memory_files.read_journal(player_id))
    # A guarded NPC (below the trust gate) is never marked, whatever is said
    with get_session() as session:
        session.query(NPCRelationship).filter_by(npc_id=villain_id).first().revealed_secrets = []
        session.query(NPCRelationship).filter_by(npc_id=villain_id).first().trust_level = 10
        session.commit()
    assert dialogue.note_confessions(player_id, said) == []
