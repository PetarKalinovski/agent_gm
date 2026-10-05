"""Story set pieces land where and when they should — round 9 (fleet10, 2026-10-05).

- fleet10-tamsin T2-T6: "SING" ("At Wolf's Hollow, the player finds the hut…",
  quest_status trigger, no lead) fired in the square; the nag pushed and the
  DM told the Hollow as a memory at T6, six replies before she went.
- fleet10-bram T15: picking up the whistle delivered "The Whistle in the Hut"
  (read) — whose substance is Vex Marrow rousing from cover — and Vex never
  appeared.
- fleet10-wenna T6-T7: "A Tell at the Stable" was delivered on the glance
  ("Sorrel" + "north wall"); her exact question got a denial.
- fleet10-tamsin T6-T7: the misfire used up the spacing, and "The Man at the
  Wall" never fired while she stood with Sorrel asking about it.
- fleet10-bram T16, fleet10-tamsin T13: the DM's @fight killed the wolves act
  3 and the finale name.
- fleet10-tamsin T12-T20: Vex Marrow stood in the Hollow for nine replies,
  unseen, while the prose said "alone".

The binding was replayed over every campaign in the fleet databases (407
beats, 12 change binding — all scenes at a place); the memory audit over
every fleet transcript (one hit, fleet10-tamsin T6).
"""

import pytest

from src.models import (
    NPC, Campaign, Connection, DMState, Faction, Location, LocationType, NPCRelationship, Player, Quest,
    QuestStatus, StoryBeat, WorldClock, get_session,
)
from src.services import campaign as camp
from src.services import memory_files
from src.web.turn_audit import audit_turn, recalled_unvisited_place


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    camp.reset_state()
    yield
    camp.reset_state()


HUT = {"id": "hut_wall", "kind": "read", "label": "Scratches on the hut wall", "x": 28, "y": 38,
       "payload": {"text": "Tally marks gouged into the doorframe, grouped in threes. Under them, one word, "
                           "carved deep and old: SING."}}
WHISTLE = {"id": "whistle", "kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now. Old bone worn smooth by a "
                                               "thumb; finer work than any shepherd's."}}}
CRATE = {"id": "crate", "kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc",
                              "description": "Freshly minted. The counting-house stamp is still sharp."}}}
SING_TAMSIN = ("At Wolf's Hollow, the player finds the hut with its tally marks gouged in threes and the word SING "
               "carved beneath — and the cracked bone whistle, finer work than any shepherd's, lying split in the "
               "dirt nearby. This is where the quest's third and fourth steps resolve.")
WHISTLE_IN_THE_HUT = (
    "Inside the hut at Wolf's Hollow, Bram finds the cracked bone whistle — split down the barrel, silent, "
    "finer work than any shepherd's. Taking it rouses Vex Marrow from cover nearby; he does not reveal who he "
    "works for yet, only warns Bram to leave what isn't his and get off Fangs ground, and if pressed on the "
    "silver in his pocket he claims it's a traveler's pay — a lie Bram can see through only if he presses a "
    "second time or threatens him, at which point Vex breaks and names the counting-house his pay comes from.")
TELL_AT_THE_STABLE = (
    "At the Stable Yard, Sorrel Tack is brushing down a nervous horse and keeps glancing toward the north wall. "
    "If asked what's on his mind, he admits he saw a man in an ash-gray cloak crouched by the wall two nights "
    "back, gone before he could call out — and he won't say more unless pressed on why he never told the Watch, "
    "at which point he confesses he's afraid Coldiron will think he's making excuses.")
MAN_AT_THE_WALL = (
    "Sorrel Tack, mucking out the Stable Yard, is visibly jumpy. If asked about the howls or pressed on what "
    "he's seen, he admits — low, glancing around first — that two nights ago he saw a man in an ash-gray cloak "
    "crouched by the north wall, not running from the wolves but waiting for them.")
T6_MEMORY = (
    "The walk is short, but your mind is still back at the Hollow — the hut with its tally marks gouged deep "
    "in threes, the word SING cut crude beneath them like a joke only one man found funny, and the whistle "
    "itself lying cracked in the dirt where something, or someone, had snapped it in half.")


