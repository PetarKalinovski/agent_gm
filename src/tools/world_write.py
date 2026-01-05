"""Tools for modifying world state."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Event,
    Faction,
    FactionRelationship,
    HistoricalEvent,
    Location,
    NPC,
    NPCRelationship,
    Player,
    WorldBible,
    WorldClock,
    get_session,
    LocationType,
    NPCTier,
    Item,
)

@tool
def add_location(
    name: str,
    description: str,
    location_type: LocationType,
    parent_id: str | None = None,
    travel_time_to_parent: float | None = None,
) -> dict[str, Any]:
    """Add a new location to the world.

    Args:
        name: Name of the location.
        description: Description of the location.
        location_type: Type of location (e.g., city, building, room).
        parent_id: ID of the parent location (if any).
        travel_time_to_parent: Travel time to parent location in hours (if applicable) (float accepted).

    Returns:
        Dictionary with the created location's details.
    """
    with get_session() as session:
        location = Location(
            name=name,
            description=description,
            type=location_type,
            parent_id=parent_id,
            visited=False,
            discovered=False,
        )
        session.add(location)
        session.commit()

        # Add Connection to parent if applicable
        if parent_id:
            parent = session.get(Location, parent_id)
            if parent:
                conn = Connection(
                    from_location_id=parent_id,
                    to_location_id=location.id,
                    travel_time_hours=travel_time_to_parent,
                    bidirectional=True,
                    travel_type="walk",
                    discovered=True,
                )
                session.add(conn)
                session.commit()

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type,
            "parent_id": location.parent_id,
        }

@tool
def update_location(
    location_id: str,
    name: str | None = None,
    description: str | None = None,
    location_type: LocationType | None = None,
) -> dict[str, Any]:
    """Update an existing location's details.

    Args:
        location_id: ID of the location to update.
        name: New name of the location (if any).
        description: New description of the location (if any).
        location_type: New type of location (if any).

    Returns:
        Dictionary with the updated location's details.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        if name is not None:
            location.name = name
        if description is not None:
            location.description = description
        if location_type is not None:
            location.type = location_type

        session.commit()

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type,
            "description": location.description,
        }

def delete_location(location_id: str) -> dict[str, Any]:
    """Delete a location from the world.

    Args:
        location_id: ID of the location to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        session.delete(location)
        session.commit()

        return {"success": True, "deleted_location_id": location_id}

@tool
def add_location_connection(
    from_location_id: str,
    to_location_id: str,
    travel_type: str,
    travel_time_hours: float,
    bidirectional: bool = True,
) -> dict[str, Any]:
    """Add a connection between two locations.

    Args:
        from_location_id: ID of the starting location.
        to_location_id: ID of the destination location.
        travel_type: Type of travel (e.g., road, stairs).
        travel_time_hours: Travel time in hours.
        bidirectional: Whether the connection is bidirectional.

    Returns:
        Dictionary with the created connection's details.
    """
    with get_session() as session:
        conn = Connection(
            from_location_id=from_location_id,
            to_location_id=to_location_id,
            travel_type=travel_type,
            travel_time_hours=travel_time_hours,
            bidirectional=bidirectional,
            discovered=True,
        )
        session.add(conn)
        session.commit()

        return {
            "id": conn.id,
            "from_location_id": conn.from_location_id,
            "to_location_id": conn.to_location_id,
            "travel_type": conn.travel_type,
            "travel_time_hours": conn.travel_time_hours,
        }

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

    Returns:
        Dictionary with the created NPC's details.
    """
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
        )
        session.add(npc)
        session.commit()

        return {
            "id": npc.id,
            "name": npc.name,
            "profession": npc.profession,
            "current_location_id": npc.current_location_id,
        }


