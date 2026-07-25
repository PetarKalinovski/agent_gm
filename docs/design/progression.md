# Progression: Levels, XP, Gear, Consumables, Code-Rolled Checks

**Status:** implementation-ready spec, v1 scope frozen.
**Owner system:** progression. Touches: enemy-tiers, campaign, canvas, cinematics.

---

## 0. The one-paragraph pitch

The player has a **Level (1-10)** and **XP**. XP is granted by code, only on quest
completion and combat kills — there is no tool the DM can call to grant it. Level
buys three fixed numbers (hearts, damage, dodge cooldown) and one soft capability
(a better shot at every `roll_check`). The player **equips** a weapon, an armour,
and a trinket from the HUD over REST — no DM turn — and each is a **tier label**
(`crude`→`legendary`) that code converts into numbers. **Potions** are drunk with
**Q** mid-fight, resolved client-side and confirmed by REST. **Gold** buys tiered
gear at code-owned prices, so the LLM cannot price a legendary sword at 5 coins.
Risky non-combat actions go through **`roll_check`**, where the DM picks a skill
domain, a difficulty *word*, and a modifier *word*; code rolls the d20 and returns
only an outcome label. The dice never appear in prose; the player sees a small
`⚄ Success` / `⚄ Failed` floater so the outcome feels rolled, not negotiated.

Every number in this document lives in **one new module**:
`src/services/progression.py`. Nothing else may contain a progression constant.

---

## 1. Non-negotiables this design honours

| Rule | How this design satisfies it |
|---|---|
| Code enforces, LLM narrates | Every number is in a fixed table. The DM/creator pick labels: quest `scale`, gear `tier`, consumable `effect_tag`, check `difficulty`/`modifier`/`skill`, merchant stock `kind`+`tier`. |
| No LLM round-trip on a fast path | Equip / unequip / quick-slot / drink potion / level-up are REST + client. The DM finds out on its **next** turn through a `[SINCE YOUR LAST TURN]` context block, built from a process-local ledger (zero tokens spent generating it). |
| SQLite, no migrations | 4 new nullable columns on `players`, 1 on `quests`. No new tables. Item mechanics ride inside the existing `Player.inventory` JSON via new **optional** Pydantic fields. |
| Old worlds unchanged | Every read goes through a defaulting helper (`level or 1`, `xp or 0`, `equipment or {}`). A level-1 character with no equipment produces **exactly** today's combat numbers: 5 hearts at healthy, 1 damage. This is asserted by a test. |
| Reuse the event pipeline | `roll_check` returns `event: "check_result"`; quest completion returns a `progression` block that the tracker turns into `xp_gain` + `level_up` SSE events. The combat REST path returns the same block in its JSON response. |
| Audit adapts | `audit_turn` gains a `code_events` argument so code-path truths (level-ups, potions) never produce false positives, plus two new checks — one of which (`risky_action_without_check`) is the point of the whole `roll_check` feature. |

---

## 2. Schema deltas (exact)

`src/models/base.py::_migrate_missing_columns` adds missing columns as
`ALTER TABLE ... ADD COLUMN <type> DEFAULT NULL`. Existing rows therefore get
**NULL, not the SQLAlchemy default**. Every new column must be typed
`Mapped[X | None]` and read through a defaulting helper. This is the single
easiest way to break `emberfall.db`; do not skip it.

### 2.1 `players` (`src/models/player.py`)

```python
    # --- Progression (nullable: existing rows read as level 1 / 0 xp) ---
    level: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    xp: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # Equipped item ids, pointing INTO Player.inventory (items are never moved
    # out of the bag — every existing inventory code path keeps working).
    # {"weapon": "iron_sword" | null, "armor": ... , "trinket": ...}
    equipment: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    # Item id bound to the Q hotkey (canvas quick-use)
    quick_slot_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

### 2.2 `quests` (`src/models/quests.py`)

```python
    # Effort scale — a LABEL. Code maps it to XP. NULL reads as "job".
    scale: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    # errand | job | mission | saga
```

### 2.3 `npcs` — **no change owned by this spec**

Combat XP wants an enemy tier. That column (`NPC.enemy_tier`) belongs to the
**enemy-tiers spec**. Progression reads it defensively:

```python
tier = getattr(npc, "enemy_tier", None) or _ARCHETYPE_TO_TIER.get(npc.combat_archetype or "", "regular")
```

so v1 ships and works whether or not that spec has landed. Do **not** add the
column here — two specs adding the same column is a merge conflict waiting to
happen.

### 2.4 `Item` (`src/models/item.py`) — Pydantic only, no DB change

```python
    # --- Mechanical fields (all optional; legacy items have none) ---
    slot: str | None = Field(None, description="weapon | armor | trinket — equippable if set")
    tier: str | None = Field(None, description="crude|common|fine|superior|master|legendary")
    effect_tag: str | None = Field(None, description="heal_light | heal_strong | surge")
```

`Item.from_dict` on a legacy dict still validates (all three default to `None`).
`to_dict()` now emits three extra keys with `null` values — harmless everywhere
they land (inventory JSON, HUD, DM context).

---

## 3. `src/services/progression.py` — the single source of numbers

New module. Pure functions + constant tables. No I/O except the session the
caller passes in. Everything below is literal, copy-able content.

### 3.1 Levels and XP

```python
# Cumulative XP required to BE this level. Index = level.
XP_THRESHOLDS = {1: 0, 2: 100, 3: 250, 4: 450, 5: 700,
                 6: 1000, 7: 1400, 8: 1900, 9: 2500, 10: 3200}
MAX_LEVEL = 10
```

```python
# level -> (hearts_bonus, damage_bonus, dodge_cooldown_ms, gear_tier_cap, unlock_text)
LEVEL_TABLE = {
    1:  (0, 0, 750, 2, None),
    2:  (1, 0, 750, 2, "You take a hit better than you used to."),
    3:  (1, 0, 700, 3, "Superior gear no longer fights you."),
    4:  (2, 0, 700, 3, "You take a hit better than you used to."),
    5:  (2, 1, 650, 4, "Your strikes land harder."),
    6:  (3, 1, 650, 4, "You take a hit better than you used to."),
    7:  (3, 1, 600, 5, "The finest gear answers to you."),
    8:  (4, 1, 550, 5, "You move out of the way faster."),
    9:  (4, 2, 550, 5, "Your strikes land harder."),
    10: (4, 2, 500, 5, "You are the thing others tell stories about."),
}
```

`gear_tier_cap` is the **highest gear tier index the player may equip**. It is
the "new capability" half of levelling: gold alone cannot buy power.

### 3.2 XP sources (the only two)

```python
QUEST_XP = {"errand": 60, "job": 120, "mission": 220, "saga": 400}   # NULL scale -> "job"

ENEMY_XP = {"fodder": 15, "regular": 30, "elite": 70, "champion": 150, "boss": 300}
_ARCHETYPE_TO_TIER = {"brute": "elite", "stalker": "regular",
                      "skirmisher": "regular", "test": "fodder"}
```

**Interface contract with the enemy-tiers spec:** that spec owns
`NPC.enemy_tier` and its label vocabulary. It must use exactly the five keys of
`ENEMY_XP`. If it adds a sixth, it adds the row here too. Nothing else in this
spec depends on it.

`roll_check` awards **no** XP (rejected: it turns every conversation into a
grind, and the DM chooses when checks happen — that is a DM-controlled XP faucet
through the back door).

### 3.3 Gear tiers — one ladder, three effect tables

```python
TIER_LABELS = ("crude", "common", "fine", "superior", "master", "legendary")  # index 0..5
SLOTS = ("weapon", "armor", "trinket")

WEAPON_DAMAGE   = (0, 1, 2, 3, 4, 5)      # added to base damage 1
ARMOR_HEARTS    = (0, 1, 1, 2, 2, 3)      # added to base 5 full hearts
TRINKET_IFRAMES = (0, 40, 80, 120, 160, 200)  # ms added to the 350ms dodge i-frame