@pytest.fixture()
def emberfall(db):
    """fleet10's Emberfall in miniature: act 1, Day 1 16:00, the player in the square."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=16))
        session.add(DMState())
        fangs = Faction(name="The Ashen Fangs")
        watch = Faction(name="The Lantern Watch")
        session.add_all([fangs, watch])
        town = Location(name="Emberfall", type=LocationType.SETTLEMENT, discovered=True)
        session.add(town)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True,
                          parent_id=town.id)
        yard = Location(name="Stable Yard", type=LocationType.DISTRICT, visited=True, discovered=True,
                        parent_id=town.id)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, visited=False, discovered=True,
                          objects=[HUT, WHISTLE, CRATE])
        session.add_all([square, yard, hollow])
        session.flush()
        session.add_all([Connection(from_location_id=square.id, to_location_id=where.id, travel_type="walk",
                                    travel_time_hours=0.5, bidirectional=True, discovered=True)
                         for where in (yard, hollow)])
        people = {
            "coldiron": NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                            current_location_id=square.id, faction_id=watch.id, danger_tier="dangerous"),
            "piper": NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id),
            "sorrel": NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id),
            "vex": NPC(name="Vex Marrow", profession="Ashen Fangs scout", current_location_id=hollow.id,
                       faction_id=fangs.id, danger_tier="dangerous",
                       description_physical="A lean man in an ash-gray cloak, a bone whistle on a cord."),
            "vane": NPC(name="Aldric Vane", profession="Ashen Fangs wolf-caller", current_location_id=hollow.id,
                        faction_id=fangs.id, danger_tier="boss"),
            "hollow_wolf": NPC(name="Hollow Wolf", profession="wolf of the drilled pack", species="wolf",
                               current_location_id=hollow.id, faction_id=fangs.id, danger_tier="weak",
                               combat_archetype="skirmisher", description_physical="A rangy gray wolf."),
            "grayflank": NPC(name="Gray-Flank Wolf", profession="wolf of the drilled pack", species="wolf",
                             current_location_id=hollow.id, faction_id=fangs.id, danger_tier="weak",
                             combat_archetype="skirmisher"),
        }
        session.add_all(people.values())
        player = Player(name="Tamsin Rook", current_location_id=square.id, position_x=50, position_y=50)
        session.add(player)
        session.flush()
        teeth = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE,
                      assigned_by_npc_id=people["coldiron"].id,
                      objectives=[{"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": hollow.id}])
        session.add(teeth)
        campaign = Campaign(
            player_id=player.id, title="What Sings in the Hollow", status="active", current_act=1,
            premise="Someone drills the wolves.", villain_npc_id=people["vane"].id, villain_name="Aldric Vane",
            successor_npc_id=people["vex"].id, successor_name="Vex Marrow",
            ally_npc_ids=[people["coldiron"].id, people["piper"].id, people["sorrel"].id],
            acts=[{"act": 1, "title": "Teeth", "premise": "The howls keep time."},
                  {"act": 2, "title": "Whistles", "premise": "The pack answers."},
                  {"act": 3, "title": "The Howl", "premise": "It ends at the hollow."}],
            fixed_truths=[{"id": "truth_villain", "kind": "villain_identity", "revealed": False,
                           "statement": "Aldric Vane trains the wolves.", "spoiler_keywords": ["vane", "trains"]}])
        session.add(campaign)
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, campaign=campaign.id, square=square.id, yard=yard.id, hollow=hollow.id,
                   town=town.id, teeth=teeth.id, fangs=fangs.id)
        return ids


def _beat(w, act, seq, title, **kw):
    with get_session() as session:
        row = StoryBeat(campaign_id=w["campaign"], act=act, sequence=seq, title=title,
                        beat_type=kw.pop("beat_type", "revelation"), directive=kw.pop("directive", title),
                        trigger_type=kw.pop("trigger_type", "time"),
                        trigger_data=kw.pop("trigger_data", {"delay": "days"}),
                        status=kw.pop("status", "pending"), **kw)
        session.add(row)
        session.commit()
        return row.id


def _row(beat_id):
    with get_session() as session:
        return session.get(StoryBeat, beat_id)


def _set(model, ident, **fields):
    with get_session() as session:
        row = session.get(model, ident)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def _clock(hour):
    with get_session() as session:
        session.query(WorldClock).first().hour = hour
        session.commit()


def _met(w, who):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=w[who], player_id=w["player"], trust_level=50))
        session.commit()


def _place(beat_id):
    with get_session() as session:
        return camp._place_ids(session, session.get(StoryBeat, beat_id))


# --- 1. A beat is set where its directive opens, and waits there -------------

def test_sing_opening_at_the_hollow_waits_there_and_never_fires_in_the_square(emberfall):
    """fleet10-tamsin T2-T3: the quest went active in the square and SING fired there."""
    w = emberfall
    sing = _beat(w, 1, 5, "SING", directive=SING_TAMSIN, trigger_type="quest_status",
                 trigger_data={"quest_id": w["teeth"], "status": "active"},
                 delivery_keywords=["tally marks", "SING", "bone whistle", "cracked", "hut"],
                 status="armed", armed_day=1, armed_hour=15, fallback_day=8, fallback_hour=15)
    assert _place(sing) == {w["hollow"]}
    assert not camp.run_campaign_tick(w["player"]).get("fired_beat")
    assert _row(sing).status == "armed"
    _set(Player, w["player"], current_location_id=w["hollow"])
    fired = camp.run_campaign_tick(w["player"])["fired_beat"]
    assert fired and fired["title"] == "SING"


def test_a_beat_about_the_things_of_one_place_is_bound_there(emberfall):
    """No place in its words, but its keywords name two of the Hollow's authored Things."""
    w = emberfall
    beat = _beat(w, 1, 2, "Marks and a Whistle", directive="Show the player the marks and the whistle.",
                 delivery_keywords=["hut", "bone whistle", "cracked"])
    one = _beat(w, 1, 3, "A Whistle", directive="Coldiron turns the whistle over.",
                delivery_keywords=["bone whistle", "cracked"])      # one Thing: it can be carried off
    assert _place(beat) == {w["hollow"]}
    assert _place(one) == set()


