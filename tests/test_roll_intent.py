"""The dice before the prose, and no dice for a question.

fleet6-bram: prose_before_roll on 3 of 3 rolls, and T19 rolled a
critical-success eavesdrop on "Where do I find the Captain this hour?";
fleet6-wenna T10 rolled to read a public notice board and was cut by its
nail. A code classifier of the player's words orders the roll first when
they attempt something risky, and the audit flags a roll on a turn that
attempted nothing (src/services/roll_intent.py, turn_audit.roll_without_attempt).
"""

import pytest

from src.services import roll_intent
from src.web.turn_audit import roll_without_attempt

# What the fleets typed (trimmed), and the family the classifier should name
RISKY = [
    ("I pull my coat tighter and move off the road into the tree line, keeping low, staying quiet.", "stealth"),
    ("\"I'll do it,\" I say to the girl. \"But I want the ten coin and the tincture now — call it insurance.\"",
     "haggling"),
    ("I'm watching the figure in the window from the corner of my eye as we move, casual.", "stealth"),
    ("I head down into the mist, staying low and keeping to the treeline where the shadows are thickest.",
     "stealth"),
    ("I drift toward the hawker's stall, pointing away from his immediate line of sight.", "deception"),
    ("I slip the purse from his belt as the crowd jostles us.", "theft"),
    ("I try to persuade him to open the gate for me.", "persuasion"),
    ("I climb up the palisade where the timbers are lowest.", "athletics"),
    ("I pick the lock on the strongbox while Maera's back is turned.", "force"),
    ("I leap down from the hayloft onto the rider's back.", "athletics"),
    ("I climb—scrambling up the nearest tree trunk with my blade clenched between my teeth.", "athletics"),
]

# Asked, read, looked or walked — and the negated acts the regexes once took
NOT_RISKY = [
    # fleet6-bram T19
    "I nod, jaw tight. \"Fine. I'll take it to him.\" I pocket the whistle. \"Where do I find the Captain this hour?\"",
    # fleet6-bram T14
    "I push off toward the nearest lit window — the healer's place. I need this wound closed, then I'm finding Piper.",
    # fleet6-wenna T10
    "I meet Coldiron's eyes. \"Did the rider say anything about howls?\" I pull out my notebook. \"And I want to see "
    "that notice board before I leave.\"",
    "I drop coin on the counter without haggling, then find a seat by the fire.",
    "The key goes back to Maera — I'm not one to steal from people who've been honest with me.",
    "I keep my hands visible, my movements lazy, non-threatening.",
    "I stop in the road and listen, despite the cold creeping down my spine.",
    "I climb the stairs to Mirelle's door and knock.",
    "I force the words out through broken breaths.",
    "I sit with the letter. \"I want to know his name first,\" I tell her.",
    "I settle onto the bench, casual, like I belong here.",
    # Everyday climbs and jumps, and discretion in a talk: the roll-first
    # line is binding, so it must never fire on these
    "I climb into bed and pull the blanket up.",
    "I climb onto the stool at the bar and wave the Keeper over.",
    "I climbed the stairs, slow, my ribs aching.",
    "I jump down from the cart and stretch.",
    "I keep quiet about the letter and let her talk.",
    "I stay quiet and let him talk himself out.",
]


@pytest.mark.parametrize("text,family", RISKY)
def test_a_risky_act_is_named_and_orders_the_roll_first(text, family):
    act = roll_intent.risky_act(text)
    assert act is not None and act[0] == family
    line = roll_intent.directive(text)
    assert line.startswith("[THIS REPLY: call roll_check FIRST, before any text")


@pytest.mark.parametrize("text", NOT_RISKY)
def test_a_question_a_walk_or_a_negated_act_orders_nothing(text):
    assert roll_intent.risky_act(text) is None
    assert roll_intent.directive(text) is None


def test_a_plan_said_aloud_is_no_sneak_but_an_announced_watch_is_an_attempt():
    """Quotes are what the player SAYS: "I'll sneak in tomorrow" is a plan.
    "We mark where it comes from" said at the fence is the attempt itself."""
    assert roll_intent.risky_act('"Tomorrow night I\'ll sneak past the gate," I tell Sorrel.') is None
    said = '"Stay quiet. We mark the howl, we mark where it comes from," I murmur.'
    assert roll_intent.searching(said)
    assert roll_intent.attempted(said)


def test_engine_inputs_are_never_classified():
    assert roll_intent.risky_act("[OPENING] Sneak into the tale.") is None
    assert roll_intent.directive("[COMBAT OUTCOME — victory]") is None


def test_roll_without_attempt_flags_the_unasked_roll():
    rolls = [{"outcome": "critical_success", "skill": "perception",
              "reason": "spotting Coldiron checking the lanterns himself at this hour"}]
    warning = roll_without_attempt(NOT_RISKY[0], rolls)
    assert warning and warning["check"] == "roll_without_attempt"
    assert "spotting Coldiron" in warning["detail"]
    assert "never invent one to roll on" in warning["detail"]