TIER_PRICE = {
    "weapon":  (10, 40, 120, 300, 700, 2000),
    "armor":   (15, 50, 150, 350, 800, 2200),
    "trinket": (20, 60, 160, 380, 850, 2400),
}
```

One vocabulary for all three slots keeps the DM prompt short and the creator
agent honest. (Considered and rejected: per-slot label sets like
`padded/mail/plate` — flavourful, but triples what the LLM must remember and
invites it to invent a seventh label.)

### 3.4 Consumables

```python
CONSUMABLES = {
    # tag           hearts  status_steps  buff        duration_ms  price  default name
    "heal_light":  {"hearts": 2, "status_steps": 1, "buff": None,     "ms": 0,     "price": 25},
    "heal_strong": {"hearts": 4, "status_steps": 2, "buff": None,     "ms": 0,     "price": 70},
    "surge":       {"hearts": 0, "status_steps": 0, "buff": "damage", "ms": 20000, "price": 60},
}
SURGE_DAMAGE_BONUS = 1
```

`status_steps` applies **only out of combat** (hearts do not exist outside a
fight); `hearts` applies **only in combat**. One item, two honest readings of
the same fiction.

### 3.5 Skill checks

```python
SKILLS = ("stealth", "persuasion", "athletics", "perception", "cunning")
DIFFICULTY_DC = {"easy": 8, "tricky": 12, "hard": 16, "desperate": 20}
MODIFIERS = ("none", "advantage", "disadvantage")

# Player.traits words that grant +2 in a domain. Fixed, code-owned.
TRAIT_BONUS = {
    "stealth":    {"quick", "nimble", "silent", "sneaky", "small", "light-footed"},
    "persuasion": {"charming", "silver-tongued", "honest", "commanding", "kind", "noble"},
    "athletics":  {"strong", "tough", "hardy", "athletic", "big", "trained"},
    "perception": {"sharp", "observant", "wary", "paranoid", "keen"},
    "cunning":    {"cunning", "clever", "educated", "scholarly", "crafty"},
}
TRAIT_BONUS_VALUE = 2
```

Roll: `d20 + level + trait_bonus` vs `DC`.
`advantage` = roll 2d20 keep highest; `disadvantage` = keep lowest.

```python
# total - DC -> outcome
#   nat 20                      -> "critical_success"
#   nat 1                       -> "critical_failure"
#   total >= DC                 -> "success"
#   DC-3 <= total < DC          -> "partial"
#   total < DC-3                -> "failure"
```

`partial` exists because the DM prompt already preaches partial success; code
should hand it to them rather than hope.

RNG lives in a module-level `_RNG = random.Random()` with
`def set_seed(n: int) -> None` for tests. Never `random.random()` directly.

### 3.6 Public functions

```python
def player_level(p) -> int:                      # p.level or 1, clamped 1..10
def player_xp(p) -> int:                         # p.xp or 0

def tier_index(label: str | None) -> int | None  # "fine" -> 2, unknown/None -> None

def mechanize_item(item: dict) -> dict | None:
    """Mechanical reading of an inventory entry, or None if it is plain stuff.

    Returns {"slot": str|None, "tier": int|None, "effect_tag": str|None}.
    Legacy repair: an item with no `slot` but type in ("weapon","armor")
    reads as that slot at tier index 1 ("common"), so worlds that already
    contain an 'Iron Sword' become equippable the moment this ships.
    Legacy repair 2: type == "consumable" with effects {"heal": n} and no
    effect_tag reads as heal_light (n < 40) or heal_strong (n >= 40).
    """

def resolve_loadout(p) -> dict:
    """{"weapon": item_dict|None, "armor": ..., "trinket": ...}.
    Silently drops pointers to items no longer in the inventory and to items
    above the player's gear_tier_cap (a level cannot be lost, so the latter
    only fires if a table is retuned)."""

def combat_stats(p) -> dict:
    """The ONLY place player combat numbers are computed. See 3.7."""

def min_level_for_tier(tier_idx: int) -> int:
    """Lowest level whose gear_tier_cap >= tier_idx (derived from LEVEL_TABLE,
    never a second table). With the v1 table: 0-2 -> 1, 3 -> 3, 4 -> 5, 5 -> 7."""

def can_equip(p, item: dict) -> tuple[bool, str]:   # (ok, reason_code)
def equip(session, p, item_id: str) -> dict          # mutates p.equipment
def unequip(session, p, slot: str) -> dict

def catalog_price(item: dict) -> int | None:
    """Code-owned price for mechanical items; None for plain items (whose
    authored `value` stands)."""

def xp_for_quest(quest) -> int
def xp_for_enemy(npc) -> int

def grant_xp(session, p, amount: int, source: str) -> dict:
    """Adds XP, resolves any number of level-ups, returns the progression
    block (see 3.8). Caller commits."""

def roll_skill_check(p, skill: str, difficulty: str, modifier: str) -> dict:
    """Returns {"outcome": ..., "skill":..., "difficulty":..., "_debug": {...}}.
    The _debug key holds roll/DC/bonus FOR LOGS ONLY — callers must strip it
    before it can reach the DM."""

def apply_consumable(session, p, item_id: str, in_combat: bool) -> dict
def default_quick_slot(p) -> str | None
```

### 3.7 The combat formulas (this is the contract with game.js)

```python
HEALTH_FRACTION = {"healthy": 1.0, "winded": 0.8, "hurt": 0.6,
                   "badly_hurt": 0.4, "critical": 0.2, "dead": 0.0}
BASE_HEARTS = 5
BASE_DAMAGE = 1
BASE_IFRAME_MS = 350
HEART_CAP = 12

max_hearts = min(HEART_CAP, BASE_HEARTS + level_hearts + ARMOR_HEARTS[armor_tier])
hearts     = 0 if health == "dead" else max(1, round(max_hearts * HEALTH_FRACTION[health]))
damage     = BASE_DAMAGE + level_damage + WEAPON_DAMAGE[weapon_tier]
dodge_cd   = LEVEL_TABLE[level].dodge_cooldown_ms
iframe_ms  = BASE_IFRAME_MS + TRINKET_IFRAMES[trinket_tier]
```

**Backward-compatibility proof (asserted by `test_level1_unarmed_matches_legacy`):**
at level 1 with nothing equipped, `max_hearts = 5` and
`hearts = round(5 * f)` = 5 / 4 / 3 / 2 / 1 for healthy / winded / hurt /
badly_hurt / critical — byte-identical to today's
`{healthy:5, winded:4, hurt:3, badly_hurt:2, critical:1}` map in
`GameRenderer._playerHpFromHealth`, and `damage = 1`, today's flat value.

Ranges: hearts 5→12, damage 1→8.
**Contract for the enemy-tiers spec:** tune enemy HP so a tier-appropriate
`elite` takes **4-6 player hits** at the level the player is expected to meet it.
Today's `ARCHETYPES` HP (brute 7 / skirmisher 3 / stalker 4) is balanced against
`damage == 1` and stays correct for level 1-4 characters.

### 3.8 The progression block (returned by `grant_xp`)

```json
{
  "xp_gained": 120,
  "source": "quest",
  "xp": 340,
  "level": 3,
  "previous_level": 2,
  "leveled_up": true,
  "xp_into_level": 90,
  "xp_for_level": 200,
  "unlocked": ["Superior gear no longer fights you."],
  "stats": {"max_hearts": 6, "damage": 1}
}
```

`xp_for_level` is `XP_THRESHOLDS[level+1] - XP_THRESHOLDS[level]` (0 at level 10,
where `xp_into_level` is 0 and the HUD bar renders full).

---

## 4. XP grant sites (the only two, both code)

### 4.1 Quest completion — `src/tools/world_write/quests.py`

In `_apply_rewards`, **before** anything else:

```python
    # The DM authors quest rewards. XP is NOT theirs to author — strip and log.
    for forbidden in ("xp", "experience", "level", "levels"):
        if forbidden in rewards:
            logger.warning(f"Quest rewards tried to grant {forbidden}={rewards[forbidden]!r} — ignored")
            rewards = {k: v for k, v in rewards.items() if k != forbidden}
```

Gear rewards: an `items` entry that is a dict with `slot`/`tier` keys is built
through `Item(...)` with `value=catalog_price(...)` instead of whatever `value`
the DM wrote.

In `update_quest_status`, inside the existing `if status == QuestStatus.COMPLETED:`
branch and the same transaction:

```python
        progression = grant_xp(session, player, xp_for_quest(quest), "quest")
```

and the tool result gains:

```python
        if progression:
            result["progression"] = progression       # -> xp_gain / level_up SSE
```

`xp_for_quest` reads `quest.scale or "job"`.

**Idempotence:** `update_quest_status(quest, "completed")` called twice must not
double-pay. Guard: award only when `quest.status != QuestStatus.COMPLETED` on
entry (the existing code re-applies rewards today — this fixes that too).

### 4.2 Combat — `src/services/combat.py::apply_combat_result`

Signature gains `hearts_healed: int = 0`.

```python
        # Potions drunk mid-fight are real wounds that did not stick
        effective_hits = max(0, hits_taken - max(0, hearts_healed))
        ...
        floor = _HITS_TO_STATUS.get(min(effective_hits, 3))
