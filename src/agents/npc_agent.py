"""NPC Agent - Handles NPC dialogue using Strands Agents with session management."""

from typing import Any

from strands import Agent
from strands.session.file_session_manager import FileSessionManager

from src.agents.base import create_agent
from src.tools.world_read import get_npc, get_npc_relationship, get_world_clock
from src.tools.world_write import update_npc_relationship, update_npc_mood, update_npc
from src.tools.narration import speak


def build_npc_system_prompt(npc: dict[str, Any], relationship: dict[str, Any]) -> str:
    """Build the system prompt for an NPC conversation.

    Args:
        npc: NPC data dictionary.
        relationship: Relationship data with the player.

    Returns:
        System prompt string.
    """
    # Build secrets list (hide revealed ones)
    revealed_indices = set(relationship.get('revealed_secrets', []))
    hidden_secrets = [
        s for i, s in enumerate(npc.get('secrets', []))
        if i not in revealed_indices
    ]

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

**Your Secrets (never reveal unless trust is very high 80+):**
{chr(10).join(f'- {s}' for s in hidden_secrets) if hidden_secrets else '- None'}

**IMPORTANT Guidelines:**
1. **Stay in character** - You are {npc['name']}, not an AI assistant
2. **Use the `speak` tool** for ALL your dialogue - ALWAYS call speak() with your response
3. Speak in first person as {npc['name']}
4. Match your voice_pattern and current mood in your speech
5. Keep responses conversational and concise (2-4 sentences typically)
6. You can include actions in *asterisks* when calling speak()
7. React to the player based on your relationship and goals
8. If player is rude or you want to leave, include [END_CONVERSATION] in your response

**Conversation Management:**
- The system automatically tracks conversation history
- You can reference past exchanges naturally
- Use `update_npc_relationship` to update trust if something significant happens
- Use `update_npc_mood` if your emotional state changes
- Use `update_npc` to evolve your character based on events:
  - Add/remove goals when circumstances change
  - Add new secrets you've learned or developed
  - Update your physical description if you're injured or changed
  - Change your profession if your role evolves

**Example:**
Player: "Hello there!"
You: Call speak(npc_name="{npc['name']}", text="Well met, traveler. What brings you to these parts?", tone="friendly")
"""
    return prompt


# NPC tools
NPC_TOOLS = [
    speak,
    update_npc_relationship,
    update_npc_mood,
    update_npc,
]


class NPCAgent:
    """Agent that handles NPC dialogue and personality using Strands."""

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

        # Build system prompt
        system_prompt = build_npc_system_prompt(self.npc, self.relationship)

        # Create session manager for this specific NPC conversation
        session_manager = FileSessionManager(session_id=f"{self.player_id}_{self.npc_id}")

        # Create the Strands agent with session management
        self.agent = create_agent(
            agent_name="npc_agent",
            system_prompt=system_prompt,
            tools=NPC_TOOLS,
            session_manager=session_manager,
        )

        # Generate greeting
        trust = self.relationship.get("trust_level", 50)
        disposition = self.relationship.get("current_disposition", "neutral")

        greeting_prompt = f"""The player approaches you.

Context:
- Your disposition toward them: {disposition}
- Your trust level: {trust}/100

Generate an appropriate greeting based on your character and relationship.
Use the speak tool to deliver your greeting."""

        # Call the agent - Strands handles conversation history automatically
        response = self.agent(greeting_prompt)
        return str(response)

    def respond(self, player_input: str) -> dict[str, Any]:
        """Generate NPC response to player input.

        Args:
            player_input: What the player says.

        Returns:
            Dictionary with response and any actions to take.
        """
        if not self.agent:
            return {"response": "...", "conversation_ended": True}

        # Simply call the agent - it has the full conversation history via session manager
        response = self.agent(f'The player says: "{player_input}"')
        response_text = str(response)

        # Check if conversation ended
        conversation_ended = "[END_CONVERSATION]" in response_text
        if conversation_ended:
            response_text = response_text.replace("[END_CONVERSATION]", "").strip()

        return {
            "response": response_text,
            "conversation_ended": conversation_ended,
        }

    def end_conversation(self) -> None:
        """End the conversation and save a summary."""
        # Record that a conversation happened
        clock = get_world_clock()
        summary = f"Had a conversation on Day {clock.get('day', 1)}"

        update_npc_relationship(
            npc_id=self.npc_id,
            player_id=self.player_id,
            add_key_moment=summary
        )

        # Session is automatically saved by FileSessionManager
        # No need to manually persist messages
