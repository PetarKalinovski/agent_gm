"""The scene brief: the DM's only source of facts, assembled in code.

The DM has no read tools any more, so every answer the engine would give
must already be in the brief: what each NPC remembers (their memory file),
whether they would join (the join gate itself), what a merchant sells and
for how much to THIS player (the counter's own numbers), the catalog's
price list, and which ways out are blocked. And it speaks in commands, not
tool names.
"""

import pytest

from src.models import (
    Connection, DMState, Location, LocationType, NPC, NPCRelationship, Player, Quest, QuestStatus,
    WorldClock, get_session,
)
from src.services import memory_files
from src.services.scene_brief import build_scene_brief


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    monkeypatch.setattr(memory_files, "memory_root", lambda: root)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    return root


@pytest.fixture()
def world(db, mem):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=20, minute=0))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0,
                          discovered=True, visited=True, rest={"kind": "inn", "price": 8, "quality": "good"})
        tavern = Location(name="The Drowned Lantern", type=LocationType.POI, depth=0, discovered=True)
        gate = Location(name="North Gate", type=LocationType.POI, depth=0, discovered=True)
        docks = Location(name="Saltreach Docks", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add_all([square, tavern, gate, docks])
        session.flush()
        session.add(Connection(from_location_id=square.id, to_location_id=tavern.id, travel_type="walk",
                               travel_time_hours=0.1, bidirectional=True, discovered=True))
        session.add(Connection(from_location_id=square.id, to_location_id=gate.id, travel_type="walk",
                               travel_time_hours=0.5, bidirectional=True, discovered=True,
                               requirements=["gate pass"]))
        brann = NPC(name="Captain Brann Coldiron", profession="watch captain", current_location_id=square.id,
                    secrets=["He sold the gate rota to smugglers."], goals=["Keep the north road open"])
        piper = NPC(name="Piper Vell", profession="courier", current_location_id=square.id)
        sorrel = NPC(name="Sorrel Tack", profession="merchant", current_location_id=square.id,
                     inventory_notable=[
                         {"id": "ashwood_blade", "name": "Ashwood Blade", "type": "weapon",
                          "slot": "weapon", "tier": "fine"},
                         {"id": "dockhand_draught", "name": "Dockhand's Draught", "type": "consumable",
                          "effect_tag": "heal_light", "quantity": 2},
                     ])
        vex = NPC(name="Vex Marrow", profession="fence", current_location_id=square.id,
                  inventory_notable=[{"id": "lockpick", "name": "Lockpick", "type": "misc", "value": 5}])
        wolf = NPC(name="Hollow Wolf", current_location_id=square.id, danger_tier="standard")
        maera = NPC(name="Maera Quill", profession="scribe", current_location_id=docks.id)
        session.add_all([brann, piper, sorrel, vex, wolf, maera])
        player = Player(name="Ilse Varna", current_location_id=square.id, currency=50)
        session.add(player)
        session.flush()
        session.add_all([
            NPCRelationship(npc_id=brann.id, player_id=player.id, trust_level=70, current_disposition="neutral"),
            NPCRelationship(npc_id=piper.id, player_id=player.id, trust_level=50, current_disposition="neutral"),
            NPCRelationship(npc_id=sorrel.id, player_id=player.id, trust_level=50, current_disposition="friendly"),
            NPCRelationship(npc_id=vex.id, player_id=player.id, trust_level=10, current_disposition="hostile"),
            NPCRelationship(npc_id=maera.id, player_id=player.id, trust_level=50, current_disposition="neutral"),
        ])
        session.add(Quest(title="Teeth on the North Road", description="Wolves on the road.",
                          status=QuestStatus.NOT_STARTED, assigned_by_npc_id=brann.id))
        session.commit()
        return {"player": player.id, "brann": brann.id, "piper": piper.id, "sorrel": sorrel.id,
                "vex": vex.id, "wolf": wolf.id, "maera": maera.id, "square": square.id}


def _brief(world, addressed=None):
    with get_session() as session:
        return build_scene_brief(session, session.get(Player, world["player"]), addressed_npc_id=addressed)


def _dossier(text, name):
    """The lines of one NPC's dossier."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("- "):
            inside = line.startswith(f"- {name} [")
        elif not line.startswith("    "):
            inside = False
        if inside:
            out.append(line)
    return "\n".join(out)


def test_return_shape_and_present_people(world):
    brief = _brief(world)
    assert set(brief) == {"text", "present", "strangers", "refusal"}
    assert set(brief["present"]) == {world[k] for k in ("brann", "piper", "sorrel", "vex", "wolf")}
    assert brief["refusal"] is None
    assert _brief(world, addressed=world["vex"])["refusal"]["npc_id"] == world["vex"]


def test_a_job_kept_for_a_later_act_is_not_on_offer(world, monkeypatch):
    """Round 5: offer_directive kept a later act's quest back, but the
    dossier still listed it, so the DM pitched it from there."""
    with get_session() as session:
        qid = session.query(Quest).filter(Quest.title == "Teeth on the North Road").one().id
    assert "Teeth on the North Road" in _dossier(_brief(world)["text"], "Captain Brann Coldiron")
    monkeypatch.setattr("src.services.campaign.later_act_quest_ids", lambda session, player_id: {qid})
    assert "Teeth on the North Road" not in _dossier(_brief(world)["text"], "Captain Brann Coldiron")


def test_blocked_exits_say_why(world):
    text = _brief(world)["text"]
    gate = next(line for line in text.splitlines() if "North Gate" in line and "·" in line)
    assert "BLOCKED (route: gate pass)" in gate
    tavern = next(line for line in text.splitlines() if "The Drowned Lantern" in line and "·" in line)
    assert "BLOCKED" not in tavern


def test_addressed_dossier_carries_the_whole_memory_file(world):
    pid, brann = world["player"], world["brann"]
    memory_files.add_npc_note(pid, brann, "She took the north road job for 60 coin.")
    for i in range(6):
        memory_files.append_exchange(pid, brann, f"question {i}", [f"answer {i}"], "Day 1 19:00")
    dossier = _dossier(_brief(world, addressed=brann)["text"], "Captain Brann Coldiron")
    assert "Their memory of you" in dossier
    assert "(Day 1) She took the north road job for 60 coin." in dossier
    assert 'Day 1 19:00 · Ilse: "question 0"' in dossier          # whole file, not a window
    assert 'Day 1 19:00 · Brann Coldiron: "answer 5"' in dossier
    assert "never spoken with you" not in dossier


def test_bystander_dossier_carries_notes_and_the_last_three_lines(world):
    pid, piper = world["player"], world["piper"]
    memory_files.add_npc_note(pid, piper, "Thinks Ilse is a smuggler")
    for i in range(4):
        memory_files.append_exchange(pid, piper, f"question {i}", [f"answer {i}"], "Day 1 19:30")
    dossier = _dossier(_brief(world)["text"], "Piper Vell")
    assert "Knows of you: (Day 1) Thinks Ilse is a smuggler" in dossier
    words = next(line for line in dossier.splitlines() if "Last words between you" in line)
    assert words.count("·") == 3
    assert "answer 3" in words and "question 3" in words and "answer 2" in words
    assert "question 2" not in words


def test_a_stranger_has_never_spoken(world):
    dossier = _dossier(_brief(world)["text"], "Hollow Wolf")
    assert "never spoken with you" in dossier
    assert "Last words" not in dossier and "memory" not in dossier.lower()
    # No relationship row: the gates read no trust, so the brief prints none
    assert "a stranger, no trust built yet" in dossier and "trust 50/100" not in dossier
    assert "trust 50/100, neutral" in _dossier(_brief(world)["text"], "Piper Vell")


def test_would_join_uses_the_join_gate(world):
    text = _brief(world)["text"]
    # trust 50 is the stranger default: one conversation recruits nobody
    assert "Would come along if asked: no — " in _dossier(text, "Piper Vell")
    assert "Would come along if asked: yes" in _dossier(text, "Captain Brann Coldiron")
    wolf = _dossier(text, "Hollow Wolf")           # no relationship row: the gate says no
    assert "Would come along if asked: no — " in wolf and "trust" in wolf
    vex = _dossier(text, "Vex Marrow")
    assert "Would come along if asked: no — Vex Marrow would sooner see the player dead" in vex


def test_party_members_get_no_join_line(world):
    with get_session() as session:
        session.get(Player, world["player"]).party_members = [world["piper"]]
        session.commit()
    dossier = _dossier(_brief(world)["text"], "Piper Vell")
    assert "IN YOUR PARTY" in dossier and "Would come along" not in dossier


def test_merchant_stock_is_priced_for_this_player(world):
    text = _brief(world)["text"]
    sells = next(line for line in _dossier(text, "Sorrel Tack").splitlines() if "Sells" in line)
    # friendly: 0.9 x the catalog (fine weapon 120, heal_light 25)
    assert "Ashwood Blade (fine weapon) 108 coin" in sells
    assert "Dockhand's Draught x2 (potion: heal_light) 23 coin" in sells
    assert "@buy <item> | from: Sorrel Tack" in sells
    assert "Sorrel Tack likes them" in sells
    # Someone who will not talk will not trade either
    vex = _dossier(text, "Vex Marrow")
    assert "Sells" not in vex and "WILL NOT TALK" in vex


def test_price_list_comes_from_the_tables(world):
    from src.services import progression

    text = _brief(world)["text"]
    assert "Price list" in text
    weapon = progression.TIER_PRICE["weapon"]
    assert (f"Weapon: crude {weapon[0]} · common {weapon[1]} · fine {weapon[2]} · "
            f"superior {weapon[3]} · master {weapon[4]} coin") in text
    assert f"heal_light {progression.CONSUMABLES['heal_light']['price']}" in text
    assert "legendary" not in text.split("Price list")[1].split("Potions")[0]
    assert "A night's sleep here: 8 coin (inn, good bed) — write @rest." in text


def test_commands_not_tool_names(world):
    text = _brief(world)["text"]
    brann = _dossier(text, "Captain Brann Coldiron")
    assert "@quest-start it" in brann
    assert "write @reveal Captain Brann Coldiron <index> FIRST" in brann
    for tool_word in ("reveal_secret", "activate_quest", "the `rest` tool", "read tools"):
        assert tool_word not in text


def test_talking_to_someone_elsewhere_still_brings_their_memory(world):
    pid, maera = world["player"], world["maera"]
    memory_files.append_exchange(pid, maera, "Can you forge a pass?", ["For a price."], "Day 1 12:00")
    text = _brief(world, addressed=maera)["text"]
    assert "THE PLAYER IS TALKING TO: Maera Quill" in text and "NOT here" in text
    assert 'Maera Quill: "For a price."' in text
    assert "Maera Quill [" in text.split("People you know elsewhere:")[1]


def test_one_malformed_npc_does_not_cost_the_scene(world):
    # Authored JSON is not always the declared shape: a bare-string secret
    # and goal read as one entry, junk in revealed_secrets is ignored, and a
    # dossier that still fails degrades to one line instead of losing the brief
    with get_session() as session:
        piper = session.get(NPC, world["piper"])
        piper.secrets = "Carries letters for the smugglers."
        piper.goals = "Get out of Saltreach"
        rel = session.query(NPCRelationship).filter(NPCRelationship.npc_id == world["piper"]).first()
        rel.revealed_secrets = [[0], "x", None]
        session.commit()
    text = _brief(world)["text"]
    piper = _dossier(text, "Piper Vell")
    assert "Wants: Get out of Saltreach" in piper
    assert "Hides 1 secret(s)" in piper
    assert "Market Square" in text and "Price list" in text


def test_a_failing_dossier_degrades_to_one_line(world, monkeypatch):
    import src.services.scene_brief as brief_mod

    real = brief_mod._dossier

    def flaky(session, player, npc, **kw):
        if npc.name == "Piper Vell":
            raise ValueError("bad row")
        return real(session, player, npc, **kw)

    monkeypatch.setattr(brief_mod, "_dossier", flaky)
    brief = _brief(world)
    assert world["piper"] in brief["present"]
    assert "- Piper Vell [" in brief["text"] and "details unavailable" in brief["text"]
    assert "Would come along if asked" in _dossier(brief["text"], "Captain Brann Coldiron")


def test_secret_indices_are_reveal_secrets_indices(world):
    # reveal_secret indexes the raw list: an empty entry must not shift them
    with get_session() as session:
        session.get(NPC, world["brann"]).secrets = ["", "He sold the gate rota to smugglers."]
        session.commit()
    brann = _dossier(_brief(world)["text"], "Captain Brann Coldiron")
    assert "[1] He sold the gate rota" in brann and "[0]" not in brann


def test_a_covered_face_names_the_recognition_check(world):
    from src.services import perception
    from src.services import progression

    hood = {"id": "ashfall_hood", "name": "Ashfall Hood", "type": "armor", "slot": "armor",
            "tier": "common", "quantity": 1, "attire": {"style": "rough", "concealing": True}}
    with get_session() as session:
        player = session.get(Player, world["player"])
        player.inventory = [hood]
        player.equipment = {"armor": progression.item_key(hood)}
        session.commit()
    perception.clear_recognition()
    try:
        piper = _dossier(_brief(world)["text"], "Piper Vell")
    finally:
        perception.clear_recognition()
    assert "face is covered" in piper
    assert f'roll_check skill "stealth", difficulty "tricky", about_npc_id "{world["piper"]}"' in piper


def test_legacy_relationship_memory_shows_up_migrated(world):
    pid, piper = world["player"], world["piper"]
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(NPCRelationship.npc_id == piper).first()
        rel.key_moments = ["First met on Day 1"]
        rel.recent_messages = [{"role": "player", "content": "Deliver this?", "day": 1},
                               {"role": "npc", "content": "Two coin.", "day": 1}]
        session.commit()
    dossier = _dossier(_brief(world)["text"], "Piper Vell")
    assert "Knows of you: First met on Day 1" in dossier
    assert 'Piper Vell: "Two coin."' in dossier
    assert memory_files._read(memory_files.npc_doc(pid, piper)) is not None


# --- fleet9: the job as written, the report back, what the character wants ---

def _teeth(world, status, steps):
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.title == "Teeth on the North Road").one()
        quest.status, quest.objectives = status, steps
        session.commit()


def test_work_on_offer_carries_its_steps_and_where_the_first_is(world):
    """fleet9-bram T17-T18: the Keeper's pitch contradicted its own steps."""
    _teeth(world, QuestStatus.NOT_STARTED, [
        {"text": "Ask Piper Vell what the couriers saw", "kind": "talk_to", "target_id": world["piper"]},
        {"text": "Walk the north road", "kind": "custom"},
    ])
    dossier = _dossier(_brief(world)["text"], "Captain Brann Coldiron")
    assert ("Steps: 1) Ask Piper Vell what the couriers saw — Piper Vell, here; 2) Walk the north road"
            in dossier)
    assert "what they ask for is its steps as written, never another room, key or rule" in dossier


def test_a_giver_reported_back_to_adds_something_new(world):
    """fleet9-tamsin T16-T19: the player and Piper retold the howl relay and
    the wolf to the Captain, and he added only the pass."""
    pid, brann = world["player"], world["brann"]
    _teeth(world, QuestStatus.ACTIVE, [
        {"text": "Find out what drives the wolves", "kind": "custom"},
        {"text": "Bring proof to the Captain", "kind": "deliver", "item_name": "Bone Whistle", "target_id": brann},
    ])
    memory_files.append_exchange(pid, brann, "I'll look into it.", ["Good. Mind the howl."], "Day 1 19:00")
    assert "Reporting back" not in _brief(world)["text"], "nothing learned since they spoke"
    memory_files.add_journal_entry(pid, 'Sorrel: "A man in an ash-gray cloak at the north wall."')
    line = next(x for x in _dossier(_brief(world)["text"], "Captain Brann Coldiron").splitlines()
                if "Reporting back" in x)
    assert 'Reporting back on "Teeth on the North Road": they learned this since you last spoke' in line
    assert "ash-gray cloak" in line and "never a recap of what the player just told them" in line
    assert "the next lead (Find out what drives the wolves)" in line
    assert "an admission from their 'Would now confess' line" in line         # trust 70: he would
    # Once they have spoken again, it is no longer news
    memory_files.append_exchange(pid, brann, "A man at the wall.", ["Ash-gray. I know the cloak."], "Day 1 20:30")
    assert "Reporting back" not in _brief(world)["text"]


def test_a_find_is_news_only_when_it_is_the_job_s_own(world):
    """fleet9-bram: the Captain's report-back line led with a coil of rope
    out of a market crate."""
    pid, brann = world["player"], world["brann"]
    _teeth(world, QuestStatus.ACTIVE, [
        {"text": "Find out what drives the wolves", "kind": "custom"},
        {"text": "Bring proof to the Captain", "kind": "deliver", "item_name": "Bone Whistle", "target_id": brann},
    ])
    memory_files.append_exchange(pid, brann, "I'll look into it.", ["Good. Mind the howl."], "Day 1 19:00")
    memory_files.add_journal_entry(pid, "Found Coil of Hemp Rope in the abandoned market crate: Twenty feet.")
    assert "Reporting back" not in _brief(world)["text"]
    memory_files.add_journal_entry(pid, "Found Bone Whistle in the bracken: Split down the barrel.")
    line = next(x for x in _brief(world)["text"].splitlines() if "Reporting back" in x)
    assert "Bone Whistle" in line and "Hemp Rope" not in line


def test_a_step_that_is_the_report_names_it(world):
    _teeth(world, QuestStatus.ACTIVE, [
        {"text": "Find the whistle", "kind": "custom", "done": True},
        {"text": "Tell the Captain what you found", "kind": "talk_to", "target_id": world["brann"]},
    ])
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["brann"]).one()
        rel.trust_level = 40                                       # below the secret's gate
        session.commit()
    dossier = _dossier(_brief(world)["text"], "Captain Brann Coldiron")
    assert ('Reporting back on "Teeth on the North Road": their next step is this report ("Tell the Captain '
            'what you found")') in dossier
    assert "a tell that they are holding something back (never the secret itself)" in dossier


