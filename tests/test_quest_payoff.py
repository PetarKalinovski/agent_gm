"""The moment a job pays: the giver's trust moves, the DM can see what it
hands over, and a payout the story never showed is caught (round 4).

fleet5-bram: Coldiron sat at trust 55 after his mission was done, so his
trust-gated clue never came out; T18 paid 60 coin and the prose named only a
charm (described as something else). fleet5-tamsin T9 / fleet5-wenna T12:
"Counting the Howls" paid 10 coin and a Yarrow Tincture on Piper's thanks
alone — the player never learned she carried a healing draught.
"""

from types import SimpleNamespace

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import DMOrchestrator
from src.models import NPC, NPCRelationship, Player, Quest, QuestStatus, WorldClock, get_session
from src.tools.world_write.quests import (
    QUEST_GIVER_TRUST, _complete_quest, reward_coin, reward_words, update_quest_status,
)
from src.web.turn_audit import audit_turn, reward_unnarrated_warning

CHARM = {"name": "Wolf-Tooth Charm", "kind": "trinket", "tier": "common",
         "about": "A wolf's fang bound in brass wire on a Watch cord."}
TINCTURE = {"name": "Yarrow Tincture", "kind": "potion", "about": "Piper's own: green, bitter."}


# --- Pay in words -------------------------------------------------------------

def test_reward_words_say_the_price_plainly():
    # A described item is the player's to keep, said with what it is
    # (fleet7-bram T0/T1: "a tooth for proof" out of a bare "Wolf-Tooth Charm")
    # and by its name in quotes, the words to say (fleet9-bram T0)
    assert reward_words({"currency": 60, "items": [CHARM]}) == (
        '60 coin and a Wolf-Tooth Charm (say "a Wolf-Tooth Charm"; handed over finished, the player\'s to keep — '
        "a wolf's fang bound in brass wire on a Watch cord)")
    assert reward_words({"gold": 10, "items": [TINCTURE, "Old Key"]}) == (
        '10 coin, a Yarrow Tincture (say "a Yarrow Tincture"; handed over finished, the player\'s to keep — '
        "Piper's own: green, bitter) and an Old Key")
    assert reward_words({"gold": 10, "items": ["Old Key"]}) == "10 coin and an Old Key"
    assert reward_words({"currency": 20, "reputation": {"f1": 5}}, factions={"f1": "Lantern Watch"}) == \
        "20 coin and standing with the Lantern Watch"
    assert reward_words({"currency": 20, "reputation": {"f1": 5}}) == \
        "20 coin and standing with the quest-giver's faction"
    assert reward_words({"xp": 500}) == "" and reward_words(None) == "" and reward_coin({"coins": 7}) == 7


def test_the_quest_block_shows_what_each_reward_item_is(monkeypatch):
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [
        {"id": "q1", "title": "Teeth on the North Road", "objectives": [],
         "rewards": {"currency": 60, "items": [CHARM]}}])
    lines = DMOrchestrator._quest_block(SimpleNamespace())
    reward = next(line for line in lines if "Reward" in line)
    assert "60 coin and a Wolf-Tooth Charm (A wolf's fang bound in brass wire on a Watch cord.)" in reward


# --- The giver's trust --------------------------------------------------------

def _job(session, *, trust=55, met=True):
    session.add(WorldClock(day=2, hour=9))
    giver = NPC(name="Captain Brann Coldiron")
    session.add(giver)
    session.flush()
    player = Player(name="Bram Holloway", currency=25)
    session.add(player)
    session.flush()
    if met:
        session.add(NPCRelationship(npc_id=giver.id, player_id=player.id, trust_level=trust,
                                    current_disposition="measuring", last_interaction_day=1))
    quest = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=giver.id,
                  rewards={"currency": 60, "items": [CHARM]})
    session.add(quest)
    session.commit()
    return player.id, giver.id, quest.id


def _rel(player_id, npc_id):
    with get_session() as session:
        return session.query(NPCRelationship).filter_by(player_id=player_id, npc_id=npc_id).first()


def test_finishing_a_job_raises_the_givers_trust(db):
    with get_session() as session:
        player_id, giver_id, quest_id = _job(session)
        outcome = _complete_quest(session, session.get(Quest, quest_id), session.get(Player, player_id))
        session.commit()
    rel = _rel(player_id, giver_id)
    assert rel.trust_level == 55 + QUEST_GIVER_TRUST == 65, "past the secret gate (60) Coldiron sat under"
    assert rel.last_interaction_day == 2 and rel.current_disposition == "measuring"
    assert outcome["giver_trust"] == {"npc_id": giver_id, "trust": 65}
    with get_session() as session:      # a retried completion pays nothing and thanks nobody twice
        again = _complete_quest(session, session.get(Quest, quest_id), session.get(Player, player_id))
        session.commit()
    assert again["already_completed"] and _rel(player_id, giver_id).trust_level == 65


def test_trust_is_capped(db):
    with get_session() as session:
        player_id, giver_id, quest_id = _job(session, trust=96)
    update_quest_status(quest_id=quest_id, status="completed")
    assert _rel(player_id, giver_id).trust_level == 100


def test_a_giver_with_no_row_is_thanked_as_a_stranger(db):
    """fleet6 (all three runs): Coldiron, Piper and Maera paid out with no
    relationship row and the thanks did nothing. The row is the relationship,
    not an introduction: it is written here, the giver's name still unknown
    to the player (the gate keeps their role label)."""
    from src.services.dialogue import player_has_met

    with get_session() as session:
        player_id, giver_id, quest_id = _job(session, met=False)
    update_quest_status(quest_id=quest_id, status="completed")
    assert _rel(player_id, giver_id).trust_level == 50 + QUEST_GIVER_TRUST
    with get_session() as session:
        assert session.get(Player, player_id).currency == 85, "the pay still lands"
        rel = session.query(NPCRelationship).filter_by(player_id=player_id, npc_id=giver_id).one()
        assert rel.name_known is False and rel.knows_player_name is False
        assert not player_has_met(session, player_id, giver_id)


