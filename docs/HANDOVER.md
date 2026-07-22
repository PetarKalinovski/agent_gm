# Forge — Handover & Roadmap

_Last updated: 2026-07-22. Companion to `CLAUDE.md` (architecture reference);
this doc is the narrative: where the project is, why it's built this way,
and what's next._

## The vision

Forge is an LLM-driven RPG **generator** — the DM, world, NPCs, and assets are
generated — evolving from "text RPG with a map" into a **cinematic game**:
adaptive music, AI voice acting, cutscene presentation, and real action
gameplay (Hades-like top-down combat in the world itself, no separate battle
screens). The core rule that makes this feasible: **code enforces, LLM
narrates.** Mechanics (time, combat hitboxes, quest rewards, damage) live in
deterministic code; the LLM skins and narrates them. The LLM is never in a
real-time loop.

## State of the world (July 2026)

### Landed and working

| Area | Summary | Key files |
|---|---|---|
| DM + consequence layer | DM orchestrator with health/inventory/reputation/combat/quest tools; minimum time cost per turn; tension auto-escalation; scheduled events re-surface until narrated; permadeath + rebirth in a persistent world | `src/agents/dm_orchestrator.py`, `src/services/world_tick.py` |
| Offscreen world sim | World advances once per in-game day in the background (faction goals, NPC moves, future events) | `src/services/world_simulation.py` |
| Audio layer | Per-world Suno music palette (6 moods) + ElevenLabs/Qwen3-TTS NPC voices; structured SSE events (`speech`, `scene`, `tension`); frontend crossfade + voice queue with ducking | `src/services/music_generator.py`, `src/services/voice_generator.py`, `src/web/static/js/audio.js` |
| Walk animation | 6-frame filmstrip cycles, ONE Gemini call per direction, right mirrored from left; artifact-immune slicing; normalized to idle framing | `src/services/image_generator.py` (`generate_walk_cycle`) |
| Game feel | Ground shadows, walk bob/lean, footstep dust, camera smoothing + look-ahead, day/night tint | `src/web/static/js/game.js` |
| Collision | `Location.obstacles` polygons; auto-detected from backgrounds via vision model (footprint = bottom slice of box); in-game editor (Ctrl+E → C); point-in-polygon movement blocking | `image_generator.detect_obstacles`, `game.js` |
| Ambient NPCs | Client-side wandering near home position, pause near player | `game.js` (`_updateNpcWander`) |
| Combat gray-box | Ctrl+K test enemy: dodge (Space, i-frames), melee arc (J), telegraphed enemy lunge, hitstop/knockback/screenshake, exits lock | `game.js` (`_updateCombat`) |

### Explicitly NOT done yet

- **No real playtest of the audio layer.** The Suno/ElevenLabs code paths are
  ported verbatim from a working project and unit-tested, but no live session
  has generated a palette or voice line end-to-end in the browser.
- **Combat is a prototype.** No archetype library, no real enemies (only the
  Ctrl+K gray box), no DM integration, no combat animations.
- **No cutscene mode, no "video".** Decision on record: no generated video —
  cinematics will be letterboxed stills + Ken Burns + music + TTS.
- **Old characters still have 2-frame walk sets** until their missing frames
  404 in the frontend, which auto-triggers regeneration in the new format.

## Design decisions on record (don't relitigate casually)

1. **¾ top-down in-world combat, not side view, not arena scenes.** Side view
   was evaluated: saves ~3× animation volume but costs platformer physics, an
   arena pipeline, and dual enemy representations. The user explicitly wants
   "what exists in the world becomes combat."
2. **Combat mechanics are a fixed archetype library; the LLM only skins.**
   Hitboxes, timings, i-frames are hand-tuned constants. A "melee_rusher" is a
   wolf in fantasy, a drone in sci-fi — same numbers.
3. **Combat runs 100% client-side at 60fps.** The DM sets up encounters and
   narrates results afterward. Never mid-fight.
4. **Frames that must be consistent are generated in ONE image.** Independent
   AI generations morph (style/scale drift). This applies to any future
   animation work (attack cycles, etc.) — extend the filmstrip technique.
5. **Structured SSE events ride tool-result dicts** (the `npc_death`/`speech`/
   `scene` mechanism in `ToolUsageTracker._parse_tool_output_for_events`).
   New event types should follow the same pattern.

## Gotchas & operational notes

- **Gemini model names rot.** `gemini-2.5-flash` still appears in ListModels
  but 404s ("no longer available to new users"). Current: `vision_model:
  gemini-3.5-flash`, image gen `gemini-2.5-flash-image` (verify it stays
  alive). `API_KEY_INVALID` can mean *key restrictions*, not a wrong key —
  restrict the key to the Generative Language API in Google Cloud console.
- **Gemini decorates sprite sheets unpredictably** (grid lines, ground lines,
  letterbox cards) no matter what the prompt says. The slicer in
  `_slice_walk_strip` is immune (clears full-span rows/cols); keep it that way.