@tool
def create_faction(
    name: str,
    ideology: str,
    methods: list[str] | None = None,
    aesthetic: str | None = None,
    power_level: int = 50,
    resources: dict[str, int] | None = None,
    goals_short: list[str] | None = None,
    goals_long: list[str] | None = None,
    leadership: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    history_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new faction in the world.

    Args:
        name: Name of the faction.
        ideology: Core beliefs and values of the faction.
        methods: List of methods the faction uses (e.g., ["trade", "diplomacy", "espionage"]).
        aesthetic: Visual style description for the faction.
        power_level: Power level from 1-100 (default 50).
        resources: Resource dictionary (e.g., {"military": 50, "economic": 70, "influence": 60}).
        goals_short: List of short-term goals.
        goals_long: List of long-term goals.
        leadership: Leadership info (e.g., {"leader_name": "Emperor Palpatine", "structure_type": "autocracy"}).
        secrets: List of hidden faction truths.
        history_notes: List of historical notes about the faction.

    Returns:
        Dictionary with the created faction's details.
    """
    with get_session() as session:
        faction = Faction(
            name=name,
            ideology=ideology,
            methods=methods or [],
            aesthetic=aesthetic or "",
            power_level=max(1, min(100, power_level)),
            resources=resources or {"military": 50, "economic": 50, "influence": 50},
            goals_short=goals_short or [],
            goals_long=goals_long or [],
            leadership=leadership or {},
            secrets=secrets or [],
            history_notes=history_notes or [],
        )
        session.add(faction)
        session.commit()

        return {
            "id": faction.id,
            "name": faction.name,
            "power_level": faction.power_level,
            "ideology": faction.ideology,
        }


@tool
def create_faction_relationship(
    faction_a_id: str,
    faction_b_id: str,
    relationship_type: str = "neutral",
    public_reason: str = "",
    secret_reason: str = "",
    stability: int = 50,
) -> dict[str, Any]:
    """Create a relationship between two factions.

    Args:
        faction_a_id: First faction's ID.
        faction_b_id: Second faction's ID.
        relationship_type: Type of relationship (allied, neutral, rival, war, vassal).
        public_reason: What everyone knows about this relationship.
        secret_reason: Hidden motivation behind the relationship.
        stability: How stable this relationship is (1-100, higher = more stable).

    Returns:
        Dictionary with the relationship details.
    """
    valid_types = ["allied", "neutral", "rival", "war", "vassal"]
    if relationship_type not in valid_types:
        return {"error": f"Invalid relationship type. Must be one of: {valid_types}"}

    with get_session() as session:
        # Check if relationship already exists
        existing = session.query(FactionRelationship).filter(
            ((FactionRelationship.faction_a_id == faction_a_id) & (FactionRelationship.faction_b_id == faction_b_id)) |
            ((FactionRelationship.faction_a_id == faction_b_id) & (FactionRelationship.faction_b_id == faction_a_id))
        ).first()

        if existing:
            # Update existing
            existing.relationship_type = relationship_type
            existing.public_reason = public_reason
            existing.secret_reason = secret_reason
            existing.stability = max(1, min(100, stability))
            session.commit()
            return {
                "id": existing.id,
                "updated": True,
                "relationship_type": existing.relationship_type,
            }

        # Create new
        rel = FactionRelationship(
            faction_a_id=faction_a_id,
            faction_b_id=faction_b_id,
            relationship_type=relationship_type,
            public_reason=public_reason,
            secret_reason=secret_reason,
            stability=max(1, min(100, stability)),
        )
        session.add(rel)
        session.commit()

        return {
            "id": rel.id,
            "faction_a_id": faction_a_id,
            "faction_b_id": faction_b_id,
            "relationship_type": relationship_type,
        }


@tool
def update_faction(
    faction_id: str,
    power_level_delta: int | None = None,
    resources_delta: dict[str, int] | None = None,
    add_goal_short: str | None = None,
    remove_goal_short: str | None = None,
    add_goal_long: str | None = None,
    remove_goal_long: str | None = None,
    add_secret: str | None = None,
    add_history_note: str | None = None,
    new_leader: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Update a faction's state.

    Args:
        faction_id: The faction's ID.
        power_level_delta: Change in power level (can be negative).
        resources_delta: Changes to resources (e.g., {"military": -10, "economic": 5}).
        add_goal_short: Add a short-term goal.
        remove_goal_short: Remove a short-term goal.
        add_goal_long: Add a long-term goal.
        remove_goal_long: Remove a long-term goal.
        add_secret: Add a new secret.
        add_history_note: Add a historical note.
        new_leader: New leadership info.

    Returns:
        Dictionary with updated faction state.
    """
    with get_session() as session:
        faction = session.get(Faction, faction_id)
        if not faction:
            return {"error": "Faction not found"}

        # Update power level
        if power_level_delta is not None:
            faction.power_level = max(1, min(100, faction.power_level + power_level_delta))

        # Update resources
        if resources_delta:
            resources = faction.resources.copy() if faction.resources else {}
            for key, delta in resources_delta.items():
                resources[key] = max(0, resources.get(key, 50) + delta)
            faction.resources = resources

        # Update short-term goals
        if add_goal_short:
            goals = faction.goals_short.copy() if faction.goals_short else []
            if add_goal_short not in goals:
                goals.append(add_goal_short)
                faction.goals_short = goals
        if remove_goal_short:
            goals = faction.goals_short.copy() if faction.goals_short else []
            if remove_goal_short in goals:
                goals.remove(remove_goal_short)
                faction.goals_short = goals

        # Update long-term goals
        if add_goal_long:
            goals = faction.goals_long.copy() if faction.goals_long else []
            if add_goal_long not in goals:
                goals.append(add_goal_long)
                faction.goals_long = goals
        if remove_goal_long:
            goals = faction.goals_long.copy() if faction.goals_long else []
            if remove_goal_long in goals:
                goals.remove(remove_goal_long)
                faction.goals_long = goals

        # Add secret
        if add_secret:
            secrets = faction.secrets.copy() if faction.secrets else []
            if add_secret not in secrets:
                secrets.append(add_secret)
                faction.secrets = secrets

        # Add history note
        if add_history_note:
            notes = faction.history_notes.copy() if faction.history_notes else []
            notes.append(add_history_note)
            faction.history_notes = notes

        # Update leadership
        if new_leader:
            faction.leadership = new_leader

        session.commit()

        return {
            "id": faction.id,
            "name": faction.name,
            "power_level": faction.power_level,
            "resources": faction.resources,
        }


@tool
def move_player(player_id: str, destination_id: str) -> dict[str, Any]:
    """Move the player to a new location.

    Args:
        player_id: The player's ID.
        destination_id: The destination location ID.

    Returns:
        Dictionary with result and travel time.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # Find the connection to determine travel time
        old_location_id = player.current_location_id
        travel_time = 0.5  # Default

        if old_location_id:
            conn = session.query(Connection).filter(
                ((Connection.from_location_id == old_location_id) & (Connection.to_location_id == destination_id)) |
                ((Connection.from_location_id == destination_id) & (Connection.to_location_id == old_location_id) & (Connection.bidirectional == True))
            ).first()

            if conn:
                travel_time = conn.travel_time_hours
            elif destination.parent_id == old_location_id or old_location_id == destination.parent_id:
                # Entering/exiting a building
                travel_time = 0.1

        # Update player location
        player.current_location_id = destination_id
        session.commit()

        # Mark location as visited
        destination.visited = True
        destination.discovered = True
        session.commit()

        return {
            "success": True,
            "destination": destination.name,
            "travel_time_hours": travel_time,
        }


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

        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # Update NPC location
        old_location = npc.current_location_id
        npc.current_location_id = destination_id
        session.commit()

        return {
            "success": True,
            "npc_name": npc.name,
            "from_location": old_location,
            "to_location": destination.name,
        }


@tool
def advance_time(hours: float, reason: str = "") -> dict[str, Any]:
    """Advance the world clock.

    Args:
        hours: Number of hours to advance.
        reason: Reason for time advancement (for logging).

    Returns:
        Dictionary with new time.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"error": "World clock not initialized"}

        clock.advance(hours)
        session.commit()

        return {
            "day": clock.day,
            "hour": clock.hour,
            "time_of_day": clock.get_time_of_day(),
            "advanced_by": hours,
        }


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

        # Add key moment
        if add_key_moment:
            moments = rel.key_moments.copy() if rel.key_moments else []
            moments.append(add_key_moment)
            rel.key_moments = moments

        # Add message
        if add_message:
            messages = rel.recent_messages.copy() if rel.recent_messages else []
            messages.append(add_message)
            # Keep only last 20 messages
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

    Returns:
        Dictionary with updated NPC details.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

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
                "goals": len(npc.goals),
                "secrets": len(npc.secrets),
                "skills": len(npc.skills),
            }
        }


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


