"""Death is permanent, fails quests, and the world records it."""

from src.models import Event, Player, Quest, QuestStatus, get_session
from src.tools.world_write.player import update_player_health
from src.tools.world_write.quests import create_quest


def test_death_fails_quests_and_records_event(player):
    quest = create_quest("Save the Docks", "Urgent", ["Defend the docks"], start_active=True)

    result = update_player_health(player, "dead", cause="crushed by a falling crane")

    assert result["player_died"] is True
    assert "Save the Docks" in result["quests_failed"]

    with get_session() as session:
        p = session.get(Player, player)
        assert p.health_status == "dead"
        assert p.active_quests == []

        q = session.get(Quest, quest["id"])
        assert q.status == QuestStatus.FAILED

        death_event = session.query(Event).filter(Event.event_type == "player").first()
        assert death_event is not None
        assert "crushed by a falling crane" in death_event.description
        assert death_event.player_visible is True


def test_nonfatal_health_change_has_no_side_effects(player):
    create_quest("Still Alive", "Fine", ["Breathe"], start_active=True)

    result = update_player_health(player, "critical")
    assert result["success"] is True
    assert "player_died" not in result

    with get_session() as session:
        p = session.get(Player, player)
        assert len(p.active_quests) == 1
