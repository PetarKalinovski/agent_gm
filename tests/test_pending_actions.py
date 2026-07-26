"""Between-turn ledger: recording, draining, the context block, tool equivalents."""

from src.web import pending_actions
from src.web.pending_actions import (
    MAX_PENDING,
    consume_note,
    drain,
    equip_note,
    format_pending_actions,
    record_action,
    tool_equivalents,
    unequip_note,
)


def _move(player_id, frm, to, minutes=6, first_visit=False):
    record_action(player_id, "move", f"{frm} -> {to}",
                  tools=("move_player", "advance_time"),
                  minutes=minutes, first_visit=first_visit)


# ---------------------------------------------------------------------------
# Record / drain
# ---------------------------------------------------------------------------

def test_record_and_drain_clears():
    _move("p-drain", "Market Square", "Dock Row")
    actions = drain("p-drain")
    assert len(actions) == 1
    assert actions[0]["kind"] == "move"
    assert drain("p-drain") == []


def test_drain_is_per_player():
    _move("p-a", "A", "B")
    _move("p-b", "C", "D")
    assert len(drain("p-a")) == 1
    assert len(drain("p-b")) == 1


def test_record_without_player_id_is_ignored():
    record_action("", "take", "nothing")
    assert drain("") == []


def test_clear_forgets_queue():
    _move("p-clear", "A", "B")
    pending_actions.clear("p-clear")
    assert drain("p-clear") == []


def test_cap_drops_oldest_and_marks_omitted():
    pid = "p-cap"
    for i in range(15):
        record_action(pid, "take", f"trinket {i}", tools=("add_to_inventory",))
    actions = drain(pid)
    real = [a for a in actions if a["kind"] != "omitted"]
    assert len(real) == MAX_PENDING
    assert len(actions) == MAX_PENDING + 1
    assert actions[0]["kind"] == "omitted"
    # The oldest three fell off the front
    assert real[0]["summary"] == "trinket 3"
    block = format_pending_actions(actions)
    assert block.splitlines()[4] == "- (earlier canvas actions omitted)"


# ---------------------------------------------------------------------------
# Context block
# ---------------------------------------------------------------------------

def test_empty_actions_render_nothing():
    assert format_pending_actions([]) == ""


def test_consecutive_moves_collapse():
    pid = "p-moves"
    _move(pid, "Market Square", "Dock Row", minutes=6)
    _move(pid, "Dock Row", "The Salt Tavern", minutes=8)
    _move(pid, "The Salt Tavern", "Cellar", minutes=4)
    block = format_pending_actions(drain(pid))
    assert "- Travelled: Market Square -> Dock Row -> The Salt Tavern -> Cellar (18 min passed)" in block
    assert block.count("Travelled:") == 1


def test_first_visit_adds_describe_hint():
    pid = "p-first"
    _move(pid, "Market Square", "The Salt Tavern", first_visit=True)
    block = format_pending_actions(drain(pid))
    assert "They are now in The Salt Tavern" in block
    assert "Call describe_location for it before anything else.]" in block

    _move(pid, "The Salt Tavern", "Market Square", first_visit=False)
    block = format_pending_actions(drain(pid))
    assert "describe_location" not in block
    assert block.endswith(")]")


def test_object_bullets_use_kind_labels():
    pid = "p-objects"
    record_action(pid, "take", "bundle of oilcloth", tools=("add_to_inventory",))
    record_action(pid, "read", 'notice nailed by the door ("Bounty stands at the pier.")')
    record_action(pid, "open", "fish crate (found salted eels)", tools=("add_to_inventory",))
    record_action(pid, "search", "straw bedding (found nothing but a broken comb)")
    block = format_pending_actions(drain(pid))
    assert "- Picked up: bundle of oilcloth" in block
    assert '- Read: notice nailed by the door ("Bounty stands at the pier.")' in block
    assert "- Opened: fish crate (found salted eels)" in block
    assert "- Searched: straw bedding (found nothing but a broken comb)" in block


def test_block_header_names_tools_without_backticks():
    pid = "p-header"
    _move(pid, "A", "B")
    block = format_pending_actions(drain(pid))
    assert block.startswith("[SINCE YOUR LAST TURN the player acted on the canvas.")
    assert "`" not in block
    assert "move_player, advance_time, or add_to_inventory" in block