def test_a_trust_line_for_the_same_job_is_not_paid_twice(db):
    """fleet-bram T13, fleet-wenna T8, fleet2-wenna T10, fleet4-bram T10: the
    DM followed @quest-done with its own `@trust Coldiron +10/+15` — with the
    engine's thanks that is +25 for one job. A new mood, or a slight, still lands."""
    from src.services.commands import CommandRunner

    with get_session() as session:
        player_id, giver_id, _quest_id = _job(session)
    runner = CommandRunner(player_id, present_ids=[giver_id])
    assert runner.run("@quest-done Teeth on the North Road").ok
    assert _rel(player_id, giver_id).trust_level == 65
    again = runner.run("@trust Captain Brann Coldiron +15")
    assert not again.ok and "not paid twice" in again.message and f"+{QUEST_GIVER_TRUST}" in again.message
    mood = runner.run("@trust Coldiron +10 | approving")
    assert mood.ok and "not paid twice" in mood.message
    rel = _rel(player_id, giver_id)
    assert rel.trust_level == 65 and rel.current_disposition == "approving"
    assert runner.run("@trust Coldiron -5").ok and _rel(player_id, giver_id).trust_level == 60
    assert any("not paid twice" in line for line in runner.ledger())


def test_trust_for_someone_else_still_lands_after_a_payday(db):
    from src.services.commands import CommandRunner

    with get_session() as session:
        player_id, giver_id, _quest_id = _job(session)
        piper = NPC(name="Piper Quillow")
        session.add(piper)
        session.flush()
        session.add(NPCRelationship(npc_id=piper.id, player_id=player_id, trust_level=50))
        session.commit()
        piper_id = piper.id
    runner = CommandRunner(player_id, present_ids=[giver_id, piper_id])
    assert runner.run("@quest-done Teeth on the North Road").ok
    assert runner.run("@trust Piper +10").ok and _rel(player_id, piper_id).trust_level == 60


# --- reward_unnarrated --------------------------------------------------------

HOWLS = [{"title": "Counting the Howls", "coin": 10, "items": ["Yarrow Tincture"]}]
TEETH = [{"title": "Teeth on the North Road", "coin": 60, "items": ["Wolf-Tooth Charm"]}]


def test_a_payout_on_thanks_alone_names_everything_paid():
    warning = reward_unnarrated_warning(
        "Piper's gratitude still sits warm in your chest as you head for the inn.", HOWLS)
    assert warning["check"] == "reward_unnarrated"
    assert "paid 10 coin and a Yarrow Tincture" in warning["detail"]
    assert "never showed the 10 coin or the Yarrow Tincture" in warning["detail"]
    assert "Never pay it again" in warning["detail"]


def test_the_charm_shown_but_not_the_coin():
    """fleet5-bram T18: 'a charm strung on sinew, a wolf's tooth worn smooth' — no coin."""
    warning = reward_unnarrated_warning(
        "He presses something small and cold into your hand — a charm strung on sinew.", TEETH)
    assert "never showed the 60 coin changing hands" in warning["detail"]


def test_a_shown_payout_is_clean():
    assert reward_unnarrated_warning("He counts sixty coin into your palm and a charm with it.", TEETH) is None
    assert reward_unnarrated_warning(
        "That's owed, and I pay what's owed. He presses a yellowed wolf's tooth on a cord on you.", TEETH) is None
    assert reward_unnarrated_warning("Piper folds a yarrow draught and ten coin into your hand.", HOWLS) is None


def _state(quests, currency):
    return {"quests": quests, "currency": currency, "inventory": []}


def test_the_audit_flags_a_quest_done_reply_that_never_showed_the_pay(db):
    with get_session() as session:
        player_id, _giver, quest_id = _job(session)
    update_quest_status(quest_id=quest_id, status="completed")
    before = _state([{"id": quest_id, "title": "Teeth on the North Road"}], 25)
    after = _state([], 85)
    tools = [{"name": "update_quest_status", "status": "success"}]
    warnings = audit_turn("Coldiron nods once and turns back to the gate.", tools, before, after)
    unshown = [w for w in warnings if w["check"] == "reward_unnarrated"]
    assert unshown and "60 coin and a Wolf-Tooth Charm" in unshown[0]["detail"]
    # Shown in speech, it is clean; without a successful @quest-done there is nothing to check
    shown = audit_turn("He turns back to the gate.", tools, before, after,
                       spoken_text="Sixty coin, as promised, and the charm. Wear it.")
    assert not [w for w in shown if w["check"] == "reward_unnarrated"]
    assert not [w for w in audit_turn("He nods.", [], before, after) if w["check"] == "reward_unnarrated"]


def test_a_failed_quest_pays_nothing_to_show(db):
    with get_session() as session:
        _player_id, _giver, quest_id = _job(session)
    update_quest_status(quest_id=quest_id, status="failed")
    before = _state([{"id": quest_id, "title": "Teeth on the North Road"}], 25)
    warnings = audit_turn("The road closes.", [{"name": "update_quest_status", "status": "success"}],
                          before, _state([], 25))
    assert not [w for w in warnings if w["check"] == "reward_unnarrated"]
