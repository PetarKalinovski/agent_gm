"""The opening turn: a new character's first moment is a story, not a prompt."""

import re
from types import SimpleNamespace

from src.models import (
    Campaign, DMState, Location, LocationType, NPC, Player, Quest, QuestStatus, StoryBeat,
    WorldBible, WorldClock, Cinematic, get_session,
)
from src.services import opening
from src.services.cinematics import request_cinematic


def _world(session, *, with_bible=True, player_name="Rook Fennick", background="", description=""):
    session.add(WorldClock(day=1, hour=20, minute=0))
    session.add(DMState(current_arc="Something is organizing the wolves."))
    if with_bible:
        session.add(WorldBible(name="Emberfall", genre="fantasy", tagline="A dusk frontier town",
                               current_situation="Wolves press the north road.",
                               pc_suggested_name="Rook Fennick",
                               pc_starting_situation="A courier with one last letter."))
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                      description="Stalls fold under a copper sky; the Watch stand near the well.")
    session.add(square)
    session.flush()
    session.add(NPC(name="Captain Brann Coldiron", current_location_id=square.id))
    session.add(NPC(name="Dead Man", current_location_id=square.id, status="dead"))
    player = Player(name=player_name, current_location_id=square.id,
                    background=background, description=description)
    session.add(player)
    session.commit()
    return player.id, square.id


# Every tool the DM used to be told to call. The opening speaks the reply
# format now (speaker lines, @ command lines); only roll_check is still a tool.
_LEGACY_TOOLS = (
    "speak", "prompt_npc_agent", "get_available_quests_for_npc", "create_quest", "activate_quest",
    "describe_location", "narrate", "update_quest_status", "update_quest_objectives", "move_player",
    "advance_time", "get_current_location", "get_npcs_at_location", "start_combat", "play_cinematic",
)
_NOT_TOOLS = {"roll_check", "talk_to", "go_to"}   # the one tool + objective step kinds


def _assert_no_tool_names(text: str) -> None:
    for name in _LEGACY_TOOLS:
        assert not re.search(rf"\b{name}\b", text), f"directive names the tool {name!r}"
    for ident in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text):
        assert ident in _NOT_TOOLS, f"directive names a tool-like identifier {ident!r}"


def test_is_first_turn_reads_the_dm_history():
    assert opening.is_first_turn(SimpleNamespace(agent=SimpleNamespace(messages=[]))) is True
    assert opening.is_first_turn(SimpleNamespace(agent=SimpleNamespace(messages=[{"role": "user"}]))) is False

    class Broken:
        @property
        def agent(self):
            raise RuntimeError("no session file")

    assert opening.is_first_turn(Broken()) is False


def test_opening_plays_a_title_card_past_the_daily_cap_and_is_ledgered(db):
    with get_session() as session:
        player_id, _ = _world(session)
    # A DM card already burned today's allowance …
    assert request_cinematic("title_card", player_id=player_id, title="Earlier", source="dm")["ok"]
    assert not request_cinematic("title_card", player_id=player_id, title="Again", source="dm")["ok"]

    payload = opening.opening_payload(player_id)
    assert payload["opening"] is True
    assert payload["cinematic"]["kind"] == "title_card"
    assert payload["cinematic"]["title"] == "Emberfall"
    assert payload["cinematic"]["subtitle"] == "A dusk frontier town"
    with get_session() as session:
        rows = session.query(Cinematic).filter(Cinematic.source == "opening").all()
        assert len(rows) == 1 and rows[0].status == "played"


def test_scene_is_the_location_row_and_the_living_present(db):
    with get_session() as session:
        player_id, _ = _world(session)
    payload = opening.opening_payload(player_id)
    assert payload["scene"]["location"] == "Market Square"
    assert "copper sky" in payload["scene"]["description"]
    assert payload["scene"]["time_of_day"] == "evening"
    assert payload["scene"]["npcs"] == ["Stranger"], "the player's screen shows who they see, not a name"


def test_directive_without_a_campaign_uses_the_world_and_the_arc(db):
    with get_session() as session:
        player_id, _ = _world(session)
    text = opening.opening_payload(player_id)["dm_message"]
    assert text.startswith("[OPENING]")
    assert "Rook Fennick's first moment" in text
    assert "Wolves press the north road." in text
    assert "A courier with one last letter." in text
    assert "Something is organizing the wolves." in text
    assert "do not re-describe the place" in text
    assert "END this response by handing them one concrete next thing to do" in text
    assert f"Present: Stranger [real name Captain Brann Coldiron, not yet known to the player]." in text
    _assert_no_tool_names(text)


def test_suggested_name_matches_case_and_spacing_insensitively(db):
    with get_session() as session:
        player_id, _ = _world(session, player_name="  rook   FENNICK ")
    assert "A courier with one last letter." in opening.opening_payload(player_id)["dm_message"]