- **Suno** (via sunoapi.org): submit → poll, 2–5 min per track. Palette
  generation is triggered by the first `/api/assets/music/manifest` call and
  runs in background; total failure trips the failure-cooldown so polling
  doesn't hammer the API.
- **ElevenLabs `eleven_v3` bills per character.** `audio.max_tts_chars: 900`
  in `config/settings.yaml` is the cost guard. TTS applies to `speak()`
  dialogue only, not narration.
- **Qwen3-TTS local server** (free voice cloning): needs a reference clip at
  `data/assets/{world}/voices/refs/{npc_id}.wav` AND the server on
  `localhost:8002` (start from `C:\Users\Dell\Documents\qwen3-tts`).
- **API keys** live in `.env`: OPENROUTER, GEMINI, ANTHROPIC, SUNO,
  ELEVENLABS. The telltale repo (`C:\Users\Dell\Documents\loka\telltale`) was
  the source for the audio code and shares the same keys.
- **Schema changes**: nullable columns only — `src/models/base.py` auto-adds
  them to existing SQLite DBs. No migration tool.
- **After changing world_write tools or the DM prompt**: `uv run pytest`
  (`test_dm_prompt_tools.py` enforces prompt↔tools sync).
- **index.html is a 4,300-line monolith with THREE near-duplicate SSE
  handlers** (~lines 1830/2550/4220 — world-create, game play, world-forge).
  Only the game-play one (middle) feeds audio/canvas. Touching SSE means
  checking all three until the long-promised module split happens.

## Quick verification tour

```
uv run main.py web        # localhost:12000
```
1. Enter a world, click once (audio unlock) → music manifest kicks off palette
   generation; tracks fade in as Suno finishes.
2. Talk to an NPC → `speech` SSE event carries `audio_id`; voice plays a few
   seconds behind text; music ducks.
3. Ctrl+E → C → G: auto-detect collision for the room; walk into a table.
4. Ctrl+K: gray-box combat. Space dodge, J attack.
5. HUD 🔊 chip: music/voice volume, mute.

## Roadmap

### Now (validation & feel)
1. **Real playtest session** — the whole audio layer + collision + combat
   gray-box has never met a human player. Expect autoplay quirks, voice
   latency tuning, Suno failures, obstacle false-positives. Fix what falls out.
2. **Combat feel tuning** — dodge distance/cooldown, windup/lunge timings,
   hitstop length (`_updateCombat` constants). The gray-box must feel good
   before any art or integration is layered on.

### Next (combat becomes real)
3. **Archetype library** — data-driven enemy definitions (melee_rusher,
   ranged_skirmisher, heavy, swarm): HP, speed, hitbox radii, windup/active/
   recover ms, damage. Hand-tuned constants in a Python/JSON table served to
   the client.
4. **Hostile NPCs** — a hostility flag (DM tool or world-sim event) turns a
   world NPC into a combatant using an archetype + their existing sprite.
   Death routes through the existing `npc_death` flow.
5. **DM integration** — combat start context from the DM ("three cultists,
   melee_rusher × 3"); results (damage taken, deaths, items) POSTed back and
   summarized into the next DM turn so consequences apply via existing tools.
   Player death plugs into the permadeath/rebirth flow.
6. **Combat animations via filmstrip** — attack/hurt cycles per direction
   using the same one-call technique; procedural VFX (slashes, telegraphs)
   stay code-drawn.

### Then (cinematic layer completes)
7. **Cutscene mode** — letterboxed presentation: Ken Burns pan/zoom over a
   generated still, music sting, optional TTS narration. Triggered by a DM
   tool (`play_cutscene`) for deaths, quest climaxes, world events. This is
   the agreed "video" substitute.
8. **Consistency chain for scene art** — feed the previous location image +
   character refs back into generation (technique from telltale's
   `agents/art/generator.py`) so a world's art stays coherent.
9. **Music stingers** — triumph on quest completion (needs a structured
   quest event via the tool-result mechanism), danger auto-switch on combat.
10. **Narrator voice** (optional TTS for `narrate()`) once cost/latency is
    understood from playtests.

### World depth (parallelizable)
11. **World verbs** — E-to-talk prompt when near an NPC, visible item pickups,
    doors/containers. Moves interaction from chat-first to world-first.
12. **Visible NPC schedules** — world sim already moves NPCs daily; show
    arrivals/departures, maybe walk-in/walk-out at location edges.
13. **In-game map** — Location pin/map fields already exist in the model.

### Tech debt (schedule before frontend work compounds)
14. **index.html module split** — extract shared SSE parsing + panels; the
    three-handler duplication is the biggest foot-gun in the codebase.
15. **Turn dedup on refresh-mid-generation; mid-turn HUD deltas.**
16. **Mobile** — currently unusable.
