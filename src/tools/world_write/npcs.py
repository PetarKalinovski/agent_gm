"""Tools for NPC write operations."""

from typing import Any

from strands import tool

from src.models import (
    NPC,
    NPCRelationship,
    NPCTier,
    WorldClock,
    get_session,
)
from src.services.combat import ARCHETYPES, DANGER_TIERS


def _clean_label(value: str | None, menu: tuple[str, ...]) -> str | None:
    """A label from a fixed menu, or None. Never a coerced near-miss."""
    if value is None:
        return None
    candidate = str(value).strip().lower()
    return candidate if candidate in menu else None


@tool
def add_npc(
    name: str,
    tier: NPCTier,
    species: str | None,
    age: int | None,
    profession: str | None,
    faction_id: str | None,
    current_location_id: str | None,
    home_location_id: str | None,
    description_physical: str | None,
    description_personality: str | None,
    voice_pattern: str | None,
    goals: list[str] | None,
    secrets: list[str] | None,
    current_mood: str | None,
    combat_archetype: str | None = None,
    danger_tier: str | None = None,
) -> dict[str, Any]:
    """Add a new NPC to the world.

    Args:
        name: Name of the NPC.
        tier: Tier of the NPC (e.g., main, secondary, minor).
        species: Species of the NPC.
        age: Age of the NPC.
        profession: Profession of the NPC.
        faction_id: ID of the faction the NPC belongs to.
        current_location_id: Current location ID of the NPC.
        home_location_id: Home location ID of the NPC.
        description_physical: Physical description of the NPC.
        description_personality: Personality description of the NPC.
        voice_pattern: Description of the NPC's voice pattern.
        goals: List of the NPC's goals.
        secrets: List of secrets the NPC holds.
        current_mood: Current mood of the NPC.
        combat_archetype: How they FIGHT, if it ever comes to violence. One of:
            "brute" (hulking, armoured, slow heavy swings),
            "skirmisher" (fast, light, relentless),
            "stalker" (circles and waits, then commits — predators, duellists),
            "ranged" (keeps distance, attacks the ground you're standing on —
            archers, casters, spitters). Leave None to auto-infer.
        danger_tier: How DANGEROUS they are, ABSOLUTELY — never relative to the
            player. This is a permanent fact about the character. One of:
            "harmless"  — cannot hurt anyone: beggars, children, the bedridden,
                          drunks, terrified civilians. They flee, they do no damage.
            "weak"      — cutpurses, starving strays, green conscripts.
            "standard"  — a competent armed adult: town guard, bandit, war hound.
            "dangerous" — veterans and monsters: a captain, an ogre, an assassin.
            "deadly"    — the best in the world or worse than human: a champion,
                          a troll, an archmage. Most characters die to these.
            "boss"      — a legend, a set-piece, the thing the whole arc points at.
                          A dragon. A god. Use this at most a handful of times per
                          world; it triggers an intro cinematic and a named health
                          bar. Leave None to auto-infer (never infers "boss").

    Returns:
        Dictionary with the created NPC's details.
    """
    # An unknown label is dropped, not coerced: a NULL tier infers from the
    # description, which is a better answer than a word nobody wrote.
    archetype_label = _clean_label(combat_archetype, ARCHETYPES)
    tier_label = _clean_label(danger_tier, DANGER_TIERS)
    warnings = []
    if combat_archetype and not archetype_label:
        warnings.append(f"combat_archetype {combat_archetype!r} ignored — use one of {list(ARCHETYPES)}")
    if danger_tier and not tier_label:
        warnings.append(f"danger_tier {danger_tier!r} ignored — use one of {list(DANGER_TIERS)}")

    with get_session() as session:
        npc = NPC(
            name=name,
            tier=tier,
            species=species,
            age=age,
            profession=profession,
            faction_id=faction_id,
            current_location_id=current_location_id,
            home_location_id=home_location_id,
            description_physical=description_physical,
            description_personality=description_personality,
            voice_pattern=voice_pattern,
            goals=goals,
            secrets=secrets,
            current_mood=current_mood,
            combat_archetype=archetype_label,
            danger_tier=tier_label,
        )
        session.add(npc)
        session.commit()

        result = {
            "id": npc.id,
            "name": npc.name,
            "profession": npc.profession,
            "current_location_id": npc.current_location_id,
            "danger_tier": npc.danger_tier,
            "combat_archetype": npc.combat_archetype,
        }
        if warnings:
            result["warnings"] = warnings
        return result


