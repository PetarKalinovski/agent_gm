"""The DM's instructions and the engine must agree on what the DM can do.

The original bug this file guarded: the prompt commanded ~8 tools that were
never registered, so consequences could not happen. The DM now has ONE tool
(roll_check) and a command language; the same guarantee is that every
`@command` the instructions teach is one the engine actually runs, and that
no instruction still names a tool the DM no longer has.
"""

import inspect
import re
from types import SimpleNamespace

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import DM_TOOLS, PROMPTS_DIR, DMOrchestrator, load_dm_prompt
from src.services.commands import COMMANDS

# Tools the DM had before 2026-10-04; naming any of them in its instructions
# would send it looking for a call it cannot make
RETIRED_TOOLS = (
    "update_player_health", "add_to_inventory", "remove_from_inventory", "adjust_currency",
    "transfer_item", "use_item", "move_player", "move_npc", "advance_time", "kill_npc",
    "start_combat", "join_party", "leave_party", "update_npc_relationship", "update_npc_mood",
    "update_player_reputation", "activate_quest", "update_quest_status", "create_quest",
    "update_quest_objectives", "update_dm_state", "schedule_world_event", "create_event",
    "play_cinematic", "stock_merchant", "reveal_secret", "prompt_npc_agent",
    "prompt_creator_agent", "speak", "narrate", "describe_location", "get_current_location",
    "get_npcs_at_location", "get_available_destinations", "get_world_state_summary",
)


def _verbs(text: str) -> set[str]:
    return {v.lower().replace("_", "-") for v in re.findall(r"(?m)(?:^|[`\s(])@([A-Za-z][\w-]*)", text)}


def _canon(verb: str) -> str:
    return verb.lower().replace("_", "-")


def test_the_dm_has_exactly_one_tool():
    names = {getattr(t, "tool_name", None) or getattr(t, "__name__", str(t)) for t in DM_TOOLS}
    assert names == {"roll_check"}


def test_every_taught_command_exists():
    known = {_canon(v) for v in COMMANDS}
    taught = _verbs(load_dm_prompt())
    unknown = taught - known
    assert not unknown, f"the DM's instructions teach commands the engine does not run: {sorted(unknown)}"


def test_every_command_is_documented():
    documented = _verbs((PROMPTS_DIR / "commands.md").read_text(encoding="utf-8"))
    missing = {_canon(v) for v in COMMANDS} - documented
    assert not missing, f"commands.md never documents: {sorted(missing)}"


def test_no_retired_tool_is_named_in_the_instructions():
    prompt = load_dm_prompt()
    named = [t for t in RETIRED_TOOLS if re.search(rf"`{t}`|\b{t}\(", prompt)]
    assert not named, f"the DM's instructions still name retired tools: {named}"


def _stub_context_sources(monkeypatch, *, campaign=""):
    """No DB, no LLM — just the seams `_build_context` reads."""
    monkeypatch.setattr(dm_orchestrator, "get_player",
                        lambda pid: {"name": "Ilse Varrow", "health_status": "winded", "inventory": [],
                                     "level": 4, "equipped": {"weapon": "Iron Sword (fine)"}})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *args, **kwargs: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context",
                        lambda tick: "### WORLD STATE (pre-turn briefing)")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda tick: campaign)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Ilse Varrow")
    fake._player_block = lambda: ["### THE PLAYER", "- Ilse Varrow: winded, level 4, 0 coin."]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    return fake


def test_build_context_seams():
    """FROZEN SEAMS: server.py passes these by name."""
    params = inspect.signature(DMOrchestrator._build_context).parameters
    for name in ("canvas_notes", "addressed_npc_id", "command_ledger", "extra_blocks"):
        assert name in params


def test_build_context_orders_blocks_and_ends_with_the_player(monkeypatch):
    fake = _stub_context_sources(monkeypatch, campaign="### CAMPAIGN: The Long Howl")
    context = DMOrchestrator._build_context(
        fake, "I look around",
        canvas_notes="[SINCE YOUR LAST TURN the player acted on the canvas.]",
        command_ledger=["@join Piper: refused — she does not trust you enough"],
        extra_blocks=["[REALITY AUDIT of your previous turn]"],
    )
    assert (context.index("### THE PLAYER")
            < context.index("HOW YOUR LAST COMMANDS LANDED")
            < context.index("### WORLD STATE")
            < context.index("[SINCE YOUR LAST TURN")
            < context.index("### CAMPAIGN:")
            < context.index("[REALITY AUDIT")
            < context.index("Player says:"))
    assert context.rstrip().endswith("Player says: I look around")


def test_build_context_omits_empty_blocks(monkeypatch):
    fake = _stub_context_sources(monkeypatch)
    context = DMOrchestrator._build_context(fake, "I look around")
    assert "SINCE YOUR LAST TURN" not in context
    assert "### CAMPAIGN" not in context
    assert "HOW YOUR LAST COMMANDS LANDED" not in context


def test_prompt_examples_use_no_real_world_names(tmp_path):
    """Examples are read as facts. The @journal example once quoted the
    playtest world's SEALED villain truth and the DM repeated it in Act 1."""
    from src.config import set_runtime_db_path
    from src.data.playtest_seed import create_playtest_world
    from src.models import NPC, Location, get_session, init_db

    db = tmp_path / "seed.db"
    create_playtest_world(str(db))
    set_runtime_db_path(str(db))
    init_db(str(db))
    with get_session() as session:
        names = [n.name for n in session.query(NPC).all()] + [l.name for l in session.query(Location).all()]
    prompt = load_dm_prompt()
    # A compass word names nothing ("North Gatehouse" is the seed's; "North
    # Bridge" in an example is no copy of it)
    compass = {"North", "South", "East", "West"}
    words = {w for name in names for w in re.findall(r"[A-Z][a-z']{3,}", name)} - compass
    leaked = sorted(w for w in words if re.search(rf"\b{re.escape(w)}\b", prompt))
    assert not leaked, f"prompt examples name things from the Emberfall seed: {leaked}"


def test_prompt_examples_use_no_seed_props(tmp_path):
    """Round-3 autoplay: the example `@give Gate Pass ... north gate` was
    copied nearly verbatim on a seed whose first objective IS that pass, and
    `@interact Split supply crate` named a real Wolf's Hollow object. Props,
    quest titles and items from the seed never appear in the examples."""
    from src.config import set_runtime_db_path
    from src.data.playtest_seed import create_playtest_world
    from src.models import NPC, Location, Quest, get_session, init_db

    db = tmp_path / "seed.db"
    create_playtest_world(str(db))
    set_runtime_db_path(str(db))
    init_db(str(db))
    phrases: set[str] = {"gate pass", "north gate"}
    with get_session() as session:
        for loc in session.query(Location).all():
            phrases |= {str(o.get("label") or "") for o in (loc.objects or [])}
        phrases |= {q.title for q in session.query(Quest).all()}
        for npc in session.query(NPC).all():
            for item in npc.inventory_notable or []:
                phrases.add(item.get("name", "") if isinstance(item, dict) else str(item))
    prompt = load_dm_prompt().lower()
    leaked = sorted(p for p in phrases if len(p.split()) >= 2 and p.lower() in prompt)
    assert not leaked, f"prompt examples reuse props from the Emberfall seed: {leaked}"
