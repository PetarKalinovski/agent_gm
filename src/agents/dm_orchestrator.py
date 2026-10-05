"""DM Orchestrator Agent - The main dungeon master using Strands Agents.

One agent, one voice for the whole world, ONE real tool (roll_check). The
DM's streamed reply is the turn: prose the player reads, speaker lines the
server voices (src/web/dialogue_markup.py), and `@command` lines the server
hides and applies through the engine (src/services/commands.py). What the DM
needs to know — exits, people and their memories, prices, its own notes —
is assembled in code each turn (src/services/scene_brief.py,
src/services/memory_files.py). Its instructions are markdown files beside
this module: prompts/dm.md (the rules) and prompts/commands.md (the command
reference).

Why not tools: a tool call costs a full model round-trip, and the turn used
to need four to eight of them in sequence (measured 2026-10-04: 38-85 s a
turn, 7.5 min for the opening). A command line costs nothing — most turns
are one model call now.
"""

import re
from pathlib import Path
from typing import Any, Callable


from src.agents.core.base_agent import BaseGameAgent
from src.agents.turn_window import (
    TurnWindowManager,
    chronicle_hand_off,
    compact_loaded_history,
)
from src.core.types import AgentContext
from src.tools.world_read import (
    get_world_clock,
    get_player,
    get_active_quests,
    get_world_bible_for_dm,
    get_current_location,
)
from src.tools.world_write import roll_check
from src.services.world_tick import run_world_tick, format_world_tick_context
from src.services.campaign import run_campaign_tick, format_campaign_context, no_spine_context
from src.services.perception import appearance_lines

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_dm_prompt() -> str:
    """The DM's rules followed by its command reference, both markdown."""
    rules = (PROMPTS_DIR / "dm.md").read_text(encoding="utf-8")
    commands_path = PROMPTS_DIR / "commands.md"
    commands = commands_path.read_text(encoding="utf-8") if commands_path.exists() else ""
    tail = f"\n\n## COMMANDS\n\n{commands.strip()}" if commands.strip() else ""
    return rules.rstrip() + tail + "\n"


DM_SYSTEM_PROMPT = load_dm_prompt()

# The one decision the engine must make BEFORE the prose is written
DM_TOOLS: list[Callable] = [roll_check]

# An input the engine wrote ("[OPENING] …", "[COMBAT OUTCOME] …"), not the
# player. The real aftermath input is "[COMBAT OUTCOME — resolved in
# real-time …" (combat.combat_outcome_message): the tag ends at a dash too,
# or the aftermath spent an event's delivery attempt after all.
_ENGINE_INPUT = re.compile(r"^\s*\[[A-Z][A-Z_'-]*(?: [A-Z][A-Z_'-]*)*(?:\]|\s*[—–:-])")

NO_QUEST_ORDER = (
    "[NO ACTIVE QUEST — the player has nothing to work on. Within this reply or the next, turn "
    "the live thread into work: someone present offers what their dossier lists under \"Work they "
    "can offer\" (@quest-start <title>), or write @quest-new whose first step names a real person "
    "or place from this brief. Never invent a person or place for it.]"
)

# The last thing the DM reads before the player's words
REPLY_BUDGET = (
    "[THIS REPLY: @commands first; at most 3 short speaker lines (each under 40 words); "
    "1-2 short paragraphs of prose; end on a choice the player makes.]"
)


def _reward_words(rewards: Any) -> str:
    """'60 coin and a Wolf-Tooth Charm (A wolf's fang bound in brass wire…)'
    — what completing a quest hands over, each item with its authored
    description so the payout is narrated as what lands in the pack."""
    from src.tools.world_write.quests import reward_words

    return reward_words(rewards, about=True)


# --- Authored work, offered -------------------------------------------------
# fleet5-wenna T13-19: seven turns at Maera's bar and "A Letter for a Buried
# Man" was never offered; fleet5-bram T19: right after @quest-done the DM
# walked Bram away from Piper, whose "Counting the Howls" he had half played
# already. A dossier's "Work they can offer" line is passive, so the engine
# names the moment: one binding line, one quest, the exact title and its pay.
# It fires when (a) a quest was completed since the last reply (for two
# replies, or until new work starts), (b) a giver has shared the scene for
# OFFER_STREAK replies already, or (c) the player asks for work, to earn, to
# help, or what someone wants, or (d) the player already did part of a job
# nobody offered them (job_offers.credited — fleet9-bram asked Sorrel at T7
# and stood the howl at T9 for Piper's "Counting the Howls", never pitched):
# its giver offers it on their next appearance, crediting the work. Never on
# an engine turn ([OPENING], [COMBAT OUTCOME], an ambush). A quest stops
# being pushed once it starts, once its giver's own lines in a reply offered
# it (named its pay, or asked for its work — job_offers.pitched; a decline
# would otherwise be answered by a re-pitch), or after OFFER_MAX pushes. The
# giver merely speaking, or the title said in passing, is no offer: fleet9-
# bram T2 Piper's "I've been counting the howls for a week" matched the
# title, T3 she only pointed at Sorrel, and the job was never pushed again.
# A pointer to a giver a step away is no offer: it spends no push, and the
# offer comes the reply the player stands with them. The line carries the
# job's steps and where the first one's work is (fleet9-bram T17-T18: the
# Keeper's pitch moved the satchel "upstairs" and said "look, don't take").
# Remembered per player in memory: a restart forgets it, which costs at most
# a repeated push.
OFFER_STREAK = 2           # replies a giver shares the scene before the push
OFFER_MAX = 2              # pushes per quest, then the dossier line alone
OFFER_AFTER_PAYOFF = 2     # replies after a completion that offer the next job
_OFFERS: dict[str, dict[str, Any]] = {}