@tool
def update_npc(
    npc_id: str,
    description_physical: str | None = None,
    description_personality: str | None = None,
    voice_pattern: str | None = None,
    profession: str | None = None,
    status: str | None = None,
    add_goal: str | None = None,
    remove_goal: str | None = None,
    add_secret: str | None = None,
    remove_secret: str | None = None,
    add_skill: str | None = None,
    add_notable_item: str | None = None,
    danger_tier: str | None = None,
    combat_archetype: str | None = None,

) -> dict[str, Any]:
    """Update an NPC's attributes, goals, or secrets.

    Use this when an NPC changes physically, gains new goals, or evolves their character.
    NPCs can call this on themselves to reflect changes from events or interactions.

    Args:
        npc_id: The NPC's ID.
        description_physical: New physical description (e.g., "now bears a scar across their left cheek").
        description_personality: New personality description.
        voice_pattern: New speech pattern description.
        profession: New profession.
        status: New status (alive, dead, missing, imprisoned).
        add_goal: Add a new goal to the NPC's goals list.
        remove_goal: Remove a goal from the NPC's goals list (exact match).
        add_secret: Add a new secret to the NPC's secrets list.
        remove_secret: Remove a secret from the NPC's secrets list (exact match).
        add_skill: Add a new skill to the NPC's skills list.
        add_notable_item: Add a notable item to the NPC's inventory.
        danger_tier: How dangerous this character is in a real fight, stated
            ABSOLUTELY — never relative to the player. One of "harmless",
            "weak", "standard", "dangerous", "deadly". Set it when you learn
            something permanent about them (the "farmhand" turns out to be a
            retired duellist). This is a fact about the character, not a
            difficulty dial for the next fight.
        combat_archetype: How they fight: "brute", "skirmisher", "stalker" or
            "ranged".
    Returns:
        Dictionary with updated NPC details.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        # Fight labels are validated BEFORE anything is written, so a bad word
        # never leaves a half-applied update behind.
        if danger_tier is not None:
            if str(danger_tier).strip().lower() == "boss":
                return {"error": (
                    "danger_tier 'boss' cannot be set here — a boss is an authored "
                    "set-piece. Create the character through prompt_creator_agent with "
                    "danger_tier='boss', or set it in the world editor."
                )}
            if _clean_label(danger_tier, DANGER_TIERS) is None:
                return {"error": f"danger_tier must be one of {list(DANGER_TIERS)}"}
        if combat_archetype is not None and _clean_label(combat_archetype, ARCHETYPES) is None:
            return {"error": f"combat_archetype must be one of {list(ARCHETYPES)}"}

        if danger_tier is not None:
            npc.danger_tier = _clean_label(danger_tier, DANGER_TIERS)
        if combat_archetype is not None:
            npc.combat_archetype = _clean_label(combat_archetype, ARCHETYPES)

        # Update simple attributes
        if description_physical is not None:
            npc.description_physical = description_physical
        if description_personality is not None:
            npc.description_personality = description_personality
        if voice_pattern is not None:
            npc.voice_pattern = voice_pattern
        if profession is not None:
            npc.profession = profession
        if status is not None:
            npc.status = status

        # Update goals
        if add_goal:
            goals = npc.goals.copy() if npc.goals else []
            if add_goal not in goals:
                goals.append(add_goal)
                npc.goals = goals
        if remove_goal:
            goals = npc.goals.copy() if npc.goals else []
            if remove_goal in goals:
                goals.remove(remove_goal)
                npc.goals = goals

        # Update secrets
        if add_secret:
            secrets = npc.secrets.copy() if npc.secrets else []
            if add_secret not in secrets:
                secrets.append(add_secret)
                npc.secrets = secrets
        if remove_secret:
            secrets = npc.secrets.copy() if npc.secrets else []
            if remove_secret in secrets:
                secrets.remove(remove_secret)
                npc.secrets = secrets

        # Update skills
        if add_skill:
            skills = npc.skills.copy() if npc.skills else []
            if add_skill not in skills:
                skills.append(add_skill)
                npc.skills = skills

        # Update inventory
        if add_notable_item:
            inventory = npc.inventory_notable.copy() if npc.inventory_notable else []
            if add_notable_item not in inventory:
                inventory.append(add_notable_item)
                npc.inventory_notable = inventory

        session.commit()

        return {
            "success": True,
            "npc_id": npc.id,
            "npc_name": npc.name,
            "updated_fields": {
                "physical": description_physical is not None,
                "personality": description_personality is not None,
                "voice": voice_pattern is not None,
                "profession": profession is not None,
                "status": status is not None,
                "danger_tier": npc.danger_tier,
                "combat_archetype": npc.combat_archetype,
                "goals": len(npc.goals),
                "secrets": len(npc.secrets),
                "skills": len(npc.skills),
            }
        }


@tool
def delete_npc(npc_id: str) -> dict[str, Any]:
    """Delete an NPC from the world.

    Args:
        npc_id: ID of the NPC to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        session.delete(npc)
        session.commit()

        return {"success": True, "deleted_npc_id": npc_id}