def test_roll_without_attempt_spares_real_attempts_danger_and_recognition():
    rolls = [{"outcome": "failure", "skill": "stealth", "reason": "creeping down"}]
    assert roll_without_attempt(RISKY[0][0], rolls) is None                         # a sneak
    assert roll_without_attempt("I scan the treeline for whoever is out there.", rolls) is None  # a search
    assert roll_without_attempt(NOT_RISKY[0], rolls, danger_present=True) is None    # a confrontation
    recognised = [{"outcome": "success", "skill": "perception", "reason": "", "about_npc_id": "n1"}]
    assert roll_without_attempt(NOT_RISKY[0], recognised) is None                    # the context asked
    assert roll_without_attempt("[OPENING] begin", rolls) is None
    assert roll_without_attempt(NOT_RISKY[0], []) is None


def test_the_roll_first_line_rides_in_the_context(monkeypatch):
    """dm_orchestrator._build_context adds it on an ordinary turn only."""
    from types import SimpleNamespace

    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import REPLY_BUDGET, DMOrchestrator
    from src.services import complications, stakes

    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Tamsin Rook", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {"fired_events": []})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(complications, "directive_line", lambda *a, **k: None)
    monkeypatch.setattr(complications, "unanswered_line", lambda *a, **k: None)
    monkeypatch.setattr(stakes, "waited_line", lambda *a, **k: None)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Tamsin Rook")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- Teeth [q1]"]
    risky = "I slip the purse from his belt as the crowd jostles us."
    context = DMOrchestrator._build_context(fake, risky)
    assert context.index("[THIS REPLY: call roll_check FIRST") < context.index(REPLY_BUDGET)
    assert "call roll_check FIRST" not in DMOrchestrator._build_context(fake, "Where is the Captain?")
    assert "call roll_check FIRST" not in DMOrchestrator._build_context(fake, risky, constrained=True)


# --- Round 6 (fleet7): no dice in 21 turns -------------------------------------
# Replayed over every fleet player input (390): four new risky verdicts, each
# a real attempt (fleet7-wenna T14 and T16, fleet7-bram T9, fleet2-bram T12's
# sprint through a fight); fleet5 2/58 and fleet6 5/59 unchanged.

@pytest.mark.parametrize("text,family", [
    # fleet7-wenna T14, to the man with the wolves — the threat is SAID
    ('I pull out the cracked bone whistle. "Tell me what this means, what SING means, or I\'m taking this '
     'bone back and the Captain will send soldiers."', "intimidation"),
    ('"Hand over the ledger, or else."', "intimidation"),
    ('"Talk, or I\'ll drag you to the Watch myself."', "intimidation"),
    # fleet7-wenna T16: hurt, at night, off the villain's ground
    ("I clutch my torn arm and don't argue—I turn and run for the road.", "athletics"),
    ("I flee into the birches.", "athletics"),
    # fleet7-bram T9: facing down the Torn-Ear Wolf
    ("I hurl stones at the fence, shouting rough and loud to make myself big.", "intimidation"),
    ("I wave my arms to drive it off.", "intimidation"),
    ("I try to flee before the gate closes.", "athletics"),
    ("I break for the treeline.", "athletics"),
    ('"Answer me, or I\'ll call the Watch."', "intimidation"),
    ('"Tell me, or I\'ll take this to the Captain."', "intimidation"),
])
def test_a_threat_said_aloud_a_flight_and_facing_down_a_beast_are_risky(text, family):
    act = roll_intent.risky_act(text)
    assert act and act[0] == family, (text, act)


@pytest.mark.parametrize("text", [
    # fleet5-tamsin T14: an ultimatum with no harm in it
    '"I\'ve got a pass. Show yourself, or I\'m assuming you\'re not interested in talking."',
    '"Tell me if the gate is open, or should I ask the Captain?"',
    '"We go now or I\'ll take the north road alone."',
    "I won't flee. I stand my ground and ask him his name.",
    "I head back toward the square.",
    # Review: someone else's flight, a cart, the chill, a rest, a plan said aloud
    "I ask Piper where the wolves fled.",
    "I ask about the carters who fled the north road.",
    "I tell her to flee while she still can.",
    "I ask whether the townsfolk fleeing south made it.",
    "I drive the cart away from the gate.",
    "I hold my hands to the fire to chase the chill away.",
    "We break for the night at the inn.",
    '"Tell me which road, or I\'ll take the long way round."',
    '"Tell me when she\'s back, or I\'ll hang around till she is."',
    '"Tell me now, or I\'ll call it a night."',
])
def test_an_ultimatum_without_harm_a_plan_or_a_walk_is_not_risky(text):
    assert roll_intent.risky_act(text) is None, text
