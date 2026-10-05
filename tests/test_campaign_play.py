"""The campaign spine in step with play (round-3 playtests, 2026-10-04).

fleet4-bram played 21 turns spineless after one transient generation error;
fleet4-tamsin spent act-2 material in act 1 while the spine still waited to
offer a finished quest and replay delivered scenes; Maera said a SEALED truth
word for word and nothing noticed; the unrevealed villain explained himself.

Nothing here touches the network: generation is stubbed, the generator is
injected, and memory files live under tmp_path.
"""

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.models import (
    NPC,
    Campaign,
    DMState,
    Location,
    LocationType,
    NPCRelationship,
    NPCTier,
    Player,
    Quest,
    QuestStatus,
    StoryBeat,
    WorldClock,
    get_session,
)
from src.services import campaign as camp
from src.services import memory_files

_REAL_SPAWN = camp._spawn_generation


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    """No LLM generation, no memory files outside tmp_path."""
    spawned: list[str] = []
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: spawned.append(player_id))
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    camp.reset_state()
    yield spawned
    camp.reset_state()


@pytest.fixture()
def town(player):
    """Emberfall in miniature: the square, the inn, the hollow, their people."""
    ids = {"player": player}
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18))
        session.add(DMState())
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True)
        lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, visited=True, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, visited=False, discovered=True)
        session.add_all([square, lantern, hollow])
        session.flush()
        people = {
            "coldiron": NPC(name="Captain Brann Coldiron", tier=NPCTier.MAJOR, profession="Captain of the Watch",
                            current_location_id=square.id, danger_tier="dangerous"),
            "piper": NPC(name="Piper Quillow", tier=NPCTier.MINOR, profession="herbalist's apprentice",
                         current_location_id=square.id),
            "sorrel": NPC(name="Sorrel Tack", tier=NPCTier.MINOR, profession="stable hand",
                          current_location_id=square.id),
            "maera": NPC(name="Maera Duskwell", tier=NPCTier.MINOR, profession="innkeeper",
                         current_location_id=lantern.id),
            "vane": NPC(name="Aldric Vane", tier=NPCTier.MAJOR, profession="wolf-caller",
                        current_location_id=hollow.id, danger_tier="boss"),
        }
        session.add_all(people.values())
        session.flush()
        quest = Quest(title="Teeth on the North Road", description="Find what drills the wolves.",
                      status=QuestStatus.NOT_STARTED, assigned_by_npc_id=people["coldiron"].id,
                      objectives=[{"id": "o1", "text": "Reach Wolf's Hollow", "kind": "go_to",
                                   "target_id": hollow.id, "done": False}])
        session.add(quest)
        session.flush()
        session.get(Player, player).current_location_id = square.id
        ids.update({"square": square.id, "lantern": lantern.id, "hollow": hollow.id, "quest": quest.id})
        ids.update({key: npc.id for key, npc in people.items()})
        session.commit()
    return ids


def _truths():
    return [
        {"key": "truth_villain", "kind": "villain_identity",
         "statement": "Aldric Vane was a Watch sergeant until he was drummed out.",
         "spoiler_keywords": ["aldric vane", "wolf-caller", "drummed out"]},
        {"key": "truth_secret", "kind": "the_secret",
         "statement": "Coldiron's roster of twenty is a lie; there are eleven.",
         "spoiler_keywords": ["eleven", "roster", "coldiron"]},
        {"key": "truth_cost", "kind": "the_cost",
         "statement": "The town cannot pay the toll and keep the road open both.",
         "spoiler_keywords": ["toll", "pay", "thin numbers"]},
    ]


def _beat(key, act, sequence, **kwargs):
    beat = {
        "key": key, "act": act, "sequence": sequence,
        "beat_type": "revelation", "title": f"Beat {key}",
        "directive": f"Deliver {key}.", "fixed_truths": [],
        "reveals_truth_ids": [], "delivery_keywords": [key],
        "trigger_type": "time", "trigger_data": {"delay": "soon"},
        "cinematic": "none", "consequence_met": [], "consequence_missed": [],
        "clock_label": None, "directive_missed": None, "met_condition": None,
    }
    beat.update(kwargs)
    return beat


def _filler(count, start=90):
    """Act-3 padding past MIN_BEATS that never arms; the last is the finale."""
    return [_beat(f"f{i}", 3, start + i, trigger_type="location",
                  trigger_data={"location_id": "nowhere"}) for i in range(count)]


def _persist(town, beats):
    payload = {
        "title": "The Toll Road", "premise": "Someone drills the wolves.",
        "stakes_personal": "It is your road now.",
        "villain_npc_id": town["vane"], "villain_name": "Aldric Vane",
        "acts": [{"act": 1, "title": "Teeth", "premise": "The cart is late."},
                 {"act": 2, "title": "Whistles", "premise": "The pack answers."},
                 {"act": 3, "title": "The Howl", "premise": "It ends at the hollow."}],
        "fixed_truths": _truths(),
        "beats": beats + _filler(max(0, camp.MIN_BEATS - len(beats))),
    }
    result = camp.persist_campaign(town["player"], payload)
    assert result["status"] == "active", result
    return result