# The player asking for work, pay, a way to help, or a price — said as an
# ask. Bare "earned", "favor", "you need" and "trade for" were 13 of the old
# pattern's 15 hits over every fleet transcript ("the fog works in my favor",
# "she's earned honesty", "you need to decide…"), each one a job pitched at
# a player who asked for none; this one keeps 3, all real asks.
_ASKS_FOR_WORK = re.compile(
    r"\b(?:any|some|got|have|need|more|paying|honest|odd)\s+(?:paid\s+)?(?:work|jobs?|errands?|tasks?)\b"
    r"|\b(?:looking|searching|hunting|out)\s+for\s+(?:work|a\s+job|jobs|employment|coin)\b"
    r"|\bhir(?:e\s+me|ing)\b"
    r"|\b(?:I|me|to)\s+(?:could\s+|can\s+|might\s+)?earn\b|\b(?:can|could|might|may)\s+I\s+earn\b"
    r"|\bearn(?:ing)?\s+(?:some|a\s+few|a\s+bit|my|coin|money|a\s+living)\b"
    r"|\b(?:do\s+you\s+a|owe\s+(?:you|me)\s+a|trade|trading|swap)\s+favou?rs?\b|\bfavou?r\s+for\s+a\s+favou?r\b"
    r"|\btrade\s+you\b"
    r"|\b(?:can|could|may|might)\s+I\s+(?:help|be\s+of\s+use|assist|do\s+(?:something|anything)\s+for)\b"
    r"(?!\s+(?:him|her|them|it)\b)"
    r"|\b(?:I'?m\s+here\s+to|I\s+came\s+to|I'?d\s+like\s+to|I\s+want\s+to|let\s+me)\s+help\b"
    r"(?!\s+(?:you\s+up|him|her|them|it)\b)"
    r"|\bI\s+(?:can|could|might)\s+be\s+of\s+use\b|\bI\s+(?:can|could|'?ll|will)\s+lend\s+a\s+hand\b"
    r"|\blend\s+you\s+a\s+hand\b|\byou\s+need\s+a\s+hand\b|\bneed\s+a\s+hand\s+with\b"
    r"|\bneed\s+any\s+help\b|\bneed\s+(?:some\s+)?help\s+with\s+anything\b"
    r"|\banything\s+(?:I\s+can\s+do|that\s+needs\s+doing|needs?\s+doing|you\s+need)\b"
    r"|\bwhat\s+(?:do|would|will)\s+you\s+(?:want|need)\s+(?:from\s+me|in\s+(?:return|exchange)"
    r"|for\s+(?:it|that|this)|me\s+to\s+do|done)\b"
    r"|\bwhat\s+(?:do|would)\s+you\s+(?:want|need)\s*[?\"”]"
    r"|\bwhat'?s\s+it\s+worth\b",
    re.IGNORECASE,
)


def reset_offer_state() -> None:
    """Forget every player's offer bookkeeping (tests, world switches)."""
    _OFFERS.clear()


