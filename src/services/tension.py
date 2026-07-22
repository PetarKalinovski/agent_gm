"""Mechanical tension enforcement — code enforces, the LLM narrates.

Tension drives the adaptive music layer and pacing, but the DM rarely
remembers to call `update_dm_state` in the heat of a scene (playtest: an
entire wolf standoff on the 'explore' track). This module guarantees a
tension floor from things code can measure — health loss, combat — and
decays tension back down when danger has clearly passed.

The DM can still raise tension narratively at any time; enforcement only
ever raises it to a floor or cools it after sustained calm, never fights
an explicit DM escalation.
"""

from typing import Any

from loguru import logger

from src.models import DMState, Player, get_session


HEALTH_SEVERITY = {
    "healthy": 0,
    "winded": 1,
    "hurt": 2,
    "badly_hurt": 3,
    "critical": 4,
    "dead": 5,
}

# "falling" is a DM-authored cooldown state — ranks as calm
TENSION_RANK = {"low": 0, "falling": 0, "rising": 1, "high": 2, "climax": 3}
RANK_TO_TENSION = ["low", "rising", "high", "climax"]

# Calm turns required before tension steps down one level, per current level
DECAY_TURNS = {"climax": 2, "high": 3, "rising": 4, "falling": 2}


def snapshot_turn_tension(player_id: str) -> dict[str, Any]:
    """Capture health + tension at turn start (compare after the turn)."""
    with get_session() as session:
        player = session.get(Player, player_id)
        dm_state = session.query(DMState).first()
        return {
            "health": player.health_status if player else "healthy",
            "tension": dm_state.tension if dm_state else "low",
        }


def apply_tension_floor(minimum: str, reason: str = "") -> dict[str, Any]:
    """Raise tension to at least `minimum` (never lowers). Used by combat
    start and other code paths that know danger is real right now."""
    floor_rank = TENSION_RANK.get(minimum)
    if floor_rank is None:
        return {"error": f"invalid tension level {minimum!r}"}
    with get_session() as session:
        dm_state = session.query(DMState).first()
        if not dm_state:
            return {"changed": False, "reason": "no dm_state"}
        current_rank = TENSION_RANK.get(dm_state.tension, 0)
        if current_rank >= floor_rank:
            dm_state.turns_since_danger = 0
            session.commit()
            return {"changed": False, "tension": dm_state.tension}
        dm_state.tension = RANK_TO_TENSION[floor_rank]
        dm_state.turns_since_danger = 0
        session.commit()
        logger.info(f"Tension floor applied: {dm_state.tension} ({reason})")
        return {"changed": True, "tension": dm_state.tension, "reason": reason}


def enforce_tension(turn_start: dict[str, Any], player_id: str) -> dict[str, Any]:
    """Post-turn tension enforcement.

    - Health worsened this turn → tension floor (winded/hurt → rising,
      badly_hurt → high, critical → climax). Death is left alone — the
      client's somber/death handling keys off health directly.
    - DM changed tension itself this turn → respected, decay counter resets.
    - Otherwise a calm-turn counter runs; sustained calm steps elevated
      tension down one level at a time (climax→high→rising→low). The
      world-tick's quiet-turn escalation (low→rising after 8 quiet turns)
      is unchanged — together they make the pacing breathe.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        dm_state = session.query(DMState).first()
        if not player or not dm_state:
            return {"changed": False, "reason": "world not initialized"}

        old_sev = HEALTH_SEVERITY.get(turn_start.get("health", "healthy"), 0)
        new_sev = HEALTH_SEVERITY.get(player.health_status, 0)
        health_worsened = new_sev > old_sev and player.health_status != "dead"

        current = dm_state.tension or "low"
        current_rank = TENSION_RANK.get(current, 0)
        dm_changed = current != turn_start.get("tension", current)

        # 1) Danger floor from health loss
        if health_worsened:
            if new_sev >= 4:
                floor = 3  # climax
            elif new_sev == 3:
                floor = 2  # high
            else:
                floor = 1  # rising
            dm_state.turns_since_danger = 0
            if floor > current_rank:
                dm_state.tension = RANK_TO_TENSION[floor]
                session.commit()
                logger.info(
                    f"Tension enforced {current} -> {dm_state.tension} "
                    f"(health {turn_start.get('health')} -> {player.health_status})"
                )
                return {"changed": True, "tension": dm_state.tension, "reason": "health"}
            session.commit()
            return {"changed": False, "tension": current}

        # 2) DM authored a change this turn — trust it, reset the calm counter
        if dm_changed:
            dm_state.turns_since_danger = 0
            session.commit()
            return {"changed": False, "tension": current, "reason": "dm_authored"}

        # 3) Calm turn — decay elevated tension after sustained quiet
        calm_needed = DECAY_TURNS.get(current)
        if calm_needed is None:  # low: nothing to decay
            return {"changed": False, "tension": current}

        calm = (dm_state.turns_since_danger or 0) + 1
        if calm >= calm_needed:
            new_rank = max(0, current_rank - 1)
            # "falling" resolves to low; elevated levels step down one
            dm_state.tension = "low" if current == "falling" else RANK_TO_TENSION[new_rank]
            dm_state.turns_since_danger = 0
            session.commit()
            logger.info(f"Tension decayed {current} -> {dm_state.tension} after {calm} calm turns")
            return {"changed": True, "tension": dm_state.tension, "reason": "decay"}

        dm_state.turns_since_danger = calm
        session.commit()
        return {"changed": False, "tension": current, "calm_turns": calm}
