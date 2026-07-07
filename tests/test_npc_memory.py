"""NPC memory: the relationship row is the durable record of every exchange."""

from src.models import NPC, NPCRelationship, WorldClock, get_session, init_db, reset_engine
from src.tools.agents_as_tools import _record_npc_exchange
from src.tools.world_write.npcs import update_npc_relationship


def _make_npc(name="Barkeep"):
    with get_session() as session:
        npc = NPC(name=name)
        session.add(npc)
        session.add(WorldClock(day=3, hour=14))
        session.commit()
        return npc.id


def _get_rel(npc_id, player_id):
    with get_session() as session:
        return session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id,
        ).first()


def test_exchange_is_recorded_and_survives_engine_restart(player, tmp_path):
    npc_id = _make_npc()
    _record_npc_exchange(npc_id, player, "Any news?", "The docks burned last night.",
                         is_first_interaction=True)

    # Simulate a full process restart: drop the engine, re-init the same DB
    reset_engine()
    init_db(tmp_path / "test.db")

    rel = _get_rel(npc_id, player)
    assert rel is not None
    contents = [m["content"] for m in rel.recent_messages]
    assert "Any news?" in contents
    assert "The docks burned last night." in contents
    assert rel.last_interaction_day == 3
    assert any("First met on Day 3" in m for m in rel.key_moments)


def test_conversation_end_records_key_moment(player):
    npc_id = _make_npc()
    _record_npc_exchange(npc_id, player, "I'll be back.", "Safe travels.",
                         conversation_ended=True)

    rel = _get_rel(npc_id, player)
    assert any("Talked on Day 3" in m for m in rel.key_moments)


def test_overflow_compresses_into_summary_instead_of_vanishing(player):
    npc_id = _make_npc()
    for i in range(25):
        _record_npc_exchange(npc_id, player, f"question {i}", f"answer {i}")

    rel = _get_rel(npc_id, player)
    # Recent window stays bounded
    assert len(rel.recent_messages) <= 20
    # The newest exchanges are in the window...
    assert any("answer 24" in m["content"] for m in rel.recent_messages)
    # ...and the oldest were folded into the summary, not dropped
    assert "question 0" in (rel.summary or "")


def test_update_npc_relationship_key_moments_actually_persist(player):
    """Regression: in-place list mutation made SQLAlchemy skip the UPDATE."""
    npc_id = _make_npc()
    update_npc_relationship(npc_id, player, add_key_moment="Saved my life")
    update_npc_relationship(npc_id, player, add_key_moment="Betrayed me at the docks")

    rel = _get_rel(npc_id, player)
    assert rel.key_moments == ["Saved my life", "Betrayed me at the docks"]
