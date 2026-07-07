"""Quests must sync to the player and apply rewards on completion."""

from src.models import Player, get_session
from src.tools.world_write.quests import activate_quest, create_quest, update_quest_status


def _player_state(player_id):
    with get_session() as session:
        p = session.get(Player, player_id)
        return {
            "active": list(p.active_quests or []),
            "completed": list(p.completed_quests or []),
            "currency": p.currency,
            "inventory": list(p.inventory or []),
            "reputation": dict(p.reputation or {}),
        }


def test_activate_quest_syncs_player_list(player):
    quest = create_quest("Find the Ring", "Find it", ["Search the caves"])
    activate_quest(quest["id"])

    state = _player_state(player)
    assert quest["id"] in state["active"]


def test_completion_applies_rewards_and_moves_lists(player):
    quest = create_quest(
        "Slay the Rat",
        "A big rat",
        ["Kill the rat"],
        rewards={"currency": 50, "items": ["Rat Tail"], "reputation": {"guild": 10}},
        start_active=True,
    )
    before = _player_state(player)
    assert quest["id"] in before["active"]

    result = update_quest_status(quest["id"], "completed")
    assert result["status"] == "completed"
    assert result.get("rewards_applied"), "rewards must actually be applied"

    after = _player_state(player)
    assert quest["id"] not in after["active"]
    assert quest["id"] in after["completed"]
    assert after["currency"] == before["currency"] + 50
    assert any(i.get("name") == "Rat Tail" for i in after["inventory"])
    assert after["reputation"].get("guild") == 60  # 50 default + 10


def test_failed_quest_leaves_no_lists(player):
    quest = create_quest("Doomed Task", "Won't happen", ["Impossible"], start_active=True)
    update_quest_status(quest["id"], "failed")

    state = _player_state(player)
    assert quest["id"] not in state["active"]
    assert quest["id"] not in state["completed"]
