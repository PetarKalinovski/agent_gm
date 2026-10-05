"""The orders `_build_context` adds around the player's words.

fleet4: nine turns with no quest after a payout (tamsin T11-T20), and
too_talky — the audit's most frequent warning — survived its own
[REALITY AUDIT] feedback (bram T19-T20). The no-quest order rides with the
quest list; the reply budget is the last thing before "Player says:".
"""

from types import SimpleNamespace

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import NO_QUEST_ORDER, REPLY_BUDGET, DMOrchestrator


def _fake(monkeypatch, *, quests=None, ticks=None):
    monkeypatch.setattr(dm_orchestrator, "get_player",
                        lambda pid: {"name": "Tamsin Rook", "health_status": "hurt", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])

    def tick(*args, **kwargs):
        if ticks is not None:
            ticks.append((args, kwargs))
        return {}

    monkeypatch.setattr(dm_orchestrator, "run_world_tick", tick)
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE (pre-turn briefing)")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "### CAMPAIGN: spine")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Tamsin Rook")
    fake._player_block = lambda: ["### THE PLAYER", "- Tamsin Rook: hurt, level 2, 84 coin."]
    fake._quest_block = lambda: ["### ACTIVE QUESTS"] + (quests or ["- none"])
    return fake


def test_no_active_quest_orders_work_with_the_quest_list(monkeypatch):
    context = DMOrchestrator._build_context(_fake(monkeypatch), "I ask Maera about the roster.")
    assert NO_QUEST_ORDER in context
    assert context.index("### ACTIVE QUESTS") < context.index("[NO ACTIVE QUEST") < context.index("### WORLD STATE")


def test_an_active_quest_needs_no_order(monkeypatch):
    fake = _fake(monkeypatch, quests=["- The Name on the Roster [q2] — ◦ Talk to Hesk"])
    assert "[NO ACTIVE QUEST" not in DMOrchestrator._build_context(fake, "I find Hesk.")


def test_the_reply_budget_is_the_last_thing_before_the_player(monkeypatch):
    context = DMOrchestrator._build_context(
        _fake(monkeypatch), "I look around", extra_blocks=["[REALITY AUDIT of your previous turn]"],
    )
    assert context.index("[REALITY AUDIT") < context.index("[THIS REPLY:")
    assert context.endswith(f"{REPLY_BUDGET}\n\nPlayer says: I look around")
    assert "at most 3 short speaker lines" in REPLY_BUDGET and "end on a choice" in REPLY_BUDGET


def test_engine_turns_get_no_work_order_and_the_opening_no_budget(monkeypatch):
    opening = DMOrchestrator._build_context(_fake(monkeypatch), "[OPENING] This is Tamsin's first moment.")
    assert "[NO ACTIVE QUEST" not in opening and "[THIS REPLY:" not in opening
    aftermath = DMOrchestrator._build_context(_fake(monkeypatch), "[COMBAT OUTCOME] Victory.")
    assert "[NO ACTIVE QUEST" not in aftermath and "[THIS REPLY:" in aftermath
    assert aftermath.rstrip().endswith("Player says: [COMBAT OUTCOME] Victory.")


def test_an_ambush_reply_gets_neither_and_the_tick_spends_no_attempt(monkeypatch):
    ticks: list = []
    context = DMOrchestrator._build_context(
        _fake(monkeypatch, ticks=ticks), "I ask him his name.",
        extra_blocks=["[AMBUSH — ALREADY HAPPENING.]"], constrained=True,
    )
    assert "[NO ACTIVE QUEST" not in context and "[THIS REPLY:" not in context
    assert ticks == [(("p1",), {"constrained": True, "aftermath": False})]


def test_engine_inputs_are_constrained_for_the_world_tick(monkeypatch):
    ticks: list = []
    fake = _fake(monkeypatch, ticks=ticks)
    DMOrchestrator._build_context(fake, "[COMBAT OUTCOME] Victory.")
    DMOrchestrator._build_context(fake, "I walk to the gate.")
    assert [kwargs["constrained"] for _, kwargs in ticks] == [True, False]


def test_the_real_combat_outcome_input_is_an_engine_turn(monkeypatch):
    """The frontend sends combat.combat_outcome_message — "[COMBAT OUTCOME —
    resolved in real-time …", no closing bracket after the tag. Read as the
    player's words, the aftermath spent the howl's delivery attempt and got
    a work order (fleet4-wenna T10)."""
    from src.services.combat import combat_outcome_message

    ticks: list = []
    message = combat_outcome_message({"outcome": "victory"})
    context = DMOrchestrator._build_context(_fake(monkeypatch, ticks=ticks), message)
    assert [kwargs["constrained"] for _, kwargs in ticks] == [True]
    # ...whose world tick still stages an event at the player's feet
    # (fleet10-bram T17-T18)
    assert [kwargs["aftermath"] for _, kwargs in ticks] == [True]
    assert "[NO ACTIVE QUEST" not in context
    assert context.rstrip().endswith(f"Player says: {message}")
