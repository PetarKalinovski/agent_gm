"""The opening turn — a new character's first moment is a story, not a prompt.

Before this, the web client printed "Your story begins at X. What do you do?"
and waited. No scene, no people, no hook; the campaign spine that was
authored at character creation sat unused until the player happened to type.

Now the first entry into the world runs an opening sequence, all in code:

  1. a title card (the campaign's title over its first act, or the world's
     name over its tagline) — the one documented bypass of the cinematic
     rarity ledger, still written to it so later cards throttle against it;
  2. the scene, straight from the Location row (zero model latency);
  3. an auto-sent `[OPENING]` directive the DM answers like any turn — open
     on a person, a pressure, or a problem, and END by handing the player
     their first lead. It is about THIS character (their own background; the
     bible's starting situation only for the bible's suggested PC) and it
     speaks the reply format — speaker lines and @ command lines — never a
     tool name.

`is_first_turn` is the idempotence key: once the DM has a message, a refresh
gets `{"opening": False}` and the client shows history instead.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import (
    DMState, Location, NPC, Player, Quest, QuestStatus, WorldBible, WorldClock, get_session,
)
from src.services.campaign import _act_entry, get_campaign_row, note_opening_offers
from src.services.cinematics import request_cinematic
from src.services.lead import compute_lead
from src.services.objectives import active_quests_for
from src.services.progression import QUEST_XP, quest_scale

logger = logging.getLogger(__name__)

OPENING_TAG = "[OPENING]"

# The opening must end on a hook the player can act on. The DM writes the
# hidden command lines itself (src/services/commands.py), so this names the
# reply format, never a tool.
_HAND_OVER = "END this response by handing them one concrete next thing to do."
# fleet8-wenna T0 (and fleet6/7-bram T0): the Wolf-Tooth Charm was offered as
# "a tooth off the thing that's doing this" — a bounty to fetch. The pay is
# named as written, each item by its own name — in quotes since fleet9-bram
# T0 ("a tooth off one of the brutes", a third round running)
_OFFER_AUTHORED = (
    "No quest is active yet, so someone present offers work — prefer what is already written: "
    "{offers}. Have them offer it, naming what it pays — each item by its own name, as written (say the words in "
    "quotes), a finished thing they hand over (never a trophy the player must cut or bring back) — and write "
    "@quest-start "
    "<title> on its own line in the reply where the player takes it — not in this one: they have not answered "
    "yet (the engine starts it when they say yes or set out)."
)
# The first step must point AWAY from whoever offers the work: they speak in
# this same reply, and speaking ticks a talk_to aimed at them
# (dialogue.record_exchanges) — the quest would arrive already done, no lead.
_OFFER_NEW = (
    "No quest is active yet, so {who} offers work. On its own line write @quest-new <title> | "
    "about: <one line>{giver} | step: go_to <place>. The first step (go_to <place> or "
    "talk_to <name>) is the NEXT thing to do, naming a real person or place from the scene (its "
    "ways out, people, known places), never the one offering the work."
)
_GIVER = " | from: <who offers it>"
_SPEAKER_LINES = 'Every spoken line is a speaker line on its own line: Name (tone): "words".'

# What the server writes on the placeholder player before anyone customises it
_PLACEHOLDER_TEXT = frozenset({"a mysterious figure awaiting their story."})
WHO_CHARS = 700


CAMPAIGN_WAIT_SECONDS = 45.0
CAMPAIGN_POLL_SECONDS = 2.0


def wait_for_campaign(player_id: str, timeout: float = CAMPAIGN_WAIT_SECONDS,
                      poll: float = CAMPAIGN_POLL_SECONDS, sleep=None) -> str | None:
    """Give a campaign that is still being authored a moment to finish.

    The client is buffering sprites for minutes at this point, so a short wait
    costs nothing and turns "Emberfall" into the campaign's own title card.
    Returns the campaign status seen last (None when there is no row).
    """
    import time as _time
    sleep = sleep or _time.sleep
    deadline = _time.monotonic() + max(0.0, timeout)
    status = None
    while True:
        with get_session() as session:
            row = get_campaign_row(session, player_id)
            status = row.status if row is not None else None
        if status != "pending" or _time.monotonic() >= deadline:
            return status
        sleep(poll)


def is_first_turn(dm: Any) -> bool:
    """True when the DM has no conversation with this player yet."""
    try:
        messages = getattr(dm.agent, "messages", None)
    except Exception as e:  # a DM that cannot load its history gets no opening
        logger.debug(f"first-turn check failed: {e}")
        return False
    return not messages


def _card_text(campaign, bible) -> tuple[str | None, str | None]:
    if campaign is not None and (campaign.title or "").strip():
        act = _act_entry(campaign, 1) if campaign.acts else {}
        return campaign.title.strip(), (act.get("title") or "").strip() or None
    if bible is not None and (bible.name or "").strip():
        return bible.name.strip(), (bible.tagline or "").strip() or None
    return None, None


def is_suggested_character(player, bible) -> bool:
    """True when this is the character the world bible was written for.

    `pc_starting_situation` ("You are a courier with one last letter…") is
    authored for the bible's suggested PC. The 2026-10-04 playtest handed it
    to Ilse Varrow, an apothecary, while her campaign was still pending, and
    the DM opened on a courier she is not.
    """
    if bible is None or player is None:
        return False
    suggested = " ".join((bible.pc_suggested_name or "").split()).casefold()
    return bool(suggested) and " ".join((player.name or "").split()).casefold() == suggested


def who_they_are(player) -> str:
    """The player's own words about their character, one clipped line."""
    parts: list[str] = []
    for raw in (getattr(player, "background", None), getattr(player, "description", None)):
        text = " ".join(str(raw or "").split())
        if text and text.casefold() not in _PLACEHOLDER_TEXT and text not in parts:
            parts.append(text)
    who = " ".join(parts)
    return who if len(who) <= WHO_CHARS else who[: WHO_CHARS - 1].rstrip() + "…"