def _persona(world, background, description=""):
    with get_session() as session:
        player = session.get(Player, world["player"])
        player.background, player.description = background, description
        session.commit()
    return next((x for x in _brief(world)["text"].splitlines() if x.startswith("What this character wants")), "")


def test_the_character_s_own_wants_are_held_out_to_them(world):
    """fleet9-tamsin: a card-sharp and pickpocket was offered no mark, purse,
    fence or con in 21 replies; Hesk's fence secret sat unused."""
    line = _persona(world, "A card-sharp and pickpocket run out of three cities.")
    assert "money that is not nailed down" in line and "a fat purse on a careless belt" in line
    assert "Sorrel Tack's wares and till" in line                    # two things for sale: a stall
    assert "Vex Marrow's wares" not in line, "one lockpick is no stall"
    # His "sold the gate rota to smugglers" is in her line of work
    assert "Captain Brann Coldiron keeps a secret in this very line of work" in line
    assert "smugglers" not in line and "rota" not in line, "the secret itself stays sealed"
    assert "never two running" in line
    soldier = _persona(world, "Twenty years in a border company that was disbanded without pay.")
    assert "a fight worth having" in soldier and "Here: Captain Brann Coldiron, an officer." in soldier
    folk = _persona(world, "A collector of folk tales who writes down what people remember.")
    assert "a story nobody has written down" in folk


@pytest.mark.parametrize("background, description", [
    ("", "A mysterious figure awaiting their story."),          # the placeholder
    ("A quiet traveller looking for her brother.", "Tall, in a blue coat."),
    ("A scout, stealthy and patient.", ""),                      # stealth is no stealing
])
def test_no_want_is_made_up(world, background, description):
    assert _persona(world, background, description) == ""