def test_a_custom_character_gets_their_own_background_not_the_suggested_situation(db):
    """Playtest 2026-10-04: Ilse the apothecary was opened on as a courier."""
    with get_session() as session:
        player_id, _ = _world(session, player_name="Ilse Varrow",
                              background="An apothecary who left the guild under a cloud.",
                              description="Ink-stained fingers, a satchel of tinctures.")
    text = opening.opening_payload(player_id)["dm_message"]
    assert "courier" not in text.lower()
    assert ("Who they are: An apothecary who left the guild under a cloud. "
            "Ink-stained fingers, a satchel of tinctures.") in text
    assert "Wolves press the north road." in text, "the world's situation is everyone's"
    assert "Ilse Varrow's first moment" in text
    _assert_no_tool_names(text)


def test_placeholder_description_is_not_a_character(db):
    with get_session() as session:
        player_id, _ = _world(session, player_name="Unnamed",
                              description="A mysterious figure awaiting their story.")
    text = opening.opening_payload(player_id)["dm_message"]
    assert "Who they are" not in text
    assert "courier" not in text.lower()


def test_no_bible_suggested_name_means_no_starting_situation(db):
    with get_session() as session:
        player_id, _ = _world(session)
        session.query(WorldBible).first().pc_suggested_name = ""
        session.commit()
    assert "courier" not in opening.opening_payload(player_id)["dm_message"].lower()


def test_hand_over_without_work_on_offer_asks_for_a_new_quest_in_reply_format(db):
    with get_session() as session:
        player_id, _ = _world(session)
    text = opening.opening_payload(player_id)["dm_message"]
    assert "@quest-new" in text and "@quest-start" not in text
    assert "real person or place from the scene" in text
    assert 'Name (tone): "words"' in text
    _assert_no_tool_names(text)


def test_a_new_quest_first_step_points_away_from_the_giver(db):
    """The giver speaks in the same reply, and speaking ticks a talk_to aimed at
    them — a one-step "Talk to Brann" quest would arrive already done."""
    with get_session() as session:
        player_id, _ = _world(session)
    text = opening.opening_payload(player_id)["dm_message"]
    assert "never the one offering the work" in text
    assert "| from: <who offers it>" in text
    assert "step: go_to <place>" in text


def test_nobody_present_asks_for_work_without_a_giver(db):
    with get_session() as session:
        player_id, square_id = _world(session)
        session.query(NPC).filter(NPC.current_location_id == square_id).delete()
        session.commit()
    text = opening.opening_payload(player_id)["dm_message"]
    assert "Present:" not in text
    assert "someone arriving or something posted here offers work" in text
    assert "from:" not in text
    _assert_no_tool_names(text)


def test_hand_over_prefers_authored_work_from_someone_present(db):
    with get_session() as session:
        player_id, _ = _world(session)
        brann = session.query(NPC).filter(NPC.name == "Captain Brann Coldiron").one()
        elsewhere = NPC(name="Far Away Fen", current_location_id=None)
        session.add(elsewhere)
        session.flush()
        session.add(Quest(title="Teeth on the North Road", description="Wolves.",
                          status=QuestStatus.NOT_STARTED, assigned_by_npc_id=brann.id))
        session.add(Quest(title="Not Here", description="", status=QuestStatus.NOT_STARTED,
                          assigned_by_npc_id=elsewhere.id))
        session.commit()
    text = opening.opening_payload(player_id)["dm_message"]
    assert '"Teeth on the North Road" (Stranger [real name Captain Brann Coldiron, not yet known to the player])' in text
    assert "@quest-start <title>" in text and "@quest-new" not in text
    assert "Not Here" not in text
    _assert_no_tool_names(text)


def test_the_offer_names_what_the_job_pays(db):
    """fleet5-tamsin T0: the job was offered to a thief with no word of pay
    (it paid 60 coin and a charm), so she had nothing to bargain over."""
    with get_session() as session:
        player_id, _ = _world(session)
        brann = session.query(NPC).filter(NPC.name == "Captain Brann Coldiron").one()
        session.add(Quest(title="Teeth on the North Road", description="Wolves.", status=QuestStatus.NOT_STARTED,
                          assigned_by_npc_id=brann.id,
                          rewards={"currency": 60, "xp": 400,
                                   "items": [{"name": "Wolf-Tooth Charm", "kind": "trinket", "about": "A fang."}]}))
        session.commit()
    text = opening.opening_payload(player_id)["dm_message"]
    assert ('"Teeth on the North Road" (Stranger [real name Captain Brann Coldiron, not yet known to the player]), which pays 60 coin and a Wolf-Tooth Charm '
            '(say "a Wolf-Tooth Charm"; handed over finished, the player\'s to keep — a fang). Have them offer it, '
            "naming what it pays") in text
    assert "400" not in text and "xp" not in text.lower(), "growth is never a price"
    _assert_no_tool_names(text)