# The opening hands over ONE job: the biggest authored one here. With two
# on offer (round 3's seed added Piper's "Counting the Howls" beside the main
# job, both in the square) "Have them offer it" read as either, and the Lead
# follows whichever starts last
OPENING_OFFERS = 1


def _quests_on_offer(session, location) -> list[tuple[Any, str]]:
    """(quest, giver name) for the authored, unstarted quests a person
    standing here can hand out — the biggest first, at most OPENING_OFFERS."""
    if location is None:
        return []
    givers = {
        n.id: n.name for n in session.query(NPC).filter(
            NPC.current_location_id == location.id, NPC.status == "alive"
        ).all()
    }
    if not givers:
        return []
    rows = session.query(Quest).filter(
        Quest.status == QuestStatus.NOT_STARTED, Quest.assigned_by_npc_id.in_(list(givers))
    ).all()
    rows = sorted((q for q in rows if (q.title or "").strip()),
                  key=lambda q: -QUEST_XP[quest_scale(q)])   # stable: ties keep DB order
    return [(q, givers[q.assigned_by_npc_id]) for q in rows][:OPENING_OFFERS]


def as_the_player_sees(session, player, npc) -> str:
    """How the opening names someone present: their name once the player
    knows it, else their role label with the real name marked unknown.
    Round 6: every session drew an unintroduced_name warning at T0, because
    "Present:" named Captain Brann Coldiron to a character who had never
    heard it, and the DM labelled his lines with it."""
    from src.services.dialogue import display_name

    shown = display_name(session, getattr(player, "id", None), npc)
    if shown == npc.name:
        return npc.name
    return f"{shown} [real name {npc.name}, not yet known to the player]"