def _row(title):
    with get_session() as session:
        return session.query(StoryBeat).filter(StoryBeat.title == title).one()


def _truth(player_id, truth_id):
    with get_session() as session:
        campaign = camp.get_campaign_row(session, player_id)
        return next(t for t in campaign.fixed_truths if t["id"] == truth_id)


def _set_clock(day, hour):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = day, hour
        session.commit()


def _meet(player_id, npc_id):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=npc_id, player_id=player_id, trust_level=50))
        session.commit()


PIPER_BEAT = dict(
    title="Piper's Pattern", delivery_keywords=["Piper", "chart", "drilled", "pattern"],
    directive="Piper Quillow shows the player her chart of howls: it repeats like a drill.",
    lead="Piper keeps trying to show someone her notes.",
)


def _piper_ops(town):
    return [{"op": "npc_mood", "npc_id": town["piper"], "mood": "hopeful"},
            {"op": "give_item", "item_name": "Piper's Howl Chart", "description": "Howls, charted."}]


# ----------------------------------------------------------------------
# 1. A failed campaign is retried — with backoff, at most three attempts
# ----------------------------------------------------------------------

def _age(player_id, minutes):
    with get_session() as session:
        row = camp.get_campaign_row(session, player_id)
        row.created_at = datetime.utcnow() - timedelta(minutes=minutes)
        session.commit()


def test_a_failed_campaign_is_retried_after_its_backoff_then_given_up(town, quiet):
    calls = []

    def boom(brief):
        calls.append(brief)
        raise RuntimeError("Agent 'campaign_forge' not found in config")

    assert camp.ensure_campaign(town["player"], generate=boom, background=False)["status"] == "failed"
    with get_session() as session:
        assert camp.get_campaign_row(session, town["player"]).generation_attempts == 1

    # Inside the backoff: the turn plays on, nothing is spawned
    assert camp.run_campaign_tick(town["player"]) == {}
    assert quiet == []

    # Past it: the context build asks for a retry in the background
    _age(town["player"], 2)
    assert camp.run_campaign_tick(town["player"]) == {}
    assert quiet == [town["player"]]

    camp.ensure_campaign(town["player"], generate=boom, background=False)
    _age(town["player"], 10)
    camp.ensure_campaign(town["player"], generate=boom, background=False)
    with get_session() as session:
        row = camp.get_campaign_row(session, town["player"])
        assert (row.status, row.generation_attempts) == ("failed", camp.MAX_GENERATION_ATTEMPTS)
        assert session.query(Campaign).count() == 1, "a retry replaces the failed row"
    assert len(calls) == 3

    # Out of attempts: never again, however long it has been
    _age(town["player"], 600)
    quiet.clear()
    assert camp.ensure_campaign(town["player"], generate=boom, background=False)["status"] == "exists"
    assert camp.run_campaign_tick(town["player"]) == {} and quiet == []
    assert len(calls) == 3


def test_a_retry_that_succeeds_brings_the_spine(town):
    def boom(brief):
        raise RuntimeError("transient")

    camp.ensure_campaign(town["player"], generate=boom, background=False)
    _age(town["player"], 2)
    payload = {"title": "Second Time Lucky", "acts": [], "fixed_truths": _truths(),
               "villain_npc_id": town["vane"], "villain_name": "Aldric Vane",
               "beats": [_beat(f"b{i}", 1, i) for i in range(1, 7)]}
    result = camp.ensure_campaign(town["player"], generate=lambda brief: payload, background=False)
    assert result["status"] == "active"
    with get_session() as session:
        row = camp.get_campaign_row(session, town["player"])
        assert row.title == "Second Time Lucky" and row.generation_attempts == 2


def test_a_stale_pending_row_out_of_attempts_is_marked_failed(town):
    with get_session() as session:
        session.add(Campaign(player_id=town["player"], status="pending",
                             generation_attempts=camp.MAX_GENERATION_ATTEMPTS,
                             created_at=datetime.utcnow() - timedelta(hours=1)))
        session.commit()
    result = camp.ensure_campaign(town["player"], generate=lambda brief: pytest.fail("no retry"),
                                  background=False)
    assert result["status"] == "exists"
    with get_session() as session:
        row = camp.get_campaign_row(session, town["player"])
        assert row.status == "failed" and row.generation_error


def test_a_due_retry_starts_once_while_it_runs(monkeypatch):
    """The tick asks every turn; a 30-second generation must not be claimed twice."""
    started, release = [], threading.Event()

    def slow(player_id, **kwargs):
        started.append(player_id)
        release.wait(5)

    monkeypatch.setattr(camp, "ensure_campaign", slow)
    _REAL_SPAWN("p1")
    _REAL_SPAWN("p1")
    release.set()
    for _ in range(50):
        if "p1" not in camp._GENERATING:
            break
        threading.Event().wait(0.02)
    assert started == ["p1"]


# ----------------------------------------------------------------------
# 2. No spine yet: the DM is told
# ----------------------------------------------------------------------

