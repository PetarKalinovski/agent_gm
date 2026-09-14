"""The opening waits a moment for a campaign that is still being authored."""

from src.models import Campaign, Player, get_session
from src.services import opening


def _player(session):
    p = Player(name="Wren")
    session.add(p)
    session.commit()
    return p.id


def test_no_campaign_row_returns_immediately(db):
    with get_session() as session:
        player_id = _player(session)
    calls = []
    assert opening.wait_for_campaign(player_id, timeout=10, sleep=lambda s: calls.append(s)) is None
    assert calls == []


def test_active_campaign_returns_immediately(db):
    with get_session() as session:
        player_id = _player(session)
        session.add(Campaign(player_id=player_id, title="Done", status="active"))
        session.commit()
    calls = []
    assert opening.wait_for_campaign(player_id, timeout=10, sleep=lambda s: calls.append(s)) == "active"
    assert calls == []


def test_pending_campaign_is_polled_until_it_flips(db):
    with get_session() as session:
        player_id = _player(session)
        row = Campaign(player_id=player_id, title="", status="pending")
        session.add(row)
        session.commit()
        campaign_id = row.id
    polls = []

    def fake_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            with get_session() as session:
                session.get(Campaign, campaign_id).status = "active"
                session.commit()

    assert opening.wait_for_campaign(player_id, timeout=60, poll=0.01, sleep=fake_sleep) == "active"
    assert len(polls) == 2


def test_pending_campaign_gives_up_at_the_deadline(db):
    with get_session() as session:
        player_id = _player(session)
        session.add(Campaign(player_id=player_id, title="", status="pending"))
        session.commit()
    polls = []
    assert opening.wait_for_campaign(player_id, timeout=0.0, poll=0.01, sleep=lambda s: polls.append(s)) == "pending"
    assert polls == []