@tool
def move_npc(npc_id: str, destination_id: str) -> dict[str, Any]:
    """Move an NPC to a new location.

    Use this when an NPC travels with the player or moves on their own.

    Args:
        npc_id: The NPC's ID.
        destination_id: The destination location ID.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        from src.models import Location
        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # A party member sent somewhere the player is not stops being one —
        # the engine moves companions WITH the player, never apart from them
        from src.services import party as party_mod
        left = None
        owner = party_mod.party_of(session, npc.id)
        if owner is not None and destination_id != owner.current_location_id:
            left = party_mod.leave(session, owner, npc, reason="sent elsewhere")

        # Update NPC location
        old_location = npc.current_location_id
        npc.current_location_id = destination_id
        session.commit()

        result = {
            "success": True,
            "npc_name": npc.name,
            "from_location": old_location,
            "to_location": destination.name,
        }
        if left and left.get("success"):
            result["party_left"] = True
            result["note"] = (
                f"{npc.name} was in the player's party; sending them away took them out "
                "of it. They will not follow the player any more unless they join again."
            )
        return result


@tool
def update_npc_mood(npc_id: str, new_mood: str, reason: str = "") -> dict[str, Any]:
    """Update an NPC's current mood.

    Args:
        npc_id: The NPC's ID.
        new_mood: The new mood (e.g., "happy", "angry", "nervous").
        reason: Reason for the mood change.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        npc.current_mood = new_mood
        session.commit()

        return {"success": True, "npc": npc.name, "new_mood": new_mood}


@tool
def update_npc_relationship(
    npc_id: str,
    player_id: str,
    trust_delta: int = 0,
    new_disposition: str | None = None,
    add_key_moment: str | None = None,
    add_message: dict | None = None
) -> dict[str, Any]:
    """Update the relationship between an NPC and player.

    Args:
        npc_id: The NPC's ID.
        player_id: The player's ID.
        trust_delta: Change in trust level (-100 to 100).
        new_disposition: New disposition (e.g., "friendly", "hostile").
        add_key_moment: A key moment to record.
        add_message: A message to add to recent history.

    Returns:
        Dictionary with updated relationship.
    """
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        if not rel:
            # Create new relationship
            rel = NPCRelationship(
                npc_id=npc_id,
                player_id=player_id,
                trust_level=50,
                current_disposition="neutral",
            )
            session.add(rel)

        # Update trust
        rel.trust_level = max(0, min(100, rel.trust_level + trust_delta))

        # Update disposition
        if new_disposition:
            rel.current_disposition = new_disposition

        # Add key moment (copy — mutating the loaded list in place makes
        # SQLAlchemy see old == new and skip the UPDATE)
        if add_key_moment:
            moments = list(rel.key_moments or [])
            moments.append(add_key_moment)
            rel.key_moments = moments

        # Add message
        if add_message:
            messages = list(rel.recent_messages or [])
            messages.append(add_message)
            rel.recent_messages = messages[-20:]

        # Update last interaction
        clock = session.query(WorldClock).first()
        if clock:
            rel.last_interaction_day = clock.day

        session.commit()

        return {
            "trust_level": rel.trust_level,
            "current_disposition": rel.current_disposition,
        }


@tool
def kill_npc(npc_id: str, cause_of_death: str = "unknown") -> dict[str, Any]:
    """Kill an NPC, setting their status to dead.

    Use this when an NPC dies in combat, from an accident, assassination, or any other cause.
    The frontend will play a death animation when this is called.

    Args:
        npc_id: The NPC's ID.
        cause_of_death: Description of how the NPC died.

    Returns:
        Dictionary with death details and a trigger for frontend animation.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        if npc.status == "dead":
            return {"error": f"{npc.name} is already dead"}

        # Set status to dead — and leave what they carried where they fell
        npc.status = "dead"
        from src.services.loot import leave_body
        from src.services.scene_objects import public_row
        body = leave_body(session, npc)
        session.commit()

        result = {
            "success": True,
            "event": "npc_death",  # Special event type for frontend
            "npc_id": npc.id,
            "npc_name": npc.name,
            "cause_of_death": cause_of_death,
            "message": f"{npc.name} has died. Cause: {cause_of_death}",
        }
        if body:
            result["body"] = public_row(body)
            result["instruction"] = (
                "Whatever they carried is on them, where they fell — a body the "
                "player can search on the canvas. Do NOT hand it over with "
                "add_to_inventory, and do not say what is on it."
            )

    # Their death completes any "kill" objective aimed at them
    from src.services import objectives as objectives_mod
    ticked = objectives_mod.note_npc_deaths(None, [npc_id])
    if ticked:
        result["objectives_done"] = objectives_mod.done_lines(ticked)
    return result


@tool
def reveal_secret(npc_id: str, player_id: str, secret_index: int) -> dict[str, Any]:
    """Mark a secret as revealed to the player.

    Args:
        npc_id: The NPC's ID.
        player_id: The player's ID.
        secret_index: Index of the secret in the NPC's secrets list.

    Returns:
        Dictionary with the revealed secret.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        if secret_index >= len(npc.secrets):
            return {"error": "Secret index out of range"}

        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        if not rel:
            rel = NPCRelationship(
                npc_id=npc_id,
                player_id=player_id,
            )
            session.add(rel)

        # The engine decides whether they trust the player enough to say it
        from src.services.standing import secret_refusal
        refused = secret_refusal(npc, rel)
        if refused:
            return refused

        revealed = rel.revealed_secrets.copy() if rel.revealed_secrets else []
        if secret_index not in revealed:
            revealed.append(secret_index)
            rel.revealed_secrets = revealed

        session.commit()

        return {
            "secret": npc.secrets[secret_index],
            "total_secrets": len(npc.secrets),
            "revealed_count": len(revealed),
        }