def test_a_person_led_or_news_beat_is_not_bound_by_its_opening(emberfall):
    w = emberfall
    corner = _beat(w, 1, 2, "Corner", directive="Have Sorrel Tack corner Tamsin in Market Square, breathless.")
    news = _beat(w, 1, 3, "Word", directive="Have Aldric Vane send word through Vex Marrow or leave a sign at "
                                            "Wolf's Hollow — he knows her name.")
    led = _beat(w, 1, 4, "Led", directive="At the Stable Yard, Sorrel waits.",
                lead_target={"type": "npc", "id": w["sorrel"]})
    staged = _beat(w, 1, 5, "Push", directive="Have Vex Marrow lead a push at Market Square timed with the howl.")
    assert _place(corner) == set() and _place(news) == set() and _place(led) == set()
    assert _place(staged) == {w["square"]}


def test_the_nag_is_held_while_the_player_is_away_from_a_place_bound_beat(emberfall):
    """fleet10-tamsin T4-T5: campaign_beat_not_delivered pushed SING into the square."""
    w = emberfall
    sing = _beat(w, 1, 5, "SING", directive=SING_TAMSIN, status="fired", delivery_attempts=1,
                 delivery_keywords=["tally marks", "SING", "bone whistle", "cracked", "hut"])
    audit = {"beat": {"beat_id": sing, "title": "SING",
                      "delivery_keywords": ["tally marks", "SING", "bone whistle", "cracked", "hut"]}}
    checks = [x["check"] for x in audit_turn("Piper chews her sweetroot and counts the gaps.", [], {}, {},
                                             campaign=audit)]
    assert "campaign_beat_not_delivered" not in checks
    _set(Player, w["player"], current_location_id=w["hollow"])
    checks = [x["check"] for x in audit_turn("The wind moves in the birches.", [], {}, {}, campaign=audit)]
    assert "campaign_beat_not_delivered" in checks


def test_a_memory_of_a_place_never_visited_is_flagged(emberfall):
    """fleet10-tamsin T6: "your mind is still back at the Hollow", six replies before she went."""
    w = emberfall
    camp.run_campaign_tick(w["player"])
    unvisited = camp.get_last_tick(w["player"])["audit"]["unvisited_places"]
    assert unvisited == ["Wolf's Hollow"]          # the town holding the square is no stranger
    assert recalled_unvisited_place(T6_MEMORY, unvisited)[0] == "Wolf's Hollow"
    warnings = audit_turn(T6_MEMORY, [], {}, {}, campaign={"unvisited_places": unvisited})
    assert any(x["check"] == "memory_of_unvisited_place" for x in warnings)
    # Not memories: a plan, a place someone else speaks of, a place they have been
    for text in ("Your thoughts are already on the Hollow and the road north.",
                 'Piper: "You remember what I said about the Hollow, don\'t you?"',
                 "Your mind is still back at the Market Square, on the Captain's face."):
        assert recalled_unvisited_place(text, unvisited) is None


# --- 2. A find is the cue of someone's entrance, never the entrance -------------