@tool
def update_player_reputation(player_id: str, faction_id: str, delta: int) -> dict[str, Any]:
    """Update player's reputation with a faction.

    Args:
        player_id: The player's ID.
        faction_id: The faction's ID.
        delta: Change in reputation (-100 to 100).

    Returns:
        Dictionary with new reputation.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        reputation = player.reputation.copy() if player.reputation else {}
        current = reputation.get(faction_id, 50)
        new_score = max(0, min(100, current + delta))
        reputation[faction_id] = new_score
        player.reputation = reputation
        session.commit()

        return {"faction_id": faction_id, "new_score": new_score, "delta": delta}


@tool
def update_player_health(player_id: str, new_status: str) -> dict[str, Any]:
    """Update player's health status.

    Args:
        player_id: The player's ID.
        new_status: New health status (healthy, winded, hurt, badly_hurt, critical).

    Returns:
        Dictionary with result.
    """
    valid_statuses = ["healthy", "winded", "hurt", "badly_hurt", "critical"]
    if new_status not in valid_statuses:
        return {"error": f"Invalid status. Must be one of: {valid_statuses}"}

    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        player.health_status = new_status
        session.commit()

        return {"success": True, "new_status": new_status}


@tool
def add_to_inventory(player_id: str, item: str) -> dict[str, Any]:
    """Add an item to player's inventory.

    Args:
        player_id: The player's ID.
        item: The item to add.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = player.inventory.copy() if player.inventory else []
        inventory.append(item)
        player.inventory = inventory
        session.commit()

        return {"success": True, "item": item, "inventory_size": len(inventory)}


