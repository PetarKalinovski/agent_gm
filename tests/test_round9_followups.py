"""Follow-ups to the round-8 fix reviews (2026-10-05).

- A predicate possessive of the player's name reads "yours", never "your"
  ("a bed that isn't Bram's to search" came out "isn't your to search").
- With a beast in the room, a stand-off gesture that never names it still
  gets the roll-first line (the encounter director already held the ambush
  for that roll, but the DM was never told to cast it).
- The state carries who stands here, so the canvas can re-sync its people
  after the engine moved someone mid-turn.
- The repeated-details line never tells the DM to rest the plot's own things.
"""

import pytest

from src.models import NPC, Player, Quest, get_session
from src.services import encounters, roll_intent, voice_tics
from src.services.dialogue import without_player_name

from tests.test_stakes_round7 import _clear_directors, yard  # noqa: F401


@pytest.mark.parametrize("text, expected", [
    ("a bed that isn't Bram's to search", "a bed that isn't yours to search"),
    ("The choice is Bram's.", "The choice is yours."),
    ("What comes next was all Bram's", "What comes next was all yours"),
    ("Bram's horse is lame.", "Your horse is lame."),
    ("the coin is Bram's to spend", "the coin is yours to spend"),
])
def test_a_possessive_standing_for_the_thing_is_yours(text, expected):
    assert without_player_name(text, "Bram Holloway") == expected


def test_a_beast_in_the_room_makes_an_unnamed_stand_off_a_roll_first(yard):
    # fleet8-wenna T12: the wolf named only in her quotes
    words = 'I keep my voice gentle and don\'t break eye contact. "Easy, wolf. Easy."'
    assert roll_intent.directive(words) is None                      # no beast word
    assert encounters.beast_here(yard["player"])
    line = roll_intent.directive(words, beast_here=True)
    assert line and line.startswith(roll_intent.ROLL_FIRST_MARK)
    assert "@fight <its name>" in line


def test_no_beast_no_line_and_a_question_is_never_a_stand_off(yard):
    assert roll_intent.directive("Where does the north road go?", beast_here=True) is None
    with get_session() as session:
        session.get(NPC, yard["wolf"]).status = "dead"
        session.commit()
    assert not encounters.beast_here(yard["player"])


def test_a_hound_at_heel_is_no_stand_off(yard):
    with get_session() as session:
        session.get(Player, yard["player"]).party_members = [yard["wolf"]]
        session.commit()
    assert not encounters.beast_here(yard["player"])


def test_the_state_says_who_stands_here(yard):
    from src.web.server import build_player_state

    state = build_player_state(yard["player"])
    assert set(state["present_ids"]) == {yard["wolf"], yard["sorrel"]}
    with get_session() as session:
        session.get(NPC, yard["sorrel"]).current_location_id = None
        session.commit()
    assert build_player_state(yard["player"])["present_ids"] == [yard["wolf"]]


def test_the_plots_own_things_are_never_a_tic(yard):
    with get_session() as session:
        session.add(Quest(title="Proof", description="", objectives=[
            {"text": "Bring the cracked bone whistle", "kind": "obtain", "item_name": "Cracked Bone Whistle"}]))
        session.commit()
        player = session.get(Player, yard["player"])
        words = {voice_tics._key(w) for t in voice_tics._story_things(session, player)
                 for w, _, _ in voice_tics._words(t)}
    assert {"whistle", "bone"} <= words