def _hut_beats(w):
    sing = _beat(w, 1, 4, "SING", directive="When Bram reaches Wolf's Hollow, show him the tally marks.",
                 trigger_type="location", trigger_data={"location_id": w["hollow"]},
                 lead_target={"type": "location", "id": w["hollow"]}, status="delivered",
                 delivery_keywords=["tally marks", "SING"])
    hut = _beat(w, 1, 5, "The Whistle in the Hut", beat_type="confrontation", directive=WHISTLE_IN_THE_HUT,
                trigger_type="after_beat", trigger_data={"beat_id": sing, "delay": "immediate"},
                delivery_keywords=["bone whistle", "cracked", "Vex Marrow", "Fangs ground", "counting-house"],
                consequence_met=[{"op": "npc_mood", "npc_id": w["vex"], "mood": "hostile"}],
                status="armed", armed_day=1, armed_hour=19, scheduled_day=1, scheduled_hour=19,
                fallback_day=4, fallback_hour=19)
    return sing, hut


def test_picking_up_the_whistle_brings_vex_on_instead_of_retiring_his_entrance(emberfall):
    """fleet10-bram T15: 'delivered by play (read), never fired: The Whistle in the Hut'."""
    w = emberfall
    _sing, hut = _hut_beats(w)
    _clock(19)
    _set(Player, w["player"], current_location_id=w["hollow"])
    _set(Campaign, w["campaign"], last_beat_day=1, last_beat_hour=18)      # SING an hour ago: spacing
    memory_files.add_journal_entry(w["player"], "Found Cracked Bone Whistle: Split down the barrel — silent "
                                                "now. Old bone worn smooth by a thumb; finer work than any "
                                                "shepherd's.")
    prose = ("You close your hand around the whistle — bone-white, cracked clean down the barrel. The two "
             "wolves lift their heads as one.")
    result = camp.mark_beat_delivery(w["player"], prose, player_input="I pick up the whistle.")
    assert hut not in result["played"] and _row(hut).status == "armed"
    # ...it comes due next reply, the spacing waived, the find told as done
    fired = camp.run_campaign_tick(w["player"])["fired_beat"]
    assert fired and fired["title"] == "The Whistle in the Hut"
    notes = " ".join(fired["notes"])
    assert "THE FIND IS THE CUE" in notes and "ENTRANCE" in notes and "Vex Marrow" in notes
    # The whistle in hand and "cracked" again are not Vex stepping out
    camp.mark_beat_delivery(w["player"], "The cracked bone whistle sits cold in your fist.",
                            player_input="I wait, whistle in hand.")
    assert _row(hut).status == "fired"
    # ...his own words are
    camp.mark_beat_delivery(
        w["player"], "A man rises from the bracken, ash-gray cloak, bone whistle on a cord. The cracked one in "
                     "your hand draws his eye.",
        speakers=["Vex Marrow"],
        spoken=[{"npc_id": w["vex"], "npc_name": "Vex Marrow",
                 "text": "Leave what isn't yours. Get off Fangs ground."}],
        player_input="Who are you?")
    assert _row(hut).status == "delivered"


def test_a_find_with_nobody_to_step_out_still_tells_a_plain_beat(emberfall):
    """The round-7 rule stands (fleet8-bram T9's notice board): a read tells a beat no one enters in."""
    w = emberfall
    sing, _hut = _hut_beats(w)
    plain = _beat(w, 1, 6, "Marks on the Wall", directive="At Wolf's Hollow, the tally marks in threes and SING.",
                  trigger_type="after_beat", trigger_data={"beat_id": sing, "delay": "days"},
                  delivery_keywords=["tally marks", "SING"], status="armed", armed_day=1, armed_hour=19)
    _set(Player, w["player"], current_location_id=w["hollow"])
    memory_files.add_journal_entry(w["player"], 'Scratches on the hut wall: "Tally marks gouged into the doorframe, '
                                                'grouped in threes. Under them, one word, carved deep and old: '
                                                'SING."')
    result = camp.mark_beat_delivery(w["player"], "You read the wall.", player_input="I read the scratches.")
    assert plain in result["played"]


def test_a_find_away_from_the_person_leaves_their_scene_to_its_trigger(emberfall):
    w = emberfall
    _sing, hut = _hut_beats(w)
    _set(NPC, w["vex"], current_location_id=w["yard"])
    _set(Player, w["player"], current_location_id=w["hollow"])
    memory_files.add_journal_entry(w["player"], "Found Cracked Bone Whistle: Split down the barrel — silent now.")
    camp.mark_beat_delivery(w["player"], "You pocket the whistle.", player_input="I take the whistle.")
    row = _row(hut)
    assert row.status == "armed" and (row.fallback_day, row.fallback_hour) == (4, 19)


# --- 3. "If asked … admits X": only the admission tells it ---------------------

