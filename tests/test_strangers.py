"""Strangers and things: what the player (and the DM) is told about a scene.

Playtest 2026-10-04 (fleet critique):
  * "Talk chips spoil the villain" — chips read "Talk to Vex", "Talk to
    Aldric" (the boss) before either was introduced, and the player called
    them by name.
  * "NPCs know what nobody told them" — the DM labelled an unseen voice
    "Vex Marrow:" and NPCs used a name the player never gave.
  * "Authored mystery bypassed" — THE SCENE never listed the Hollow's
    objects, so the DM invented substitute clues.

A person the player has not met is called by a stable, spoiler-free role
label everywhere (dossier, speaker resolution, chips) until they speak
under their own name; the story's villain and any boss get no chip until
then; the place's authored objects are in the brief by label and kind,
never by contents.
"""

import pytest

from src.models import (
    Campaign, Faction, Location, LocationType, NPC, NPCRelationship, Player, WorldClock, get_session,
)
from src.services import dialogue, memory_files
from src.services.dialogue import (
    display_name, is_role_label, player_has_met, resolve_speaker, role_label, stranger_label,
)
from src.services.scene_brief import build_scene_brief
from src.services.scene_objects import ENGINE_SRC
from src.services.suggestions import suggest_actions


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    monkeypatch.setattr(memory_files, "memory_root", lambda: root)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    return root


HOLLOW_OBJECTS = [
    {"kind": "read", "label": "Scratches on the hut wall", "x": 28, "y": 38,
     "payload": {"text": "Tally marks grouped in threes. Under them, carved deep: SING."}},
    {"kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
     "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                          "description": "The same make as the one Coldiron never mentions."}}},
    {"kind": "open", "label": "Split supply crate", "x": 40, "y": 60,
     "payload": {"lock": "Nailed shut with Fang iron", "item": {"name": "Greywater Silver Piece"}}},
]


@pytest.fixture()
def world(db, mem):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21, minute=0))
        fangs = Faction(name="The Ashen Fangs")
        session.add(fangs)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True, visited=True,
                          objects=HOLLOW_OBJECTS)
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add_all([hollow, square])
        session.flush()
        vex = NPC(name="Vex Marrow", profession="Ashen Fangs scout", faction_id=fangs.id,
                  danger_tier="dangerous", current_location_id=hollow.id,
                  description_physical="A lean, menacing man in an ash-gray cloak, a necklace of wolf teeth, "
                                       "and a bone whistle on a cord.")
        aldric = NPC(name="Aldric Vane", profession="Ashen Fangs wolf-caller", faction_id=fangs.id,
                     danger_tier="boss", current_location_id=hollow.id,
                     description_physical="A broad, still man in a wolf-pelt mantle, ash rubbed into his beard.")
        wolf = NPC(name="Hollow Wolf", species="wolf", profession="wolf of the drilled pack",
                   danger_tier="weak", current_location_id=hollow.id,
                   description_physical="A rangy gray wolf, ribs showing.")
        hesk = NPC(name="Hesk Bramble", profession="Hawker of blades, tools and remedies",
                   current_location_id=hollow.id,
                   description_physical="A stooped, wiry hawker with a leather apron hung with knives.",
                   inventory_notable=[{"id": "skinning_knife", "name": "Skinning Knife", "type": "misc", "value": 4}])
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        session.add_all([vex, aldric, wolf, hesk, brann, sorrel])
        player = Player(name="Tamsin Rook", current_location_id=hollow.id, currency=30)
        session.add(player)
        session.commit()
        return {"player": player.id, "hollow": hollow.id, "square": square.id, "vex": vex.id,
                "aldric": aldric.id, "wolf": wolf.id, "hesk": hesk.id, "brann": brann.id, "sorrel": sorrel.id}


def _meet(world, *keys):
    with get_session() as session:
        for key in keys:
            session.add(NPCRelationship(npc_id=world[key], player_id=world["player"]))
        session.commit()


def _brief(world, addressed=None):
    with get_session() as session:
        return build_scene_brief(session, session.get(Player, world["player"]), addressed_npc_id=addressed)