def test_combat_entries_carry_tools_but_no_prose():
    pid = "p-combat"
    record_action(pid, "combat", "fight resolved: victory",
                  tools=("update_player_health", "kill_npc"))
    actions = drain(pid)
    # Nothing to say — the [COMBAT OUTCOME] turn already tells that story
    assert format_pending_actions(actions) == ""
    assert tool_equivalents(actions) == {"update_player_health", "kill_npc"}


def test_progression_notes_render_as_sentences():
    pid = "p-progression"
    record_action(pid, "equip", equip_note("Harbor Mail", "armor"))
    record_action(pid, "consume", consume_note("Healing Draught", 2, in_combat=True))
    record_action(pid, "levelup", "")
    block = format_pending_actions(drain(pid))
    assert "- They now wear Harbor Mail." in block
    assert "- They drank a Healing Draught mid-fight, and look steadier (2 left)." in block
    assert "- They have grown from what they survived. Acknowledge it ONCE, obliquely" in block
    assert 'never say "level", "XP", or a number.' in block


def test_note_templates():
    assert equip_note("Iron Sword", "weapon") == "They now wield Iron Sword."
    assert equip_note("Ring of Salt", "trinket") == "They now carry Ring of Salt."
    assert equip_note("Iron Sword", "weapon", automatic=True) == (
        "They immediately wield the Iron Sword they just got."
    )
    assert unequip_note("Iron Sword") == "They put away their Iron Sword."
    assert consume_note("Surge Tonic", 1, effect="surge") == (
        "They drank a Surge Tonic — for the next moments they hit harder (1 left)."
    )


def test_equip_note_carries_the_new_reading_when_it_changed():
    assert equip_note("Ashfall Hood", "armor",
                      reads_as="someone who does not want to be seen") == (
        "They now wear Ashfall Hood. To strangers they now read as someone who "
        "does not want to be seen."
    )
    assert equip_note("Ashfall Hood", "armor", automatic=True,
                      reads_as="someone who does not want to be seen") == (
        "They immediately wear the Ashfall Hood they just got. To strangers they "
        "now read as someone who does not want to be seen."
    )


def test_equip_note_without_a_reading_is_byte_identical():
    """A swap between two plain coats says nothing and must add nothing."""
    for automatic in (False, True):
        plain = equip_note("Harbor Mail", "armor", automatic=automatic)
        assert equip_note("Harbor Mail", "armor", automatic=automatic,
                          reads_as=None) == plain
        assert equip_note("Harbor Mail", "armor", automatic=automatic,
                          reads_as="") == plain


def test_mixed_block_orders_by_record_order():
    pid = "p-mixed"
    _move(pid, "Market Square", "Dock Row", minutes=6, first_visit=True)
    record_action(pid, "take", "coil of wire", tools=("add_to_inventory",))
    record_action(pid, "levelup", "")
    lines = [l for l in format_pending_actions(drain(pid)).splitlines() if l.startswith("- ")]
    assert lines[0].startswith("- Travelled:")
    assert lines[1] == "- Picked up: coil of wire"
    assert lines[2].startswith("- They have grown")


# ---------------------------------------------------------------------------
# Tool equivalents (the audit's suppression feed)
# ---------------------------------------------------------------------------

def test_tool_equivalents():
    pid = "p-equiv"
    _move(pid, "A", "B")
    assert tool_equivalents(drain(pid)) == {"move_player", "advance_time"}

    record_action(pid, "read", "a notice")
    assert tool_equivalents(drain(pid)) == set()

    record_action(pid, "search", "straw bedding", tools=("add_to_inventory",))
    assert tool_equivalents(drain(pid)) == {"add_to_inventory"}

    record_action(pid, "search", "empty barrel")
    assert tool_equivalents(drain(pid)) == set()


def test_tool_equivalents_for_consume_and_growth():
    pid = "p-equiv2"
    record_action(pid, "consume", consume_note("Healing Draught", 1))
    assert tool_equivalents(drain(pid)) == {"use_item", "update_player_health"}

    record_action(pid, "equip", equip_note("Harbor Mail", "armor"))
    record_action(pid, "levelup", "")
    assert tool_equivalents(drain(pid)) == set()


def test_tool_equivalents_of_empty_ledger():
    assert tool_equivalents([]) == set()