def test_the_glance_alone_never_tells_an_if_asked_beat(emberfall):
    """fleet10-wenna T6: 'He glances toward the north wall' marked 'A Tell at the Stable' delivered."""
    w = emberfall
    keywords = ["Sorrel", "ash-gray cloak", "north wall"]
    assert camp.setup_keywords(TELL_AT_THE_STABLE, keywords) == ["north wall"]     # a name stays: it seconds
    assert camp.setup_keywords(MAN_AT_THE_WALL, keywords) == []                      # the wall is in what he admits
    # A second press, a resistance first, a beat that tells its news unasked: no admission shape
    assert camp.admission("If pressed on who owns it, he deflects first, but if pressed a second time, harder, "
                          "he admits it was Vane's.") is None
    assert camp.admission("He is terse — deflects if pressed with 'the Watch has what it needs' — but his eyes go "
                          "to the gatehouse when he says it.") is None
    assert camp.admission("He tells Bram plainly he wore the same grey cloak. If pressed on why, Vane only says "
                          "'ask him what happened the week I left'.") is None
    assert camp.admission("Let him name the fence only if asked directly, otherwise just the sour tell.") is None
    tell = _beat(w, 1, 2, "A Tell at the Stable", directive=TELL_AT_THE_STABLE, delivery_keywords=keywords,
                 trigger_type="location", trigger_data={"location_id": w["yard"]},
                 lead_target={"type": "npc", "id": w["sorrel"]}, status="fired", delivery_attempts=1)
    names = ["Sorrel Tack", "Captain Brann Coldiron", "Stable Yard"]
    glance = "Sorrel: Eight bells, then past nine. He glances toward the north wall as he says it."
    told = "Sorrel: Two nights back I saw a man in an ash-gray cloak crouched by the wall, gone before I called."
    with get_session() as session:
        row = session.get(StoryBeat, tell)
        assert not camp.beat_keywords_delivered(row, glance, names, [])
        assert camp.beat_keywords_delivered(row, told, names, [])


def test_the_players_question_makes_the_admission_due_this_reply(emberfall):
    """fleet10-wenna T7: the exact question, answered "I haven't seen anything"."""
    w = emberfall
    _beat(w, 1, 2, "A Tell at the Stable", directive=TELL_AT_THE_STABLE,
          delivery_keywords=["Sorrel", "ash-gray cloak", "north wall"],
          trigger_type="location", trigger_data={"location_id": w["yard"]},
          lead_target={"type": "npc", "id": w["sorrel"]}, status="fired", delivery_attempts=2)
    _set(Player, w["player"], current_location_id=w["yard"])
    _met(w, "sorrel")
    asked = ("Sorrel, you looked north when you mentioned the howls. Have you seen something up there, or heard "
             "something besides the wolves?")
    line = camp.asked_note(w["player"], asked)
    assert line.startswith("[THE PLAYER ASKED — Sorrel Tack ADMITS IT NOW: he saw a man in an ash-gray cloak")
    assert "won't say more" not in line and "no denial, no roll" in line
    assert camp.asked_note(w["player"], "Which way to the inn?") == ""          # not about it
    assert camp.asked_note(w["player"], "I brush the horse down.") == ""        # no question
    assert camp.asked_note(w["player"], "[OPENING] Have you seen something?") == ""
    # With someone else there, the question must be put to him
    _set(NPC, w["coldiron"], current_location_id=w["yard"])
    assert camp.asked_note(w["player"], "Have you seen anything up there?") == ""
    assert camp.asked_note(w["player"], "Captain, I'm off to see Sorrel. What have you seen up north?") == ""
    assert camp.asked_note(w["player"], "Sorrel — what have you seen up there?").startswith("[THE PLAYER ASKED")
    assert camp.asked_note(w["player"], "Have you seen anything?", addressed_npc_id=w["sorrel"])


# --- 4. Spacing never holds a scene the player stands in -------------------------

def test_a_location_scene_with_its_person_here_is_not_held_by_spacing(emberfall):
    """fleet10-tamsin T6-T7: SING's misfire held 'The Man at the Wall' while she asked Sorrel."""
    w = emberfall
    wall = _beat(w, 1, 3, "The Man at the Wall", directive=MAN_AT_THE_WALL, trigger_type="location",
                 trigger_data={"location_id": w["yard"]}, lead_target={"type": "npc", "id": w["sorrel"]},
                 delivery_keywords=["Sorrel", "ash-gray cloak", "north wall"], status="armed",
                 armed_day=1, armed_hour=15, fallback_day=8, fallback_hour=15)
    _set(Campaign, w["campaign"], last_beat_day=1, last_beat_hour=16)
    _set(Player, w["player"], current_location_id=w["yard"])
    _set(NPC, w["sorrel"], current_location_id=w["square"])
    assert not camp.run_campaign_tick(w["player"]).get("fired_beat")      # he is not here: spacing holds
    _set(NPC, w["sorrel"], current_location_id=w["yard"])
    assert camp.run_campaign_tick(w["player"])["fired_beat"]["beat_id"] == wall