def test_no_spine_line_while_the_campaign_is_missing_pending_or_failed(town):
    assert "NO STORY SPINE YET" in camp.no_spine_context(town["player"])
    assert "@schedule" in camp.no_spine_context(town["player"])
    with get_session() as session:
        session.add(Campaign(player_id=town["player"], status="pending"))
        session.commit()
    assert camp.no_spine_context(town["player"]) == camp.NO_SPINE_CONTEXT
    with get_session() as session:
        camp.get_campaign_row(session, town["player"]).status = "failed"
        session.commit()
    assert camp.no_spine_context(town["player"]) == camp.NO_SPINE_CONTEXT
    with get_session() as session:
        camp.get_campaign_row(session, town["player"]).status = "won"
        session.commit()
    assert camp.no_spine_context(town["player"]) == ""


def test_no_spine_line_is_silent_with_a_live_campaign(town):
    _persist(town, [_beat("b1", 1, 1)])
    assert camp.no_spine_context(town["player"]) == ""


def test_the_context_carries_the_no_spine_line_when_there_is_no_campaign_block(monkeypatch):
    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator

    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *args, **kwargs: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda tick: "")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda tick: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: camp.NO_SPINE_CONTEXT)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Rook")
    fake._player_block = lambda: ["### THE PLAYER", "- Rook"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    context = DMOrchestrator._build_context(fake, "I look around")
    assert "NO STORY SPINE YET" in context
    assert context.index("NO STORY SPINE YET") < context.index("Player says:")


# ----------------------------------------------------------------------
# 3. Play can deliver beats
# ----------------------------------------------------------------------

def test_a_beat_its_person_already_played_out_is_delivered_by_play(town):
    """fleet4-tamsin T12-15: Piper showed her howl pattern in play; the beat
    stayed armed and would have staged it again."""
    beats = [
        _beat("b1", 1, 1, consequence_met=_piper_ops(town),
              lead_target={"type": "npc", "id": town["piper"]}, **PIPER_BEAT),
        _beat("b2", 1, 2, trigger_type="after_beat", trigger_data={"beat_id": "b1", "delay": "immediate"},
              delivery_keywords=["gate"]),
    ]
    _persist(town, beats)
    spoken = [{"npc_id": town["piper"], "npc_name": "Piper Quillow",
               "text": "Every night I mark them. Drilled, I tell you — the same pattern, like soldiers."}]
    result = camp.mark_beat_delivery(town["player"], spoken[0]["text"], speakers=["Piper Quillow"],
                                     spoken=spoken)

    row = _row("Piper's Pattern")
    assert result["played"] == [row.id] and row.status == "delivered"
    assert row.fired_day is None, "never fired"
    with get_session() as session:
        assert session.get(NPC, town["piper"]).current_mood == "hopeful", "the scene's mood stands"
        assert not session.get(Player, town["player"]).inventory, "play decided what changed hands"
        assert "Piper's Pattern" in session.query(DMState).first().completed_beats
    assert any(e.get("by_play") == "keywords" and e["title"] == "Piper's Pattern"
               for e in camp.pop_pending_events(town["player"]))
    # The chain is intact: what waited on it arms and fires
    assert camp.run_campaign_tick(town["player"])["fired_beat"]["title"] == "Beat b2"


@pytest.mark.parametrize("text, spoken", [
    # the words land, but Piper is not part of this turn
    ("Someone says the howls are drilled, the same pattern every night.", []),
    # Piper speaks, but her name is not evidence: one keyword is not enough
    ("Piper waves her chart at you.", [{"text": "Look at it!"}]),
    # the words are the narration's, not hers (fleet4-wenna T8: the villain
    # snarled while the prose said "whistle" and "last stand" — his finale)
    ("Her chart shows the howls drilled into the same pattern.", [{"text": "Look at it!"}]),
])
def test_play_needs_its_person_and_its_words(town, text, spoken):
    _persist(town, [_beat("b1", 1, 1, consequence_met=_piper_ops(town),
                          lead_target={"type": "npc", "id": town["piper"]}, **PIPER_BEAT)])
    spoken = [dict(s, npc_id=town["piper"], npc_name="Piper Quillow") for s in spoken]
    result = camp.mark_beat_delivery(town["player"], "\n".join([text] + [s["text"] for s in spoken]),
                                     spoken=spoken)
    assert result["played"] == [] and _row("Piper's Pattern").status == "armed"


def test_a_beat_offering_a_quest_play_finished_is_never_fired(town):
    """fleet4-tamsin: "Coldiron Wants Eyes He Can Spare" (pending) would have
    offered "Teeth on the North Road", completed at T11."""
    beats = [
        _beat("b1", 1, 1, delivery_keywords=["kennel"]),
        _beat("b2", 1, 2, title="Coldiron Wants Eyes", trigger_type="after_beat",
              trigger_data={"beat_id": "b1", "delay": "soon"},
              directive="Captain Coldiron corners the player, offering the quest 'Teeth on the North Road'.",
              consequence_met=[{"op": "activate_quest", "quest_id": town["quest"]}]),
    ]
    _persist(town, beats)
    with get_session() as session:
        session.get(Quest, town["quest"]).status = QuestStatus.COMPLETED
        session.commit()
    result = camp.mark_beat_delivery(town["player"], "You count your coin by the fire.")
    assert result["played"] == [_row("Coldiron Wants Eyes").id]
    with get_session() as session:
        assert session.get(Quest, town["quest"]).status == QuestStatus.COMPLETED, "never re-activated"
    assert _row("Beat b1").status == "armed"


def test_an_introduction_play_already_made_delivers_the_beat(town):
    """fleet4-tamsin: she parleyed with Aldric Vane at Wolf's Hollow on day 1;
    a beat that only "reveals" him there was spent by it."""
    beats = [_beat("b1", 1, 1, beat_type="confrontation", title="The Man at the Treeline",
                   trigger_type="time", trigger_data={"delay": "days"},
                   directive="Reveal Aldric Vane to the player at Wolf's Hollow — he whistles the pack to heel.",
                   delivery_keywords=["Aldric Vane", "whistle"],
                   consequence_met=[{"op": "npc_mood", "npc_id": town["vane"], "mood": "contemptuous"}])]
    _persist(town, beats)
    assert camp.run_campaign_tick(town["player"])["fired_beat"] is None
    assert _row("The Man at the Treeline").status == "armed", "a stranger at the treeline is no introduction"

    with get_session() as session:
        session.get(Location, town["hollow"]).visited = True
        session.commit()
    _meet(town["player"], town["vane"])
    camp.run_campaign_tick(town["player"])
    assert _row("The Man at the Treeline").status == "delivered"


def test_meeting_the_villain_never_unseals_what_his_beat_reveals(town):
    """"The Ex-Sergeant's Whistle" reveals that Vane was drummed out: knowing
    his face is not knowing that. Delivered by the meeting, the truth would sit
    on the player's "What you know" panel unheard. It fires instead, told the
    player already knows him."""
    beats = [_beat("b1", 1, 1, beat_type="twist", title="The Ex-Sergeant's Whistle",
                   trigger_type="time", trigger_data={"delay": "immediate"},
                   directive="Reveal Aldric Vane to the player at Wolf's Hollow — he whistles the pack to heel "
                             "and says, bitterly, the Watch threw him out.",
                   reveals_truth_ids=["truth_villain"], delivery_keywords=["Aldric Vane", "whistle"],
                   consequence_met=[{"op": "npc_mood", "npc_id": town["vane"], "mood": "contemptuous"}])]
    _persist(town, beats)
    with get_session() as session:
        session.get(Location, town["hollow"]).visited = True
        session.get(Player, town["player"]).current_location_id = town["hollow"]
        session.commit()
    _meet(town["player"], town["vane"])

    fired = camp.run_campaign_tick(town["player"])["fired_beat"]
    assert fired and fired["title"] == "The Ex-Sergeant's Whistle"
    assert _truth(town["player"], "truth_villain")["revealed"] is False
    assert any("already knows Aldric Vane" in note and "no introduction" in note for note in fired["notes"])
    assert "already knows Aldric Vane" in camp.format_campaign_context(camp.get_last_tick(town["player"]))


def test_catching_someone_is_not_introducing_them(town):
    beats = [_beat("b1", 1, 1, title="Maera's Ledger", trigger_type="time", trigger_data={"delay": "days"},
                   directive="Have the player catch Maera Duskwell at The Drowned Lantern paying a fence.",
                   reveals_truth_ids=["truth_cost"])]
    _persist(town, beats)
    _meet(town["player"], town["maera"])
    camp.run_campaign_tick(town["player"])
    assert _row("Maera's Ledger").status == "armed", "knowing Maera is not knowing her secret"


@pytest.mark.parametrize("kind", ["deadline", "world_event", "finale"])
def test_play_never_delivers_a_clock_a_hard_consequence_or_the_finale(town, kind):
    fields = dict(consequence_met=_piper_ops(town), lead_target={"type": "npc", "id": town["piper"]},
                  **PIPER_BEAT)
    if kind == "deadline":
        fields.update(beat_type="deadline_event", clock_label="The pattern breaks",
                      trigger_data={"deadline": "short"},
                      met_condition={"check": "truth_revealed", "truth_id": "truth_secret"},
                      consequence_missed=[{"op": "npc_mood", "npc_id": town["piper"], "mood": "scared"}])
        beats = [_beat("b1", 1, 1, **fields)]
    elif kind == "world_event":
        fields["consequence_met"] = fields["consequence_met"] + [
            {"op": "world_event", "name": "The town hears", "description": "Word spreads."}]
        beats = [_beat("b1", 1, 1, **fields)]
    else:   # the only act-1 beat delivered → act 2 has none → ... use act 3 directly
        beats = [_beat(f"a{i}", 1, i) for i in range(1, 6)] + [_beat("b1", 3, 99, beat_type="confrontation",
                                                                      cinematic="finale", **fields)]
    _persist(town, beats)
    if kind == "finale":
        with get_session() as session:
            camp.get_campaign_row(session, town["player"]).current_act = 3
            session.query(StoryBeat).filter(StoryBeat.title == "Piper's Pattern").one().status = "armed"
            session.commit()
    spoken = [{"npc_id": town["piper"], "npc_name": "Piper Quillow",
               "text": "My chart! Drilled, the same pattern every night."}]
    result = camp.mark_beat_delivery(town["player"], spoken[0]["text"], spoken=spoken)
    assert result["played"] == []


def test_a_campaign_arriving_late_delivers_what_play_already_covered(town):
    """Round 3: the campaign landed during T2, after the opening had started
    the quest its own act-1 beat would offer."""
    with get_session() as session:
        session.get(Quest, town["quest"]).status = QuestStatus.ACTIVE
        session.get(Player, town["player"]).active_quests = [town["quest"]]
        session.commit()
    beats = [
        _beat("b1", 1, 1, title="A Captain Who Needs Hands", trigger_type="after_beat",
              trigger_data={"beat_id": "b2", "delay": "soon"},
              consequence_met=[{"op": "activate_quest", "quest_id": town["quest"]},
                               {"op": "move_npc", "npc_id": town["maera"], "to": town["hollow"]}]),
        _beat("b2", 1, 2, delivery_keywords=["kennel"]),
    ]
    _persist(town, beats)
    assert _row("A Captain Who Needs Hands").status == "delivered"
    with get_session() as session:
        assert session.get(NPC, town["maera"]).current_location_id == town["hollow"], \
            "staging a later scene may need still happens"
    assert _row("Beat b2").status == "armed"


def test_a_beat_delivered_by_play_never_pulls_someone_out_of_the_scene(town):
    """A beat that would have walked Piper to the inn and Vane into the
    square, played out while she stands beside the player: nobody narrated
    her leaving or him arriving, so neither moves — while Maera, offstage, is
    still staged where a later scene needs her."""
    ops = [{"op": "npc_mood", "npc_id": town["piper"], "mood": "hopeful"},
           {"op": "move_npc", "npc_id": town["piper"], "to": town["lantern"]},
           {"op": "move_npc", "npc_id": town["vane"], "to": town["square"]},
           {"op": "move_npc", "npc_id": town["maera"], "to": town["hollow"]}]
    _persist(town, [_beat("b1", 1, 1, consequence_met=ops,
                          lead_target={"type": "npc", "id": town["piper"]}, **PIPER_BEAT)])
    spoken = [{"npc_id": town["piper"], "npc_name": "Piper Quillow",
               "text": "Drilled, I tell you — the same pattern every night."}]
    result = camp.mark_beat_delivery(town["player"], spoken[0]["text"], spoken=spoken)
    assert result["played"] == [_row("Piper's Pattern").id]
    with get_session() as session:
        assert session.get(NPC, town["piper"]).current_location_id == town["square"]
        assert session.get(NPC, town["vane"]).current_location_id == town["hollow"]
        assert session.get(NPC, town["maera"]).current_location_id == town["hollow"]
    assert _row("Piper's Pattern").applied_effects == ["Piper Quillow is now hopeful",
                                                       "Maera Duskwell arrived at Wolf's Hollow"]


# ----------------------------------------------------------------------
# 4. Sealed truths said aloud: detected, recorded, adapted to
# ----------------------------------------------------------------------

MAERA = ("Twenty names on his roster. That's what he tells the town. I've poured drinks for eleven "
         "of his men this month. Eleven, love — not twenty.")
KNOWN = ["Captain Brann Coldiron", "Maera Duskwell", "Aldric Vane", "Wolf's Hollow", "Tamsin Rook"]


def test_two_distinctive_terms_in_one_line_give_a_truth_away():
    truths = [{"id": t["key"], **t} for t in _truths()]
    hits = camp.sealed_truth_hits(truths, f"Maera sets the kettle down.\n{MAERA}", KNOWN)
    assert set(hits) == {"truth_secret"}
    assert set(hits["truth_secret"]) == {"eleven", "roster", "twenty"}


@pytest.mark.parametrize("text", [
    # fleet4-tamsin T7: everyday words the generator wrote as spoilers
    "Sport. No. The wolves pay the toll for me. Simple arithmetic.",
    # fleet2-wenna T1: the terms strewn across different speakers' lines
    "I've a thin roster and no spare man to lose.\nI've been marking the times for eleven nights, look.",
    # a name the player knows is context, not evidence
    "Captain Coldiron keeps his roster close.",
    # small counts are everywhere
    "Two nights back, three weeks gone — Coldiron's roster says so.",
])
def test_ordinary_talk_gives_nothing_away(text):
    truths = [{"id": t["key"], **t} for t in _truths()]
    assert camp.sealed_truth_hits(truths, text, KNOWN) == {}


def test_a_truth_said_aloud_is_revealed_journaled_and_its_beat_delivered(town):
    """fleet4-tamsin T19: Maera told Coldiron's lie; the truth stayed sealed,
    and the act-3 deadline keyed on it would have counted it unmet."""
    beats = [_beat("b1", 1, 1, title="Eleven, Not Twenty", trigger_type="time", trigger_data={"delay": "days"},
                   reveals_truth_ids=["truth_secret"], delivery_keywords=["bluffing", "beg"],
                   consequence_met=[{"op": "npc_mood", "npc_id": town["coldiron"], "mood": "desperate"}])]
    _persist(town, beats)
    spoken = [{"npc_id": town["maera"], "npc_name": "Maera Duskwell", "text": MAERA}]
    result = camp.mark_beat_delivery(town["player"], "She folds her hands.\n" + MAERA,
                                     speakers=["Maera Duskwell"], spoken=spoken)

    assert result["truths_revealed"] == ["truth_secret"]
    truth = _truth(town["player"], "truth_secret")
    assert truth["revealed"] is True and truth["revealed_by"] == "play" and truth["revealed_day"] == 1
    with get_session() as session:
        campaign = camp.get_campaign_row(session, town["player"])
        assert camp.evaluate_condition(session, {"check": "truth_revealed", "truth_id": "truth_secret"},
                                       campaign, None)
    journal = memory_files.read_journal(town["player"])
    assert any(entry.endswith('Maera Duskwell said: "Twenty names on his roster. I\'ve poured drinks for '
                              'eleven of his men this month. Eleven, love — not twenty."') for entry in journal), journal
    assert _row("Eleven, Not Twenty").status == "delivered" and result["played"]
    assert "[KNOWN]  the_secret" in camp.format_campaign_context(camp.run_campaign_tick(town["player"]))


def test_a_fired_beat_is_delivered_once_its_truth_is_out(town):
    beats = [_beat("b1", 1, 1, title="Eleven, Not Twenty", reveals_truth_ids=["truth_secret"],
                   delivery_keywords=["bluffing", "beg", "proud"])]
    _persist(town, beats)
    _set_clock(2, 8)
    assert camp.run_campaign_tick(town["player"])["fired_beat"]["title"] == "Eleven, Not Twenty"
    camp.mark_beat_delivery(town["player"], MAERA)
    assert _row("Eleven, Not Twenty").status == "delivered"


def test_the_audit_names_the_leak_with_the_same_test(town):
    from src.web.turn_audit import audit_turn

    _persist(town, [_beat("b1", 1, 1)])
    audit = camp.run_campaign_tick(town["player"])["audit"]
    assert "Captain Brann Coldiron" in audit["known_words"]
    warnings = audit_turn("She folds her hands.", [], None, None, campaign=audit, spoken_text=MAERA,
                          spoken=[{"npc_name": "Maera Duskwell", "text": MAERA}])
    leak = [w for w in warnings if w["check"] == "campaign_truth_frontrun"]
    assert leak and "eleven" in leak[0]["detail"] and "records it as known" in leak[0]["detail"]
    quiet = audit_turn("Captain Coldiron keeps his roster close.", [], None, None, campaign=audit)
    assert not [w for w in quiet if w["check"] == "campaign_truth_frontrun"]


def test_the_beat_revealing_a_truth_on_schedule_is_no_front_run(town):
    """The two-term test is exactly what a delivered reveal says: the DM
    doing what "Eleven, Not Twenty" told it to must not be scolded for it."""
    from src.web.turn_audit import audit_turn

    _persist(town, [_beat("b1", 1, 1, title="Eleven, Not Twenty", reveals_truth_ids=["truth_secret"],
                          delivery_keywords=["eleven", "roster"])])
    _set_clock(2, 8)
    tick = camp.run_campaign_tick(town["player"])
    assert tick["fired_beat"]["title"] == "Eleven, Not Twenty"
    assert "truth_secret" not in {t["id"] for t in tick["audit"]["sealed_truths"]}
    assert "truth_villain" in {t["id"] for t in tick["audit"]["sealed_truths"]}
    said = "Eleven. The roster says twenty, and it lies."
    warnings = audit_turn("He will not meet your eyes.", [], None, None, campaign=tick["audit"],
                          spoken_text=said, spoken=[{"npc_name": "Captain Brann Coldiron", "text": said}])
    assert not [w for w in warnings if w["check"] in ("campaign_truth_frontrun", "campaign_beat_not_delivered")]


# ----------------------------------------------------------------------
# 5. The villain before their reveal
# ----------------------------------------------------------------------

def _brief(town, location_key="hollow"):
    from src.services.scene_brief import build_scene_brief

    with get_session() as session:
        player = session.get(Player, town["player"])
        player.current_location_id = town[location_key]
        session.commit()
        return build_scene_brief(session, player)["text"]


def test_the_unrevealed_villain_is_menace_without_a_why(town):
    """fleet4-wenna T11: he improvised a grievance the SEALED truth contradicts."""
    _persist(town, [_beat("b1", 1, 1)])
    text = _brief(town)
    assert "STORY VILLAIN, NOT YET REVEALED" in text and "No name, no grievance" in text

    _meet(town["player"], town["vane"])     # named himself — still no why
    text = _brief(town)
    assert "No grievance, no history, no plan" in text and "No name" not in text

    with get_session() as session:
        campaign = camp.get_campaign_row(session, town["player"])
        campaign.fixed_truths = [dict(t, revealed=t["id"] == "truth_villain") for t in campaign.fixed_truths]
        session.commit()
    assert "STORY VILLAIN" not in _brief(town)


def test_a_held_villain_never_offers_his_secrets_at_any_trust(town):
    """A parley can raise trust past the secret gate; "Would now confess"
    under "No grievance, no history" was an open door to the why."""
    _persist(town, [_beat("b1", 1, 1)])
    with get_session() as session:
        session.get(NPC, town["vane"]).secrets = ["Coldiron's predecessor drummed him out of the Watch."]
        session.add(NPCRelationship(npc_id=town["vane"], player_id=town["player"], trust_level=90))
        session.commit()
    text = _brief(town)
    assert "STORY VILLAIN, NOT YET REVEALED" in text
    assert "Would now confess" not in text and "drummed him out" not in text
    assert "Hides 1 secret(s)" in text

    with get_session() as session:
        campaign = camp.get_campaign_row(session, town["player"])
        campaign.fixed_truths = [dict(t, revealed=t["id"] == "truth_villain") for t in campaign.fixed_truths]
        session.commit()
    assert "Would now confess" in _brief(town), "revealed by the story, he may tell it"


def test_without_a_spine_every_unmet_boss_is_held_back(town):
    assert "STORY VILLAIN, NOT YET REVEALED" in _brief(town)
    assert "STORY VILLAIN" not in _brief(town, "square"), "a captain is not a boss"
    _meet(town["player"], town["vane"])
    assert "STORY VILLAIN" not in _brief(town)


# ----------------------------------------------------------------------
# 6-7. What the DM is told when a beat fires into the world as it is
# ----------------------------------------------------------------------

def test_a_beat_about_someone_elsewhere_says_they_come_to_the_player(town):
    """fleet4-wenna T20: "What Sorrel Saw" fired while the player sat in the
    inn; the DM walked her into the square without @move."""
    beats = [_beat("b1", 1, 1, title="What Sorrel Saw",
                   directive="Have Sorrel Tack, around the stables in Market Square, mention the ash-gray cloak.")]
    _persist(town, beats)
    with get_session() as session:
        session.get(Player, town["player"]).current_location_id = town["lantern"]
        session.commit()
    _set_clock(2, 8)
    block = camp.format_campaign_context(camp.run_campaign_tick(town["player"]))
    assert "WHERE PEOPLE ARE: the player is at The Drowned Lantern; Sorrel Tack is at Market Square" in block
    assert "comes to them HERE" in block and "Never move the player in prose" in block


def test_a_beat_about_someone_here_gets_no_note(town):
    beats = [_beat("b1", 1, 1, directive="Have Sorrel Tack mention the ash-gray cloak.")]
    _persist(town, beats)
    _set_clock(2, 8)
    assert "WHERE PEOPLE ARE" not in camp.format_campaign_context(camp.run_campaign_tick(town["player"]))


def test_a_fired_beat_never_pitches_a_quest_the_player_already_took(town):
    beats = [_beat("b1", 1, 1, title="Coldiron's Request", reveals_truth_ids=["truth_secret"],
                   directive="Captain Coldiron admits the roster lie, offering the quest 'Teeth on the North Road'.",
                   consequence_met=[{"op": "activate_quest", "quest_id": town["quest"]}])]
    _persist(town, beats)
    with get_session() as session:
        session.get(Quest, town["quest"]).status = QuestStatus.ACTIVE
        session.commit()
    _set_clock(2, 8)
    block = camp.format_campaign_context(camp.run_campaign_tick(town["player"]))
    assert 'NOTE: The player already took "Teeth on the North Road" — do not offer it again' in block


def test_a_beat_whose_own_op_started_the_quest_still_offers_it(town):
    beats = [_beat("b1", 1, 1, directive="Captain Coldiron is offering the quest 'Teeth on the North Road'.",
                   consequence_met=[{"op": "activate_quest", "quest_id": town["quest"]}])]
    _persist(town, beats)
    _set_clock(2, 8)
    block = camp.format_campaign_context(camp.run_campaign_tick(town["player"]))
    assert "already took" not in block


# ----------------------------------------------------------------------
# The campaign author
# ----------------------------------------------------------------------

def test_the_forge_sees_the_first_job_and_never_writes_the_players_actions(town):
    from src.agents.campaign_forge import CAMPAIGN_FORGE_PROMPT, build_campaign_brief

    brief = build_campaign_brief(town["player"])
    assert "[IN PLAY]" in brief
    in_play = brief.split("[IN PLAY]", 1)[1].split("\n## ", 1)[0]
    assert "Teeth on the North Road" in in_play and "steps: Reach Wolf's Hollow" in in_play
    assert brief.count("Teeth on the North Road") == 1, "listed once, as in play"
    assert "THE WORLD OFFERS, THE PLAYER ACTS" in CAMPAIGN_FORGE_PROMPT
    assert "his purse still on his" in CAMPAIGN_FORGE_PROMPT
    assert "QUESTS ALREADY STEER THE PLAYER" in CAMPAIGN_FORGE_PROMPT


def test_a_retried_forge_sees_the_work_already_done(town):
    """A retry lands minutes into play: the first job may be finished, and
    whoever stands near the player then is no opening offer."""
    from src.agents.campaign_forge import build_campaign_brief

    with get_session() as session:
        session.get(Quest, town["quest"]).status = QuestStatus.COMPLETED
        session.add(Quest(title="Maera's Cellar", description="Rats.", status=QuestStatus.NOT_STARTED,
                          assigned_by_npc_id=town["maera"]))
        session.get(Player, town["player"]).current_location_id = town["lantern"]
        session.commit()
    brief = build_campaign_brief(town["player"])
    in_play = brief.split("[IN PLAY]", 1)[1].split("\n## ", 1)[0]
    assert "Teeth on the North Road (DONE)" in in_play
    assert "Maera's Cellar" not in in_play, "not offered by any opening"
    assert "Maera's Cellar" in brief, "still unstarted work the story may use"


def test_a_beat_never_gives_what_the_world_already_holds(town):
    """fleet5-bram: a beat's give_item put the Cracked Bone Whistle in the
    pack while the Hollow's whistle object still held it — a find the player
    never made, and a second whistle on the ground."""
    with get_session() as session:
        session.get(Location, town["hollow"]).objects = [
            {"id": "whistle_0", "kind": "take", "label": "Cracked bone whistle", "x": 50, "y": 50,
             "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item", "quantity": 1}},
             "consumed": False}]
        session.get(NPC, town["maera"]).inventory_notable = [{"name": "Water-Stained Letter"}]
        session.commit()
    gifts = [{"op": "give_item", "item_name": "Cracked Bone Whistle", "description": "Proof."},
             {"op": "give_item", "item_name": "Water-Stained Letter", "description": "A letter."},
             {"op": "give_item", "item_name": "Ashen Token", "description": "A token nobody holds."}]
    _persist(town, [_beat("b1", 1, 1, consequence_met=gifts)])
    ops = _row("Beat b1").consequence_met
    assert [op["item_name"] for op in ops] == ["Ashen Token"]

    # A campaign persisted before the rule: the op is skipped as it fires
    with get_session() as session:
        player = session.get(Player, town["player"])
        campaign = session.query(camp.Campaign).filter_by(player_id=player.id).first()
        applied, _ = camp.execute_ops(session, gifts[:1], campaign, player)
        session.commit()
        assert applied == []
        assert not any(i.get("name") == "Cracked Bone Whistle" for i in player.inventory or [])