def _work_on_offer(session, location, player=None) -> list[str]:
    """Authored, unstarted quests a person standing here can hand out, with
    their pay: fleet5-tamsin T0's job offer named no price, and the thief
    had nothing to bargain over (the quest paid 60 coin and a charm)."""
    from src.tools.world_write.quests import reward_factions, reward_words

    out = []
    for q, giver in _quests_on_offer(session, location):
        if player is not None:
            row = session.get(NPC, q.assigned_by_npc_id)
            giver = as_the_player_sees(session, player, row) if row is not None else giver
        pay = reward_words(q.rewards, factions=reward_factions(session, q.rewards))
        out.append(f'"{q.title}" ({giver})' + (f", which pays {pay}" if pay else ""))
    return out


def _opening_quest_ids(session, player, location) -> list[str]:
    """The first job the opening hands over: the quests already active, or
    else the authored ones it tells the DM to offer (`_hand_over`)."""
    active = active_quests_for(session, player)
    if active:
        return [q.id for q in active]
    return [q.id for q, _giver in _quests_on_offer(session, location)]


def _hand_over(session, player, location, npc_names: list[str]) -> str:
    if active_quests_for(session, player):
        return f"{_HAND_OVER} {_SPEAKER_LINES}"
    offers = _work_on_offer(session, location, player)
    if offers:
        offer = _OFFER_AUTHORED.format(offers="; ".join(offers))
    else:
        offer = _OFFER_NEW.format(
            who="someone present" if npc_names else "someone arriving or something posted here",
            giver=_GIVER if npc_names else "",
        )
    return f"{_HAND_OVER} {offer} {_SPEAKER_LINES}"


def _lost_in(session, text: str) -> str:
    """One sentence per lost person (dead or missing) the opening's story
    text speaks of — by their name, or by their trade ("a carter dead on
    the north road") — naming them as the world has them. "" when it
    speaks of none. Never raises."""
    import re

    from src.services.dialogue import is_beast

    out: list[str] = []
    try:
        for npc in session.query(NPC).filter(NPC.status.in_(("dead", "missing"))).order_by(NPC.name).all():
            if not npc.name or is_beast(npc):
                continue
            trades = [w for w in re.findall(r"[a-z]{4,}", str(npc.profession or "").lower())]
            if npc.name not in text and not any(re.search(rf"\b{re.escape(t)}s?\b", text, re.IGNORECASE)
                                                for t in trades):
                continue
            when = " ".join(str(npc.description_physical or "").split()).rstrip(".")
            trade = str(npc.profession or "person").strip().lower()
            out.append(f"The {trade} this speaks of is {npc.name} — {npc.status}"
                       + (f" ({when})" if when else "")
                       + f": {npc.name}, {npc.status}, by that name; never a survivor, never someone "
                       "who came back, never anyone in an event still to come (the player has not heard "
                       "the name yet).")
    except Exception as e:
        logger.debug(f"opening's lost people skipped: {e}")
        return ""
    return " ".join(out)