def test_a_beat_told_away_from_its_place_charges_no_spacing(emberfall):
    w = emberfall
    _beat(w, 1, 3, "The Pack at the Gate", beat_type="deadline_event", directive="At Wolf's Hollow the pack howls.",
          status="armed", armed_day=1, armed_hour=10, scheduled_day=1, scheduled_hour=15,
          fallback_day=1, fallback_hour=15)
    _set(Campaign, w["campaign"], last_beat_day=1, last_beat_hour=10)
    fired = camp.run_campaign_tick(w["player"])["fired_beat"]
    assert fired and fired["title"] == "The Pack at the Gate"
    with get_session() as session:
        assert session.get(Campaign, w["campaign"]).last_beat_hour == 10


# --- 5. The DM's @fight meets the pack, never the story's own wolves early -----

def test_fighting_a_story_wolf_before_its_beat_fights_one_of_its_pack(emberfall):
    """fleet10-bram T16: `@fight Gray-Flank Wolf, Hollow Wolf` killed act 3's drilled pack."""
    from src.services.commands import CommandRunner

    w = emberfall
    _beat(w, 3, 12, "The Pack Answers", beat_type="deadline_event",
          directive="Hollow Wolf and Gray-Flank Wolf run the North Gatehouse's perimeter at dusk.")
    _set(Player, w["player"], current_location_id=w["hollow"])
    runner = CommandRunner(w["player"], present_ids=[w["hollow_wolf"], w["grayflank"]])
    res = runner.run("@fight Hollow Wolf")
    assert res.ok, res.message
    enemy = [e["npc_id"] for e in res.result["enemies"]]
    assert enemy and w["hollow_wolf"] not in enemy
    with get_session() as session:
        stand = session.get(NPC, enemy[0])
        assert stand.danger_tier == "weak" and stand.current_location_id == w["hollow"]
        assert stand.description_physical == "A rangy gray wolf."
    assert "Hollow Wolf is the story's own" in res.message and "never hurt or dead" in res.message


def test_a_packmate_still_standing_takes_the_next_fight(emberfall):
    """Review: every @fight on a story wolf made a new wolf (and a sprite to draw); the stand-in the player
    fled from fights the next one."""
    from src.services.commands import CommandRunner

    w = emberfall
    _beat(w, 3, 12, "The Pack Answers", beat_type="deadline_event",
          directive="Hollow Wolf and Gray-Flank Wolf run the North Gatehouse's perimeter at dusk.")
    _set(Player, w["player"], current_location_id=w["hollow"])
    first = CommandRunner(w["player"], present_ids=[w["hollow_wolf"]]).run("@fight Hollow Wolf")
    stand = first.result["enemies"][0]["npc_id"]
    second = CommandRunner(w["player"], present_ids=[w["hollow_wolf"]]).run("@fight Hollow Wolf")
    assert second.ok, second.message
    assert [e["npc_id"] for e in second.result["enemies"]] == [stand]
    with get_session() as session:
        assert session.query(NPC).filter(NPC.current_location_id == w["hollow"],
                                         NPC.species == "wolf").count() == 3


def test_the_beat_that_stages_the_wolves_fights_them_for_real(emberfall):
    from src.services.commands import CommandRunner

    w = emberfall
    _beat(w, 3, 14, "Finale", beat_type="confrontation", status="fired", delivery_attempts=1,
          directive="At Wolf's Hollow, Vane stands among the drilled pack — Hollow Wolf at heel.")
    _set(Player, w["player"], current_location_id=w["hollow"])
    res = CommandRunner(w["player"], present_ids=[w["hollow_wolf"]]).run("@fight Hollow Wolf")
    assert res.ok and [e["npc_id"] for e in res.result["enemies"]] == [w["hollow_wolf"]]


# --- 6. The villain's people wait offstage for their entrance --------------------

def test_the_successor_is_offstage_until_his_entrance_fires(emberfall):
    """fleet10-tamsin T12-T20: Vex stood unseen in the Hollow while the prose said 'alone'."""
    w = emberfall
    _sing, hut = _hut_beats(w)
    with get_session() as session:
        vex = session.get(NPC, w["vex"])
        assert w["vex"] in camp.offstage_ids(session, w["player"])
        assert camp.villain_held_line(session, w["player"], vex) == camp.CAST_OFFSTAGE_LINE
        # Never a beast, an ally or anyone else in the story's light
        for who in ("hollow_wolf", "sorrel", "coldiron", "piper"):
            assert w[who] not in camp.offstage_ids(session, w["player"])
    _set(StoryBeat, hut, status="fired")
    with get_session() as session:
        assert w["vex"] not in camp.offstage_ids(session, w["player"])


