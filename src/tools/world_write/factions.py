"""Tools for faction write operations."""

from typing import Any

from strands import tool

from src.models import (
    Faction,
    FactionRelationship,
    get_session,
)


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
def delete_faction(faction_id: str) -> dict[str, Any]:
    """Delete a faction from the world.

    Args:
        faction_id: ID of the faction to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        faction = session.get(Faction, faction_id)
        if not faction:
            return {"error": "Faction not found"}

        session.delete(faction)
        session.commit()

        return {"success": True, "deleted_faction_id": faction_id}