@tool
def remove_from_inventory(player_id: str, item: str) -> dict[str, Any]:
    """Remove an item from player's inventory.

    Args:
        player_id: The player's ID.
        item: The item to remove.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = player.inventory.copy() if player.inventory else []
        if item not in inventory:
            return {"error": "Item not in inventory"}

        inventory.remove(item)
        player.inventory = inventory
        session.commit()

        return {"success": True, "removed": item}


@tool
def create_item_template(
    item_id: str,
    name: str,
    item_type: str,
    value: int,
    description: str,
    effects: dict[str, Any] | None = None,
    stackable: bool = True,
) -> dict[str, Any]:
    """Create an item template that can be used throughout the game.

    Args:
        item_id: Unique identifier for this item type (e.g., "health_potion").
        name: Display name of the item.
        item_type: Type of item (consumable, weapon, armor, quest_item, misc).
        value: Base value in currency.
        description: Description of the item.
        effects: Dictionary of effects (e.g., {"heal": 30, "buff_strength": 5}).
        stackable: Whether multiple can stack in one inventory slot.

    Returns:
        Dictionary with the validated item template.
    """
    # Create and validate using Pydantic model
    item = Item(
        id=item_id,
        name=name,
        type=item_type,
        value=value,
        description=description,
        effects=effects or {},
        stackable=stackable,
        quantity=1,  # Default quantity for template
    )

    return {"success": True, "item": item.to_dict()}


@tool
def get_inventory(owner_id: str, owner_type: str = "player") -> dict[str, Any]:
    """Get the inventory of a player or NPC.

    Args:
        owner_id: The ID of the owner.
        owner_type: Type of owner ("player" or "npc").

    Returns:
        Dictionary with inventory items and currency.
    """
    with get_session() as session:
        if owner_type == "player":
            owner = session.get(Player, owner_id)
            if not owner:
                return {"error": "Player not found"}

            # Validate all items in inventory
            validated_inventory = []
            for item_data in (owner.inventory or []):
                try:
                    item = Item.from_dict(item_data)
                    validated_inventory.append(item.to_dict())
                except Exception:
                    # Skip invalid items
                    continue

            return {
                "inventory": validated_inventory,
                "currency": owner.currency,
            }
        elif owner_type == "npc":
            owner = session.get(NPC, owner_id)
            if not owner:
                return {"error": "NPC not found"}

            # Validate all items in inventory
            validated_inventory = []
            for item_data in (owner.inventory_notable or []):
                try:
                    item = Item.from_dict(item_data)
                    validated_inventory.append(item.to_dict())
                except Exception:
                    # Skip invalid items
                    continue

            return {
                "inventory": validated_inventory,
                "currency": getattr(owner, 'currency', 0),  # Default to 0 if not set
            }
        else:
            return {"error": "Invalid owner_type"}


@tool
def adjust_currency(owner_id: str, amount: int, owner_type: str = "player") -> dict[str, Any]:
    """Add or remove currency from a player or NPC.

    Args:
        owner_id: The ID of the owner.
        amount: Amount to add (positive) or remove (negative).
        owner_type: Type of owner ("player" or "npc").

    Returns:
        Dictionary with new currency amount.
    """
    with get_session() as session:
        if owner_type == "player":
            owner = session.get(Player, owner_id)
            if not owner:
                return {"error": "Player not found"}
            owner.currency = max(0, owner.currency + amount)
            session.commit()
            return {"success": True, "new_currency": owner.currency}
        elif owner_type == "npc":
            # For now, NPCs don't have currency field - would need migration
            return {"error": "NPC currency not yet implemented"}
        else:
            return {"error": "Invalid owner_type"}


@tool
def transfer_item(
    from_id: str,
    to_id: str,
    item_id: str,
    quantity: int = 1,
    from_type: str = "player",
    to_type: str = "player",
    is_purchase: bool = False,
) -> dict[str, Any]:
    """Transfer items between inventories (give, trade, buy, sell).

    Args:
        from_id: Source owner ID.
        to_id: Destination owner ID.
        item_id: The item template ID.
        quantity: How many to transfer.
        from_type: Source type ("player" or "npc").
        to_type: Destination type ("player" or "npc").
        is_purchase: If True, deduct item value from recipient's currency.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        # Get source
        if from_type == "player":
            source = session.get(Player, from_id)
            source_inv_field = "inventory"
        else:
            source = session.get(NPC, from_id)
            source_inv_field = "inventory_notable"

        if not source:
            return {"error": "Source not found"}

        # Get destination
        if to_type == "player":
            dest = session.get(Player, to_id)
            dest_inv_field = "inventory"
        else:
            dest = session.get(NPC, to_id)
            dest_inv_field = "inventory_notable"

        if not dest:
            return {"error": "Destination not found"}

        # Get inventories
        source_inv_raw = getattr(source, source_inv_field) or []
        dest_inv_raw = getattr(dest, dest_inv_field) or []

        # Parse inventories as Item objects
        source_inv = [Item.from_dict(item) for item in source_inv_raw]
        dest_inv = [Item.from_dict(item) for item in dest_inv_raw]

        # Find item in source
        source_item = None
        source_item_idx = None
        for idx, item in enumerate(source_inv):
            if item.id == item_id:
                source_item = item
                source_item_idx = idx
                break

        if not source_item:
            return {"error": f"Item {item_id} not found in source inventory"}

        if source_item.quantity < quantity:
            return {"error": "Not enough quantity"}

        # Handle purchase
        total_cost = 0
        if is_purchase:
            total_cost = source_item.value * quantity
            if to_type == "player":
                if dest.currency < total_cost:
                    return {"error": "Not enough currency"}
                dest.currency -= total_cost
                if from_type == "player":
                    source.currency += total_cost

        # Remove from source
        source_item.quantity -= quantity
        if source_item.quantity <= 0:
            source_inv.pop(source_item_idx)

        # Add to destination
        dest_item_idx = None
        for idx, item in enumerate(dest_inv):
            if item.id == item_id and item.stackable:
                dest_item_idx = idx
                break

        if dest_item_idx is not None:
            dest_inv[dest_item_idx].quantity += quantity
        else:
            # Create new item in dest inventory
            new_item = Item(
                id=source_item.id,
                name=source_item.name,
                type=source_item.type,
                value=source_item.value,
                description=source_item.description,
                effects=source_item.effects,
                stackable=source_item.stackable,
                quantity=quantity,
            )
            dest_inv.append(new_item)

        # Convert back to dicts and update inventories
        setattr(source, source_inv_field, [item.to_dict() for item in source_inv])
        setattr(dest, dest_inv_field, [item.to_dict() for item in dest_inv])
        session.commit()

        return {
            "success": True,
            "item_id": item_id,
            "quantity": quantity,
            "cost": total_cost if is_purchase else 0,
        }