```

(`defeat` still forces `critical` — you cannot potion your way out of being
downed.)

XP is awarded per NPC that **transitions to dead in this call** (the existing
loop already skips `npc.status == "dead"`), so a retried POST awards nothing:

```python
            xp_total += xp_for_enemy(npc)
        ...
        if xp_total and player.health_status != "dead":
            changes["progression"] = grant_xp(session, player, xp_total, "combat")
```

Defeat and flight award nothing beyond kills actually made.

`combat_outcome_message` gains, when a level was gained:

```
  "The character has grown from this (the engine already applied it). Acknowledge
   it ONCE, obliquely, in the fiction — never say 'level', 'XP', or any number."
```

---

## 5. Tools (DM-facing)

Only **two** new DM tools. Everything else the player does themselves.

### 5.1 `roll_check` — `src/tools/world_write/progression.py`

```python
@tool
def roll_check(
    player_id: str,
    skill: str,
    difficulty: str,
    modifier: str = "none",
    reason: str = "",
) -> dict[str, Any]:
    """Resolve a risky, uncertain action with an engine roll. Use this BEFORE
    narrating whether it worked.

    Call it for anything the player could plausibly fail: sneaking, lying,
    persuading, climbing, picking a lock, spotting an ambush, forcing a door,
    holding a poker face. Do NOT call it for safe actions (walking, asking a
    question, buying openly) or for real-time fights (that is `start_combat`).

    You choose the WORDS. The engine owns the numbers and will not tell you
    what they were — narrate the outcome it returns and nothing about how it
    was decided.

    Args:
        player_id: The player's ID.
        skill: One of "stealth", "persuasion", "athletics", "perception", "cunning".
        difficulty: One of "easy", "tricky", "hard", "desperate".
        modifier: "advantage" if the fiction clearly favours them (a distraction
            they set up, a lie backed by evidence, a rope they brought),
            "disadvantage" if it clearly works against them (wounded, watched,
            no tools), otherwise "none".
        reason: One short phrase for the log, e.g. "slipping past the dock guard".

    Returns:
        {"outcome": "critical_success"|"success"|"partial"|"failure"|"critical_failure", ...}
    """