def build_opening_directive(session, player, campaign, location, bible, dm_state,
                            npc_names: list[str], time_of_day: str) -> str:
    """The `[OPENING]` message the DM answers. Reads the spine's premise and
    the player's lead; never a beat directive (those stay sealed). Names the
    reply format (speaker lines, @ command lines), never a tool."""
    where = location.name if location is not None else "where they stand"
    # "you", said here too: fleet9-bram's opening went third person and the
    # whole session stayed there
    lines = [
        f"{OPENING_TAG} This is {player.name}'s first moment in the world. The player has "
        f"ALREADY been shown {where} ({time_of_day}) — a title card and the scene description are "
        "on their screen, so do not re-describe the place. Write to them as \"you\" — never their name, "
        "never he or she.",
    ]
    who = who_they_are(player)
    if who:
        lines.append(f"Who they are: {who}")
    if npc_names:
        lines.append(f"Present: {', '.join(npc_names[:6])}.")

    story_at = len(lines)
    if campaign is not None:
        act = _act_entry(campaign, 1) if campaign.acts else {}
        story = [f'The story you are delivering: "{campaign.title}".']
        if campaign.premise:
            story.append(campaign.premise.strip())
        if campaign.stakes_personal:
            story.append(f"Why it is {player.name}'s: {campaign.stakes_personal.strip()}")
        if act.get("title") or act.get("premise"):
            story.append(f"Act I — {act.get('title') or ''}: {act.get('premise') or ''}".strip())
        lines.append(" ".join(s for s in story if s))
    else:
        world = []
        if bible is not None and bible.current_situation:
            world.append(f"The world right now: {bible.current_situation.strip()}")
        if bible is not None and bible.pc_starting_situation and is_suggested_character(player, bible):
            world.append(f"Their starting situation: {bible.pc_starting_situation.strip()}")
        if dm_state is not None and dm_state.current_arc:
            world.append(f"Your arc: {dm_state.current_arc.strip()}")
        if world:
            lines.append(" ".join(world))
    # The arc's dead by name, in the arc line itself (fleet9-tamsin T2: "a
    # carter dead on the north road three weeks ago" became "Driver
    # survived… hasn't said three words straight since")
    named = _lost_in(session, lines[-1]) if len(lines) > story_at else ""
    if named:
        lines[-1] = f"{lines[-1]} {named}"

    lead = compute_lead(session, player)
    if lead and lead.get("text"):
        lines.append(
            f'Their lead, already on their screen: "{lead["text"]}" (their screen shows people '
            "only by as much of their name as has been said aloud — a role until then). Make "
            "the opening the moment that line starts to matter."
        )

    lines.append(
        "Open on a person, a pressure, or a problem — someone present wants something, or "
        "something here is wrong. A few paragraphs, second person, present tense. "
        + _hand_over(session, player, location, npc_names)
    )
    return "\n".join(lines)


def opening_payload(player_id: str) -> dict[str, Any] | None:
    """Everything the client plays before the DM's first words."""
    if not player_id:
        return None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return None
        location = session.get(Location, player.current_location_id) if player.current_location_id else None
        clock = session.query(WorldClock).first()
        time_of_day = clock.get_time_of_day() if clock else "day"
        bible = session.query(WorldBible).first()
        dm_state = session.query(DMState).first()
        campaign = get_campaign_row(session, player_id)
        if campaign is not None and campaign.status != "active":
            campaign = None
        npc_names: list[str] = []
        scene = None
        if location is not None:
            from src.services.dialogue import display_name

            from src.services.campaign import offstage_ids

            hidden = offstage_ids(session, player.id)
            present = [n for n in session.query(NPC).filter(
                NPC.current_location_id == location.id, NPC.status == "alive"
            ).all() if n.name and n.id not in hidden]
            # The DM reads who is here as the player knows them; the
            # player's own screen gets the labels alone
            npc_names = [as_the_player_sees(session, player, n) for n in present]
            scene = {
                "location": location.name,
                "description": location.description or "",
                "time_of_day": time_of_day,
                "atmosphere": [],
                "npcs": [display_name(session, player.id, n) for n in present],
            }
        title, subtitle = _card_text(campaign, bible)
        directive = build_opening_directive(
            session, player, campaign, location, bible, dm_state, npc_names, time_of_day
        )
        first_job = _opening_quest_ids(session, player, location) if campaign is not None else []

    # The opening hands over the first job, so a beat whose job was offering
    # it is already told (fleet4-tamsin: "Coldiron Wants Eyes He Can Spare"
    # waited all session to offer the quest the opening had started, and
    # completed at T11). After the directive is built: its lead still reads
    # the beat this scene is delivering.
    if first_job:
        delivered = note_opening_offers(player_id, first_job)
        if delivered:
            logger.info(f"opening delivers the beat(s) offering the first job: {delivered}")

    cinematic = None
    if title:
        try:
            result = request_cinematic(
                "title_card", player_id=player_id, title=title, subtitle=subtitle,
                source="opening", dm_callable_only=False,
            )
            if result.get("ok"):
                cinematic = result["payload"]
            else:
                logger.info(f"opening title card refused: {result.get('reason')}")
        except Exception as e:
            logger.warning(f"opening title card failed: {e}")

    return {"opening": True, "cinematic": cinematic, "scene": scene, "dm_message": directive}