def test_a_beat_led_to_a_place_waits_for_the_player_to_get_there(town):
    """Round 4: "The Whistle Doesn't Lie" fires "when Wenna reaches Wolf's
    Hollow", but its quest_status trigger came true in the Market Square and
    the beat stalled there after three nagged turns."""
    beat = _beat("hollow", 1, 1, title="Scent Lines", trigger_type="quest_status",
                 trigger_data={"quest_id": town["quest"], "status": "active"},
                 directive="When the player reaches Wolf's Hollow, show the drag-trails.",
                 lead="Look for what the wolves leave at Wolf's Hollow.",
                 lead_target={"type": "location", "id": town["hollow"]})
    _persist(town, [beat])
    with get_session() as session:
        session.get(Quest, town["quest"]).status = QuestStatus.ACTIVE
        session.commit()
    tick = camp.run_campaign_tick(town["player"])
    assert not tick.get("fired_beat")
    assert _row("Scent Lines").status == "armed"

    with get_session() as session:
        session.get(Player, town["player"]).current_location_id = town["hollow"]
        session.commit()
    tick = camp.run_campaign_tick(town["player"])
    assert (tick.get("fired_beat") or {}).get("title") == "Scent Lines"


def test_a_place_led_beat_waits_at_its_place_past_its_fallback_then_is_skipped(town):
    """Round 6 (fleet7-tamsin "SING"): a backstop that fired a Hollow scene in
    the yard made the DM walk the player there in prose. It waits there
    instead — and, never reached, is skipped so the act can still close."""
    beat = _beat("hollow", 1, 1, title="Scent Lines", trigger_type="quest_status",
                 trigger_data={"quest_id": town["quest"], "status": "active"},
                 lead_target={"type": "location", "id": town["hollow"]})
    _persist(town, [beat])
    with get_session() as session:
        row = session.query(StoryBeat).filter(StoryBeat.title == "Scent Lines").one()
        row.fallback_day, row.fallback_hour = 1, 0
        session.commit()
    tick = camp.run_campaign_tick(town["player"])
    assert not tick.get("fired_beat")
    assert _row("Scent Lines").status == "armed"

    with get_session() as session:
        session.query(WorldClock).first().day = 1 + camp.STALLED_SKIP_DAYS + 1
        session.commit()
    camp.run_campaign_tick(town["player"])
    assert _row("Scent Lines").status == "skipped"
