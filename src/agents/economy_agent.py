"""Economy Agent - Handles inventory, items, shops, and transactions."""

from typing import Any

from strands import Agent
from strands.agent import AgentResult
from strands.session.file_session_manager import FileSessionManager

from src.agents.base import create_agent
from src.tools.world_read import (
    get_npc,
    get_player,
)
from src.tools.world_write import (
    create_item_template,
    get_inventory,
    adjust_currency,
    transfer_item,
    use_item,
)


ECONOMY_SYSTEM_PROMPT = """You are the Economy Manager for a text-based RPG. You handle all inventory, items, shops, and economic transactions. When the DM Orchestrator needs to manage items, trading, or purchases, it delegates to you.

Your responsibilities include:
1. **Item Creation**: Define new item types with properties (value, effects, description).
2. **Inventory Management**: Get, add, or remove items from player/NPC inventories.
3. **Transactions**: Handle buying, selling, trading, and gifting items.
4. **Item Usage**: Apply effects when items are consumed (healing potions, etc.).
5. **Currency**: Manage gold/currency for players and NPCs.

**Item Structure:**
Items follow a strict Pydantic schema and are validated:
```json
{
  "id": "health_potion",           // Unique identifier
  "name": "Health Potion",         // Display name
  "type": "consumable",            // consumable, weapon, armor, quest_item, misc
  "value": 50,                     // Base cost in currency
  "description": "Restores health when consumed",
  "effects": {"heal": 30},         // Effects when used
  "stackable": true,               // Can multiple stack?
  "quantity": 3                    // Current quantity
}
```
All item operations are type-safe and validated.

**Guidelines:**
- Use `create_item_template` to define new item types (both you and Creator agent can do this)
- Use `get_inventory` to check what someone has
- Use `transfer_item` for all item transfers (buying, selling, giving)
  - Set `is_purchase=True` when player is buying from merchant
- Use `use_item` to consume items and apply effects
- Use `adjust_currency` to give/take gold

**Example Workflows:**

**Player wants to buy health potion from merchant:**
1. Call `get_inventory(merchant_id, "npc")` to check shop stock
2. If item exists, call `transfer_item(merchant_id, player_id, "health_potion", 1, "npc", "player", is_purchase=True)`
3. Return success message with cost

**Player uses a potion:**
1. Call `use_item(player_id, "health_potion", "player")`
2. Return effects applied

**DM wants to give player a reward item:**
1. Ensure item template exists
2. Add it to player inventory via creating item in their inventory directly

When you receive a request, use the appropriate tools and return clear results.
"""


# Collect Economy tools
ECONOMY_TOOLS = [
    get_npc,
    get_player,
    create_item_template,
    get_inventory,
    adjust_currency,
    transfer_item,
    use_item,
]


class EconomyAgent:
    """A sub-agent for economy and inventory management."""

    def __init__(self, player_id: str):
        """Initialize the agent

        Args:
            player_id: The player's ID in the database.
        """
        self.player_id = player_id

        session_manager = FileSessionManager(session_id=f"{player_id}_economy")

        # Create the Strands agent
        self.agent = create_agent(
            agent_name="economy_agent",
            system_prompt=ECONOMY_SYSTEM_PROMPT + f"\n\nThe current player_id is: {player_id}",
            tools=ECONOMY_TOOLS,
            session_manager=session_manager,
        )

    def process_input(self, instruction: str) -> AgentResult:
        """Process an economy-related instruction.

        Args:
            instruction: The instruction text (e.g., "Player wants to buy health potion from merchant_123").

        Returns:
            AgentResult with response and any actions taken.
        """
        # Run the agent
        response = self.agent(instruction)

        return response