def test_a_successor_who_spoke_with_the_player_is_on_stage(emberfall):
    w = emberfall
    _hut_beats(w)
    _met(w, "vex")
    with get_session() as session:
        assert w["vex"] not in camp.offstage_ids(session, w["player"])


def test_the_brief_keeps_an_offstage_scout_to_signs(emberfall):
    from src.services.scene_brief import build_scene_brief

    w = emberfall
    _hut_beats(w)
    _set(Player, w["player"], current_location_id=w["hollow"])
    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, w["player"]))["text"]
    vex_block = text.split("real name Vex Marrow", 1)[1].split("\n- ", 1)[0]
    # As the villain's: no speaker label to keep, a glimpse by what is seen
    assert "OFFSTAGE until the story brings them on" in vex_block
    assert "as the speaker label until they introduce themselves" not in vex_block
    assert "If glimpsed at all, in prose they are only" in vex_block


def test_an_offstage_scout_is_no_one_to_fight(emberfall):
    from src.services.commands import CommandRunner

    w = emberfall
    _hut_beats(w)
    _set(Player, w["player"], current_location_id=w["hollow"])
    res = CommandRunner(w["player"], present_ids=[w["vex"]]).run("@fight Vex Marrow")
    assert not res.ok and res.result["refused"] == "offstage"


def test_a_line_from_the_offstage_scout_is_audited(emberfall):
    w = emberfall
    _hut_beats(w)
    _set(Player, w["player"], current_location_id=w["hollow"])
    camp.run_campaign_tick(w["player"])
    audit = camp.get_last_tick(w["player"])["audit"]
    assert [c["id"] for c in audit["offstage_cast"]] == [w["vex"]]
    warnings = audit_turn("He steps out of the birches.", [], {}, {}, campaign=audit,
                          spoken=[{"npc_id": w["vex"], "npc_name": "Ash-Cloaked Man", "text": "Leave."}])
    assert any(x["check"] == "cast_before_their_scene" for x in warnings)


