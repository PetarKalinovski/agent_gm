"""The party — companions who travel with the player and fight beside them.

`Player.party_members` (a JSON list of NPC ids) was a column nobody moved.
Now the ENGINE moves it:

  * joining and leaving go through two tools the DM and the NPC agent share,
    gated here in code (alive, standing here, room in the party, and
    `standing.join_refusal` — hostile never follows, warm always does,
    everyone else needs JOIN_TRUST);
  * `follow` moves every member with the player inside move_player's own
    transaction and `settle` stands them beside the arrival door, so a
    companion is never a move_npc the DM has to remember;
  * the simulator refuses to move a member, the DM's move_npc takes them
    out of the party, and `sync` drops anyone a campaign op killed or moved
    — with a line the DM reads that same turn;
  * `ally_roster` puts the fit members into every start_combat; `wound`
    marks the ones who went down (never dead — that is the DM's call in the
    fiction), `heal` clears it when the party rests.

Every threshold lives in `standing.py`. This module never quotes one.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Event, NPC, Player, WorldClock, get_session
from src.services import standing

logger = logging.getLogger(__name__)

MAX_PARTY = 3                 # companions, not counting the player
WOUNDED = "wounded"           # the one NPC.condition the engine writes
# Where each companion stands relative to the player on arrival (normalized
# scene units): flanking a step behind, the third at their back
FOLLOW_OFFSETS = ((-6.0, 2.5), (6.0, 2.5), (0.0, 6.0))


# ----------------------------------------------------------------------
# Membership
# ----------------------------------------------------------------------

def member_ids(player) -> list[str]:
    """The ids on the row, de-duplicated, in join order. Never raises."""
    raw = getattr(player, "party_members", None) or []
    out: list[str] = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def members(session, player) -> list[NPC]:
    """The rows behind the ids that still resolve and are alive, in order.

    Read-only: the dead and the vanished are hidden here and DROPPED by
    `sync`, which is the only thing that rewrites the list on its own.
    """
    rows: list[NPC] = []
    if player is None:
        return rows
    for npc_id in member_ids(player):
        npc = session.get(NPC, npc_id)
        if npc is not None and npc.status == "alive":
            rows.append(npc)
    return rows


def all_member_ids(session) -> set[str]:
    """Every NPC in anyone's party (a world holds a handful of players)."""
    ids: set[str] = set()
    for player in session.query(Player).all():
        ids.update(member_ids(player))
    return ids


def party_of(session, npc_id: str | None) -> Player | None:
    """The player this NPC travels with, if any."""
    if not npc_id:
        return None
    for player in session.query(Player).all():
        if npc_id in member_ids(player):
            return player
    return None


def is_member(session, npc_id: str | None) -> bool:
    return party_of(session, npc_id) is not None


def is_member_of(session, player_id: str | None, npc_id: str | None) -> bool:
    if not player_id or not npc_id:
        return False
    player = session.get(Player, player_id)
    return player is not None and npc_id in member_ids(player)


def is_fit(npc) -> bool:
    """Alive and not wounded: the ones who fight."""
    return npc is not None and npc.status == "alive" and (npc.condition or "") != WOUNDED


def names(session, player) -> list[str]:
    return [npc.name for npc in members(session, player)]


# ----------------------------------------------------------------------
# Join / leave
# ----------------------------------------------------------------------

def can_join(session, player, npc) -> dict[str, Any] | None:
    """Why this NPC cannot join right now, or None when they can.

    Codes: no_npc, no_player, dead, already, not_here, full, taken, and the
    standing refusals (hostile, trust).
    """
    if player is None:
        return {"error": "no_player", "message": "No such player."}
    if npc is None:
        return {"error": "no_npc", "message": "No such NPC."}
    if npc.status != "alive":
        return {"error": "dead", "message": f"{npc.name} is {npc.status} — nobody follows anyone in that state."}
    if npc.id in member_ids(player):
        return {"error": "already", "message": f"{npc.name} is already travelling with them."}
    if npc.current_location_id != player.current_location_id:
        return {"error": "not_here", "message": f"{npc.name} is not here — nobody joins a party from another town."}
    if len(members(session, player)) >= MAX_PARTY:
        return {"error": "full", "message": f"The party is full ({MAX_PARTY} companions). Someone has to leave first."}
    other = party_of(session, npc.id)
    if other is not None and other.id != player.id:
        return {"error": "taken", "message": f"{npc.name} already travels with {other.name}."}
    refusal = standing.join_refusal(npc, standing.relationship_for(session, player.id, npc.id))
    if refusal:
        return refusal
    return None


def join(session, player, npc) -> dict[str, Any]:
    """Add them. Runs inside the caller's transaction; the caller commits."""
    refusal = can_join(session, player, npc)
    if refusal:
        return refusal
    player.party_members = member_ids(player) + [npc.id]      # reassign, never mutate
    logger.info(f"Party: {npc.name} joined {player.name}")
    return {"success": True, "npc_id": npc.id, "npc_name": npc.name, "party": names(session, player)}


def leave(session, player, npc, reason: str = "") -> dict[str, Any]:
    """Remove them where the party stands; records the parting as an Event."""
    if player is None or npc is None:
        return {"error": "no_npc", "message": "No such NPC."}
    ids = member_ids(player)
    if npc.id not in ids:
        return {"error": "not_member", "message": f"{npc.name} is not in the party."}
    player.party_members = [i for i in ids if i != npc.id]
    clock = session.query(WorldClock).first()
    session.add(Event(
        name=f"{npc.name} parts ways with {player.name}",
        description=(f"{npc.name} left the party of {player.name}" + (f": {reason}" if reason else ".")),
        event_type="player",
        occurred_day=clock.day if clock else 1,
        occurred_hour=clock.hour if clock else 8,
        locations_involved=[player.current_location_id] if player.current_location_id else [],
        npcs_involved=[npc.id],
        player_visible=True, player_witnessed=True, narrated_to_player=True,
    ))
    logger.info(f"Party: {npc.name} left {player.name} ({reason or 'no reason given'})")
    return {"success": True, "npc_id": npc.id, "npc_name": npc.name, "party": names(session, player)}