def _block(text, head):
    """One dossier: its head line (starting `- <head>`) and its indented lines."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("- "):
            inside = line.startswith(f"- {head}")
        elif not line.startswith("    "):
            inside = False
        if inside:
            out.append(line)
    return "\n".join(out)


def _chips(world, lead=None):
    with get_session() as session:
        player = session.get(Player, world["player"])
        return suggest_actions(session, player, lead, player.current_location_id)


# ----------------------------------------------------------------------
# role_label
# ----------------------------------------------------------------------

def test_role_labels_for_the_playtest_cast(world):
    with get_session() as session:
        label = {key: role_label(session.get(NPC, world[key]))
                 for key in ("vex", "aldric", "wolf", "hesk", "brann", "sorrel")}
    assert label == {
        "vex": "Ash-Cloaked Man",           # a scout's trade is the secret: read by looks
        "aldric": "Wolf-Mantled Man",       # a boss is read by looks
        "wolf": "Hollow Wolf",              # a wolf is a wolf
        "hesk": "Hawker",                   # "of blades, tools and remedies" dropped
        "brann": "Watch Captain",           # the institution is public
        "sorrel": "Stable Hand",
    }


@pytest.mark.parametrize("profession, physical, expected", [
    ("Herbalist's apprentice", "", "Herbalist's Apprentice"),
    ("Bartender & Information Broker", "", "Bartender"),
    ("Plague doctor and researcher", "", "Plague Doctor"),
    ("Prophet (unwilling)", "", "Prophet"),
    ("Keyholder of the Iron Bank", "", "Bank Keyholder"),
    ("Keeper of The Drowned Lantern", "", "Keeper"),
    ("CEO, villain, genius", "", "CEO"),
    ("Spy / Information Broker", "A sharp-faced woman with dark hair and clever eyes.", "Sharp-Faced Woman"),
    ("Spymaster of the Court", "Tall, elegant woman in an elaborate crimson gown.", "Crimson-Gowned Woman"),
    ("", "An old, weathered veteran with a gray beard.", "Old Veteran"),
    ("", "A stooped figure in dusty grey robes.", "Grey-Robed Figure"),
    ("", "", "Stranger"),
    ("Syndicate lookout", "", "Stranger"),      # covert, and no looks to go on
])
def test_role_label_shapes(profession, physical, expected):
    npc = NPC(name="Jax Vane", profession=profession, description_physical=physical, species="human")
    label = role_label(npc)
    assert label == expected
    assert 1 <= len(label.split()) <= 3


def test_a_role_label_never_carries_the_name():
    # The look would be "Ash-Cloaked Woman" — but "Ash" is her name
    npc = NPC(name="Mirelle Ash", profession="", species="human",
              description_physical="A tired woman in an ash-gray cloak.")
    assert role_label(npc) == "Tired Woman"
    # A name made of the trade keeps the trade ("Kelp Farmer" for Kelp Farmer Finn)
    assert role_label(NPC(name="Kelp Farmer Finn", profession="Kelp farmer", species="human")) == "Kelp Farmer"


@pytest.mark.parametrize("name, profession, physical, expected", [
    # A possessive in the trade is someone's name — her surname, here; the
    # next trade listed stands in (labels found across the shipped worlds)
    ("Mici Teller", "Edward Teller's Wife & Mathematician", "", "Mathematician"),
    ("Peter Oppenheimer", "Oppenheimer's Son", "Small, dark-haired boy with blue eyes.", "Small Boy"),
    # An epithet is part of the name: no "Grey-Cloaked Man" for Gareth the Grey
    ("Gareth the Grey", "Ashen Fangs scout", "A lean man in a grey cloak.", "Lean Man"),
    ('Jax "The Shadow" Vane', "Spy", "A wiry man in a vane-patterned coat.", "Wiry Man"),
])
def test_a_role_label_never_carries_a_surname_or_epithet(name, profession, physical, expected):
    npc = NPC(name=name, profession=profession, description_physical=physical, species="human")
    assert role_label(npc) == expected


@pytest.mark.parametrize("profession, physical, expected", [
    ("None", "A baby born without grafts.", "Baby"),                 # was the label "None"
    ("n/a", "", "Stranger"),
    ("Maester serving House Ashford", "", "Maester"),                # was "Serving House Ashford"
    ("Knight, loyal to Prince Maekar", "", "Knight"),
    ("Retired adventurer turned innkeeper", "", "Innkeeper"),
    ("Santa Fe Office Manager & 'Gatekeeper'", "", "Office Manager"),  # was "Fe Office Manager"
    # Looks: an adverb goes with the word it bends ("Solidly Man" was a label)
    ("Spy", "A solidly built man in the crimson and black uniform of the guard.", "Crimson-Uniformed Man"),
    ("Spy", "Massively built knight, broad as a bull.", "Knight"),
    ("Spy", "A burly, bearded man.", "Burly Man"),
    # A colour is never a participle; how a thing is made is not what sets it apart
    ("Spy", "A man in a red cloak.", "Red-Cloaked Man"),
    ("Spy", "A man in a blood-red cloak.", "Red-Cloaked Man"),
    ("Spy", "Young knight. Wears simple but well-made armor.", "Simple-Armored Knight"),
    ("Spy", "A career soldier in an MP uniform.", "Uniformed Soldier"),
])
def test_role_label_review_shapes(profession, physical, expected):
    assert role_label(NPC(name="Jory Pell", profession=profession, description_physical=physical,
                          species="human")) == expected


@pytest.mark.parametrize("name, species, physical, beast", [
    ("Hollow Wolf", "wolf", "", True),
    # Species left at its "human" default: the name and the looks decide
    ("Hollow Wolf", "human", "A rangy gray wolf, ribs showing.", True),
    ("Shadowmaw", "human", "A rangy gray wolf, ribs showing.", False),     # the pipeline reads the name
    # People whose names carry an animal (the sprite test called them animals,
    # so their real names became their labels)
    ('Ser Androw "The Bull" Bulwer', "Human (Andal)", "Massively built knight, broad as a bull.", False),
    ("Commander Fox", "human", "Clone trooper with red helmet markings.", False),
    ("Robin (Tim Drake)", "human", "Young, wearing a red and black suit.", False),
    ("Beast Boy", "human", "Green-skinned teenager.", False),
    ('Garrick "the Wolf" Thorne', "human", "A scarred hunter with a wolf-pelt hood.", False),
    ("Elias Fox", "human", "A wiry man with a fox-fur collar.", False),
    # A person the pipeline never called an animal stays a person, whatever
    # the description says first
    ("Reginald Brassworth", "human", "A bull of a man going to fat.", False),
])
def test_is_beast(name, species, physical, beast):
    npc = NPC(name=name, species=species, description_physical=physical, profession="")
    assert dialogue.is_beast(npc) is beast


def test_a_person_named_for_an_animal_is_still_a_stranger():
    garrick = NPC(name='Garrick "the Wolf" Thorne', species="human", profession="Raider chief",
                  description_physical="A scarred man with a wolf-pelt hood.")
    assert role_label(garrick) == "Raider Chief"
    assert stranger_label(garrick, met=False) == "Raider Chief"


def test_player_has_met_is_a_relationship_row(world):
    with get_session() as session:
        assert not player_has_met(session, world["player"], world["vex"])
        assert not player_has_met(session, None, world["vex"])
    _meet(world, "vex")
    with get_session() as session:
        assert player_has_met(session, world["player"], world["vex"])


def test_display_name_is_the_role_until_met(world):
    pid = world["player"]
    with get_session() as session:
        vex, wolf = session.get(NPC, world["vex"]), session.get(NPC, world["wolf"])
        assert display_name(session, pid, vex) == "Ash-Cloaked Man"
        assert stranger_label(vex, met=False) == "Ash-Cloaked Man"
        assert stranger_label(vex, met=True) is None
        assert display_name(session, pid, wolf) == "Hollow Wolf"
        assert stranger_label(wolf, met=False) is None
    _meet(world, "vex")
    with get_session() as session:
        assert display_name(session, pid, session.get(NPC, world["vex"])) == "Vex Marrow"


# ----------------------------------------------------------------------
# Speaker resolution and introductions
# ----------------------------------------------------------------------

def test_a_role_label_resolves_to_the_stranger_in_the_room(world):
    here = [world[k] for k in ("vex", "aldric", "wolf", "hesk")]
    with get_session() as session:
        assert resolve_speaker(session, "Ash-Cloaked Man", here).id == world["vex"]
        assert resolve_speaker(session, "the ash-cloaked man", here).id == world["vex"]
        assert resolve_speaker(session, "Wolf-Mantled Man", here).id == world["aldric"]
        assert resolve_speaker(session, "Hawker", here).id == world["hesk"]
        # Not in the room: a role label is nobody across the world
        assert resolve_speaker(session, "Watch Captain", here) is None
        assert resolve_speaker(session, "Watch Captain", [world["brann"]]).id == world["brann"]
        # Names still resolve as before
        assert resolve_speaker(session, "Vex", here).id == world["vex"]


def test_a_role_label_beats_a_partial_name_match(world):
    # "Ash-Cloaked Man" shares the word "ash" with Mirelle Ash; the role
    # label must win before the partial-word tiers ever see it
    with get_session() as session:
        mirelle = NPC(name="Mirelle Ash", profession="carter's widow", current_location_id=world["hollow"])
        session.add(mirelle)
        session.commit()
        here = [world["vex"], mirelle.id]
        assert resolve_speaker(session, "Ash-Cloaked Man", here).id == world["vex"]
        assert resolve_speaker(session, "Ash-Cloaked Man", [world["hesk"]]) is None
        assert resolve_speaker(session, "Mirelle Ash", here).id == mirelle.id


def test_a_curly_apostrophe_is_the_same_label(world):
    with get_session() as session:
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=world["hollow"])
        session.add(piper)
        session.commit()
        assert role_label(piper) == "Herbalist's Apprentice"
        assert resolve_speaker(session, "Herbalist’s Apprentice", [piper.id]).id == piper.id
        assert is_role_label("Herbalist’s Apprentice", piper)


def test_two_strangers_who_look_alike_stay_unattributed(world):
    with get_session() as session:
        twin = NPC(name="Corin Marrow", profession="Ashen Fangs scout", current_location_id=world["hollow"],
                   description_physical="A lean man in an ash-gray cloak.")
        session.add(twin)
        session.commit()
        assert role_label(twin) == "Ash-Cloaked Man"
        assert resolve_speaker(session, "Ash-Cloaked Man", [world["vex"], twin.id]) is None


def test_is_role_label(world):
    with get_session() as session:
        vex, wolf = session.get(NPC, world["vex"]), session.get(NPC, world["wolf"])
        assert is_role_label("Ash-Cloaked Man", vex) and is_role_label("the ash-cloaked man", vex)
        assert not is_role_label("Vex Marrow", vex) and not is_role_label("Vex", vex)
        assert not is_role_label("Hollow Wolf", wolf)       # a beast's label is its name


def test_speaking_as_a_stranger_is_remembered_but_introduces_no_one(world):
    pid, vex = world["player"], world["vex"]
    dialogue.record_turn_dialogue(pid, "Who's there?", [
        {"npc_id": vex, "npc_name": "Ash-Cloaked Man", "label": "Ash-Cloaked Man", "text": "Nobody you know."},
    ])
    assert 'Vex Marrow: "Nobody you know."' in memory_files.read_npc_memory(pid, vex)
    with get_session() as session:
        assert not player_has_met(session, pid, vex)
    # The line where he gives his name is the introduction
    dialogue.record_turn_dialogue(pid, "Name?", [
        {"npc_id": vex, "npc_name": "Ash-Cloaked Man", "label": "Vex", "text": "Vex. Remember it."},
    ])
    with get_session() as session:
        assert player_has_met(session, pid, vex)


def test_the_display_name_stands_in_for_a_missing_label(world):
    pid = world["player"]
    # A note shown under the role label, with no raw label: still a stranger
    dialogue.record_turn_dialogue(pid, "", [{"npc_id": world["hesk"], "npc_name": "Hawker", "text": "Knives!"}])
    # The legacy shapes (no label; the resolved real name) introduce as before
    dialogue.record_turn_dialogue(pid, "", [{"npc_id": world["sorrel"], "text": "Morning."}])
    dialogue.record_turn_dialogue(pid, "", [{"npc_id": world["brann"], "npc_name": "Captain Brann Coldiron",
                                             "text": "Stay off the north road."}])
    # A wolf's label is its name
    dialogue.record_turn_dialogue(pid, "", [{"npc_id": world["wolf"], "label": "Hollow Wolf", "text": "Grr."}])
    with get_session() as session:
        met = {k for k in ("hesk", "sorrel", "brann", "wolf") if player_has_met(session, pid, world[k])}
    assert met == {"sorrel", "brann", "wolf"}


def test_a_known_person_answering_to_their_role_is_still_known(world):
    _meet(world, "vex")
    pid, vex = world["player"], world["vex"]
    dialogue.record_turn_dialogue(pid, "Hey.", [{"npc_id": vex, "label": "Ash-Cloaked Man", "text": "What."}])
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(NPCRelationship.npc_id == vex).one()
        assert rel.last_interaction_day == 1


# ----------------------------------------------------------------------
# The scene brief
# ----------------------------------------------------------------------

def test_things_here_lists_labels_kinds_and_dm_only_contents(world):
    text = _brief(world)["text"]
    assert "Things here (authored" in text and "@interact <thing>" in text
    assert "never invent a substitute" in text
    assert "  · Scratches on the hut wall (read)" in text
    assert "  · Cracked bone whistle (take)" in text
    assert "  · Split supply crate (open, locked)" in text
    # Round-2 autoplay: a DM that could not see the contents narrated blind and
    # replaced the authored clue — so the DM sees them, marked as its own to keep
    assert "What each holds is for YOU" in text
    for shown in ("SING", "Cracked Bone Whistle"):
        assert shown in text


def test_used_up_things_are_marked_and_bodies_listed(world):
    with get_session() as session:
        hollow = session.get(Location, world["hollow"])
        objects = [dict(o) for o in HOLLOW_OBJECTS]
        objects[1] = {**objects[1], "consumed": True}
        objects.append({"kind": "body", "label": "Gray-Flank Wolf's body", "x": 50, "y": 50, "src": ENGINE_SRC,
                        "payload": {"items": [{"name": "Wolf Pelt"}], "coin": 3}})
        hollow.objects = objects
        session.commit()
    text = _brief(world)["text"]
    assert "  · Cracked bone whistle" not in text
    assert "Already used up here: Cracked bone whistle (taken)" in text
    assert "  · Gray-Flank Wolf's body (body)" in text
    assert "Wolf Pelt" not in text


def test_an_empty_place_has_no_things_section(world):
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["square"]
        session.commit()
    text = _brief(world)["text"]
    assert "Things here" not in text and "Already used up" not in text


def test_an_unmet_person_leads_with_their_role_label(world):
    brief = _brief(world)
    text = brief["text"]
    vex = _block(text, "Ash-Cloaked Man")
    assert vex.startswith(f"- Ash-Cloaked Man — real name Vex Marrow [{world['vex']}], NOT known to the player")
    assert ("The player has not learned this person's name. Call them 'Ash-Cloaked Man' in prose and as "
            "the speaker label until they introduce themselves (they decide if/when") in vex
    assert "Knows the player's name: no" in vex
    assert "Looks: A lean, menacing man in an ash-gray cloak" in vex
    assert not any(line.startswith("- Vex Marrow") for line in text.splitlines())
    # A wolf has no name to hide
    assert _block(text, "Hollow Wolf [").startswith("- Hollow Wolf [")
    assert brief["strangers"] == {world["vex"]: "Ash-Cloaked Man", world["aldric"]: "Wolf-Mantled Man",
                                  world["hesk"]: "Hawker"}
    assert set(brief["present"]) == {world[k] for k in ("vex", "aldric", "wolf", "hesk")}


def test_a_met_person_is_named_and_knows_the_player(world):
    _meet(world, "vex")
    brief = _brief(world)
    vex = _block(brief["text"], "Vex Marrow [")
    assert vex and "Knows the player's name: yes" in vex and "not learned" not in vex
    assert world["vex"] not in brief["strangers"]


def test_talking_to_a_stranger_names_them_by_role(world):
    text = _brief(world, addressed=world["vex"])["text"]
    assert (f"THE PLAYER IS TALKING TO: the Ash-Cloaked Man (Vex Marrow [{world['vex']}] — "
            "a name the player has not learned)") in text


# ----------------------------------------------------------------------
# Chips
# ----------------------------------------------------------------------

def test_chips_approach_strangers_by_role_and_hide_the_boss(world):
    chips = _chips(world)
    labels = [c["label"] for c in chips]
    assert "Approach the ash-cloaked man" in labels
    talk = next(c for c in chips if c["label"] == "Approach the ash-cloaked man")
    assert talk == {"kind": "talk", "npc_id": world["vex"], "label": "Approach the ash-cloaked man",
                    "text": "I approach the ash-cloaked man."}
    # The boss gets no chip at all; no chip says a name the player never heard
    assert not any(c.get("npc_id") == world["aldric"] for c in chips)
    for name in ("Vex", "Aldric", "Hesk", "Marrow", "Vane"):
        assert not any(name in c["label"] or name in c["text"] for c in chips)


def test_a_met_boss_gets_a_chip(world):
    _meet(world, "aldric")
    labels = [c["label"] for c in _chips(world)]
    assert "Talk to Aldric" in labels


def test_the_campaign_villain_gets_no_chip_until_met(world):
    with get_session() as session:
        session.get(NPC, world["aldric"]).danger_tier = "dangerous"   # not a boss: the campaign alone hides him
        session.add(Campaign(player_id=world["player"], villain_npc_id=world["aldric"],
                             successor_npc_id=world["vex"], status="active"))
        session.commit()
    chips = _chips(world)
    assert not any(c.get("npc_id") in (world["aldric"], world["vex"]) for c in chips)
    _meet(world, "vex")
    assert any(c.get("npc_id") == world["vex"] and c["label"] == "Talk to Vex" for c in _chips(world))


def test_a_villain_whose_label_is_their_name_still_gets_no_chip(world):
    """No role label hides "Hedge Knight" — the name is already generic — but
    the story's villain stays off the chips until met all the same."""
    with get_session() as session:
        knight = NPC(name="Hedge Knight", profession="Hedge knight", current_location_id=world["hollow"])
        session.add(knight)
        session.flush()
        session.add(Campaign(player_id=world["player"], villain_npc_id=knight.id, status="active"))
        session.commit()
        knight_id = knight.id
    assert not any(c.get("npc_id") == knight_id for c in _chips(world))