def last_reply_text(agent: Any) -> str:
    """The DM's previous reply, raw (prose, speaker lines, @commands): every
    assistant text block after the last player turn in its history. "" when
    the agent was never built (a fresh process builds it lazily — this
    never forces that)."""
    from src.agents.turn_window import turn_starts

    messages = list(getattr(agent, "messages", None) or [])
    starts = turn_starts(messages)
    texts = []
    for message in messages[(starts[-1] + 1) if starts else 0:]:
        if isinstance(message, dict) and message.get("role") == "assistant":
            texts += [b["text"] for b in message.get("content") or []
                      if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(texts)


def _giver_lines(session, giver_id: str, text: str) -> list[str]:
    """This person's speaker lines in `text` — by name, by part of it, or by
    the role label a stranger goes by ("Herbalist's Apprentice") — their
    words only."""
    from src.services.dialogue import resolve_speaker
    from src.web.dialogue_markup import parse_dialogue_line

    out = []
    for line in (text or "").splitlines():
        parsed = parse_dialogue_line(line)
        if parsed is None:
            continue
        npc = resolve_speaker(session, parsed["speaker"], present_ids=[giver_id])
        if npc is not None and npc.id == giver_id:
            out.append(str(parsed.get("text") or ""))
    return out


def _places_inside(session, location_id: str) -> set[str]:
    """Every place inside this one (the inn off the square), at any depth."""
    from src.models import Location

    inside: set[str] = set()
    frontier = [location_id]
    while frontier:
        rows = session.query(Location.id).filter(Location.parent_id.in_(frontier)).all()
        frontier = [r[0] for r in rows if r[0] not in inside]
        inside.update(frontier)
    return inside


def offer_directive(player_id: str, present: dict[str, str], player_input: str, last_reply: str = "", *,
                    strangers: dict[str, str] | None = None, addressed_npc_id: str | None = None,
                    beat_firing: bool = False) -> str:
    """The `[OFFER THIS REPLY]` line, or "" (see the block comment above).

    `present` is the brief's {npc_id: name}; `strangers` its role labels for
    the unmet; `last_reply` the DM's previous reply (did a giver offer their
    job in it?). Call it once per unconstrained DM turn: it advances the
    bookkeeping. A beat firing this turn outranks the streak push and the
    credited offer (they wait a reply), never a payoff or the player asking."""
    from src.models import NPC, Location, Player, Quest, QuestStatus, get_session
    from src.services import job_offers, standing

    state = _OFFERS.setdefault(player_id, {"streak": {}, "pushed": {}, "said": set(),
                                           "completed": None, "payoff": 0, "active": None})
    pointed: set[str] = state.setdefault("pointed", set())
    credit_pushed: dict[str, int] = state.setdefault("credit_pushed", {})
    strangers = strangers or {}
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return ""
        quests = session.query(Quest).all()
        completed = {q.id for q in quests if q.status == QuestStatus.COMPLETED}
        active = {q.id for q in quests if q.status == QuestStatus.ACTIVE}
        # (a) a payoff since the last reply opens a window; new work closes it
        if state["completed"] is not None and completed - state["completed"]:
            state["payoff"] = OFFER_AFTER_PAYOFF
        if state["active"] is not None and active - state["active"]:
            state["payoff"] = 0
        state["completed"], state["active"] = completed, active
        # A job a later act's beat hands out waits for that act (fleet6-wenna:
        # act-3 "A Letter for a Buried Man" pitched in act 2)
        try:
            from src.services.campaign import later_act_quest_ids

            kept = later_act_quest_ids(session, player_id)
        except Exception:  # the spine unreadable: offer as before
            kept = set()
        open_work = [q for q in quests if q.status == QuestStatus.NOT_STARTED
                     and q.assigned_by_npc_id and (q.title or "").strip() and q.id not in kept]
        # The giver's own lines in the last reply offered it: the pay named,
        # or the work asked for in its own words (fleet5-bram T0: "I need
        # someone … eyes on Wolf's Hollow"). Speaking was never enough (see
        # the block comment)
        if last_reply:
            for q in open_work:
                try:
                    said = "\n".join(_giver_lines(session, q.assigned_by_npc_id, last_reply))
                    if said and job_offers.pitched(session, q, said):
                        state["said"].add(q.id)
                except Exception:  # an unreadable reply only costs a repeated push
                    pass
        offerable = [q for q in open_work
                     if q.id not in state["said"] and state["pushed"].get(q.id, 0) < OFFER_MAX]
        # (d) work the player already did on a job nobody offered them
        try:
            credit = job_offers.credited(player_id)
        except Exception as e:  # the old pushes still run
            import logging

            logging.getLogger(__name__).warning(f"credited work unreadable: {e}")
            credit = {}

        # (b) how many replies each person here has already shared with the player
        before = state["streak"]
        state["streak"] = {npc_id: before.get(npc_id, 0) + 1 for npc_id in present}
        payoff = state["payoff"] > 0
        if payoff:
            state["payoff"] -= 1
        asked = bool(_ASKS_FOR_WORK.search(player_input or ""))
        # ...offered crediting it, never as fresh work: a plain push of a job
        # the player already worked asks them to "ask Sorrel" again. The
        # player asking for work, or a payoff, still gets it past a firing
        # beat or the push cap
        worked_here = [q for q in open_work if credit.get(q.id) and q.assigned_by_npc_id in present
                       and (credit_pushed.get(q.id, 0) < OFFER_MAX or asked or payoff)]

        def willing(npc_id: str) -> NPC | None:
            npc = session.get(NPC, npc_id)
            if npc is None or npc.status != "alive":
                return None
            try:
                if standing.conversation_refusal(session, player, npc,
                                                 standing.relationship_for(session, player_id, npc_id)):
                    return None
            except Exception:
                pass
            return npc

        here = [q for q in offerable if q.assigned_by_npc_id in present and not credit.get(q.id)]
        if payoff or asked:
            picks = here
        elif not beat_firing:
            # ...and a giver the player was pointed to offers on arrival
            picks = [q for q in here if before.get(q.assigned_by_npc_id, 0) >= OFFER_STREAK or q.id in pointed]
        else:
            picks = []
        picks.sort(key=lambda q: (q.assigned_by_npc_id != addressed_npc_id,
                                  -before.get(q.assigned_by_npc_id, 0)))
        # The work already done comes first: it is owed an answer
        worked_here.sort(key=lambda q: q.assigned_by_npc_id != addressed_npc_id)
        quest = None if beat_firing and not (payoff or asked) else next(
            (q for q in worked_here if willing(q.assigned_by_npc_id)), None)
        if quest is not None:
            credit_pushed[quest.id] = credit_pushed.get(quest.id, 0) + 1
            return _credited_offer_line(session, player, quest, credit[quest.id], strangers)
        quest = next((q for q in picks if willing(q.assigned_by_npc_id)), None)
        away = None
        if quest is None and payoff and player.current_location_id:
            # Nobody here has work, but someone a step inside does (Maera
            # behind the bar of the inn off the square)
            inside = _places_inside(session, player.current_location_id)
            for q in offerable:
                giver = session.get(NPC, q.assigned_by_npc_id)
                if giver is not None and giver.current_location_id in inside and willing(giver.id):
                    quest, away = q, session.get(Location, giver.current_location_id)
                    break
        if quest is None:
            return ""
        if away is None:
            state["pushed"][quest.id] = state["pushed"].get(quest.id, 0) + 1
        else:
            pointed.add(quest.id)
        giver = session.get(NPC, quest.assigned_by_npc_id)
        pay = _reward_words_plain(session, quest.rewards)
        role = strangers.get(giver.id)
        if role is None and away is not None:
            from src.services.dialogue import player_has_met, stranger_label

            role = stranger_label(giver, met=player_has_met(session, player_id, giver.id))
        who = (f"{role if role.lower().startswith('the ') else 'the ' + role} ({giver.name} — a name the "
               "player has not learned: keep the role label)") if role else giver.name
        pays = f" (it pays {pay})" if pay else ""
        if away is not None:
            # Nobody here to say it: a lit window, not a passer-by the brief never had
            hint = ("someone here mentions they have been asking for help" if present else
                    "a sign of them from where the player stands (a lit window, a voice through the door), "
                    "never a passer-by you invent")
            return (f"[OFFER THIS REPLY] {who}, a step away at {away.name}, has work: \"{quest.title}\"{pays}. "
                    f"Point the player to it this reply — {hint} — "
                    "and they offer it when the player goes to them; @quest-start it when the player accepts.")
        steps = job_offers.job_steps(session, quest, player)
        return (f"[OFFER THIS REPLY] {who} has work: \"{quest.title}\"{pays}."
                + (f" The job, as written: {steps}." if steps else "")
                + " Have them offer it in their own voice this reply, naming the pay"
                + ("; what they ask for is these steps, and nothing they say contradicts them (no other room, no "
                   "key, no rule of their own)" if steps else "")
                + f"; write @quest-start {quest.title} only when the player accepts.")


def _credited_offer_line(session, player, quest, done: list[dict[str, str]], strangers: dict[str, str]) -> str:
    """The [OFFER THIS REPLY] line for a job the player already worked
    unasked (job_offers.credited): the giver offers it crediting what they
    did — fleet9-bram stood the howl for Piper's job nobody had offered."""
    from src.models import NPC
    from src.services import job_offers

    giver = session.get(NPC, quest.assigned_by_npc_id)
    role = strangers.get(giver.id) if giver is not None else None
    who = (f"{role if role.lower().startswith('the ') else 'the ' + role} ({giver.name} — a name the "
           "player has not learned: keep the role label)") if role else (giver.name if giver else "Its giver")
    pay = _reward_words_plain(session, quest.rewards)
    did = "; ".join(f"\"{d['text']}\"" for d in done)
    left = job_offers.job_steps(session, quest, player, skip=[d["id"] for d in done])
    return (f"[OFFER THIS REPLY] {who} has work: \"{quest.title}\"" + (f" (it pays {pay})" if pay else "")
            + f" — and the player ALREADY did part of it, unasked: {did}. Have them offer it this reply in their "
            "own voice, crediting that work as done — what the player did, not a recap of it (\"you already "
            "did that — tell me what you found, and it's yours\") — and naming the pay"
            + (f"; still to do: {left}" if left else "")
            + f". Write @quest-start {quest.title} only when the player accepts; the engine ticks what they "
            "already did.")


def _reward_words_plain(session, rewards: Any) -> str:
    """'20 coin and standing with the Lantern Watch' — a price as said aloud."""
    from src.tools.world_write.quests import reward_factions, reward_words

    return reward_words(rewards, factions=reward_factions(session, rewards))


class DMOrchestrator(BaseGameAgent):
    """The main Dungeon Master agent that orchestrates the game."""

    AGENT_NAME = "dm_orchestrator"
    DEFAULT_TOOLS = DM_TOOLS

    def __init__(self, context_or_player_id: AgentContext | str, callback_handler: Any = None):
        """Initialize the DM.

        Args:
            context_or_player_id: Either an AgentContext or player_id string.
            callback_handler: Optional callback handler for tool tracking.
                             Only used if context_or_player_id is a string.
        """
        if isinstance(context_or_player_id, str):
            context = AgentContext(
                player_id=context_or_player_id,
                session_id=context_or_player_id,
                callback_handler=callback_handler,
            )
        else:
            context = context_or_player_id

        self._world_context = self._load_world_context()
        self._player_profile = self._load_player_profile(context.player_id)
        self._player_name = self._player_profile.get("name", "Unknown")
        # What the last context build learned about the scene — the server
        # resolves speaker lines and refusals against it
        self.last_brief: dict[str, Any] = {}

        super().__init__(context)

        self.player_id = context.player_id
        self.callback_handler = context.callback_handler

    def _load_world_context(self) -> str:
        """Load world context for the system prompt."""
        world_context = get_world_bible_for_dm()
        if not world_context or "No World Bible" in world_context:
            return ""
        return f"\n\n### WORLD CONTEXT\n{world_context}"

    def _load_player_profile(self, player_id: str) -> dict[str, Any]:
        player_data = get_player(player_id)
        return player_data if isinstance(player_data, dict) and "error" not in player_data else {}

    def _get_session_id(self) -> str:
        """DM uses player_id as session ID (maintains history across sessions)."""
        return self.context.player_id

    def _conversation_setup(self):
        return TurnWindowManager(on_trim=chronicle_hand_off(self.context.player_id)), []

    def _after_history_loaded(self, agent) -> None:
        compact_loaded_history(getattr(agent, "messages", None) or [])

    def _build_system_prompt(self) -> str:
        """Build the DM system prompt with world context and who the PC is.

        Read from the markdown files on every agent build, so editing
        prompts/dm.md or prompts/commands.md takes effect on the next session.
        """
        profile = self._player_profile
        prompt = load_dm_prompt() + self._world_context
        prompt += (
            f"\n\n### THE PLAYER CHARACTER\nplayer_id: {self.context.player_id}\n"
            f"Name: {self._player_name}\n"
        )
        if profile.get("description"):
            prompt += f"Looks: {profile['description']}\n"
        if profile.get("background"):
            prompt += f"Background (theirs — build on it, never replace it): {profile['background']}\n"
        return prompt

    def _player_block(self) -> list[str]:
        from src.models import Player as _Player, get_session as _get_session

        player = get_player(self.context.player_id) or {}
        with _get_session() as session:
            row = session.get(_Player, self.context.player_id)
            coin = int(row.currency or 0) if row is not None else 0
        lines = ["### THE PLAYER"]
        gear = ", ".join(f"{slot}: {name}" for slot, name in (player.get("equipped") or {}).items() if name) \
            or "nothing notable"
        lines.append(
            f"- {player.get('name', self._player_name)}: {player.get('health_status', 'healthy')}, "
            f"level {player.get('level', 1)}, {coin} coin. Carries: {gear}."
        )
        lines.extend(appearance_lines(self.context.player_id))
        inventory = player.get("inventory") or []
        items = ", ".join(
            f"{i.get('name', i)} x{i.get('quantity', 1)}" if isinstance(i, dict) else str(i)
            for i in inventory[:15]
        ) or "empty"
        lines.append(f"- Pack: {items}")
        return [line if line.startswith(("- ", "###")) else f"- {line.lstrip('- ').strip()}" for line in lines]

    def _quest_block(self) -> list[str]:
        quests = get_active_quests()
        lines = []
        for q in quests or []:
            if not isinstance(q, dict):
                continue
            # A custom step has no target the engine can watch for: when it
            # happens in the fiction, only the DM can mark it (`@step-done`)
            marks = " · ".join(
                ("✓ " if o.get("done") else "◦ ") + str(o.get("text") or "")
                + (" [custom — tick it yourself: @step-done when it happens]"
                   if o.get("kind") == "custom" and not o.get("done") else "")
                + (f" [ticks when they hold an item named exactly '{o.get('item_name')}']"
                   if o.get("kind") == "obtain" and o.get("item_name") and not o.get("done") else "")
                # A hand-over said only in prose never ticks: the seed's
                # whistle proof needs the item to change hands in code
                + (f" [ticks when you write @hand {o.get('item_name')} | to: {o.get('target_name')}]"
                   if o.get("kind") == "deliver" and o.get("item_name") and o.get("target_name")
                   and not o.get("done") else "")
                for o in (q.get("objectives") or []) if isinstance(o, dict)
            )
            lines.append(f"- {q.get('title', '?')} [{q.get('id', '?')}]" + (f" — {marks}" if marks else ""))
            reward = _reward_words(q.get("rewards"))
            if reward:
                lines.append(f"    Reward, paid by the engine on @quest-done (never add @coin/@give for it): {reward}")
        return ["### ACTIVE QUESTS"] + (lines or ["- none"])

    def _build_context(self, player_input: str, canvas_notes: str = "",
                       addressed_npc_id: str | None = None,
                       command_ledger: list[str] | None = None,
                       extra_blocks: list[str] | None = None,
                       constrained: bool = False) -> str:
        """Everything the DM needs this turn, assembled in code.

        Args:
            player_input: What the player typed this turn.
            canvas_notes: The `[SINCE YOUR LAST TURN]` block from
                `pending_actions.format_pending_actions`.
            addressed_npc_id: The NPC the player clicked/addressed, if any.
            command_ledger: How last turn's `@commands` landed — failures and
                notable engine answers (a refused join, a blocked door). The
                DM could not see them while writing; it corrects course now.
            extra_blocks: The server's own blocks (the reality audit of the
                last turn, engine notes, the NPC being addressed). They sit
                BEFORE "Player says:" — that marker ends every context, and
                the history compactor keeps only what follows it.
            constrained: The server handed this reply an engine directive
                among `extra_blocks` (an ambush: one sentence, nothing else).
                An engine-authored input ([OPENING], [COMBAT OUTCOME]) is
                read off `player_input`. Either way the world tick spends no
                delivery attempt on it, and no work/budget order is added.
        """
        import logging
        from src.models import Player as _Player, get_session as _get_session
        from src.services import party as party_mod
        from src.services.lead import compute_lead, format_lead_context
        from src.services.scene_brief import build_scene_brief

        log = logging.getLogger(__name__)

        # The party first: a beat that moved or killed a member parts them,
        # and the brief must see the party as it is now
        party_gone: list[str] = []
        lead_block = ""
        brief = {"text": "", "present": {}, "refusal": None}
        chronicle = ""
        try:
            with _get_session() as session:
                row = session.get(_Player, self.context.player_id)
                gone = party_mod.sync(session, row)
                if gone:
                    session.commit()
                party_gone = party_mod.departure_lines(gone)
                try:
                    lead_block = format_lead_context(compute_lead(session, row))
                except Exception as e:  # never let the compass cost the turn
                    log.warning(f"lead context unavailable: {e}")
                # What they typed: a question touching a gated secret
                # (scene_brief._within_reach, fleet10-wenna T7)
                brief = build_scene_brief(session, row, addressed_npc_id=addressed_npc_id,
                                          player_input=player_input or "", engine_reply=constrained)
        except Exception as e:
            log.warning(f"scene brief unavailable: {e}", exc_info=True)
        self.last_brief = brief

        # The DM's own long memory: the chronicle of trimmed turns and its
        # scratchpad, both markdown files (src/services/memory_files.py)
        dm_notes = ""
        journal: list[str] = []
        try:
            from src.services import memory_files

            chronicle = memory_files.read_chronicle(self.context.player_id).strip()
            dm_notes = memory_files.read_dm_notes(self.context.player_id).strip()
            journal = memory_files.read_journal(self.context.player_id, limit=15)
        except Exception as e:
            log.warning(f"memory files unavailable: {e}")

        # World tick (facts) → canvas ledger (already applied) → campaign
        # (imperative). The server appends the [REALITY AUDIT] block after.
        engine_input = _ENGINE_INPUT.match(player_input or "")
        # A fight's aftermath still shows what happens where the player
        # stands (fleet10-bram T17-T18: the howl at his feet went by)
        tick_result = run_world_tick(
            self.context.player_id, constrained=constrained or bool(engine_input),
            aftermath=not constrained and (player_input or "").lstrip().startswith("[COMBAT OUTCOME"))
        world_briefing = format_world_tick_context(tick_result)
        # An ambush's one sentence carries no beat (fleet8-bram T4)
        if constrained:
            from src.services.campaign import hold_beats_this_reply
            hold_beats_this_reply(self.context.player_id)
        campaign_briefing = format_campaign_context(run_campaign_tick(self.context.player_id))
        if not campaign_briefing:
            # No live spine (missing / still authoring / failed): one line
            # so the DM holds bosses back and schedules the threats it voices
            campaign_briefing = no_spine_context(self.context.player_id)

        # Where the player said they go, this reply goes (fleet7-tamsin T20:
        # no @move; fleet7-wenna T20: a healer that did not exist, and a @move
        # into the tavern instead)
        go_there = None
        # ...unless a danger shows itself on the way out: it stops them there,
        # and no travel line (nor the engine's walk after it) carries them on
        from src.services.encounters import SHOWN_FIRST_MARK
        stopped = any(str(b).startswith(SHOWN_FIRST_MARK) for b in extra_blocks or ())
        if not (constrained or engine_input or stopped):
            from src.services import travel_intent
            go_there = travel_intent.directive(self.context.player_id, player_input)
        # ...and a beat firing as they set out goes with them (fleet8-wenna T3)
        if go_there and campaign_briefing:
            from src.services.campaign import departure_note, with_departure
            leaving = departure_note(self.context.player_id, player_input)
            if leaving:
                campaign_briefing = with_departure(campaign_briefing, leaving)
        # ...and the player asking a beat's person about it is answered now
        # (fleet10-wenna T7: the exact question got a denial)
        if campaign_briefing and not (constrained or engine_input or go_there):
            from src.services.campaign import asked_note
            asked = asked_note(self.context.player_id, player_input, addressed_npc_id)
            if asked:
                campaign_briefing = f"{campaign_briefing}\n{asked}"
        # Authored work someone here should offer NOW (see offer_directive) —
        # never as the player walks off (fleet7-wenna T20: the pitch pulled
        # her to the quest-giver instead of the healer); it waits a reply
        offer = ""
        if not (constrained or engine_input or go_there):
            try:
                offer = offer_directive(
                    self.context.player_id, brief.get("present") or {}, player_input,
                    last_reply_text(getattr(self, "_agent", None)),
                    strangers=brief.get("strangers") or {}, addressed_npc_id=addressed_npc_id,
                    beat_firing="THIS TURN — MUST DELIVER" in (campaign_briefing or ""),
                )
            except Exception as e:  # never let the nudge cost the turn
                log.warning(f"offer directive unavailable: {e}")
        # A job its giver pitched and the player took — in words or by setting
        # out on its first step (fleet7-bram T5 "Alright. I'll talk to him",
        # never started; src/services/job_offers.py). It outranks a pitch of
        # the same job: that one is already said
        start_job = ""
        if not (constrained or engine_input):
            try:
                from src.services import job_offers

                start_job = job_offers.start_directive(self.context.player_id, player_input,
                                                       addressed_npc_id=addressed_npc_id)
            except Exception as e:  # never let the bookkeeping cost the turn
                log.warning(f"start-this-job line unavailable: {e}")
        if start_job and offer:
            titles = re.findall(r"@quest-start (.+?) — ", start_job)
            if any(f'"{t}"' in offer for t in titles):
                offer = ""
        # What the player reaches for (src/services/intents.py): the thing a
        # person held out, a room, a page, a thing left where it was — and who
        # carries what a quest step asks for (fleet9: the Gate Pass and Piper's
        # chart taken in words and never given, the inn's ledger unread). Before
        # the player block: leaving the rope behind changes the pack
        from src.services import intents
        reach = intents.context_lines(
            self.context.player_id, player_input, last_reply_text(getattr(self, "_agent", None)),
            brief.get("present") or {}, brief.get("strangers") or {},
            engine_turn=bool(constrained or engine_input), travelling=bool(go_there))

        sections = ["Current context:"]
        # A refused proof the prose kept claiming, settled before anything
        # else is written (fleet8-wenna T13-T20; src/services/settle.py).
        # Not in a one-sentence ambush reply: it waits for the next
        from src.services import settle
        settle_first = "" if constrained else settle.settle_line(self.context.player_id)
        if settle_first:
            sections.append(settle_first)
        sections.append("\n".join(self._player_block()))
        quest_lines = self._quest_block()
        quest_section = "\n".join(quest_lines) + (f"\n{lead_block}" if lead_block else "")
        # Nothing on the quest list is a lead gap of its own: fleet4-tamsin
        # played nine turns after her payout with no quest while NPCs
        # re-confirmed what she knew. Not on an engine turn — the opening
        # offers work itself, and an aftermath or ambush is about the fight.
        # An [OFFER THIS REPLY] line already names the work, exactly.
        if quest_lines[-1:] == ["- none"] and not (constrained or engine_input) and not (offer or start_job):
            quest_section += "\n" + NO_QUEST_ORDER
        sections.append(quest_section)
        if brief.get("text"):
            sections.append(brief["text"])
        sections.extend(reach["facts"])
        if party_gone:
            sections.append("\n".join(f"- Party change: {line}" for line in party_gone))
        if chronicle:
            sections.append(f"### THE STORY SO FAR (older turns, condensed)\n{chronicle}")
        if journal:
            sections.append("### WHAT THE CHARACTER KNOWS (their journal — @journal adds to it)\n"
                            + "\n".join(f"- {entry}" for entry in journal))
        if dm_notes:
            sections.append(f"### YOUR NOTES (threads you meant to pay off — @plan adds to them)\n{dm_notes}")
        if command_ledger:
            sections.append(
                "### HOW YOUR LAST COMMANDS LANDED (the engine's answers — if one failed, the fiction "
                "you wrote around it is wrong: correct course this turn, quietly)\n"
                + "\n".join(f"- {line}" for line in command_ledger)
            )
        for block in (world_briefing, canvas_notes, campaign_briefing, *(extra_blocks or [])):
            if block and block.strip():
                sections.append(block.strip())
        # A world with teeth (src/services/complications.py): after a quiet
        # stretch the engine hands this reply one complication — never on an
        # engine turn, in a parley, or when a beat, an event or an offer of
        # work owns the reply (it waits; the quiet count is kept)
        from src.services import complications
        complication = None if offer else complications.directive_line(
            self.context.player_id, engine_turn=bool(constrained or engine_input), world_tick=tick_result,
            campaign_briefing=campaign_briefing, canvas_notes=canvas_notes,
            # a watch the player keeps is served or waited out (fleet8-tamsin T8)
            player_input=player_input,
        )
        if complication:
            sections.append(complication)
        # Stakes that act (src/services/complications.py, stakes.py): the
        # complication nobody answered, pressed once (fleet6-tamsin T17-T20);
        # the main quest's open road left untaken too long (fleet6-tamsin
        # held the gate pass from T2 to T20)
        engine_turn = bool(constrained or engine_input)
        unanswered = complications.unanswered_line(
            self.context.player_id, engine_turn=engine_turn, world_tick=tick_result,
            campaign_briefing=campaign_briefing, canvas_notes=canvas_notes,
        )
        if unanswered:
            sections.append(unanswered)
        from src.services import stakes
        # One push from the world per reply: a complication already landing
        # here holds the waiting clock's consequence to the next (it keeps
        # counting)
        waited = stakes.waited_line(
            self.context.player_id, engine_turn=engine_turn, player_input=player_input,
            owned=bool(offer or complication or unanswered) or complications.reply_owned(
                self.context.player_id, world_tick=tick_result, campaign_briefing=campaign_briefing,
                canvas_notes=canvas_notes),
        )
        if waited:
            sections.append(waited)
        if offer:
            sections.append(offer)
        if start_job:
            sections.append(start_job)
        # A finished job paid in the scene that earned it, and a payout made
        # apart from its giver brought to the player (fleet8-wenna T16-T18,
        # fleet8-tamsin T11-T12: both sessions' payoffs happened off screen)
        if not (constrained or (player_input or "").lstrip().startswith("[OPENING]")):
            sections.extend(settle.pay_lines(self.context.player_id, brief.get("present") or {},
                                             brief.get("strangers") or {}))
        # roll_check reads what the player typed: a social roll on a plain
        # question is no roll (fleet9-bram T1, tools/world_write/progression)
        from src.tools.world_write.progression import note_turn_input
        note_turn_input(self.context.player_id, "" if engine_turn else player_input)
        # The dice before the prose (fleet6-bram: prose_before_roll on 3 of
        # 3 rolls): the player named a risky act, so the roll comes first
        if not engine_turn:
            from src.services import encounters, roll_intent
            # A beast in the room: "I back away from it" is the stand-off too
            roll_first = roll_intent.directive(player_input,
                                               beast_here=encounters.beast_here(self.context.player_id))
            if roll_first:
                sections.append(roll_first)
            if go_there:
                sections.append(go_there)
        sections.extend(reach["binding"])
        # Recency beats the system prompt: too_talky is the audit's most
        # frequent warning and the [REALITY AUDIT] alone never fixed it
        # (fleet4-bram T19-T20 flagged back to back). The opening sets its
        # own length and an ambush is one sentence, so neither gets it.
        if not constrained and not (player_input or "").lstrip().startswith("[OPENING]"):
            sections.append(REPLY_BUDGET)
        sections.append(f"Player says: {player_input}")
        return "\n\n".join(sections)

    def process_input(self, player_input: str) -> str:
        """Process player input and generate a response."""
        return self.process(player_input)

    def get_conv_state(self) -> dict[str, Any]:
        """Get the current conversation state."""
        if self.agent.conversation_manager:
            return self.agent.conversation_manager.get_state()
        return {}

    def describe_scene(self) -> str:
        """The current location's description (CLI start screen)."""
        location = get_current_location(self.context.player_id)
        if "error" in location:
            return "You find yourself... somewhere. The details are unclear."
        clock = get_world_clock()
        npc_names = [n["name"] for n in location.get("npcs_present", [])]
        text = f"**{location['name']}** ({clock.get('time_of_day', 'day')})\n\n{location['description']}"
        if npc_names:
            text += f"\n\n*You see: {', '.join(npc_names)}*"
        return text