# ----------------------------------------------------------------------
# Movement
# ----------------------------------------------------------------------

def follow(session, player, destination_id: str, origin_id: str | None) -> list[NPC]:
    """Move every member standing with the player to the destination.

    Same transaction as the player's own move (the caller commits). A member
    who is somehow elsewhere is left where they are — `sync` will notice.
    """
    moved: list[NPC] = []
    for npc in members(session, player):
        if origin_id and npc.current_location_id != origin_id:
            continue
        npc.current_location_id = destination_id
        moved.append(npc)
    return moved


def _clamp(value: float) -> float:
    return round(max(2.0, min(98.0, float(value))), 2)


def settle(player_id: str) -> None:
    """Stand each member beside the player (own session; after the player's
    own arrival placement). Never raises — a badly placed companion is not a
    failed move."""
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return
            slot = 0
            for npc in members(session, player):
                if npc.current_location_id != player.current_location_id:
                    continue
                ox, oy = FOLLOW_OFFSETS[slot % len(FOLLOW_OFFSETS)]
                slot += 1
                npc.position_x = _clamp((player.position_x or 50.0) + ox)
                npc.position_y = _clamp((player.position_y or 50.0) + oy)
            session.commit()
    except Exception as e:
        logger.warning(f"party settle failed for {player_id}: {e}")


def sync(session, player) -> list[dict[str, Any]]:
    """Drop members who are dead, gone, or no longer where the player is.

    Returns one record per departure ({"npc_id", "name", "reason"}); the
    caller commits when the list is non-empty.
    """
    if player is None:
        return []
    keep: list[str] = []
    gone: list[dict[str, Any]] = []
    for npc_id in member_ids(player):
        npc = session.get(NPC, npc_id)
        if npc is None:
            gone.append({"npc_id": npc_id, "name": "A companion", "reason": "gone"})
            continue
        if npc.status != "alive":
            gone.append({"npc_id": npc_id, "name": npc.name, "reason": npc.status})
            continue
        if player.current_location_id and npc.current_location_id != player.current_location_id:
            gone.append({"npc_id": npc_id, "name": npc.name, "reason": "elsewhere"})
            continue
        keep.append(npc_id)
    if gone:
        player.party_members = keep
        logger.info(f"Party sync for {player.name}: dropped {[g['name'] for g in gone]}")
    return gone


_GONE_PHRASES = {
    "dead": "is dead",
    "missing": "has gone missing",
    "imprisoned": "has been taken",
    "elsewhere": "is no longer at their side — they went their own way",
    "gone": "is gone",
}


def departure_lines(gone: list[dict[str, Any]]) -> list[str]:
    return [f"{g['name']} {_GONE_PHRASES.get(g['reason'], 'is gone')} and is no longer in the party." for g in gone]


# ----------------------------------------------------------------------
# Wounds
# ----------------------------------------------------------------------

def wound(session, npc_ids: list[str] | None) -> list[str]:
    """Mark companions who went down. Returns their names. Caller commits."""
    wounded: list[str] = []
    for npc_id in npc_ids or []:
        npc = session.get(NPC, npc_id)
        if npc is None or npc.status != "alive":
            continue
        if (npc.condition or "") != WOUNDED:
            npc.condition = WOUNDED
        wounded.append(npc.name)
    return wounded


def heal(session, player) -> list[str]:
    """A night's rest puts the wounded back on their feet. Caller commits."""
    healed: list[str] = []
    for npc in members(session, player):
        if (npc.condition or "") == WOUNDED:
            npc.condition = None
            healed.append(npc.name)
    return healed


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------

def party_payload(session, player) -> list[dict[str, Any]]:
    """What the HUD shows (state.party). Pure reads."""
    if player is None:
        return []
    rows = []
    for npc in members(session, player):
        rows.append({
            "id": npc.id,
            "name": npc.name,
            "profession": npc.profession or "",
            "tier": npc.tier.value if hasattr(npc.tier, "value") else str(npc.tier),
            "condition": npc.condition,
            "here": npc.current_location_id == player.current_location_id,
        })
    return rows


def context_line(session, player) -> str:
    """'Piper (wounded), Maera' — or '' when they travel alone."""
    parts = []
    for npc in members(session, player):
        label = npc.name
        if (npc.condition or "") == WOUNDED:
            label += " (wounded — will not fight until they rest)"
        parts.append(label)
    return ", ".join(parts)


def ally_roster(session, player) -> list[dict[str, Any]]:
    """The fit members standing with the player, resolved for the canvas.

    Same tables as the enemies (`combat_params`): a companion fights at
    their OWN tier, never scaled to the player.
    """
    from src.services.combat import combat_params, infer_archetype, resolve_danger_tier

    allies = []
    for npc in members(session, player):
        if not is_fit(npc) or npc.current_location_id != player.current_location_id:
            continue
        archetype = infer_archetype(npc)
        tier = resolve_danger_tier(npc)
        if tier == "boss":
            tier = "deadly"       # a boss never fights FOR the player at boss strength
        allies.append({
            "npc_id": npc.id,
            "name": npc.name,
            "archetype": archetype,
            "tier": tier,
            "params": combat_params(tier, archetype),
        })
    return allies