def test_the_asked_line_rides_the_campaign_briefing(monkeypatch):
    """The orchestrator carries `asked_note` with the beat it answers — never on an engine turn."""
    from types import SimpleNamespace

    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator

    monkeypatch.setattr(dm_orchestrator, "get_player",
                        lambda pid: {"name": "Wenna Cole", "health_status": "healthy", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "### CAMPAIGN: spine")
    asked: list = []
    monkeypatch.setattr(camp, "asked_note", lambda pid, text, npc=None: asked.append(text) or "[THE PLAYER ASKED — X]")
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Wenna Cole")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    context = DMOrchestrator._build_context(fake, "Sorrel, have you seen something up there?")
    assert context.index("### CAMPAIGN: spine") < context.index("[THE PLAYER ASKED — X]")
    assert "[THE PLAYER ASKED" not in DMOrchestrator._build_context(fake, "[COMBAT OUTCOME] Victory.")
    assert asked == ["Sorrel, have you seen something up there?"]


# --- Review fixes ---------------------------------------------------------------

VEX_AT_THE_LANTERN = (
    "Vex Marrow is spotted drinking openly at the Drowned Lantern, bold now that the player has been seen poking "
    "at the Fangs' business. He pays Maera in freshly minted Greywater silver in plain view.")


def test_a_mood_set_on_the_villain_is_no_entrance_of_his(emberfall):
    """fleet10-tamsin's act-2 twist: Vex drinks in plain view; Vane's mood op made Vane an entrant, and the
    ENTRANCE note would have walked the villain into the tavern."""
    w = emberfall
    lantern = _beat(w, 2, 3, "Vex at the Drowned Lantern", beat_type="twist", directive=VEX_AT_THE_LANTERN,
                    lead_target={"type": "npc", "id": w["vex"]},
                    consequence_met=[{"op": "npc_mood", "npc_id": w["vane"], "mood": "hunting"}],
                    status="fired", delivery_attempts=1)
    _set(Player, w["player"], current_location_id=w["hollow"])
    with get_session() as session:
        beat, player = session.get(StoryBeat, lantern), session.get(Player, w["player"])
        assert [n.name for n in camp._entrance_people(session, beat, player)] == ["Vex Marrow"]
        notes = " ".join(camp._directive_notes(session, beat, player))
    assert "ENTRANCE" in notes and "Aldric Vane" not in notes
    # A mood op alone (nobody else brought on) still makes its person the entrance
    alone = _beat(w, 2, 4, "The Don Receives", beat_type="confrontation",
                  directive="He receives the player in his office and makes his offer.",
                  consequence_met=[{"op": "npc_mood", "npc_id": w["vex"], "mood": "calculating"}])
    with get_session() as session:
        assert [n.name for n in camp._entrance_people(session, session.get(StoryBeat, alone),
                                                      session.get(Player, w["player"]))] == ["Vex Marrow"]


def test_the_villain_held_for_another_scene_is_no_entrance(emberfall):
    """A beat that stages the villain is no ENTRANCE of his while his own first scene is still to come:
    his dossier says OFFSTAGE, and the note must not say it no longer holds."""
    w = emberfall
    first = _beat(w, 1, 2, "The Wolf-Caller Steps Out", beat_type="confrontation",
                  directive="Have Aldric Vane step out of the birches at Wolf's Hollow.")
    early = _beat(w, 1, 9, "A Shape on the Ridge", beat_type="setpiece",       # its clock came first
                  directive="Have Aldric Vane watch from the ridge as the pack circles.", status="fired",
                  delivery_attempts=1)
    with get_session() as session:
        player = session.get(Player, w["player"])
        assert camp._staged_people(session.get(StoryBeat, early), [session.get(NPC, w["vane"])])
        assert camp._entrance_people(session, session.get(StoryBeat, early), player) == []
        assert "ENTRANCE" not in " ".join(camp._directive_notes(session, session.get(StoryBeat, early), player))
        assert [n.name for n in camp._entrance_people(session, session.get(StoryBeat, first), player)] \
            == ["Aldric Vane"]


def test_a_tell_the_admission_repeats_is_still_no_evidence(emberfall):
    """fleet9-wenna's "A Face at the Wall": the glance and the admission both say "north wall" — the
    glance plus Sorrel's name told it, the same way wenna's stable beat was told."""
    w = emberfall
    face = ("Sorrel Tack, mucking out the Stable Yard, is visibly rattled and keeps glancing toward the north wall. "
            "If asked what's wrong, he admits — reluctantly, looking around first — that two nights back he saw a "
            "man in an ash-gray cloak crouched right at the north wall, not running from the howls but waiting on "
            "them. He won't give more than that unless pressed again, and even then only adds that the man moved "
            "like someone who'd worn a Watch cloak before.")
    keywords = ["Sorrel", "ash-gray cloak", "north wall", "Watch cloak"]
    assert camp.setup_keywords(face, keywords) == ["north wall"]
    beat = _beat(w, 1, 3, "A Face at the Wall", directive=face, delivery_keywords=keywords, status="fired",
                 delivery_attempts=1)
    names = ["Sorrel Tack", "Stable Yard"]
    with get_session() as session:
        row = session.get(StoryBeat, beat)
        assert not camp.beat_keywords_delivered(
            row, "Sorrel: The horses won't settle. He glances toward the north wall, then away.", names, [])
        assert camp.beat_keywords_delivered(
            row, "Sorrel: Two nights back, a man in an ash-gray cloak, crouched at the north wall.", names, [])


def test_being_sent_or_told_about_a_place_is_no_memory_of_it():
    places = ["Wolf's Hollow"]
    for text in ("Ever since you were sent to the Hollow, the Captain has watched the road.",
                 "When you left for Wolf's Hollow the sun was still up.",
                 "After you were told about the Hollow, Piper went quiet."):
        assert recalled_unvisited_place(text, places) is None, text
    assert recalled_unvisited_place("Ever since you left the Hollow, your hands have not stopped shaking.", places)


def test_someone_held_offstage_gets_no_chip(emberfall):
    """The canvas leaves the villain's people out; a talk or fight chip would put them back on screen."""
    from src.services.suggestions import suggest_actions

    w = emberfall
    with get_session() as session:
        lookout = NPC(name="Corvin Ash", profession="Ashen Fangs lookout", current_location_id=w["hollow"],
                      faction_id=w["fangs"], danger_tier="standard", current_mood="hostile")
        session.add(lookout)
        session.commit()
        lookout_id = lookout.id
    _beat(w, 2, 6, "The Lookout's Signal", beat_type="setpiece",
          directive="Have Corvin Ash signal the pack from the ridge above Wolf's Hollow.")
    _set(NPC, w["vex"], current_location_id=w["square"])
    _set(Player, w["player"], current_location_id=w["hollow"])
    with get_session() as session:
        assert lookout_id in camp.offstage_ids(session, w["player"])
        chips = suggest_actions(session, session.get(Player, w["player"]), None, w["hollow"])
    assert not any(c.get("npc_id") == lookout_id for c in chips), chips
