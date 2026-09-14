"""The Lead — the one thing the player should do next, chosen by code.

The game had a spine (campaign beats fire on their own) but no compass:
nothing on the screen ever said "go here" or "talk to them". The lead is
that compass. It is derived, never authored at runtime:

  1. the first undone objective of the most recently activated quest
     (`Player.active_quests` is append-on-activate, so the last id is the
     newest — see `_sync_player_quest_lists`);
  2. else the player-facing `lead` on the earliest armed (or stalled)
     campaign beat — the spoiler-safe half of a beat the DM has not
     delivered yet;
  3. else nothing, and the DM is told so in plain words.

`state.lead` rides the state event; the tracker under the location chip
shows its text, the canvas marks its target, the chips offer it first, and
`format_lead_context` puts the same sentence in front of the DM so prose and
screen never disagree.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from src.models import Connection, Location, NPC, Quest, QuestStatus
from src.services import objectives as objectives_mod
from src.services.travel import check_route

logger = logging.getLogger(__name__)

NO_LEAD_CONTEXT = (
    "[PLAYER'S LEAD: none. The player has NOTHING to do and can see that on their "
    "screen. Fix it THIS TURN: offer work through someone present, activate an "
    "authored quest (get_available_quests_for_npc), or give them a reason to go "
    "somewhere — a quest with one objective that names a real npc or location id.]"
)


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

def compute_lead(session, player) -> dict[str, Any] | None:
    """The player's current lead, or None."""
    if player is None:
        return None
    try:
        lead = _quest_lead(session, player)
        if lead is None:
            lead = _beat_lead(session, player)
        return lead
    except Exception as e:
        logger.warning(f"lead computation failed: {e}")
        return None


def _quest_lead(session, player) -> dict[str, Any] | None:
    for quest in objectives_mod.active_quests_for(session, player):
        objectives = objectives_mod.normalize_objectives(quest.objectives)
        current = objectives_mod.first_undone(objectives)
        if current is None:
            continue
        lead = {
            "text": current["text"],
            "kind": current["kind"],
            "source": "quest",
            "quest_id": quest.id,
            "quest_title": quest.title,
            "objective_id": current["id"],
            "item_name": current.get("item_name"),
        }
        lead.update(_target_fields(session, player, current.get("target_type"), current.get("target_id")))
        return lead
    return None


def _beat_lead(session, player) -> dict[str, Any] | None:
    from src.services.campaign import _campaign_beats, get_campaign_row

    campaign = get_campaign_row(session, player.id)
    if campaign is None or campaign.status != "active":
        return None
    candidates = [
        b for b in _campaign_beats(session, campaign)
        if b.status in ("armed", "stalled") and (getattr(b, "lead", None) or "").strip()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda b: (b.act, b.sequence))
    beat = candidates[0]
    lead = {
        "text": beat.lead.strip(),
        "kind": "story",
        "source": "beat",
        "beat_id": beat.id,
        "quest_id": None,
        "quest_title": campaign.title,
        "objective_id": None,
        "item_name": None,
    }
    target = getattr(beat, "lead_target", None) or {}
    lead.update(_target_fields(session, player, target.get("type"), target.get("id")))
    return lead


def _target_fields(session, player, target_type: str | None, target_id: str | None) -> dict[str, Any]:
    """`target` + `route_next_hop_location_id` for a resolved npc/location."""
    fields: dict[str, Any] = {"target": None, "route_next_hop_location_id": None}
    if not target_type or not target_id:
        return fields
    here_id = player.current_location_id

    if target_type == "npc":
        npc = session.get(NPC, target_id)
        if npc is None or npc.status == "dead":
            return fields
        location = session.get(Location, npc.current_location_id) if npc.current_location_id else None
        # An NPC standing somewhere the player has never found is a rumour,
        # not a waypoint: name them, point nowhere
        known = bool(location and location.discovered)
        fields["target"] = {
            "type": "npc",
            "id": npc.id,
            "name": npc.name,
            "location_id": location.id if known else None,
            "location_name": location.name if known else None,
            "here": bool(location and location.id == here_id),
        }
        if known and location.id != here_id:
            fields["route_next_hop_location_id"] = next_hop(session, player, location.id)
        return fields

    if target_type == "location":
        location = session.get(Location, target_id)
        if location is None:
            return fields
        fields["target"] = {
            "type": "location",
            "id": location.id,
            "name": location.name,
            "location_id": location.id,
            "location_name": location.name,
            "here": location.id == here_id,
        }
        if location.id != here_id:
            fields["route_next_hop_location_id"] = next_hop(session, player, location.id)
        return fields

    return fields


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------