def test_hand_over_with_an_active_quest_offers_no_new_work(db):
    with get_session() as session:
        player_id, _ = _world(session)
        quest = Quest(title="Already Going", description="", status=QuestStatus.ACTIVE)
        session.add(quest)
        session.flush()
        session.get(Player, player_id).active_quests = [quest.id]
        session.commit()
    text = opening.opening_payload(player_id)["dm_message"]
    assert "END this response by handing them one concrete next thing to do" in text
    assert "@quest-start" not in text and "@quest-new" not in text
    assert 'Name (tone): "words"' in text


def test_directive_with_a_campaign_uses_the_spine_and_the_beat_lead_only(db):
    with get_session() as session:
        player_id, _ = _world(session)
        campaign = Campaign(player_id=player_id, title="The Whistle on the North Road", status="active",
                            premise="Someone is teaching the wolves to hunt on command.",
                            stakes_personal="The last letter you carried was to a dead man.",
                            acts=[{"act": 1, "title": "Teeth", "premise": "Find who whistles."}])
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(campaign_id=campaign.id, act=1, sequence=1, beat_type="revelation",
                              title="Sealed", status="armed",
                              directive="SECRET: Kestrel breeds the wolves in the kennel.",
                              lead="Ask the Captain what the howling means."))
        session.commit()
    payload = opening.opening_payload(player_id)
    text = payload["dm_message"]
    assert '"The Whistle on the North Road"' in text
    assert "teaching the wolves" in text and "dead man" in text
    assert "Act I — Teeth: Find who whistles." in text
    assert 'Their lead, already on their screen: "Ask the Captain what the howling means."' in text
    assert "Kestrel" not in text, "a beat's directive never leaks into the opening"
    assert payload["cinematic"]["title"] == "The Whistle on the North Road"
    assert payload["cinematic"]["subtitle"] == "Teeth"
    _assert_no_tool_names(text)


def test_the_opening_delivers_the_beat_whose_job_was_offering_the_first_quest(db):
    """fleet4-tamsin: "Coldiron Wants Eyes He Can Spare" sat pending all
    session to offer the quest the opening had started (completed at T11)."""
    with get_session() as session:
        player_id, _ = _world(session)
        brann = session.query(NPC).filter(NPC.name == "Captain Brann Coldiron").one()
        quest = Quest(title="Teeth on the North Road", description="Wolves.",
                      status=QuestStatus.NOT_STARTED, assigned_by_npc_id=brann.id)
        session.add(quest)
        campaign = Campaign(player_id=player_id, title="The Toll Road", status="active", current_act=1,
                            acts=[{"act": 1, "title": "Teeth", "premise": "The cart is late."}])
        session.add(campaign)
        session.flush()
        session.add_all([
            StoryBeat(campaign_id=campaign.id, act=1, sequence=1, beat_type="confrontation",
                      title="Coldiron Wants Eyes", status="pending", trigger_type="after_beat",
                      directive="Captain Coldiron corners them, offering the quest 'Teeth on the North Road'.",
                      lead="Captain Coldiron has been watching you."),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=2, beat_type="revelation",
                      title="Piper's Pattern", status="armed", directive="Piper shows her chart."),
        ])
        session.commit()
        quest_id = quest.id

    text = opening.opening_payload(player_id)["dm_message"]
    assert '"Teeth on the North Road" (Stranger [real name Captain Brann Coldiron, not yet known to the player])' in text, "the opening still offers it"
    with get_session() as session:
        beats = {b.title: b.status for b in session.query(StoryBeat).all()}
        assert session.get(Quest, quest_id).status == QuestStatus.NOT_STARTED, "the DM's @quest-start starts it"
    assert beats == {"Coldiron Wants Eyes": "delivered", "Piper's Pattern": "armed"}


def test_pending_campaign_counts_as_none(db):
    with get_session() as session:
        player_id, _ = _world(session)
        session.add(Campaign(player_id=player_id, title="Half-written", status="pending"))
        session.commit()
    payload = opening.opening_payload(player_id)
    assert payload["cinematic"]["title"] == "Emberfall"
    assert "Half-written" not in payload["dm_message"]


def test_unknown_player_and_unplaced_player(db):
    assert opening.opening_payload("nobody") is None
    with get_session() as session:
        player_id, _ = _world(session)
        session.get(Player, player_id).current_location_id = None
        session.commit()
    payload = opening.opening_payload(player_id)
    assert payload["scene"] is None and payload["opening"] is True