def test_a_person_named_for_an_animal_is_approached(world):
    """The sprite test called "Commander Fox" a beast, and beasts get no chip."""
    with get_session() as session:
        fox = NPC(name="Commander Fox", species="human", profession="Clone Commander / Senate Guard",
                  current_location_id=world["hollow"])
        session.add(fox)
        session.get(Location, world["hollow"]).objects = []
        session.commit()
        fox_id = fox.id
    chip = next(c for c in _chips(world) if c.get("npc_id") == fox_id)
    assert chip["label"] == "Approach the clone commander"


def test_a_failed_dossier_keeps_a_stranger_anonymous(world, monkeypatch):
    from src.services import scene_brief

    def broken(*_args, **_kwargs):
        raise RuntimeError("malformed row")

    monkeypatch.setattr(scene_brief, "_dossier", broken)
    text = _brief(world)["text"]
    assert f"- Ash-Cloaked Man — real name Vex Marrow [{world['vex']}], NOT known to the player" in text
    assert "(call them 'Ash-Cloaked Man')" in text
    assert not any(line.startswith("- Vex Marrow") for line in text.splitlines())


def test_trading_with_a_stranger_uses_their_role(world):
    with get_session() as session:
        for key in ("vex", "aldric"):
            session.get(NPC, world[key]).current_location_id = world["square"]
        hollow = session.get(Location, world["hollow"])
        hollow.objects = []
        session.commit()
    chips = _chips(world)
    trade = next(c for c in chips if c["kind"] == "trade")
    assert trade["label"] == "Trade with the hawker" and trade["text"] == "I look over what the hawker has for sale."


