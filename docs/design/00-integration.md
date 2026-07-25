# 00 — Integration Architecture: One Buildable Overhaul

**Status:** binding. Where this document and a sibling spec disagree, THIS document wins.
**Inputs:** `progression.md`, `enemy-tiers.md`, `campaign.md`, `canvas.md`, `cinematics.md` (written in parallel, blind to each other).
**Ground rules preserved:** code enforces / LLM narrates; no LLM round-trip on any fast path; SQLite with nullable-column-only migration; `data/emberfall.db` plays byte-identically at level 1 with nothing equipped and every new column NULL.

---

## 0. Rulings (the conflicts, decided)

| # | Conflict | Ruling |
|---|---|---|
| R1 | **Enemy tier column + vocabulary.** progression wants `NPC.enemy_tier` (fodder/regular/elite/champion/boss, XP 15/30/70/150/300); enemy-tiers wants `NPC.danger_tier` (harmless/weak/standard/dangerous/deadly/boss, XP 0/5/15/40/100/300). | **enemy-tiers wins entirely.** One column: `npcs.danger_tier VARCHAR(20) NULL`. One vocabulary: `harmless|weak|standard|dangerous|deadly|boss`. Combat XP comes from `TIER_ROWS[tier]["xp"]` (0/5/15/40/100/300). progression's `ENEMY_XP` table, `_ARCHETYPE_TO_TIER` fallback, and `xp_for_enemy(npc)` are **deleted** — `apply_combat_result` computes XP from `resolve_danger_tier(npc)` and hands the total to progression. |
| R2 | **XP hand-off API.** enemy-tiers soft-imports `award_xp(player_id, amount, reason)`; progression defines `grant_xp(session, p, amount, source)`. | **One function: `progression.grant_xp(session, player, amount, source) -> progression_block`.** Called inside the caller's open transaction (quest completion, combat result). The soft-import shim, `award_xp`, and `get_combat_loadout` in `combat.py` are deleted — everything ships in one build, hard imports are correct. |
| R3 | **`state.combat` payload shape.** progression: `{max_hearts, hearts, damage, dodge_cooldown_ms, iframe_ms, quick_slot}`; enemy-tiers: `{level, max_hearts, light_damage, heavy_damage, parry_unlocked, heavy_unlocked}` with a flat hearts-penalty map. | **Merged dict, produced only by `progression.combat_stats(player)`:** `{level, max_hearts, hearts, light_damage, heavy_damage, dodge_cooldown_ms, iframe_ms, quick_slot, parry_unlocked: true, heavy_unlocked: true}`. `hearts` uses progression's **fraction formula** (`round(max_hearts * HEALTH_FRACTION[health])`, min 1, dead=0) — the flat penalty map is dropped (both agree at max 5; fraction is the one with the legacy-equality test). `light_damage` is progression's `damage`; `heavy_damage = light * 2 + 1` (enemy-tiers' default formula, computed in `combat_stats`). |
| R4 | **`audit_turn` signature.** Three extensions: `code_events` labels (progression), `code_actions` category labels + `campaign` (campaign), `code_actions` tool-name union (canvas), plus `player_input` prefix sniffing (enemy-tiers). | **Canvas's mechanism wins:** `audit_turn(turn_text, tools, state_before, state_after, *, code_actions: set[str] | None = None, campaign: dict | None = None)`. `code_actions` is a set of **tool names** unioned into `succeeded` — one line, fixes every check at once. Campaign's category labels are translated to tool names by a code map (`quest→{activate_quest, update_quest_status}`, `item→{add_to_inventory}`, `consequence→{create_event}`, `health→{update_player_health}`, `arrival→{move_player}`). enemy-tiers' `player_input` parameter is **dropped**: the `[COMBAT OUTCOME]` false positive is fixed instead by `/api/combat/result` recording `{"update_player_health","kill_npc"}` into the pending-actions ledger (canvas §9 retrofit). `campaign` stays as the second keyword param for the two campaign checks. |
| R5 | **Two between-turn ledgers.** progression's `_pending_canvas_notes` in server.py vs canvas's `src/web/pending_actions.py`. | **One ledger: `src/web/pending_actions.py`.** Progression's equip/unequip/consumable/level-up notes become `record_action(player_id, kind, summary, tools=…)` entries (kinds `equip|consume|levelup` join `move|take|read|open|search|combat`). `format_pending_actions` renders one merged `[SINCE YOUR LAST TURN]` block using canvas's template plus progression's note lines (level-up line keeps its "acknowledge ONCE, obliquely" instruction). `tool_equivalents` covers the new kinds: `consume → {"use_item","update_player_health"}`, `equip/levelup → set()`. `_pending_canvas_notes`, `format_canvas_notes`, and progression §7 are superseded. |
| R6 | **Boss definition.** enemy-tiers: `danger_tier == "boss"`; cinematics: `boss_title IS NOT NULL`. | **`danger_tier` is the single boss flag.** `is_boss(npc) = resolve_danger_tier(npc) == "boss"`, defined once in `src/services/combat.py`; cinematics imports it. `NPC.boss_title` is kept but demoted to **display-only epithet** ("Warden of the Ashen Gate") used as the intro subtitle and boss-bar title, falling back to `npc.profession`. A `boss_title` without `danger_tier="boss"` does nothing. |
| R7 | **Boss intro transport.** enemy-tiers: `combat_start.boss` + client `_bossIntro()` Promise seam + `boss-intro` DOM event + `BOSS_PATTERN.intro_ms`; cinematics: `result["cinematic"]` riding on `start_combat`, emitted as SSE `cinematic` before `combat_start`, `_pendingCombat` deferral, once-ever ledger. | **Cinematics' mechanism wins.** `build_combat_start` attaches `result["cinematic"]` via `boss_intro_payload()` (respects the once-per-boss ledger — rematches get no intro) AND keeps the top-level `boss` object (the client needs it for the HP bar and kill_cam regardless of whether an intro plays). The tracker emits `cinematic` before `combat_start`; `startCombat` defers spawning into `_pendingCombat` while a `blocksCombat` cinematic runs. **Deleted:** `GameRenderer.prototype._bossIntro`, the `boss-intro` DOM event, `BOSS_PATTERN["intro_ms"]`. **Kept:** the `boss-phase` DOM dispatch (fire-and-forget hook, no listener in v1). |
| R8 | **Combat result severity fields.** progression adds `hearts_healed`; enemy-tiers adds `hearts_lost`. | **Both ship.** `CombatResultRequest` gains `hearts_lost: int = 0` and `hearts_healed: int = 0`. Severity: `units = max(hearts_lost, hits_taken) - max(0, hearts_healed)`, clamped ≥0, mapped via `_HEARTS_TO_STATUS = {0: None, 1: "winded", 2: "hurt", 3+: "badly_hurt"}`. Defeat still forces `critical` — potions cannot rescue a downed player. `combat-ended` detail carries `hitsTaken`, `heartsLost`, `heartsHealed`, `defeated`. |
| R9 | **Campaign cinematic labels vs cinematic kinds; duplicate client path.** Campaign emits labels (`act_open|revelation|betrayal|arrival|countdown_expired|finale`) and dispatches a client-side `forge-cinematic` CustomEvent; cinematics expects a kind from `DM_CALLABLE` via the server-side deferred queue. | **One path: the server-side deferred queue.** During the campaign tick, code maps the beat label through a fixed table in `src/services/cinematics.py`: `CAMPAIGN_CINEMATIC_MAP = {"act_open": "title_card", "finale": "title_card", "countdown_expired": "title_card", "revelation": None, "betrayal": None, "arrival": None, "none": None}` and calls `request_cinematic(kind, …, source="campaign", defer=True)` for non-None kinds (title = act/clock label, subtitle = act premise / "Too late"). Revelation/betrayal moments are carried by the campaign toast + the DM's prose in v1 — `vision` remains a dream/memory treatment, not a plot-beat skin. The client `forge-cinematic` CustomEvent dispatches are **cut**; the toasts stay. |
| R10 | **Overlapping mechanics-in-prose audit checks.** progression's `progression_claim_without_engine` vs enemy-tiers' `mechanics_numbers_in_narration`. | **Merged into one check: `mechanics_in_narration`** — union of both regexes (digit-adjacent xp/level/HP/damage tokens PLUS bare "level up"/"XP"/"experience points"/"you feel stronger"). Fires regardless of `code_actions` — a real level-up still may not be narrated mechanically. One check id, one detail string. |
| R11 | **`[SINCE YOUR LAST TURN]` block ordering vs campaign block ordering.** | Final context order: world-tick briefing → **canvas/HUD ledger block** (facts) → **campaign block** (imperative, closest to player input) → NPC-addressing line → `[REALITY AUDIT]` last (most salient correction). |
| R12 | **Quest completion XP vs campaign `complete_quest` op.** Campaign beats complete quests through their own op; progression grants XP inside `update_quest_status`. | Extract one internal helper `_complete_quest(session, quest, player) -> dict` in `src/tools/world_write/quests.py` (status flip + `_sync_player_quest_lists` + `_apply_rewards` + idempotence guard + `grant_xp(scale)`); both the DM tool and the campaign op call it. Engine-driven quest completions therefore pay XP identically and emit the same `quest_completed` SSE + progression block. |
| R13 | **Cache-buster values.** Four specs name four different `?v=` values. | One rule: bump `?v=` on **both** script tags once per merged frontend wave; the shipped value is the merge date of Wave C (e.g. `?v=20260810a`). Never ship two different values on the two tags. |
| R14 | **`get_player`/state hearts derivation in game.js.** | `_playerHpFromHealth` → `_playerCombatStats()` reading `window.lastPlayerState.combat` (merged R3 shape), with progression's legacy fallback map. game.js never derives stats from items. |
| R15 | **Bark speakers vs NPC importance.** | MAJOR-tier NPCs are excluded from named bark speakers (one-line filter in `barks.generate_barks`); their lines resolve to ambient. |
| R16 | **`update_dm_state` tension vs `tension_floor` op vs boss climax floor.** No real conflict — all call `apply_tension_floor`. | Recorded for clarity: floors only ever raise, DM-authored changes still respected per `src/services/tension.py`. |

---

## 1. Unified schema delta

Everything is a new table or a new nullable column (`_migrate_missing_columns` adds `DEFAULT NULL`; `create_all` adds tables). No existing column changes. Every new column is `Mapped[X | None]` read through a defaulting helper.

### 1.1 New tables

| Table | Owner | Contents |
|---|---|---|
| `campaigns` | campaign | per-PC authored story: title, premise, stakes, villain/successor/ally casting, `acts` JSON, `fixed_truths` JSON, status, pacing stamps (campaign.md §1.1 verbatim) |
| `story_beats` | campaign | one row per beat: act/sequence/type/title, directive(+missed), truths, delivery keywords, trigger, schedule/fallback, met condition, consequence op lists, clock label, **`cinematic` label**, status/variant/attempt bookkeeping (campaign.md §1.1 verbatim) |
| `cinematics` | cinematics | the rarity ledger + deferred queue: kind, dedupe_key, title/subtitle, npc_id, mood, source (`dm|auto|campaign`), status (`played|pending|rejected`), reject_reason, game-clock stamp (cinematics.md §2.1 verbatim) |

### 1.2 New columns

| Table | Column | Type | Owner | Notes |
|---|---|---|---|---|
| `players` | `level` | INTEGER NULL | progression | NULL reads as 1 |
| `players` | `xp` | INTEGER NULL | progression | NULL reads as 0 |
| `players` | `equipment` | JSON NULL | progression | `{"weapon": item_id\|null, "armor": …, "trinket": …}` — pointers INTO inventory |
| `players` | `quick_slot_item_id` | VARCHAR(64) NULL | progression | Q hotkey binding |
| `quests` | `scale` | VARCHAR(20) NULL | progression (set by DM + campaign generator) | `errand\|job\|mission\|saga`; NULL reads `job` |
| `npcs` | `danger_tier` | VARCHAR(20) NULL | enemy-tiers | **the** tier column (R1); NULL = infer; `boss` authored-only |
| `npcs` | `boss_title` | VARCHAR(120) NULL | cinematics | display epithet only (R6) |
| `locations` | `objects` | JSON NULL | canvas | normalized interactables |
| `locations` | `ambient_barks` | JSON NULL | canvas | cached bark pool |
| `locations` | `ambient_barks_meta` | JSON NULL | canvas | signature + staleness stamp |
| `dm_state` | `weather` | VARCHAR(30) NULL | cinematics | `rain\|snow\|fog\|embers\|ash`; NULL = clear |

### 1.3 Pydantic-only (no DB)

`Item` gains optional `slot` (`weapon|armor|trinket`), `tier` (`crude…legendary`), `effect_tag` (`heal_light|heal_strong|surge`). Legacy repair rules from progression §3.6 (`type:"weapon"` → slot weapon @ common; `effects.heal` → heal_light/strong) stand. Scene-object item payloads are stripped to `{name,type,description,value,quantity}` by `normalize_objects` — **scene objects never carry `slot`/`tier`/`effect_tag`**; tiered gear enters the world only via `stock_merchant` and quest rewards.

### 1.4 Non-schema config

- `config/agents.yaml`: `campaign_forge`, `bark` entries (fast model, as specced).
- `config/settings.yaml` + `src/config.py`: `campaign:` block (campaign §1.3); `game.barks_enabled/bark_pool_size/bark_refresh_hours` (canvas §3).
- `music_generator.MOODS` gains `epic`.

---

## 2. Unified vocabulary

### 2.1 Fixed label menus (complete list — nothing else may be added at runtime)

| Menu | Values | Numbers live in |
|---|---|---|
| danger_tier | `harmless weak standard dangerous deadly boss` | `combat.py TIER_ROWS` (HP/dmg/speed/windup/stagger/knock/scale/**xp** 0/5/15/40/100/300) |
| combat_archetype | `brute skirmisher stalker ranged` | `combat.py ARCH_ROWS` |
| quest scale | `errand job mission saga` | `progression.QUEST_XP` 60/120/220/400 |
| gear tier | `crude common fine superior master legendary` | `progression.WEAPON_DAMAGE/ARMOR_HEARTS/TRINKET_IFRAMES/TIER_PRICE` |
| gear slot | `weapon armor trinket` | — |
| consumable | `heal_light heal_strong surge` | `progression.CONSUMABLES` |
| skill / difficulty / modifier | `stealth persuasion athletics perception cunning` / `easy tricky hard desperate` / `none advantage disadvantage` | `progression.DIFFICULTY_DC`, d20 in code |
| check outcome | `critical_success success partial failure critical_failure` | — |
| object kind | `take read open search` | `scene_objects.py` clamps |
| cinematic kind | `title_card boss_intro kill_cam vision weather` (DM-callable minus kill_cam) | `cinematics.py LIMITS/DURATIONS`, `game.js CINE` |
| weather | `rain snow fog embers ash clear` | `game.js` weather table |
| beat_type / truth kind / trigger_type | campaign.md §1.2 verbatim | `campaign.py DELAY_HOURS/DEADLINE_DAYS/…` |
| campaign cinematic label | `none act_open revelation betrayal arrival countdown_expired finale` | mapped to kinds by `CAMPAIGN_CINEMATIC_MAP` (R9) |

### 2.2 SSE events — consolidated

**New event types** (all through the existing tool-result `event` mechanism or explicit server emission):

| type | emitted by | payload |
|---|---|---|
| `check_result` | `roll_check` tool result | `{outcome, skill}` — no numbers, ever |
| `xp_gain` | tracker fan-out of any tool result carrying a `progression` block (runs AFTER the elif chain — quest completion carries both) | `{amount, source, xp, level, xp_into_level, xp_for_level}` |
| `level_up` | same fan-out when `leveled_up` | `{level, previous_level, unlocked[], stats}` |
| `campaign_beat` | `/api/play` drain after `_build_context` | campaign.md §4.2 |
| `campaign_act` / `campaign_resolved` | drain after `mark_beat_delivery` | campaign.md §4.2 |
| `cinematic` | (a) `play_cinematic` result, (b) nested `cinematic` key on ANY tool result — emitted **before** the tool's own event, (c) `/api/play` deferred-queue drain | `{kind, title, subtitle, npc_id, mood, duration_ms}` |

**Changed payloads:**

- `combat_start`: `enemies[]` each gain `tier`, `params` (resolved server-side `combat_params`), `boss: bool`, `boss_title`; top-level `boss: {npc_id, name, title} | null` (title = boss_title or profession). No `intro_ms` (R7).
- `state`: gains `level`, `xp`, `xp_into_level`, `xp_for_level`, `equipment` (three named slots), `combat` (merged R3 dict), `campaign` (spoiler-free summary or null), `weather` (indoor-suppressed server-side). Inventory entries gain `slot`, `tier`, `effect_tag`, `equipped`, `equippable`, `price`.
- **No new SSE for canvas actions** — `/api/travel` and `/api/interact` embed `build_player_state` in their REST responses; the client feeds `applyPlayerState`.
- Combat progression rides the `/api/combat/result` JSON response (`progression` key), never SSE.

### 2.3 New/changed DM tools (final signatures)

| Tool | Status | Signature / change |
|---|---|---|
| `roll_check` | NEW (`world_write/progression.py`) | `(player_id, skill, difficulty, modifier="none", reason="") -> {outcome, skill, difficulty, event:"check_result", instruction}` — `_debug` logged and stripped |
| `stock_merchant` | NEW (same file) | `(npc_id, entries: list[dict]) -> {stocked:[{name,price}]}` — legendary refused with a teaching error |
| `play_cinematic` | NEW (`world_write/cinematics.py`) | `(player_id, kind, title=None, subtitle=None, npc_id=None, mood=None)` — rejection returns NO `event` key + a say-nothing instruction |
| `get_campaign` | NEW (`world_read/campaign.py`) | `(player_id)` — read-only; **no campaign write tool exists** |
| `update_npc` | changed | `+ danger_tier` (refuses `boss`), `+ combat_archetype` |
| `add_to_inventory` | changed | `+ slot, tier, effect_tag`; catalog price override; auto-equip rule |
| `transfer_item` | changed | charges `catalog_price` for mechanical items |
| `use_item` | changed | `effect_tag` delegates to `apply_consumable` |
| `create_item_template`, `get_inventory`, `get_player` | changed | per progression §5.3 (`get_player` exposes `level` + `equipped`, never `xp`) |
| `start_combat` | unchanged signature | reads tier from the DB row; **no difficulty parameter, ever** |

Creator-agent tools: `add_npc` `+ danger_tier, combat_archetype` (menu docstrings); `add_location`/`update_location` `+ objects` (normalized). `add_npc` is never backticked in the DM prompt.

### 2.4 REST endpoints — consolidated

New: `POST /api/player/{id}/equip | unequip | quick-slot | use-consumable`; `POST /api/travel`; `POST /api/interact`; `GET /api/barks/{location_id}`; `GET /api/campaign/{player_id}` (+ `?spoilers=1` debug), `POST /api/campaign/{player_id}/generate`; `GET /api/cinematics/recent`.
Changed: `POST /api/combat/result` (+`hearts_lost`, `hearts_healed` in; +`progression`, `xp_awarded`, `xp_breakdown`, `level_up` in `changes` out); `/api/assets/location/{id}` (+per-exit `door/locked/requirements/travel_time_hours`, +top-level `objects`); `GET/PUT /api/world/npcs/{id}` (+`danger_tier`, `combat_archetype`, `boss_title`); `PUT /api/world/locations/{id}` (+`objects`).

### 2.5 DM prompt delta — ONE coherent plan

The prompt changes **once**, in six touches. No other section is edited. `TOOL_PREFIXES` in `tests/test_dm_prompt_tools.py` gains `("roll_", "stock_", "play_")`; `get_campaign` is covered by the existing `get_` prefix.

1. **THE REALITY CONTRACT** — add progression's two bullets (risky actions are decided by `roll_check`; XP/levels/gear numbers are the engine's).
2. **New section: THE CANVAS PLAYS ITSELF** — inserted immediately after THE REALITY CONTRACT. Canvas's draft, extended by two sentences to cover the merged ledger (R5): equipment changes, potions drunk, and growth also arrive in the same `[SINCE YOUR LAST TURN]` block and are equally already-true. Backticks only `move_player`, `advance_time`, `add_to_inventory`, `describe_location`.
3. **New section: THE CAMPAIGN** — inserted after NARRATIVE DIRECTOR (campaign §5.3 verbatim), plus the one-line NARRATIVE DIRECTOR arc amendment.
4. **SUCCESS & FAILURE** — rewrite point 1 (risk classes: safe / risky→`roll_check` / fight→`start_combat` / entity-less danger→`roll_check`+`update_player_health`), add point 6 ("the roll is final").
5. **GUIDELINES FOR SPECIFIC SITUATIONS** — replace **3. Combat & Danger** with enemy-tiers §12 (tiers are absolute, no difficulty dial, `[COMBAT OUTCOME]` aftermath rules — its "never print numbers, levels, or experience" line already carries progression's aftermath requirement). Then add **4. Skill Checks** (progression §10.3), **5. Gear, Coin & Growth** (progression §10.3), **6. Cinematic Moments** (cinematics §7).
6. **`_build_context`** — player briefing gains level + equipped line; block order per R11.

Creator prompt: two new sections — DANGER TIERS (enemy-tiers §6.1) and SCENE OBJECTS (canvas §8).

### 2.6 Turn audit — final consolidated shape

```python
def audit_turn(turn_text, tools, state_before, state_after, *,
               code_actions: set[str] | None = None,
               campaign: dict[str, Any] | None = None) -> list[dict[str, str]]
```

- `succeeded |= code_actions` — the single suppression mechanism (R4). Fed from `pending_actions.tool_equivalents(drained)`; `/api/combat/result` and campaign beat fires record their tool equivalents into the ledger.
- New checks (5, not 7 — R10 merged two): `risky_action_without_check` (progression §11.2 regex, exempt on `roll_check`/`start_combat`), `mechanics_in_narration` (merged R10), `cinematic_text_in_narration` (cinematics §8), `campaign_beat_not_delivered`, `campaign_truth_frontrun` (campaign §6.2, only when `campaign` passed).
- `mark_beat_delivery` runs **before** `audit_turn` so a delivered beat is never flagged.

---

## 3. Build order — waves, chunks, file-conflict analysis

**Hotspot files:** `src/web/server.py`, `src/web/static/js/game.js`, `src/web/static/index.html`. Secondary hotspots: `src/web/turn_audit.py`, `src/web/streaming.py`, `src/services/combat.py`, `src/agents/dm_orchestrator.py`, `src/web/pending_actions.py` (new but shared). Rule: within a wave, chunks touch disjoint files; any chunk touching a hotspot owns that hotspot exclusively for its wave.

### Wave A — shared foundation (parallel: yes, all files disjoint)

| Chunk | Content | Primary files |
|---|---|---|
| **A1 schema** | ALL new tables + columns + model exports + Item pydantic fields, in one chunk (single owner of `src/models/*` prevents five agents editing `__init__.py`). Boot `emberfall.db`, confirm `[migrate]` lines. | `src/models/player.py`, `quests.py`, `npc.py`, `location.py`, `world_state.py`, `campaign.py` (new), `item.py`, `models/__init__.py` |
| **A2 progression core** | Tables + pure functions (§3), `grant_xp`, `roll_skill_check`, `equip/unequip`, `apply_consumable`, `combat_stats` producing the merged R3 dict. Legacy-equality test first. | `src/services/progression.py` (new), `tests/test_progression*.py`, `test_equipment.py`, `test_consumables.py`, `test_roll_check.py` |
| **A3 tier engine** | `TIER_ROWS`/`ARCH_ROWS`/`BOSS_PATTERN` (no `intro_ms`), `combat_params`, `infer_danger_tier`/`resolve_danger_tier`, `is_boss`, ranged keywords in `infer_archetype`. Pure-table work only — `build_combat_start`/`apply_combat_result` wait for Wave B. | `src/services/combat.py` (tables/pure fns section), `tests/test_enemy_tiers.py` |
| **A4 canvas services** | `travel.py` (`check_route` extraction + `travel_now`; `move_player` refactor — owns `tools/world_write/player.py` this wave), `scene_exits.py`, `scene_objects.py`. | `src/services/travel.py`, `scene_exits.py`, `scene_objects.py` (new), `src/tools/world_write/player.py`, tests |
| **A5 cinematics service** | `request_cinematic`, limits, ledger, `drain_pending`, `boss_intro_payload`, `CAMPAIGN_CINEMATIC_MAP` (R9). Imports `is_boss` from combat (A3 lands first or stub-import — table-only dependency, low risk). | `src/services/cinematics.py` (new), `tests/test_cinematics.py` |
| **A6 campaign service + generator** | Models landed in A1; this chunk: constants, `execute_ops`/`evaluate_condition`, `arm_act`, `run_campaign_tick`, `mark_beat_delivery`, `format_campaign_context`, `persist_campaign`/`ensure_campaign`, `campaign_forge` agent, config entries. Calls `_complete_quest` via R12 — coordinate with B2 (define the helper here behind an import guard or land quests.py refactor in A-scope: **ruling — the `_complete_quest` extraction moves into this chunk's PR touching `src/tools/world_write/quests.py`**, and B2 leaves quests.py alone). | `src/services/campaign.py`, `src/agents/campaign_forge.py` (new), `src/tools/world_write/quests.py`, `src/config.py`, `config/*.yaml`, `tests/test_campaign.py` |
| **A7 audit + ledger** | `pending_actions.py` (merged R5 kinds) and the FULL `audit_turn` change set (R4 signature, 5 new checks, category→tool map). One owner for all five specs' audit needs. | `src/web/pending_actions.py` (new), `src/web/turn_audit.py`, `tests/test_pending_actions.py`, `test_turn_audit_*.py` |
| **A8 barks + music** | `barks.py` service + `epic` mood string. | `src/services/barks.py` (new), `src/services/music_generator.py`, `tests/test_barks.py` |

File-conflict note: A6 and A2 both matter to quests, but only A6 touches `quests.py`; A2's `grant_xp` is called from A6's helper. A3 and A5 share only an import direction.

### Wave B — server + tools + prompts integration (parallel: yes, with the partition below; each hotspot has exactly one owner)

| Chunk | Content | Primary files |
|---|---|---|
| **B1 combat integration** | `build_combat_start` (tier/params/boss/roster/climax floor/instruction text + `cinematic` attach per R7), `apply_combat_result` (R8 severity, DB-re-read XP, `grant_xp` in-transaction, `combat_outcome_message` rewrite, ledger recording of `{update_player_health, kill_npc}`). | `src/services/combat.py` (sole owner this wave) |
| **B2 tools** | `world_write/progression.py` (roll_check, stock_merchant), `world_write/cinematics.py` (play_cinematic), `world_read/campaign.py` (get_campaign), `add_npc`/`update_npc` params, `add_location`/`update_location` objects, inventory tools (`add_to_inventory`, `transfer_item`, `use_item`, `create_item_template`, `get_inventory`, `get_player`), `__init__` exports. | `src/tools/world_write/*` (except player.py, quests.py — done in A), `src/tools/world_read/*` |
| **B3 server.py** | ONE owner, serialized internally: equip/unequip/quick-slot/use-consumable endpoints; `/api/travel`, `/api/interact`, `/api/barks`; combat-result request/response extension; `build_player_state` merge (level/xp/equipment/combat/campaign/weather + enriched inventory); `/api/play` wiring (ledger drain → campaign tick events → cinematic queue drain → `mark_beat_delivery` → audit with `code_actions`+`campaign`); campaign + cinematics endpoints; `NPCUpdate`/`LocationUpdate` fields; `/api/assets/location/{id}` enrichment. | `src/web/server.py` (sole owner), `src/services/asset_manager.py` (one line) |
| **B4 streaming** | `check_result` branch, post-chain `progression` fan-out (xp_gain/level_up), `_push_cinematic` (nested key first, then elif), `combat_start` boss passthrough. | `src/web/streaming.py` (sole owner) |
| **B5 prompts** | The §2.5 six-touch DM prompt plan + `DM_TOOLS` registration + creator prompt sections + `_build_context` wiring (campaign tick call, block ordering R11, level/gear line) + `session.py` `mark_beat_delivery` call + `TOOL_PREFIXES`. | `src/agents/dm_orchestrator.py`, `src/agents/creation_agent.py`, `src/game/session.py`, `tests/test_dm_prompt_tools.py` |

Dependency note: B3 imports from every Wave A module and from B1/B2/B4 names — B3 merges **last** within the wave; the other four are mutually disjoint.

### Wave C — frontend (parallel: limited — three files, three owners, frozen interfaces)

| Chunk | Content | Primary files |
|---|---|---|
| **C1 game.js** | Single owner, internal order: (1) `_playerCombatStats` + `_ensureCombat`/dodge/attack from server stats; (2) server-authoritative enemy `params`, tier-coloured HP bars, `_swing` extraction, heavy charge, parry/stagger/`vulnMul`, `flees`, rings + ranged, boss layer (bar, phases, slam, combo, `boss-phase` dispatch); (3) `_useQuickSlot` + Q key + `spawnLevelUpBurst` + `showCheckResult`; (4) doors/objects/E/barks/edge-push guard/canvas-click blur; (5) cinematics (`playCinematic`/`_updateCinematic`/`_endCinematic`/`endVision`/`_startKillCam`/`_pendingCombat`), weather layer + `setWeather`, camera zoom/shake merge, tint multiply; doors/barks also gate on `this.cine`. | `static/js/game.js` (sole owner) |
| **C2 index.html** | HUD level chip + XP bar + character panel + inventory buttons; campaign chips/panel/toasts; cinematic overlay markup + CSS; SSE branches (`level_up`, `xp_gain`, `check_result`, `campaign_*`, `cinematic`); `canvas-travel`/`canvas-interact` listeners + `showLorePanel`; `combat-ended` POST body (heartsLost/heartsHealed) + progression handling; `forge-state` listener; `window.forgeTurnBusy`; NPC editor selects (danger_tier/archetype/boss_title); controls overlay rows; `endVision` in finally; weather in `applyPlayerState`; **one** cache-buster bump (R13). | `static/index.html` (sole owner) |
| **C3 audio.js** | `level_up` triumph branch; `cinematic` branch; `cineDuck`/`duckFor`/`holdVoiceQueue`/`setAmbience`; `epic` fallback chain; SFX names + optional `boom`/`whoosh` assets. | `static/js/audio.js` (sole owner), `static/sfx/*` |

C1/C2/C3 are disjoint files and can run as parallel agents **only because every cross-file interface is frozen by this document** (SSE payloads §2.2, `state.combat` R3, `combat-ended` detail R8, method names `playCinematic/setWeather/spawnLevelUpBurst/showCheckResult/_useQuickSlot/markObjectConsumed/startCombat(enemies, boss)`).

### Wave D — closure (serial, one owner)

Seed data (objects on two seed locations, one NPC per tier + one boss), CLAUDE.md updates (Mechanical consequences: Progression, Campaign spine, Tiers, Canvas-first play; Audio/cinematic flow: new SSE list), full pytest run, end-to-end manual pass (progression §12.17 + enemy-tiers §13.14 + canvas §14.14 + campaign finale + cinematic rejection check), final cache-buster verification.

---

## 4. V1 cut list

**Cut by this document for coherence (beyond the specs' own deferrals):**

1. `NPC.enemy_tier`, `ENEMY_XP`, `_ARCHETYPE_TO_TIER`, `xp_for_enemy` (R1).
2. `award_xp` + soft-import shims + `combat.py::get_combat_loadout` + `_LOADOUT_DEFAULT` (R2).
3. Flat hearts-penalty map (R3 — fraction formula only).
4. `audit_turn(player_input=…)` prefix sniffing and `code_events` label sets (R4).
5. `_pending_canvas_notes` / `format_canvas_notes` (R5 — merged into `pending_actions`).
6. `GameRenderer._bossIntro` seam, `boss-intro` DOM event, `BOSS_PATTERN.intro_ms` (R7).
7. Client-side `forge-cinematic` CustomEvent dispatches (R9 — server queue is the one path).
8. `vision` as the skin for revelation/betrayal beats (R9 — toast + prose in v1).
9. One of the two overlapping mechanics-prose audit checks (R10 — merged).
10. `boss_title` as a boss *flag* (R6 — epithet only).

**Deferred by the specs (aggregate, still deferred):** sell-back economy, guard/shield consumable, stamina, DM equip tool, per-skill progression, durability/enchant/craft (progression); extra boss pattern sets, real projectiles, adds/minions, elite modifiers, per-NPC pattern overrides, tier-scaled loot, threat-assess UI (enemy-tiers); branching beats, campaign editor, sequel chaining, ally re-casting, semantic delivery detection (campaign); image hotspots, authored door coords, object sprites, bark TTS, world tick on canvas travel, `place_object` tool, bark reactivity (canvas); `setpiece`, arrival title cards, per-location weather defaults, post-FX, cinematic replay (cinematics).

---

## 5. Risk register

| Risk | Detail | Enforcement / mitigation |
|---|---|---|
| **Latency: campaign tick in `_build_context`** | Adds indexed SQLite reads per turn on the hot path. | Pure code, ≤ a handful of queries; budget test asserts `run_campaign_tick` < 50 ms on a seeded DB. |
| **Latency: `build_player_state` growth** | Now includes combat stats + campaign summary + enriched inventory, and runs after every world-write tool. | `combat_stats` is pure dict math over the loaded row (no query — asserted in review); `campaign_state` is ≤3 queries and cacheable per turn. |
| **LLM cost: campaign_forge** | 1 fast-model call (~4k in / 8k out) per character creation, background thread. Retry ×1 on bad JSON. | `status="failed"` degrades to today's game; never blocks a turn. |
| **LLM cost: barks** | 1 fast-model call per location per 12 in-game hours, background, deduped, 300 s failure cooldown. | Config kill-switch `barks_enabled`. |
| **LLM cost: `epic` music** | One extra Suno job per world, backgrounded. | Stinger silently absent until ready. |
| **No new per-turn LLM cost anywhere else** — every fast path is REST + SQLite. | | |
| **DM grants XP / invents prices** | `rewards["xp"]` stripped + logged; `transfer_item` charges catalog price; `stock_merchant` refuses legendary. | `test_quest_rewards_cannot_inject_xp`, `test_catalog_price_overrides_authored_value`. |
| **DM narrates dice/levels/numbers** | `mechanics_in_narration` (fires even on legitimate level-ups), `check_result` returns outcome labels only, `_debug` stripped. | `[REALITY AUDIT]` self-correction loop. |
| **DM dodges `roll_check` ("everything is negotiable")** | `risky_action_without_check` fires on success-asserting resolution phrases with no check tool. | Precision guards (`(?!\s+yourself)`, 40-char `you` anchor, `start_combat` exemption) + tests. |
| **DM inflates/deflates fight difficulty** | `start_combat` has no tier parameter; `combat_params(tier, archetype)` takes no player argument (signature-asserted); `update_npc` refuses `boss`; inference never returns `boss`. | `test_combat_params_is_pure_and_deterministic`, `test_infer_danger_tier_never_returns_boss`. |
| **DM spoils campaign truths / front-runs beats** | Unfired beats never enter context; sealed truths shown only with do-not-say instruction; no campaign write tool exists; `campaign_truth_frontrun` + `campaign_beat_not_delivered` audits; REST redacts sealed truths from devtools. | Structural (starvation) first, audit second. |
| **Cinematic spam** | DB-backed ledger: same-kind game-time cooldown, per-day cap, global real-time floor, rolling session cap, identity dedupe (one entrance per boss, ever). Rejection returns no `event` key + say-nothing instruction; `cinematic_text_in_narration` catches the DM typing the card. | Survives reloads and restarts by design. |
| **Balance: heavy damage at top gear** | `heavy = light*2+1` with light up to 8 → heavy 17 vs boss 24 HP ≈ 2 hits. enemy-tiers' hits-to-kill matrix assumed a slower curve without weapon tiers. | Flagged as the ONE open tuning knob: add `test_boss_min_hits` asserting a max-loadout player needs ≥4 swings on a boss; if it fails, clamp `heavy = min(light*2+1, light+4)` in `combat_stats` — one constant, no interface change. |
| **Balance: XP pacing** | Tier XP (0-300) + quest XP (60-400) against `XP_THRESHOLDS` (100…3200). Quest-dominated early. | Numbers all live in `progression.py`; retuning is a table edit + threshold test. |
| **Client-forged XP** | `/api/combat/result` re-reads tiers from the DB; corpses award nothing; retried POST idempotent. | `test_xp_awarded_uses_db_tier`, `test_xp_not_awarded_twice_for_same_corpse`. |
| **Audit false positives from code-path mutations** | The class of bug that trained users to ignore audits. | Single mechanism: pending-actions ledger → `tool_equivalents` → `code_actions` union (canvas travel, interacts, potions, equips, combat results, campaign ops). Over-suppression guarded by `test_arrival_claim_still_fires_without_code_action`. |
| **Stale game.js vs new SSE types** | Silent no-ops. | Server params travel in payloads (JS table is fallback only); one cache-buster rule (R13); version bumped once per frontend wave. |
| **Mid-fight state event re-rolling player HP** | Progression stats snapshot onto `this.combat.stats` at `_ensureCombat`; state events during a fight don't touch live HP. | Spec'd in C1; test by manual pass. |
| **Beat fires while player mid-canvas-fight** | Campaign tick runs only inside `_build_context` (a typed turn), never during canvas play. | Structural. |

---

## 6. Consolidated test plan (pytest)

New files (from the specs, deduplicated; overlapping audit tests merged into one module):

| File | Coverage |
|---|---|
| `test_progression_tables.py` | thresholds monotonic, level table complete, six-entry tier tables, price tables |
| `test_progression.py` | legacy row defaults; `grant_xp` single/multi-level/cap; quest scale XP + NULL→job; XP-injection strip; completion idempotence (via `_complete_quest`); combat XP victory/defeat/dead-player/repeat-POST |
| `test_equipment.py` | **`test_level1_unarmed_matches_legacy`** (byte-identical 5/4/3/2/1 hearts, damage 1); tier bonuses; heart cap; gear-tier-cap gating; legacy weapon→common; equipped-stays-in-bag; auto-equip rules; slot auto-clear |
| `test_consumables.py` | quick-slot default; in/out-of-combat effects; surge buff; `hearts_healed` severity interaction; defeat still critical |
| `test_shop_pricing.py` | catalog overrides value; purchase charges table; plain items keep authored value; `stock_merchant` creates/refuses |
| `test_roll_check.py` | label-only returns, no `_debug`; invalid-word errors; nat 20/1; partial band; advantage/disadvantage; level & trait bonus (seeded) |
| `test_enemy_tiers.py` | `combat_params` pure + 2-arg signature (no-scaling guarantee); HP/damage monotonic; **standard row == legacy 7/3/4 + windups 650/340/280**; harmless 1 HP/0 dmg/flees; boss 24 flat + pattern; windup floor 220; ranged params; inference precedence, never-boss, NPCTier fallback, explicit wins |
| `test_combat.py` (extended) | payload carries tier/params/boss; boss→climax floor, non-boss→high; `hearts_lost`/`hearts_healed` severity math; old-client fallback; DB-tier XP; harmless = 0 XP; corpse idempotence; `grant_xp` called with total; outcome message names tiers, zero digits |
| `test_campaign.py` | no-campaign no-op; persist/validate/downgrade rules; every trigger type; fallback deadlines; one-beat-per-tick + priority; spacing; met/missed ops (faction −15 exact, clamped); villain succession + forced finale; delivery keywords → truths revealed; 3-attempt give-up; act transitions; finale won/lost; context hides unfired beats + marks sealed; clock urgency; public view redacts |
| `test_turn_audit.py` (one merged module) | `code_actions` union suppression + still-fires-without guard; `risky_action_without_check` fire/clean/precision (yourself, NPC-on-NPC, start_combat); `mechanics_in_narration` (digits AND bare level/XP words, even with code_actions); `cinematic_text_in_narration` fire/ordinary-prose clean; `campaign_beat_not_delivered` / `campaign_truth_frontrun` conjunction precision; 4-positional-arg backward compat |
| `test_pending_actions.py` | record/drain; cap + omitted marker; move collapsing; first-visit describe hint; `tool_equivalents` incl. consume/combat kinds; equip/levelup note templates |
| `test_canvas_travel.py` | `travel_now` moves + advances clock; blocked routes; hierarchy-gate parity with `test_travel_gate.py`; unreachable rejection; first-visit flag; `move_player` return shape unchanged |
| `test_scene_objects.py` / `test_scene_exits.py` / `test_barks.py` | per canvas §15 verbatim |
| `test_cinematics.py` | vocabulary/duration agreement; accepted event payload; rejected has no `event` key; tracker converts standalone + nested (cinematic BEFORE combat_start); all four limit types; duplicate title; **one entrance per boss ever**; kill_cam not DM-callable; weather label menu + DMState write/clear/duplicate; ledger row per call; deferred queue drains once; `combat_start` attach/absence/rematch; **`CAMPAIGN_CINEMATIC_MAP` covers every campaign label**; state weather + indoor suppression |
| `test_dm_prompt_tools.py` (extended) | `TOOL_PREFIXES += ("roll_", "stock_", "play_")`; every backticked tool in the rewritten sections registered; `add_npc` NOT backticked in DM prompt |

Existing tests that must pass **unmodified**: `test_travel_gate.py`, `test_turn_filters.py`, `test_world_simulation.py`, `test_playtest_seed.py`, `test_inventory.py` — the collective proof that a NULL-everything world plays exactly as today.

Cross-system integration tests (new, `test_integration_overhaul.py`):
- `test_quest_completion_full_chain` — `_complete_quest` → rewards + `grant_xp` + progression block → tracker emits `quest_completed`, `xp_gain`, `level_up` in that order.
- `test_campaign_complete_quest_op_pays_xp` — beat op and DM tool produce identical player state.
- `test_combat_result_ledger_suppresses_audit` — full `/api/combat/result` → next-turn audit with drained `code_actions` → no `damage_without_tool`.
- `test_boss_fight_event_order` — boss roster turn emits `cinematic(boss_intro)` before `combat_start`; rematch emits only `combat_start`.
- `test_state_payload_superset` — `build_player_state` carries every key in §2.2 with a fresh legacy DB (level 1, empty equipment, campaign null, weather clear).