def _adjacency(session) -> dict[str, set[str]]:
    """Discovered roads (both ways where bidirectional) plus the hierarchy:
    a discovered child is reachable from its parent and vice versa — the
    same edges `get_available_destinations` offers the canvas."""
    adjacency: dict[str, set[str]] = {}

    def add(u: str, v: str) -> None:
        if u and v and u != v:
            adjacency.setdefault(u, set()).add(v)

    for conn in session.query(Connection).filter(Connection.discovered == True).all():  # noqa: E712
        add(conn.from_location_id, conn.to_location_id)
        if conn.bidirectional:
            add(conn.to_location_id, conn.from_location_id)
    for loc in session.query(Location).all():
        if loc.parent_id and loc.discovered:
            add(loc.parent_id, loc.id)
            add(loc.id, loc.parent_id)
    return adjacency


def next_hop(session, player, target_location_id: str | None) -> str | None:
    """The first location on the shortest known route to the target.

    Roads the player cannot walk (requirements, gates, dress codes) are
    pruned — EXCEPT when the locked destination IS the target: a lead that
    points at the far side of a gate should light the gate, not go dark.
    None when the target is here, undiscovered, or unreachable.
    """
    start = getattr(player, "current_location_id", None)
    if not start or not target_location_id or start == target_location_id:
        return None
    target = session.get(Location, target_location_id)
    if target is None or not target.discovered:
        return None

    adjacency = _adjacency(session)
    blocked_cache: dict[tuple[str, str], bool] = {}

    def blocked(u: str, v: str) -> bool:
        key = (u, v)
        if key not in blocked_cache:
            try:
                blocked_cache[key] = bool(check_route(session, u, v, player).get("blocked"))
            except Exception as e:
                logger.debug(f"route check failed for {u}->{v}: {e}")
                blocked_cache[key] = True
        return blocked_cache[key]

    previous: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        u = queue.popleft()
        for v in sorted(adjacency.get(u, ())):
            if v in previous:
                continue
            if v != target_location_id and blocked(u, v):
                continue
            previous[v] = u
            if v == target_location_id:
                # Walk back to the hop right after `start`
                node = v
                while previous[node] != start:
                    node = previous[node]
                    if node is None:
                        return None
                return node
            queue.append(v)
    return None


# ----------------------------------------------------------------------
# DM context
# ----------------------------------------------------------------------

def format_lead_context(lead: dict[str, Any] | None) -> str:
    """The block the DM reads. Empty lead → an order to create one."""
    if not lead:
        return NO_LEAD_CONTEXT
    lines = ["[PLAYER'S LEAD — this exact line is on the player's screen right now]"]
    origin = f'  "{lead.get("text", "")}"'
    if lead.get("source") == "quest" and lead.get("quest_title"):
        origin += f"  (quest: {lead['quest_title']})"
    elif lead.get("source") == "beat":
        origin += "  (from the story spine)"
    lines.append(origin)

    target = lead.get("target") or {}
    if target:
        name = target.get("name") or "the target"
        if target.get("here"):
            where = target.get("location_name")
            lines.append(f"  {name} is here" + (f" in {where}." if where else "."))
        elif target.get("location_name"):
            hop = lead.get("route_next_hop_location_id")
            if hop and hop != target.get("location_id"):
                lines.append(f"  {name} is at {target['location_name']}; the way there from here is known.")
            else:
                lines.append(f"  {name} is at {target['location_name']}.")
        else:
            lines.append(f"  {name} — the player does not yet know where.")
    lines.append(
        "  Write as if the player knows this is what they are doing. Never contradict it,\n"
        "  never tell them to do something else instead, and if they follow it, let it pay off."
    )
    if lead.get("source") == "beat":
        lines.append(
            "  This line came from the story spine. It is the ONLY thing you know about any\n"
            "  beat that has not fired — infer nothing else from it and reveal nothing more."
        )
    return "\n".join(lines)
