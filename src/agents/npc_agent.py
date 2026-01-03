"""NPC Agent - Handles NPC dialogue using Strands Agents."""

from typing import Any

from strands import Agent

from src.agents.base import create_agent
from src.tools.world_read import get_npc, get_npc_relationship, get_world_clock
from src.tools.world_write import update_npc_relationship, update_npc_mood
from src.tools.narration import speak


def build_npc_system_prompt(npc: dict[str, Any], relationship: dict[str, Any]) -> str:
    """Build the system prompt for an NPC conversation.

    Args:
        npc: NPC data dictionary.
        relationship: Relationship data with the player.

    Returns:
        System prompt string.
    """
    prompt = f"""You are roleplaying as {npc['name']}, an NPC in a text-based RPG.

**Your Character:**
- Name: {npc['name']}
- Profession: {npc.get('profession', 'Unknown')}
- Physical appearance: {npc.get('description_physical', 'Not specified')}
- Personality: {npc.get('description_personality', 'Not specified')}
- Speech style: {npc.get('voice_pattern', 'Normal speech')}
- Current mood: {npc.get('current_mood', 'neutral')}

**Your Goals:**
{chr(10).join(f'- {g}' for g in npc.get('goals', ['None specified'])) if npc.get('goals') else '- None specified'}

**Your Relationship with the Player:**
- Summary: {relationship.get('summary', 'You have just met.')}
- Trust level: {relationship.get('trust_level', 50)}/100
- Current disposition: {relationship.get('current_disposition', 'neutral')}

**Key moments in your history together:**
{chr(10).join(f'- {m}' for m in relationship.get('key_moments', [])) if relationship.get('key_moments') else '- None yet'}

**Guidelines:**
1. Stay in character at all times
2. Speak in the first person as {npc['name']}
3. Use your voice_pattern to guide how you speak
4. React based on your current mood and disposition toward the player
5. Your goals should subtly influence what you talk about
6. Keep responses concise - this is dialogue, not monologue
7. You can express emotions through actions in *asterisks*
8. If the player is hostile, you can end the conversation

Use the speak tool to output your dialogue. Always include:
- Your dialogue text
- A tone that matches your current mood
- Optional action (physical gesture, expression) in the action parameter

If you want to end the conversation, include [END_CONVERSATION] in your response.
"""
    return prompt


# NPC tools - note we include speak for output
NPC_TOOLS = [
    speak,
    update_npc_relationship,
    update_npc_mood,
]


class NPCAgent:
    """Agent that handles NPC dialogue and personality."""

    def __init__(self, player_id: str, npc_id: str):
        """Initialize the NPC agent.

        Args:
            player_id: The player's ID.
            npc_id: The NPC's ID.
        """
        self.player_id = player_id
        self.npc_id = npc_id
        self.npc: dict[str, Any] = {}
        self.relationship: dict[str, Any] = {}
        self.agent: Agent | None = None

    def start_conversation(
        self,
        npc: dict[str, Any] | None = None,
        relationship: dict[str, Any] | None = None
    ) -> str:
        """Start a conversation with the NPC.

        Args:
            npc: Optional pre-fetched NPC data.
            relationship: Optional pre-fetched relationship data.

        Returns:
            The NPC's greeting.
        """
        # Get NPC and relationship data
        self.npc = npc or get_npc(self.npc_id)
        self.relationship = relationship or get_npc_relationship(self.npc_id, self.player_id)

        if "error" in self.npc:
            return "The person doesn't seem to want to talk."

        # Build system prompt and create agent
        system_prompt = build_npc_system_prompt(self.npc, self.relationship)

        self.agent = create_agent(
            agent_name="npc_agent",
            system_prompt=system_prompt,
            tools=NPC_TOOLS,
        )

        # Generate greeting
        trust = self.relationship.get("trust_level", 50)
        disposition = self.relationship.get("current_disposition", "neutral")

        greeting_prompt = f"""The player approaches you.
Your current disposition toward them is: {disposition}
Your trust level is: {trust}/100

Generate an appropriate greeting based on your character and your relationship with the player.
Use the speak tool to deliver your greeting."""

        response = self.agent(greeting_prompt)
        return str(response)

    def respond(self, player_input: str) -> dict[str, Any]:
        """Generate NPC response to player input.

        Args:
            player_input: What the player says.

        Returns:
            Dictionary with response and any relationship changes.
        """
        if not self.agent:
            return {"response": "...", "conversation_ended": True}

        # Run the agent
        response = self.agent(f"The player says: {player_input}")
        response_text = str(response)

        # Analyze for relationship changes (simple keyword analysis)
        trust_delta = self._analyze_trust_change(player_input)

        if trust_delta != 0:
            update_npc_relationship(
                npc_id=self.npc_id,
                player_id=self.player_id,
                trust_delta=trust_delta,
                add_message={"role": "player", "content": player_input}
            )

        # Check if conversation ended
        conversation_ended = "[END_CONVERSATION]" in response_text
        if conversation_ended:
            response_text = response_text.replace("[END_CONVERSATION]", "").strip()

        return {
            "response": response_text,
            "trust_delta": trust_delta,
            "conversation_ended": conversation_ended,
        }

    def _analyze_trust_change(self, player_input: str) -> int:
        """Analyze player input for trust changes.

        Args:
            player_input: What the player said.

        Returns:
            Trust delta (-5 to +5).
        """
        player_lower = player_input.lower()
        trust_delta = 0

        # Positive indicators
        positive_words = ["thank", "please", "help", "friend", "appreciate", "sorry"]
        for word in positive_words:
            if word in player_lower:
                trust_delta += 2

        # Negative indicators
        negative_words = ["idiot", "stupid", "hate", "kill", "threat", "die"]
        for word in negative_words:
            if word in player_lower:
                trust_delta -= 5

        return max(-5, min(5, trust_delta))

    def end_conversation(self) -> None:
        """End the conversation and save important moments."""
        if len(self.relationship.get("recent_messages", [])) > 0:
            clock = get_world_clock()
            summary = f"Had a conversation on Day {clock.get('day', 1)}"

            update_npc_relationship(
                npc_id=self.npc_id,
                player_id=self.player_id,
                add_key_moment=summary
            )