def test_a_lead_on_a_stranger_does_not_name_them(world):
    lead = {"text": "Find out who is drilling the wolves", "kind": "talk_to", "source": "quest",
            "quest_title": "Teeth on the North Road",
            "target": {"type": "npc", "id": world["vex"], "name": "Vex Marrow", "here": True},
            "route_next_hop_location_id": None}
    chip = _chips(world, lead)[0]
    assert chip["npc_id"] == world["vex"]
    assert chip["label"] == "Ask the ash-cloaked man about who is drilling the wolves"[:60]
    assert "Vex" not in chip["text"]
    # A lead that already told them the name may use it
    lead = {**lead, "text": "Talk to Vex Marrow"}
    assert _chips(world, lead)[0]["label"] == "Ask Vex about Teeth on the North Road"


def test_lookalike_strangers_are_one_chip(world):
    with get_session() as session:
        session.add(NPC(name="Corin Marrow", profession="Ashen Fangs scout", current_location_id=world["hollow"],
                        description_physical="A lean man in an ash-gray cloak."))
        session.get(Location, world["hollow"]).objects = []
        session.commit()
    labels = [c["label"] for c in _chips(world)]
    assert labels.count("Approach the ash-cloaked man") == 1


def test_one_chip_is_kept_for_the_things_here(world):
    """The Hollow's clues were pushed off when people and doors filled all four."""
    with get_session() as session:
        for name in ("Wren Ashby", "Tobin Reed"):
            session.add(NPC(name=name, profession="drover", current_location_id=world["hollow"]))
        session.commit()
    _meet(world, "hesk")
    chips = _chips(world)
    assert len(chips) == 4
    assert any(c["kind"] == "interact" for c in chips)