```

Return dict:

```python
{
  "success": True,
  "outcome": "partial",
  "skill": "stealth",
  "difficulty": "tricky",
  "event": "check_result",
  "instruction": (
     "PARTIAL SUCCESS — they get what they wanted but it costs them something "
     "concrete. Apply that cost with a tool (time, health, an item, money, "
     "trust, standing). Narrate the fiction only: never mention rolls, dice, "
     "difficulty, or odds."
  ),
}
```

Per-outcome `instruction` text (the DM reads nothing else):

| outcome | instruction |
|---|---|
| `critical_success` | "CRITICAL SUCCESS — it works, and something extra breaks their way (an overheard secret, a faster route, a witness who now owes them). Narrate the fiction only." |
| `success` | "SUCCESS — it works cleanly. Narrate the fiction only." |
| `partial` | "PARTIAL SUCCESS — they get it, but it costs them something concrete. Apply the cost with a tool. Narrate the fiction only." |
| `failure` | "FAILURE — it does not work. Apply a concrete consequence with a tool (time lost, injury, item lost, money lost, trust or standing damaged). Do not soften it into a success. Narrate the fiction only." |
| `critical_failure` | "CRITICAL FAILURE — it fails and the situation gets worse: they are seen, the rope snaps, the lie is caught. Apply a real consequence with a tool, and consider whether this starts a fight (`start_combat`). Narrate the fiction only." |

Invalid `skill`/`difficulty`/`modifier` → `{"error": "...", "valid": [...]}`;
the DM retries with a legal word. Never silently coerce — a silent coercion is a
number the DM did not choose.

The `_debug` payload (roll, DC, bonuses) is written to `logger.info` inside the
tool and **stripped from the return value**.

### 5.2 `stock_merchant` — `src/tools/world_write/progression.py`

Tiered gear has to enter the world somewhere. This is the only path besides
quest rewards.

```python
@tool
def stock_merchant(npc_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill a merchant's stock with real, priced gear.

    Each entry is {"kind": ..., "tier"/"effect_tag": ..., "name": ..., "count": 1}:
      - kind "weapon" | "armor" | "trinket" -> requires "tier": one of
        "crude", "common", "fine", "superior", "master", "legendary"
      - kind "consumable" -> requires "effect_tag": "heal_light", "heal_strong"
        or "surge"
    "name" is yours (flavour: "Ashwood Blade", "Harbor Mail", "Dockhand's Draught").
    Prices, damage, and protection are set by the engine from the tier — do not
    invent numbers, and do not tell the player a price you have not read back
    from `get_inventory`.

    Stock what the merchant would plausibly have. A village smith carries crude
    and common; only a capital armourer carries master. Legendary is never for
    sale — it is a reward.
    """
```

Rejected legendary stock returns an error naming the reason, so the DM learns
the world's rule instead of quietly getting a 2000g sword on a shelf.

Returns `{"success": True, "npc": "Coldiron", "stocked": [{"name":..., "price":...}, ...]}`.

### 5.3 Existing tools that change

| Tool | Change |
|---|---|
| `transfer_item` | `total_cost = (catalog_price(source_item.to_dict()) or source_item.value) * quantity`. Purchases of tiered gear are priced by the table, not by whatever the LLM wrote into `value`. Also stamps the catalog price onto the copy that lands in the destination inventory. |
| `add_to_inventory` | New optional args `slot: str|None = None`, `tier: str|None = None`, `effect_tag: str|None = None`. When `slot`+`tier` are given, `value` is overwritten with `catalog_price`. Then **auto-equip** (see 6.3). |
| `use_item` | If the item has an `effect_tag`, delegate to `apply_consumable(..., in_combat=False)`. Otherwise keep the legacy `effects["heal"]` path untouched. |
| `create_item_template` | Accepts `slot`/`tier`/`effect_tag`; normalises `value` through `catalog_price`. |
| `get_inventory` | Each returned item gains `slot`, `tier`, `equipped: bool`, `price: int` so the DM can quote a shop price that is true. |
| `get_player` | Adds `"level"`, `"equipped"` (three human-readable strings), and **not** `xp` — the DM has no business narrating an XP total. |

---

## 6. REST endpoints (canvas + HUD, zero DM latency)

All live in `src/web/server.py`, all call `init_db(get_active_db_path())` first,
all return a fresh `build_player_state(player_id)` under `"state"` so the HUD
re-renders from one payload, and all append a line to the pending-notes ledger
(section 7).

### 6.1 `POST /api/player/{player_id}/equip`

```jsonc
// request
{"item_id": "harbor_mail"}
// 200
{"success": true, "slot": "armor", "item_name": "Harbor Mail",
 "replaced": "Padded Coat" /* or null */,
 "equipment": {"weapon": "iron_sword", "armor": "harbor_mail", "trinket": null},
 "state": { /* build_player_state */ }}
// 400
{"error": "Harbor Mail is beyond you for now.", "reason": "level",
 "required_level": 5, "level": 3}
{"error": "That is not something you can wear or wield.", "reason": "not_equipment"}
{"error": "Not in your inventory.", "reason": "missing"}
```

`reason` codes: `missing` | `not_equipment` | `level`.

### 6.2 `POST /api/player/{player_id}/unequip`

```jsonc
{"slot": "weapon"}          // -> {"success": true, "equipment": {...}, "state": {...}}
```

### 6.3 Auto-equip (no endpoint — a rule)

When an equippable item enters the player's inventory (`add_to_inventory`,
`transfer_item` to a player, quest reward gear) **and** its slot is empty **and**
`can_equip` passes, code equips it immediately. Most players will never open the
panel; a sword that sits in the bag doing nothing is a bug report. The pending
note says so, and the state event carries the new numbers.

### 6.4 `POST /api/player/{player_id}/quick-slot`

```jsonc
{"item_id": "healing_draught"}   // or {"item_id": null} to clear
// -> {"success": true, "quick_slot": {"item_id":..., "name":..., "quantity": 3}, "state": {...}}
```

### 6.5 `POST /api/player/{player_id}/use-consumable`

```jsonc
// request
{"item_id": "healing_draught", "in_combat": true}
// 200 (in combat)
{"success": true, "item_name": "Healing Draught",
 "effect": {"kind": "heal", "hearts": 2, "buff": null, "duration_ms": 0},
 "remaining": 2, "state": {...}}
// 200 (out of combat)
{"success": true, "item_name": "Healing Draught",
 "effect": {"kind": "heal", "hearts": 0, "health": "hurt -> winded"},
 "remaining": 2, "state": {...}}
// 400
{"error": "Nothing left to drink.", "reason": "missing"}
{"error": "That does nothing when swallowed.", "reason": "not_consumable"}
```

The client applies the effect **optimistically** the instant Q is pressed and
reverts the hearts if the response is an error. A potion must not cost a network
round-trip during a lunge telegraph.

### 6.6 `POST /api/combat/result` (extended)

```jsonc
// request — one new field
{"player_id": "...", "outcome": "victory", "hits_taken": 3,
 "hearts_healed": 2, "defeated_npc_ids": ["npc-1"]}
// response — one new field
{"success": true, "changes": {...}, "dm_message": "...", "state": {...},
 "progression": { /* section 3.8 block, or absent when no XP was earned */ }}
```

`CombatResultRequest` gains `hearts_healed: int = 0` (defaulted, so an
un-updated client keeps working).

### 6.7 `build_player_state` (extended)

Added keys — **the frontend must never derive stats from raw items**:

```jsonc
{
  "...": "existing keys unchanged",
  "level": 4,
  "xp": 520,
  "xp_into_level": 70,
  "xp_for_level": 250,
  "equipment": {
    "weapon":  {"item_id": "iron_sword",  "name": "Iron Sword",  "tier": "fine"},
    "armor":   {"item_id": "harbor_mail", "name": "Harbor Mail", "tier": "common"},
    "trinket": null
  },
  "combat": {
    "max_hearts": 7,
    "hearts": 5,
    "damage": 3,
    "dodge_cooldown_ms": 700,
    "iframe_ms": 350,
    "quick_slot": {"item_id": "healing_draught", "name": "Healing Draught",
                   "effect_tag": "heal_light", "hearts": 2, "quantity": 3}
  }
}
```

Each `inventory` entry additionally carries `slot`, `tier`, `effect_tag`,
`equipped`, `equippable` (bool — `can_equip` result), `price`.

`build_player_state` runs after **every** successful state-mutating tool, so
`combat_stats` must be pure dict math over the already-loaded `Player` row. It
is. Do not add a query.

`STATE_MUTATING_TOOLS` gains `"stock_merchant"` (a merchant's stock changing does
not move the player HUD, but its price echo does show up in the DM's next
`get_inventory`; cheap, and it keeps the set honest). `roll_check` is **not**
added — it mutates nothing.

---

## 7. Telling the DM, for free

New process-local ledger in `server.py`, right next to `_pending_audit_feedback`:

```python
# Canvas/HUD actions the player took between turns. Drained into the DM's next
# context — these are ALREADY TRUE; the DM only has to acknowledge them.
_pending_canvas_notes: dict[str, list[str]] = {}
_MAX_CANVAS_NOTES = 8      # oldest dropped; a wall of notes eats the turn

def _note_canvas_action(player_id: str, text: str) -> None: ...
def _drain_canvas_notes(player_id: str) -> list[str]: ...
```

Written by: equip, unequip, use-consumable, and the combat-result endpoint when
`progression.leveled_up`. Drained in `/api/play` immediately after the audit
feedback block:

```python
            notes = _drain_canvas_notes(request.player_id)
            if notes:
                context += format_canvas_notes(notes)
```

`format_canvas_notes` (put it in `src/web/turn_audit.py` next to
`format_audit_feedback`, or a new `src/web/turn_context.py` — either is fine,
pick one and be consistent):

```
[SINCE YOUR LAST TURN — the player acted directly on the canvas. These are
ALREADY TRUE in the database. Weave them into the fiction if they matter;
never contradict them and never re-apply them with tools:
- They drank a Healing Draught mid-fight (2 left).
- They now wear Harbor Mail.
- They have grown from what they survived. Acknowledge it ONCE, obliquely —
  they carry themselves differently now. Never say "level", "XP", or a number.]
```

Note templates (fixed strings, code-owned):

| Action | Note |
|---|---|
| equip | `They now {wield|wear|carry} {name}.` (weapon→wield, armor→wear, trinket→carry) |
| unequip | `They put away their {name}.` |
| auto-equip | `They immediately {wield|wear|carry} the {name} they just got.` |
| consumable (heal) | `They drank a {name}{ mid-fight}, and look steadier ({n} left).` |
| consumable (surge) | `They drank a {name} — for the next moments they hit harder ({n} left).` |
| level up | `They have grown from what they survived. Acknowledge it ONCE, obliquely — never say "level", "XP", or a number.` |

---

## 8. SSE events

### 8.1 New payloads

```jsonc
{"type": "xp_gain", "amount": 120, "source": "quest",
 "xp": 340, "level": 3, "xp_into_level": 90, "xp_for_level": 200}

{"type": "level_up", "level": 3, "previous_level": 2,
 "unlocked": ["Superior gear no longer fights you."],
 "stats": {"max_hearts": 6, "damage": 1}}

{"type": "check_result", "outcome": "partial", "skill": "stealth"}
```

### 8.2 `src/web/streaming.py::_parse_tool_output_for_events`

Two changes:

1. New branch in the existing `elif` chain:

```python
            elif event == "check_result":
                self._notifications.append({
                    "type": "check_result",
                    "outcome": output_data.get("outcome"),
                    "skill": output_data.get("skill"),
                })
```

2. **After** the chain (not inside it — a quest-completion result carries both
   `event: "quest_completed"` and a `progression` block):

```python
            prog = output_data.get("progression")
            if isinstance(prog, dict) and prog.get("xp_gained"):
                self._notifications.append({
                    "type": "xp_gain",
                    "amount": prog["xp_gained"], "source": prog.get("source"),
                    "xp": prog.get("xp"), "level": prog.get("level"),
                    "xp_into_level": prog.get("xp_into_level"),
                    "xp_for_level": prog.get("xp_for_level"),
                })
                if prog.get("leveled_up"):
                    self._notifications.append({
                        "type": "level_up",
                        "level": prog.get("level"),
                        "previous_level": prog.get("previous_level"),
                        "unlocked": prog.get("unlocked") or [],
                        "stats": prog.get("stats") or {},
                    })
```

The combat path never streams — its `progression` block rides the
`/api/combat/result` JSON response and the frontend fires the same two UI
handlers directly.

---

## 9. Frontend

Bump the cache-buster in `index.html` (line ~1700):
`?v=20260724a` → **`?v=20260725a`** on both `game.js` and `audio.js`.

### 9.1 `static/js/game.js`

**A. `_playerHpFromHealth()` (line ~1615) → `_playerCombatStats()`**

```js
    /** Player combat numbers come from the SERVER (level + gear + health).
     *  The fallback is exactly the pre-progression behaviour, so an old
     *  server or a state event that has not arrived yet still plays. */
    _playerCombatStats() {
        const c = (window.lastPlayerState && window.lastPlayerState.combat) || null;
        if (c && typeof c.max_hearts === 'number') {
            return {
                maxHearts: c.max_hearts, hearts: c.hearts,
                damage: c.damage || 1,
                dodgeCd: c.dodge_cooldown_ms || 750,
                iframeMs: c.iframe_ms || 350,
                quickSlot: c.quick_slot || null,
            };
        }
        const map = { healthy: 5, winded: 4, hurt: 3, badly_hurt: 2, critical: 1 };
        const h = (window.lastPlayerState && window.lastPlayerState.health) || 'healthy';
        return { maxHearts: 5, hearts: map[h] ?? 5, damage: 1, dodgeCd: 750,
                 iframeMs: 350, quickSlot: null };
    }
```

**B. `_ensureCombat()` (line ~1621)** — take the stats once per fight and keep
them on `this.combat` (a mid-fight state event must not silently re-roll the
player's HP):

```js
        const st = this._playerCombatStats();
        this.combat = {
            enemies: [], playerHp: st.hearts, playerMaxHp: st.maxHearts,
            stats: st, heartsHealed: 0, buffDamage: 0, buffUntil: 0,
            hitsTaken: 0, reported: false,
            iframes: 0, dodge: 0, dodgeVec: [0, 1], dodgeCd: 0,
            attackCd: 0, hitstop: 0, shake: 0,
        };
```

**C. Dodge (line ~1878-1881)** — `c.iframes = c.stats.iframeMs;`
`c.dodgeCd = c.stats.dodgeCd;`

**D. Attack (line ~1913)** — `e.hp -= this._playerDamage();` where

```js
    _playerDamage() {
        const c = this.combat;
        const buff = (c.buffUntil > performance.now()) ? c.buffDamage : 0;
        return Math.max(1, (c.stats.damage || 1) + buff);
    }
```

**E. New: `_useQuickSlot()`** — called from the key handler on `q`:

```js
    async _useQuickSlot() {
        const c = this.combat;
        const slot = (c ? c.stats.quickSlot : null) ||
                     (window.lastPlayerState?.combat?.quick_slot);
        if (!slot || !slot.item_id || !this.playerId) return;
        if (this._quickCd && performance.now() < this._quickCd) return;
        this._quickCd = performance.now() + 600;

        // Optimistic: the fight does not wait for the network
        const healed = c ? Math.min(slot.hearts || 0, c.playerMaxHp - c.playerHp) : 0;
        if (c && healed > 0) {
            c.playerHp += healed; c.heartsHealed += healed; this._drawHearts();
        }
        this.spawnFloater(`+ ${slot.name}`, 0x4ade80);
        window.forgeAudio?.playSfx('clash', { volume: 0.5 });

        try {
            const res = await fetch(`/api/player/${this.playerId}/use-consumable`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ item_id: slot.item_id, in_combat: !!c }),
            });
            const data = await res.json();
            if (!data.success) throw new Error(data.error || 'no');
            if (data.effect && data.effect.buff === 'damage' && c) {
                c.buffDamage = data.effect.bonus || 1;
                c.buffUntil = performance.now() + (data.effect.duration_ms || 20000);
            }
            window.dispatchEvent(new CustomEvent('forge-state', { detail: data.state }));
        } catch (e) {
            if (c && healed > 0) {                       // roll back
                c.playerHp -= healed; c.heartsHealed -= healed; this._drawHearts();
            }
            this.spawnFloater('nothing left', 0x9ca3af);
        }
    }
```

**F. Key handler (line ~223)** — add `q` alongside space/j:

```js
            // Combat verbs: space = dodge, j = attack, q = quick item
            if (e.key === ' ' || key === 'j') { ... }
            if (key === 'q' && !e.ctrlKey) {
                this._useQuickSlot();
                if (this.combat) e.preventDefault();
            }
```

**G. `_finishCombat()` (line ~1804)** — detail gains `heartsHealed: c.heartsHealed || 0`.

**H. New: `spawnLevelUpBurst()`** — reuses the existing floater + particle
machinery; no new assets:

```js
    /** The moment a level lands: a gold ring of particles + a floater. */
    spawnLevelUpBurst(level) {
        this.spawnFloater(`Level ${level}`, 0xfbbf24);
        if (!this.player || !this.player.sprite) return;
        for (let i = 0; i < 14; i++) {
            const a = (i / 14) * Math.PI * 2;
            const g = new PIXI.Graphics();
            g.circle(0, 0, 3); g.fill({ color: 0xfbbf24, alpha: 0.9 });
            g.x = this.player.sprite.x;
            g.y = this.player.sprite.y - this.player.sprite.height * 0.5;
            this.particleLayer.addChild(g);
            this.particles.push({ g, vx: Math.cos(a) * 0.06, vy: Math.sin(a) * 0.06,
                                  life: 0, maxLife: 900, startAlpha: 0.9 });
        }
    }
```

**I. New: `showCheckResult(outcome)`** — the dice flick:

```js
    static CHECK_CUE = {
        critical_success: ['⚄ Success', 0x4ade80],
        success:          ['⚄ Success', 0x4ade80],
        partial:          ['⚄ Barely',  0xfbbf24],
        failure:          ['⚄ Failed',  0xf87171],
        critical_failure: ['⚄ Failed',  0xef4444],
    };
    showCheckResult(outcome) {
        const cue = GameRenderer.CHECK_CUE[outcome];
        if (cue) this.spawnFloater(cue[0], cue[1]);
    }
```

That is the entire player-visible surface of the dice: a glyph, a word, a colour.
No number, no DC, no "you rolled a 14".

### 9.2 `static/index.html`

**A. HUD chip row (line ~1241)** — a new level chip before health:

```html
            <span class="hud-chip clickable" id="hud-level-chip" title="Character">
                &#11041; Lv <span id="hud-level">1</span>
                <span class="xp-bar"><i id="hud-xp-fill"></i></span>
            </span>
```

CSS next to the other `.hud-chip` rules:

```css
        .xp-bar { display:inline-block; width:34px; height:3px; margin-left:6px;
                  background:rgba(255,255,255,0.18); border-radius:2px; vertical-align:middle; }
        .xp-bar i { display:block; height:100%; width:0%; background:#fbbf24; border-radius:2px;
                    transition:width .45s ease; }
        .hud-item button { background:rgba(255,255,255,0.10); border:1px solid rgba(255,255,255,0.18);
                  color:inherit; border-radius:6px; font:inherit; font-size:0.72rem;
                  padding:2px 7px; margin-left:6px; cursor:pointer; }
        .hud-item button:hover { background:rgba(255,255,255,0.20); }
        .hud-item button[disabled] { opacity:0.35; cursor:default; }
        .toast.level { border-color:#fbbf24; }
```

**B. `applyPlayerState()` (line ~2110)** — after the gold/quests/bag lines:

```js
            document.getElementById('hud-level').textContent = state.level ?? 1;
            const fill = document.getElementById('hud-xp-fill');
            const span = state.xp_for_level || 0;
            fill.style.width = span ? `${Math.min(100, 100 * (state.xp_into_level || 0) / span)}%` : '100%';
```

**C. `spawnStateFloaters()` (line ~2079)** — level floater safety net for the
combat REST path (deduped so the SSE `level_up` handler and this cannot both
fire):

```js
            if ((state.level ?? 1) > (prev.level ?? 1) && !levelUpShown) {
                showLevelUp({ level: state.level, unlocked: [] });
            }
```

**D. New `showLevelUp(payload)`** near `showToast`:

```js
        let levelUpShown = 0;   // debounce: level number already announced
        function showLevelUp(p) {
            if (!p || !p.level || levelUpShown === p.level) return;
            levelUpShown = p.level;
            const lines = (p.unlocked || []).map(u => `<div class="muted">${escapeHtml(u)}</div>`).join('');
            showToast(`&#11041; <b>Level ${p.level}</b>${lines}`, 'level', 5200);
            if (game && game.spawnLevelUpBurst) game.spawnLevelUpBurst(p.level);
            window.forgeAudio?.playStinger('triumph', 9000);
        }
```

**E. SSE switch (line ~2841 region)** — three new branches:

```js
                                    } else if (payload.type === 'level_up') {
                                        showLevelUp(payload);
                                    } else if (payload.type === 'xp_gain') {
                                        if (game && game.spawnFloater && payload.amount)
                                            game.spawnFloater(`+${payload.amount} XP`, 0xa5b4fc);
                                    } else if (payload.type === 'check_result') {
                                        if (game && game.showCheckResult)
                                            game.showCheckResult(payload.outcome);
```

**F. `combat-ended` handler (line ~2595)** — send hearts healed, react to XP:

```js
            const { outcome, hitsTaken, defeated, heartsHealed } = e.detail || {};
            ...  body: JSON.stringify({ ..., hearts_healed: heartsHealed || 0 }),
            ...
                if (data.progression) {
                    if (data.progression.xp_gained && game?.spawnFloater)
                        game.spawnFloater(`+${data.progression.xp_gained} XP`, 0xa5b4fc);
                    if (data.progression.leveled_up) showLevelUp(data.progression);
                }
```

**G. `forge-state` listener** (new, one line) so `_useQuickSlot` can push a fresh
state without knowing about `applyPlayerState`:

```js
        window.addEventListener('forge-state', (e) => { if (e.detail) applyPlayerState(e.detail); });
```

**H. `renderHudPanel()` (line ~2140)** — inventory rows get buttons, plus a new
`'character'` panel mode:

```js
            if (hudPanelMode === 'inventory') {
                const items = lastPlayerState.inventory || [];
                panel.innerHTML = '<h4>Inventory</h4>' + (items.length
                    ? items.map((i, idx) => {
                        const btns = [];
                        if (i.equipped) btns.push(`<button data-act="unequip" data-slot="${i.slot}">Unequip</button>`);
                        else if (i.equippable) btns.push(`<button data-act="equip" data-item="${escapeHtml(i.item_id)}">Equip</button>`);
                        else if (i.effect_tag) {
                            btns.push(`<button data-act="use" data-item="${escapeHtml(i.item_id)}">Use</button>`);
                            btns.push(`<button data-act="quick" data-item="${escapeHtml(i.item_id)}">Q</button>`);
                        }
                        return `<div class="hud-item"><span>${escapeHtml(i.name)}${i.quantity > 1 ? ` &times;${i.quantity}` : ''}` +
                               (i.tier ? ` <span class="muted">(${escapeHtml(i.tier)})</span>` : '') + `</span>` +
                               `<span>${btns.join('')}</span></div>`;
                      }).join('')
                    : '<div class="muted">Empty. Go find something shiny.</div>');
            } else if (hudPanelMode === 'character') {
                const s = lastPlayerState, eq = s.equipment || {}, c = s.combat || {};
                panel.innerHTML = '<h4>Character</h4>' +
                    `<div class="hud-item"><span>Level</span><span>${s.level ?? 1}</span></div>` +
                    `<div class="hud-item"><span>Vitality</span><span>${c.hearts ?? '?'} / ${c.max_hearts ?? '?'} hearts</span></div>` +
                    `<div class="hud-item"><span>Strike</span><span>${c.damage ?? 1}</span></div>` +
                    ['weapon','armor','trinket'].map(sl =>
                        `<div class="hud-item"><span class="muted">${sl}</span><span>` +
                        (eq[sl] ? `${escapeHtml(eq[sl].name)} <span class="muted">(${escapeHtml(eq[sl].tier)})</span>` : '—') +
                        `</span></div>`).join('');
            }
```

Panel click delegation (one listener, added once near the chip listeners):

```js
        document.getElementById('hud-panel').addEventListener('click', async (ev) => {
            const b = ev.target.closest('button[data-act]'); if (!b || !currentPlayerId) return;
            const act = b.dataset.act;
            const url = { equip: 'equip', unequip: 'unequip', use: 'use-consumable', quick: 'quick-slot' }[act];
            const body = act === 'unequip' ? { slot: b.dataset.slot }
                       : act === 'use' ? { item_id: b.dataset.item, in_combat: false }
                       : { item_id: b.dataset.item };
            b.disabled = true;
            try {
                const r = await fetch(`/api/player/${currentPlayerId}/${url}`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body) });
                const d = await r.json();
                if (d.state) applyPlayerState(d.state);
                else if (d.error) showToast(escapeHtml(d.error), '', 2600);
            } finally { b.disabled = false; }
        });
        document.getElementById('hud-level-chip').addEventListener('click', () => toggleHudPanel('character'));
```

**I. Controls overlay (line ~2192)** — add one row: `<b>Q</b><span>drink your quick item (in combat)</span>`.

### 9.3 `static/js/audio.js`

One branch in `handlePlayEvent` (line ~111):

```js
            } else if (payload.type === 'level_up') {
                this.playStinger('triumph', 9000);
```

`showLevelUp` also calls `playStinger`; the existing `_clearStinger` guard makes
the double call a no-op re-trigger, which is fine — but prefer removing the call
from `showLevelUp` if the SSE path is the only source. Keep both: the combat REST
path has no SSE event, so `showLevelUp` needs its own.

No new SFX assets in v1 (rejected: a dice-clatter one-shot — it would need an
ElevenLabs generation pass and the floater already sells it).

---

## 10. DM prompt changes (`src/agents/dm_orchestrator.py`)

### 10.1 REALITY CONTRACT — two new bullets

Insert after the "Money changed" bullet:

```
- Risky, uncertain actions — sneaking, lying, persuading, climbing, picking a
  lock, spotting a trap, forcing a door — are decided by `roll_check`, NOT by
  you. Call it BEFORE you narrate whether it worked, then narrate the outcome
  it hands back. Never decide a risky action's result yourself; never re-roll
  because you disliked the answer; never soften a failure into a success.
- XP, levels, and gear numbers are the ENGINE'S. You never grant experience,
  never announce a level, never state damage values, prices you have not read
  from `get_inventory`, or how much protection something gives. When the engine
  tells you the character has grown, acknowledge it ONCE, obliquely, in the
  fiction — never with the words "level", "XP", or a number.
```

### 10.2 SUCCESS & FAILURE — rewrite point 1, add point 6

```
1. **Risk class**: *safe* (walking, talking, browsing, buying openly) → it just
   works, no tool needed. *Risky* (sneaking, haggling, climbing, lying,
   pickpocketing, searching under pressure) → call `roll_check` and narrate what
   it returns. *Dangerous with a present, named NPC* (a fight) → `start_combat`.
   *Dangerous with no NPC entity* (traps, falls, beasts with no database entry)
   → `roll_check` with difficulty "hard" or "desperate", then apply the
   consequence with `update_player_health`.
...
6. **The roll is final.** `roll_check` returns one of: critical_success,
   success, partial, failure, critical_failure. Narrate that outcome. If the
   player argues, the world does not re-decide — they may try again with a
   different approach, which is a NEW check with a difficulty you choose to
   reflect the new plan and the noise they have already made.
```

### 10.3 New section, placed after "GUIDELINES FOR SPECIFIC SITUATIONS → 3. Combat & Danger"

```
**4. Skill Checks (the engine decides, you narrate):**
- `roll_check(player_id, skill, difficulty, modifier, reason)` — call it the
  moment the player attempts something they could fail at, BEFORE narrating a
  result.
- `skill`: "stealth", "persuasion", "athletics", "perception", "cunning".
- `difficulty`: "easy" (an unhurried try with time and cover), "tricky" (a
  competent obstacle: an alert guard, a suspicious merchant), "hard" (a real
  professional, a wall built to stop people), "desperate" (it should probably
  not work at all).
- `modifier`: "advantage" when the fiction clearly favours them — a distraction
  they set up, a lie backed by evidence in their inventory, the right tool, a
  friend running interference. "disadvantage" when it clearly does not — badly
  hurt, watched, empty-handed, already caught lying once. Otherwise "none".
- Choose the difficulty from the FICTION, not from what you want to happen. A
  drunk guard is "easy" even if you would rather the scene got tense.
- Never mention the check, the odds, the difficulty word, or any dice in your
  narration. The player sees only what happens in the world.

**5. Gear, Coin & Growth:**
- Weapons, armour and trinkets have tiers: crude, common, fine, superior,
  master, legendary — in that order. You pick the WORD; the engine sets the
  numbers and the price.
- Merchants get real, priced stock through `stock_merchant`. Stock what they
  would plausibly have: a village smith carries crude and common; only a
  capital armourer carries master. Legendary gear is never sold — it is won.
- Selling to the player: read the price back with `get_inventory` and quote
  THAT. Then `transfer_item` with is_purchase=True — the engine charges them.
  Never invent a price.
- Potions and draughts exist and the player drinks them themselves, mid-fight,
  without telling you. If a message says they did, it already happened.
- The player's growth is the engine's business. You may say a fight left them
  changed; you may never say what they gained.
```

### 10.4 `DM_TOOLS`

Add `roll_check` and `stock_merchant` (import from
`src.tools.world_write`, exported from its `__init__`).

### 10.5 `_build_context`

Add the equipped line and level to the briefing (the DM must not have to call a
tool to know what the player is holding):

```python
        loadout = player.get("equipped") or {}
        gear = ", ".join(f"{slot}: {name}" for slot, name in loadout.items() if name) or "nothing notable"
        ...
- Player level: {player.get('level', 1)}
- Player carries: {gear}
```

`get_player` supplies `equipped` as `{"weapon": "Iron Sword (fine)", ...}`.

### 10.6 `tests/test_dm_prompt_tools.py`

`TOOL_PREFIXES` gains `"roll_"` and `"stock_"` so the new prompt references are
actually enforced. **Do not** backtick the equip/unequip/use-consumable
endpoints in the prompt — they are not tools, and a `` `equip_item` `` in the
prompt would either fail the test or push someone to register a tool the design
deliberately does not have.

---

## 11. REALITY AUDIT changes (`src/web/turn_audit.py`)

### 11.1 Signature

```python
def audit_turn(turn_text, tools, state_before, state_after,
               code_events: set[str] | None = None) -> list[dict[str, str]]:
```

Defaulted, so existing callers and `tests/test_turn_filters.py` keep passing.

`server.py` builds the set for the turn:

```python
            code_events: set[str] = set()
            if drained_canvas_notes:      # captured before the stream started
                code_events.add("canvas_action")
            if any(n.get("type") == "level_up" for n in turn_notifications):
                code_events.add("level_up")
```

(`turn_notifications` = a list the `/api/play` loop already has in hand as it
drains notifications; collect the `type` values as it goes.)
The canvas-note drain happens at context build time, so remember whether the
block contained a level-up line and add `"level_up"` for that too — a level
earned in the previous turn's fight is narrated in THIS turn.

### 11.2 New check: `risky_action_without_check`

This is the check that kills "everything is negotiable". It fires only on
**resolution** phrases — sentences that assert a risky attempt SUCCEEDED — never
on attempts.

```python
_RISK_RESOLUTION = re.compile(
    r"\byou\b[^.!?\n]{0,40}\b(?:"
    r"slip past|sneak past|slip by|slip inside|go unnoticed|remain unseen|"
    r"are not seen|pass unnoticed|"
    r"manage to (?:convince|persuade|talk|climb|pry|force|pick|slip|reach)|"
    r"pick the lock|force the lock|jimmy the|"
    r"scale the|haul yourself (?:up|over)|clear the (?:wall|gap|fence)|"
    r"convince(?!\s+yourself)|persuade(?!\s+yourself)|"
    r"talk (?:him|her|them|it) (?:into|down|around|out of)|"
    r"pickpocket|lift the (?:purse|key|coin|pouch)|"
    r"catch the (?:glint|movement|flicker)|spot the (?:tripwire|ambush|trap)"
    r")\b",
    re.IGNORECASE,
)
_CHECK_TOOLS = {"roll_check"}
_RISK_EXEMPT_TOOLS = {"roll_check", "start_combat"}
```

```python
    if _RISK_RESOLUTION.search(turn_text) and not (succeeded & _RISK_EXEMPT_TOOLS):
        warnings.append({
            "check": "risky_action_without_check",
            "detail": (
                "Narration resolves a risky action (sneaking, persuading, climbing, "
                "lock-picking, spotting) in the player's favour without calling "
                "roll_check. You decided an uncertain outcome yourself — the engine "
                "decides those. Call roll_check BEFORE narrating whether it worked."
            ),
        })
```

Precision notes (all deliberate): the `(?!\s+yourself)` guards kill "you convince
yourself"; a live `start_combat` exempts the turn because a fight resolves on the
canvas and its prose legitimately describes closing distance; anchoring on `you`
within 40 characters keeps NPC-on-NPC prose out.

### 11.3 New check: `progression_claim_without_engine`

```python
_LEVEL_CLAIM = re.compile(
    r"\b(?:level(?:ed)?\s*up|gain(?:ed)?\s+(?:a\s+)?level|you\s+are\s+now\s+level"
    r"|experience\s+points?|\bXP\b|you\s+feel\s+(?:stronger|more\s+experienced))\b",
    re.IGNORECASE,
)
```

```python
    if _LEVEL_CLAIM.search(turn_text):
        warnings.append({
            "check": "progression_claim_without_engine",
            "detail": (
                "Narration names levels or experience. Progression is engine-owned "
                "and invisible: never write 'level', 'XP', 'experience points', or "
                "'you feel stronger'. If the engine told you the character grew, "
                "show it in the fiction instead — how they carry themselves, what "
                "they no longer flinch at."
            ),
        })
```

Note this one fires **regardless** of `code_events`: even a real level-up must
not be narrated in mechanical language. `code_events` is not a licence to say
"level".

### 11.4 Suppression (no false positives on code-path actions)

Three existing checks must learn about code paths:

| Check | Adaptation |
|---|---|
| `item_without_tool` | Already escapes on `inventory_changed`. A potion drunk between turns changes the inventory **before** `state_before` is snapshotted, so a later "you finish the last of the draught" would fire. Add: `and "canvas_action" not in code_events`. |
| `damage_without_tool` | A potion or a fight healed/hurt them via code. Already escapes on `health_changed`; add `and "canvas_action" not in code_events` for the between-turn case. |
| `consequences_without_tools` | Unchanged — `roll_check` failures still require a real consequence tool, which is the point. |

`format_audit_feedback` is unchanged.

---

## 12. Build checklist (implementation order)

Each step leaves the tree green. Do not reorder 1-4.

1. **`src/services/progression.py`** — all tables from §3, plus `player_level`,
   `player_xp`, `tier_index`, `mechanize_item`, `resolve_loadout`,
   `combat_stats`, `catalog_price`, `set_seed`. Pure; no DB writes yet.
   → `tests/test_progression_tables.py`, `tests/test_equipment.py` (stat halves).
2. **Schema** — the four `players` columns, `quests.scale`, the three `Item`
   fields. Run the app once against `data/emberfall.db` and confirm the
   `[migrate] Added column` lines and that play still works.
   → `tests/test_progression.py::test_legacy_player_row_defaults`.
3. **`grant_xp`, `xp_for_quest`, `xp_for_enemy`, `equip`, `unequip`,
   `apply_consumable`, `roll_skill_check`** in `progression.py` (these take a
   session).
   → `tests/test_progression.py`, `tests/test_roll_check.py`.
4. **Quest XP** — `_apply_rewards` XP-stripping + gear normalisation,
   `update_quest_status` grant + idempotence guard + `progression` in the result.
   → `tests/test_progression.py::test_quest_*`.
5. **Combat XP** — `apply_combat_result(hearts_healed=...)`, per-kill XP,
   `progression` in `changes`, the level-up line in `combat_outcome_message`.
   `CombatResultRequest.hearts_healed`, `/api/combat/result` response field.
   → `tests/test_progression.py::test_combat_*`, `tests/test_consumables.py::test_hearts_healed_*`.
6. **Item/economy tools** — `catalog_price` in `transfer_item`, new args on
   `add_to_inventory` / `create_item_template`, `effect_tag` in `use_item`,
   enriched `get_inventory`, `get_player` gaining `level`/`equipped`.
   Auto-equip rule.
   → `tests/test_shop_pricing.py`, `tests/test_equipment.py::test_auto_equip_*`.
7. **New tools** — `src/tools/world_write/progression.py` (`roll_check`,
   `stock_merchant`), exported from `world_write/__init__.py`.
   → `tests/test_roll_check.py::test_event_key`, `tests/test_shop_pricing.py::test_stock_merchant_*`.
8. **`build_player_state`** — `level`, `xp`, `xp_into_level`, `xp_for_level`,
   `equipment`, `combat`, enriched inventory entries. Add `stock_merchant` to
   `STATE_MUTATING_TOOLS`.
9. **REST endpoints** — equip / unequip / quick-slot / use-consumable, plus
   `_pending_canvas_notes` + `format_canvas_notes` + the `/api/play` drain.
10. **`streaming.py`** — `check_result` branch and the post-chain `progression`
    fan-out into `xp_gain` + `level_up`.
11. **`turn_audit.py`** — `code_events` parameter, the two new checks, the two
    suppressions; wire the set in `/api/play`.
    → `tests/test_turn_audit_progression.py`.
12. **DM prompt** — §10.1-10.5, `DM_TOOLS`, `TOOL_PREFIXES` in the prompt test.
    → `uv run pytest tests/test_dm_prompt_tools.py`.
13. **`game.js`** — §9.1 A-I.
14. **`index.html`** — §9.2 A-I. **Bump `?v=` to `20260725a` on both scripts.**
15. **`audio.js`** — §9.3.
16. **`CLAUDE.md`** — a "Progression" bullet under *Mechanical consequences*
    and a line in the *Real-time combat* bullet noting hearts/damage now come
    from the state payload.
17. Play a world end-to-end: take a quest, kill something, watch the XP floater,
    hit level 2, buy a `common` sword from a stocked merchant, equip it from the
    HUD, watch enemy HP drop twice as fast, drink a potion mid-fight with Q,
    and get the DM to sneak you past a guard (confirm the `⚄` floater and that
    the prose never mentions a roll).

---

## 13. Tests

`tests/test_progression_tables.py`
- `test_xp_thresholds_monotonic_and_cover_1_to_10` — strictly increasing, keys 1..10.
- `test_level_table_covers_every_level` — 1..10, `gear_tier_cap` non-decreasing.
- `test_tier_effect_tables_have_six_entries` — weapon/armor/trinket/price all len 6.
- `test_enemy_xp_covers_every_archetype_fallback` — every value of `_ARCHETYPE_TO_TIER` is a key of `ENEMY_XP`.

`tests/test_progression.py`
- `test_legacy_player_row_defaults` — a `Player` with `level=None, xp=None, equipment=None` reads as level 1, 0 XP, empty loadout, and `combat_stats` returns the legacy numbers.
- `test_grant_xp_levels_once_per_threshold` — 99 XP stays level 1; 100 becomes level 2.
- `test_grant_xp_multi_level_jump` — one 1000-XP grant from level 1 lands on level 6 and `unlocked` lists every crossed level's text.
- `test_xp_caps_at_level_10` — granting 99999 leaves level 10, `xp_for_level == 0`.
- `test_quest_completion_awards_scale_xp` — `scale="mission"` completion grants 220.
- `test_quest_null_scale_reads_as_job` — 120.
- `test_quest_rewards_cannot_inject_xp` — `rewards={"xp": 9999, "gold": 10}` grants gold and the scale XP only.
- `test_quest_completion_is_idempotent` — completing twice pays gold and XP once.
- `test_combat_victory_awards_enemy_xp` — killing a brute grants `ENEMY_XP["elite"]`.
- `test_combat_repeat_post_awards_nothing` — same `defeated_npc_ids` twice → XP once.
- `test_combat_defeat_awards_no_xp`.
- `test_dead_player_gains_no_xp`.

`tests/test_equipment.py`
- `test_level1_unarmed_matches_legacy` — `combat_stats` for all five health values equals `{5,4,3,2,1}` hearts and damage 1.
- `test_armor_tier_adds_hearts` / `test_weapon_tier_adds_damage` / `test_trinket_tier_adds_iframes`.
- `test_hearts_respect_health_fraction_at_high_level` — level 10 + master armour, `hurt` → `round(max*0.6)`, never below 1.
- `test_heart_cap` — nothing exceeds `HEART_CAP`.
- `test_equip_blocked_below_tier_cap_level` — level 1 equipping `superior` → `(False, "level")`.
- `test_equip_rejects_plain_item` — a `misc` note → `(False, "not_equipment")`.
- `test_legacy_typed_weapon_is_equippable_as_common` — `{"type": "weapon"}` with no slot/tier equips and gives +1 damage.
- `test_equipped_item_stays_in_inventory` — the bag still contains it, quantity intact.
- `test_slot_auto_clears_when_item_leaves_inventory` — `remove_from_inventory` then `resolve_loadout` → slot empty, no crash.
- `test_unequip_clears_slot`.
- `test_auto_equip_on_pickup_when_slot_empty` / `test_auto_equip_does_not_replace_worn_gear`.

`tests/test_consumables.py`
- `test_default_quick_slot_is_first_heal`.
- `test_use_in_combat_returns_hearts_and_decrements`.
- `test_use_out_of_combat_improves_health_status` — `hurt` + heal_light → `winded`.
- `test_use_out_of_combat_does_not_exceed_healthy`.
- `test_surge_returns_buff_and_duration_and_no_hearts`.
- `test_use_missing_item_errors` / `test_use_non_consumable_errors`.
- `test_hearts_healed_reduces_effective_hits` — `hits_taken=3, hearts_healed=2` → health floor `winded`, not `badly_hurt`.
- `test_hearts_healed_cannot_rescue_a_defeat` — defeat is still `critical`.

`tests/test_shop_pricing.py`
- `test_catalog_price_overrides_authored_value` — an item authored at `value=5` with `tier="master"` costs 700.
- `test_purchase_charges_catalog_price` — player currency drops by the table amount.
- `test_purchase_blocked_when_short` — existing behaviour preserved.
- `test_plain_item_keeps_authored_value` — a `misc` trinket-of-no-mechanics at 12g still costs 12.
- `test_stock_merchant_creates_priced_gear` — NPC inventory gains an item with the right `slot`/`tier`/`value`.
- `test_stock_merchant_rejects_legendary`.
- `test_stock_merchant_rejects_unknown_tier` — error names the legal words.

`tests/test_roll_check.py`
- `test_returns_outcome_label_only` — the returned dict contains no key whose value is an int representing a roll or DC; `_debug` is absent.
- `test_invalid_skill_returns_error_with_valid_list`.
- `test_invalid_difficulty_returns_error`.
- `test_event_key_is_check_result`.
- `test_nat20_is_critical_success_even_below_dc` (seeded).
- `test_nat1_is_critical_failure_even_above_dc` (seeded).
- `test_partial_band_is_three_below_dc` (seeded/forced roll).
- `test_advantage_beats_disadvantage_over_1000_rolls` — same seed stream, advantage success rate strictly higher.
- `test_higher_level_succeeds_more_often` — level 10 vs level 1 over 1000 seeded rolls.
- `test_trait_bonus_applies` — a player with trait "cunning" beats one without on the cunning domain over 1000 seeded rolls.

`tests/test_turn_audit_progression.py`
- `test_risky_resolution_without_roll_check_flags` — "You slip past the guard unseen." with no tools → `risky_action_without_check`.
- `test_risky_resolution_with_roll_check_is_clean`.
- `test_risky_resolution_during_combat_is_clean` — `start_combat` succeeded.
- `test_convince_yourself_is_not_flagged` — precision guard.
- `test_npc_on_npc_prose_is_not_flagged` — "The smuggler slips past the guard." → clean.
- `test_level_word_in_narration_flags` — even with `code_events={"level_up"}`.
- `test_potion_between_turns_does_not_flag_item_claim` — `code_events={"canvas_action"}` suppresses `item_without_tool`.
- `test_audit_signature_backwards_compatible` — calling `audit_turn` with four args still works.

`tests/test_dm_prompt_tools.py` (existing, extended)
- `TOOL_PREFIXES += ("roll_", "stock_")`; the existing assertion then covers the new prompt sections.

---

## 14. v1 vs later

### v1 (build now — the smallest thing that fully delivers the point)

- Levels 1-10, XP from quest completion + combat kills, hard-blocked from the DM.
- Fixed level table: hearts, damage, dodge cooldown, gear tier cap.
- Three equipment slots, six tier labels, one price table, auto-equip, HUD
  equip/unequip.
- Three consumables (`heal_light`, `heal_strong`, `surge`), Q hotkey, quick slot,
  optimistic client resolution, `hearts_healed` folded into the post-fight health
  floor.
- `stock_merchant` + catalog pricing on `transfer_item`.
- `roll_check` with 5 skills / 4 difficulties / 3 modifiers / 5 outcomes, the
  `⚄` floater, and the `risky_action_without_check` audit.
- `level_up` / `xp_gain` / `check_result` SSE events, level-up toast + burst +
  triumph stinger.
- `[SINCE YOUR LAST TURN]` canvas-note block.

### Later (explicitly out of scope, with the reason)

- **Selling gear back** (`transfer_item` player→NPC currently moves no money —
  a pre-existing gap). Gold *sinks* are the requirement; a sell path is a gold
  *source* and needs its own balance pass (40% of catalog is the obvious start).
- **`guard` consumable** (temporary shield hearts) — needs a second heart colour
  in `_drawHearts`; not worth the v1 surface.
- **Dodge charges / stamina bar** — rejected for v1 in favour of scaling the
  dodge cooldown, which is one number and zero new UI. Revisit if the fight
  loop ever needs a resource to spend.
- **A DM `equip` tool** — the DM handing someone armour and dressing them in it
  reads well, but auto-equip covers the common case and a tool invites the DM to
  equip things the player did not choose.
- **Per-skill progression** (stealth going up separately from persuasion) — that
  is the six-attribute paperwork the brief explicitly rejects.
- **XP for skill checks** — a DM-controlled XP faucet through the back door.
- **Item durability, enchantments, set bonuses, crafting** — every one of them
  multiplies the label vocabulary the LLM must hold.
- **Enemy scaling against player damage** — owned by the enemy-tiers spec; this
  spec only publishes `combat_stats()` and the 4-6-hits-per-elite target.

---

## 15. Interfaces this spec publishes to the other four systems

| System | Interface |
|---|---|
| **enemy-tiers** | Owns `NPC.enemy_tier` (labels must be keys of `ENEMY_XP`). Reads `progression.combat_stats()['damage']` (range 1-8) to size enemy HP; target = a tier-appropriate `elite` survives 4-6 player hits. Reads `LEVEL_TABLE[level].gear_tier_cap` if it wants to gate encounters by level. `_ARCHETYPE_TO_TIER` is the fallback until it lands. |
| **campaign** | Owns when quests are authored; sets `Quest.scale` (`errand`/`job`/`mission`/`saga`) — the only quest-side XP dial. May read `Player.level` to gate arc beats. Must not write `rewards["xp"]` (stripped and logged). |
| **canvas** | `state.combat` is the sole source of player fight numbers; `game.js` never computes stats from items. New key `q`. New renderer methods `spawnLevelUpBurst`, `showCheckResult`. `combat-ended` detail gains `heartsHealed`. |
| **cinematics** | New SSE types `level_up`, `xp_gain`, `check_result`. Level-up reuses the existing `triumph` stinger and the `showToast` banner (new `level` class). No new audio assets. |
| **all** | `_pending_canvas_notes` + `format_canvas_notes` is the general-purpose "the player did something between turns" channel — any system needing to inform the DM without spending a round-trip should reuse it rather than invent a second ledger. |