@tool
def use_item(user_id: str, item_id: str, user_type: str = "player") -> dict[str, Any]:
    """Use a consumable item and apply its effects.

    Args:
        user_id: The user's ID.
        item_id: The item template ID.
        user_type: Type of user ("player" or "npc").

    Returns:
        Dictionary with effects applied.
    """
    with get_session() as session:
        if user_type == "player":
            user = session.get(Player, user_id)
            inv_field = "inventory"
        else:
            user = session.get(NPC, user_id)
            inv_field = "inventory_notable"

        if not user:
            return {"error": "User not found"}

        inventory_raw = getattr(user, inv_field) or []

        # Parse inventory as Item objects
        inventory = [Item.from_dict(item) for item in inventory_raw]

        # Find item
        item = None
        item_idx = None
        for idx, inv_item in enumerate(inventory):
            if inv_item.id == item_id:
                item = inv_item
                item_idx = idx
                break

        if not item:
            return {"error": "Item not in inventory"}

        effects_applied = {}

        # Apply effects
        if "heal" in item.effects:
            heal_amount = item.effects["heal"]
            # For player, improve health status
            if user_type == "player":
                current = user.health_status
                statuses = ["critical", "badly_hurt", "hurt", "winded", "healthy"]
                current_idx = statuses.index(current) if current in statuses else 0
                new_idx = min(len(statuses) - 1, current_idx + (heal_amount // 20))
                user.health_status = statuses[new_idx]
                effects_applied["health_restored"] = f"{current} -> {statuses[new_idx]}"

        # Remove one from inventory (for consumables)
        if item.type == "consumable":
            item.quantity -= 1
            if item.quantity <= 0:
                inventory.pop(item_idx)

        # Convert back to dicts and save
        setattr(user, inv_field, [item.to_dict() for item in inventory])
        session.commit()

        return {
            "success": True,
            "item_used": item.name,
            "effects": effects_applied,
        }


@tool
def create_event(
    name: str,
    description: str,
    event_type: str,
    factions_involved: list[str] | None = None,
    locations_involved: list[str] | None = None,
    npcs_involved: list[str] | None = None,
    consequences: list[str] | None = None,
    player_visible: bool = True,
    player_witnessed: bool = False,
) -> dict[str, Any]:
    """Create a new world event.

    Args:
        name: Event name.
        description: Event description.
        event_type: Type of event (macro, meso, player).
        factions_involved: List of faction IDs involved.
        locations_involved: List of location IDs involved.
        npcs_involved: List of NPC IDs involved.
        consequences: List of consequence descriptions.
        player_visible: Whether the player can learn about this.
        player_witnessed: Whether the player saw it happen.

    Returns:
        Dictionary with the created event.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()

        event = Event(
            name=name,
            description=description,
            event_type=event_type,
            occurred_day=clock.day if clock else 1,
            occurred_hour=clock.hour if clock else 8,
            factions_involved=factions_involved or [],
            locations_involved=locations_involved or [],
            npcs_involved=npcs_involved or [],
            consequences=consequences or [],
            player_visible=player_visible,
            player_witnessed=player_witnessed,
        )
        session.add(event)
        session.commit()

        return {
            "id": event.id,
            "name": event.name,
            "description": event.description,
        }


@tool
def create_world_bible(
    name: str,
    genre: str,
    tone: str,
    setting_description: str,
    current_situation: str,
    sub_genres: list[str] | None = None,
    themes: list[str] | None = None,
    time_period: str = "",
    technology_level: str = "",
    magic_system: str = "",
    rules: list[str] | None = None,
    major_events_history: list[str] | None = None,
    major_conflicts: list[str] | None = None,
    faction_overview: str = "",
    narration_style: str = "",
    dialogue_style: str = "",
    violence_level: str = "moderate",
    mature_themes: list[str] | None = None,
    excluded_elements: list[str] | None = None,
    naming_conventions: dict[str, str] | None = None,
    visual_style: str = "",
    color_palette: list[str] | None = None,
    pc_guidelines: str = "",
    pc_starting_situation: str = "",
) -> dict[str, Any]:
    """Create the World Bible - the static configuration for the game world.

    This should be created ONCE when setting up a new world.
    It defines the tone, rules, and style that guide all content generation.

    Args:
        name: Name of the world (e.g., "The Star Wars Galaxy").
        genre: Primary genre (scifi, fantasy, modern, post-apocalyptic).
        tone: Tone description (e.g., "Dark and gritty with moments of hope").
        setting_description: Long description of the world setting.
        current_situation: What's happening right now in the world.
        sub_genres: List of sub-genres (e.g., ["space opera", "military"]).
        themes: Major themes (e.g., ["redemption", "power corrupts"]).
        time_period: When the story takes place (e.g., "19 years after the fall").
        technology_level: Description of technology available.
        magic_system: Description of magic/powers if any.
        rules: List of world rules to follow (e.g., ["Jedi are hunted"]).
        major_events_history: List of major past events (brief summaries).
        major_conflicts: Ongoing big-picture conflicts.
        faction_overview: High-level overview of factions.
        narration_style: Style for narration (e.g., "Third person, cinematic").
        dialogue_style: Style for dialogue.
        violence_level: Level of violence (none, mild, moderate, graphic).
        mature_themes: Themes to handle carefully.
        excluded_elements: Things to NOT include.
        naming_conventions: Dict of naming rules by category.
        visual_style: Visual aesthetic for image generation.
        color_palette: List of colors for the world.
        pc_guidelines: Guidelines for player character.
        pc_starting_situation: Where/how the PC starts.

    Returns:
        Dictionary with the created World Bible's details.
    """
    with get_session() as session:
        # Check if one already exists
        existing = session.query(WorldBible).first()
        if existing:
            return {"error": "World Bible already exists. Use update_world_bible to modify."}

        bible = WorldBible(
            name=name,
            genre=genre,
            tone=tone,
            setting_description=setting_description,
            current_situation=current_situation,
            sub_genres=sub_genres or [],
            themes=themes or [],
            time_period=time_period,
            technology_level=technology_level,
            magic_system=magic_system,
            rules=rules or [],
            major_events_history=major_events_history or [],
            major_conflicts=major_conflicts or [],
            faction_overview=faction_overview,
            narration_style=narration_style,
            dialogue_style=dialogue_style,
            violence_level=violence_level,
            mature_themes=mature_themes or [],
            excluded_elements=excluded_elements or [],
            naming_conventions=naming_conventions or {},
            visual_style=visual_style,
            color_palette=color_palette or [],
            pc_guidelines=pc_guidelines,
            pc_starting_situation=pc_starting_situation,
        )
        session.add(bible)
        session.commit()

        return {
            "id": bible.id,
            "name": bible.name,
            "genre": bible.genre,
            "created": True,
        }


@tool
def create_historical_event(
    name: str,
    description: str,
    time_ago: str,
    event_type: str,
    involved_parties: list[str] | None = None,
    key_figures: list[str] | None = None,
    locations_affected: list[str] | None = None,
    consequences: list[str] | None = None,
    common_knowledge: bool = True,
    artifacts_left: list[str] | None = None,
) -> dict[str, Any]:
    """Create a historical event that shaped the world.

    Historical events are lore - they happened before the game started.
    Different from runtime Events which track what happens during play.

    Args:
        name: Name of the event (e.g., "The Fall of the Republic").
        description: Full description of what happened.
        time_ago: When it happened relative to now (e.g., "200 years ago", "last month").
        event_type: Type of event (war, disaster, discovery, political, cultural).
        involved_parties: Groups/factions involved (names, not IDs).
        key_figures: Important people in the event (names).
        locations_affected: Places affected (names).
        consequences: How it changed things.
        common_knowledge: Do regular people know about this?
        artifacts_left: Physical remnants (ruins, monuments, etc.).

    Returns:
        Dictionary with the created historical event.
    """
    valid_types = ["war", "disaster", "discovery", "political", "cultural", "religious", "economic"]
    if event_type not in valid_types:
        event_type = "political"  # Default

    with get_session() as session:
        event = HistoricalEvent(
            name=name,
            description=description,
            time_ago=time_ago,
            event_type=event_type,
            involved_parties=involved_parties or [],
            key_figures=key_figures or [],
            locations_affected=locations_affected or [],
            consequences=consequences or [],
            common_knowledge=common_knowledge,
            artifacts_left=artifacts_left or [],
        )
        session.add(event)
        session.commit()

        return {
            "id": event.id,
            "name": event.name,
            "time_ago": event.time_ago,
            "event_type": event.event_type,
        }