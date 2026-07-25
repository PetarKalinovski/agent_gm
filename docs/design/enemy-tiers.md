# Enemy Tiers, Bosses & Combat Depth — implementation spec

Status: design-complete, ready to build.
Owner system: real-time canvas combat (`src/services/combat.py`, `static/js/game.js`).
Sibling specs: `progression.md` (player power), `campaign.md`, `canvas.md`, `cinematics.md`.

---

## 0. The player-facing point

> A beggar is trivial forever. A dragon is a wall you break yourself against until
> you are both **stronger** and **better**.

Everything below serves one sentence: **danger is a property of the enemy, never of
the player.** A `harmless` beggar has 1 HP and deals 0 damage at level 1, at level 8,
and at level 40. That invariance is the *only* thing that makes leveling legible —
the player measures their growth against fixed rulers.

Two absolute rules that follow:

1. **No scaling. Ever.** No enemy stat reads player level, player gear, or session
   time. The tier→params table is a constant.
2. **Top tiers gate on two independent axes.** Character power (damage per swing,
   heart pool — owned by `progression.md`) gets you *time on the boss*. Player skill
   (dodge timing, parry windows, punish-window recognition) gets you *through* it.
   Neither alone is sufficient: a level-12 character who never parries still eats a
   boss slam for 3 hearts; a god-tier dodger at level 1 needs 24 clean light hits
   without a single mistake.

---

## 1. Scope

### v1 — build now

- 6-rung absolute danger ladder (`harmless … boss`) × 4 behaviour archetypes
  (existing brute/skirmisher/stalker + **ranged**).
- All combat numbers resolved **server-side in Python** from a fixed
  `TIER × ARCHETYPE` table; the frontend consumes numbers, never invents them.
- `NPC.danger_tier` nullable column + assignment path (creator agent menu → keyword
  inference fallback → DM `update_npc` → world editor).
- **One** boss pattern set, applied to any boss-tier NPC regardless of archetype:
  50%-HP phase transition, telegraphed ground-slam, phase-2 triple-lunge combo,
  named top-of-screen HP bar, oversized sprite, intro trigger event.
- Player move-set additions: **charged heavy attack** (hold J) and **parry** (tap K,
  strict window → stagger + double damage).
- XP-per-tier table awarded by `POST /api/combat/result`, re-read from the DB.
- Reality-audit adaptation (new numeric-mechanics check + code-path suppression).

### Later — explicitly out of v1

- Second/third boss pattern sets (per-archetype boss movesets).
- Real projectiles for `ranged` (v1 uses telegraphed ground rings — no travel time).
- Adds/minion summoning, boss arena hazards, enemy status effects (burn/bleed).
- Per-NPC authored pattern overrides (`NPC.combat_pattern` JSON).
- Elite modifiers ("armoured", "frenzied") layered on a tier.
- Tier-scaled loot tables, tier-gated bounty quests.
- An in-fiction "assess the threat" read (letting the player *see* a tier label).

---

## 2. The ladder

Six rungs. The name is the whole UX: the DM and the creator agent pick one word.

| tier | fiction anchor | HP (ref) | dmg/hit (hearts) | XP | can be inferred? |
|---|---|---|---|---|---|
| `harmless` | beggar, drunk, child, bedridden patient | 1 | **0** | 0 | yes |
| `weak` | cutpurse, starving stray, conscript | 3 | 1 | 5 | yes |
| `standard` | town guard, bandit, war hound | 5 | 1 | 15 | yes |
| `dangerous` | veteran captain, ogre, assassin | 8 | 2 | 40 | yes |
| `deadly` | champion, troll, archmage | 14 | 2 | 100 | yes |
| `boss` | dragon, the Nemesis, the god-thing | 24 | 2 (slam 3) | 300 | **NO — authored only** |

`harmless` deals literally zero damage and flees. You cannot lose to a beggar, ever,
under any code path. `boss` can never be produced by keyword inference — a boss is a
set-piece with an intro and a health bar, and one must never appear by accident.

### Rejected fork

*Considered and rejected:* letting the DM pass a `difficulty` argument to
`start_combat`. It hands an LLM a balance dial, and the same NPC would be a pushover
or a wall depending on the model's mood. Tier lives on the NPC row; `start_combat`
reads it and cannot override it.

---

## 3. Archetypes

Archetype = **shape of the fight**. Tier = **weight of the fight**. Orthogonal.

| archetype | reads as | behaviour |
|---|---|---|
| `brute` | hulking, armoured, slow | big HP, long readable windup, heavy commit |
| `skirmisher` | fast, light, numerous | low HP, short windup, constant pressure |
| `stalker` | predator, duellist | orbits before committing, longest lunge |
| `ranged` **(new)** | archer, caster, spitter, grenadier | holds standoff, plants telegraphed ground rings |

### Why `ranged` earns its place

The current loop has exactly one counter-play verb: *back off, then punish the
recover window*. Every archetype dies to the same circle-strafe. `ranged` inverts it —
backing off is what gets you killed, and the answer is to close and pressure. It
costs almost nothing to build because its telegraph **is the boss slam ring**: one
renderer (`_updateRings`) serves both. No projectile physics, no new asset, no new
state in the AI machine (it reuses chase→windup→strike→recover verbatim; only the
distance-keeping branch and the strike payload differ).

*Considered and rejected:* a `swarm` archetype (spawns adds). Needs sprite cloning
and roster mutation mid-fight; deferred.

---

## 4. The parameter tables (fixed code, single source of truth)

Lives in `src/services/combat.py`. **Python is authoritative** — the resolved numbers
travel to the client inside `combat_start`.

*Considered and rejected:* keeping the table in `GameRenderer.ARCHETYPES` (today's
home). Two tables in two languages drift, JS constants are untestable by pytest, and
a stale cached `game.js` silently ships stale balance. The JS table survives only as
a fallback for the Ctrl+K gray-box enemy and for defensive defaults.

### 4.1 Archetype rows

```python
# src/services/combat.py

ARCHETYPES = ("brute", "skirmisher", "stalker", "ranged")

# Shape of the fight. Times in ms, distances in normalized 0-100 scene units.
ARCH_ROWS: dict[str, dict] = {
    "brute":      {"hp_mul": 1.4, "speed": 4.8, "windup": 650, "lunge_dist": 10,
                   "lunge_ms": 215, "recover": 800, "engage": 7,  "scale_mul": 1.12},
    "skirmisher": {"hp_mul": 0.6, "speed": 8.5, "windup": 340, "lunge_dist": 8,
                   "lunge_ms": 150, "recover": 430, "engage": 6,  "scale_mul": 0.95},
    "stalker":    {"hp_mul": 0.8, "speed": 6.8, "windup": 280, "lunge_dist": 12,
                   "lunge_ms": 140, "recover": 520, "engage": 9,  "scale_mul": 1.00,
                   "circles": True},
    "ranged":     {"hp_mul": 0.7, "speed": 6.0, "windup": 900, "lunge_dist": 0,
                   "lunge_ms": 120, "recover": 900, "engage": 22, "scale_mul": 0.98,
                   "ranged": True, "standoff": 14, "ring_radius": 9},
}
```

### 4.2 Tier rows

```python
DANGER_TIERS = ("harmless", "weak", "standard", "dangerous", "deadly", "boss")

TIER_ROWS: dict[str, dict] = {
    # hp is the REFERENCE hp (archetype hp_mul multiplies it, except boss).
    # damage is in PLAYER HEARTS and is ABSOLUTE — it never scales.
    "harmless":  {"hp": 1,  "damage": 0, "speed_mul": 0.55, "windup_mul": 1.00,
                  "recover_mul": 1.00, "lunge_mul": 1.00, "scale_mul": 0.90,
                  "stagger": 1200, "knock_mul": 1.6, "flees": True,  "xp": 0},
    "weak":      {"hp": 3,  "damage": 1, "speed_mul": 0.88, "windup_mul": 1.30,
                  "recover_mul": 1.20, "lunge_mul": 1.10, "scale_mul": 0.95,
                  "stagger": 1100, "knock_mul": 1.2, "xp": 5},
    "standard":  {"hp": 5,  "damage": 1, "speed_mul": 1.00, "windup_mul": 1.00,
                  "recover_mul": 1.00, "lunge_mul": 1.00, "scale_mul": 1.00,
                  "stagger": 900,  "knock_mul": 1.0, "xp": 15},
    "dangerous": {"hp": 8,  "damage": 2, "speed_mul": 1.10, "windup_mul": 0.85,
                  "recover_mul": 0.88, "lunge_mul": 0.92, "scale_mul": 1.08,
                  "stagger": 750,  "knock_mul": 0.7, "xp": 40},
    "deadly":    {"hp": 14, "damage": 2, "speed_mul": 1.20, "windup_mul": 0.72,
                  "recover_mul": 0.78, "lunge_mul": 0.86, "scale_mul": 1.16,
                  "stagger": 620,  "knock_mul": 0.5, "xp": 100},
    "boss":      {"hp": 24, "damage": 2, "speed_mul": 1.12, "windup_mul": 0.80,
                  "recover_mul": 0.85, "lunge_mul": 0.90, "scale_mul": 1.50,
                  "stagger": 520,  "knock_mul": 0.0, "boss": True, "xp": 300},
}

WINDUP_FLOOR_MS = 220   # human reaction floor — no enemy telegraph is shorter
```

### 4.3 The boss pattern set (v1: exactly one, archetype-agnostic)

```python
BOSS_PATTERN = {
    "intro_ms":          1600,   # cinematics window before spawn
    "phase2_at":         0.5,    # fraction of max hp
    "phase_transition_ms": 1200, # invulnerable roar
    "slam_first_at":     4000,
    "slam_every":        9000,   # phase 1
    "slam_every_p2":     6500,
    "slam_windup":       900,    # ring grows over this
    "slam_radius":       18,     # normalized units
    "slam_damage":       3,      # hearts — the only damage above 2 in the game
    "slam_recover":      1100,   # the punish window; land your heavy here
    "combo_count":       3,      # phase 2 basic attack becomes a 3-lunge chain
    "combo_gap":         180,
    "p2_speed_mul":      1.15,
    "p2_windup_mul":     0.78,
    "p2_stagger":        440,
}
```

### 4.4 Resolution function (exact)

```python
def _r(x: float) -> int:
    """Round-half-UP. Python's round() is banker's rounding — do not use it here."""
    return int(x + 0.5)


def combat_params(tier: str, archetype: str) -> dict:
    """Fixed table lookup. Deterministic, pure, no DB, no player state."""
    T = TIER_ROWS.get(tier) or TIER_ROWS["standard"]
    A = ARCH_ROWS.get(archetype) or ARCH_ROWS["skirmisher"]
    is_boss = bool(T.get("boss"))

    hp = T["hp"] if is_boss else max(1, _r(T["hp"] * A["hp_mul"]))

    params = {
        "hp": hp,
        "damage": T["damage"],
        "speed": round(A["speed"] * T["speed_mul"], 2),
        "windup": max(WINDUP_FLOOR_MS, _r(A["windup"] * T["windup_mul"])),
        "lungeDist": A["lunge_dist"],
        "lungeMs": _r(A["lunge_ms"] * T["lunge_mul"]),
        "recover": _r(A["recover"] * T["recover_mul"]),
        "engage": A["engage"],
        "scaleMul": round(A["scale_mul"] * T["scale_mul"], 3),
        "stagger": T["stagger"],
        "knockMul": T["knock_mul"],
        "circles": bool(A.get("circles")),
        "ranged": bool(A.get("ranged")),
        "standoff": A.get("standoff", 0),
        "ringRadius": A.get("ring_radius", 0),
        "flees": bool(T.get("flees")),
        "boss": is_boss,
        "tier": tier,
        "archetype": archetype,
    }
    if is_boss:
        params["bossPattern"] = dict(BOSS_PATTERN)
    return params
```

### 4.5 Resolved HP grid (for review — the table above produces exactly this)

| tier | brute | skirmisher | stalker | ranged |
|---|---|---|---|---|
| harmless | 1 | 1 | 1 | 1 |
| weak | 4 | 2 | 2 | 2 |
| standard | **7** | **3** | **4** | 4 |
| dangerous | 11 | 5 | 6 | 6 |
| deadly | 20 | 8 | 11 | 10 |
| boss | 24 | 24 | 24 | 24 |

Note the `standard` row reproduces today's shipped numbers (brute 7 / skirmisher 3 /
stalker 4) exactly — **existing worlds play identically** when everything defaults to
`standard`, which is the point of the whole rung placement.

Selected resolved windups (the reaction-time ladder):

| | brute | skirmisher | stalker |
|---|---|---|---|
| weak | 845 | 442 | 364 |
| standard | 650 | 340 | 280 |
| dangerous | 553 | 289 | 238 |
| deadly | 468 | 245 | **220** (floor) |
| boss (p1 / p2) | 520 / 406 | 272 / 220 | 224 / 220 |

---

## 5. Schema delta

One nullable column. Nothing else. Existing `data/*.db` files load unchanged;
`danger_tier IS NULL` means "infer it", which is exactly today's behaviour for
`combat_archetype`.

```python
# src/models/npc.py  — add directly under combat_archetype

    # Absolute danger rung ("harmless" | "weak" | "standard" | "dangerous"
    # | "deadly" | "boss"). NEVER scaled to the player. NULL = infer from
    # profession/description at fight time. "boss" is authored-only.
    danger_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
```

| table | column | type | default | nullable |
|---|---|---|---|---|
| `npcs` | `danger_tier` | `VARCHAR(20)` | `NULL` | yes |

No other table changes. XP lives in `progression.md`'s schema; this spec only calls
its API.

---

## 6. Tier assignment — four paths, one resolution order

```python
def resolve_danger_tier(npc: NPC) -> str:
    """Row wins; otherwise infer. The ONLY place a tier is decided."""
    if npc.danger_tier in DANGER_TIERS:
        return npc.danger_tier
    return infer_danger_tier(npc)
```

### 6.1 Creator agent / WorldForge (authoring time — the good path)

`add_npc` gains two optional params. Both are **label menus**; the agent never sees a
number.

```python
@tool
def add_npc(
    ...,
    combat_archetype: str | None = None,
    danger_tier: str | None = None,
) -> dict[str, Any]:
    """...
    Args:
        ...
        combat_archetype: How they FIGHT, if it ever comes to violence. One of:
            "brute" (hulking, armoured, slow heavy swings),
            "skirmisher" (fast, light, relentless),
            "stalker" (circles and waits, then commits — predators, duellists),
            "ranged" (keeps distance, attacks the ground you're standing on —
            archers, casters, spitters). Leave None to auto-infer.
        danger_tier: How DANGEROUS they are, ABSOLUTELY — never relative to the
            player. This is a permanent fact about the character. One of:
            "harmless"  — cannot hurt anyone: beggars, children, the bedridden,
                          drunks, terrified civilians. They flee, they do no damage.
            "weak"      — cutpurses, starving strays, green conscripts.
            "standard"  — a competent armed adult: town guard, bandit, war hound.
            "dangerous" — veterans and monsters: a captain, an ogre, an assassin.
            "deadly"    — the best in the world or worse than human: a champion,
                          a troll, an archmage. Most characters die to these.
            "boss"      — a legend, a set-piece, the thing the whole arc points at.
                          A dragon. A god. Use this at most a handful of times per
                          world; it triggers an intro cinematic and a named health
                          bar. Leave None to auto-infer (never infers "boss").
    """
```

Creator prompt addition (`CREATOR_SYSTEM_PROMPT`, new numbered item under
GENERATION PRINCIPLES):

```
## DANGER TIERS (set one on every NPC you create)

Every NPC gets a `danger_tier` — how dangerous they are in a real fight, stated
ABSOLUTELY. This is never relative to the player: a beggar is `harmless` on day one
and still `harmless` when the player is a legend. That permanence is how the player
feels themselves grow.

  harmless  — cannot hurt anyone. Beggars, children, the sick, drunks, civilians.
  weak      — cutpurses, strays, green recruits, desperate amateurs.
  standard  — a competent armed adult. Guards, bandits, hounds, soldiers.
  dangerous — veterans and real monsters. Captains, ogres, assassins, alphas.
  deadly    — the best there is. Champions, trolls, archmages. Most people die here.
  boss      — a set-piece legend. Dragons, gods, the arc's final answer. Use at most
              a handful of times per world; it gets an intro and a named health bar.
              Never hand `boss` to a random encounter.

Most NPCs are `harmless` or `standard`. A world where everyone is `dangerous` has no
ladder and teaches the player nothing.

Also set `combat_archetype` (how they fight, if it comes to that):
brute / skirmisher / stalker / ranged. Pick the word that matches the body and the
weapon — you are choosing a fighting STYLE, not a difficulty.
```

### 6.2 Inference fallback (legacy NPCs, lazy generation, forgetful agents)

```python
# src/services/combat.py — evaluated in this order, first match wins.

_TIER_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("deadly", (
        "champion", "warlord", "archmage", "high priest", "demon", "wraith",
        "revenant", "giant", "wyvern", "chimera", "abomination", "nightmare",
        "blademaster", "master assassin", "legendary", "monstrosity", "titan",
    )),
    ("dangerous", (
        "captain", "veteran", "knight", "assassin", "duelist", "duellist",
        "berserker", "ogre", "troll", "bear", "alpha", "beast", "witch",
        "sorcerer", "warden", "inquisitor", "executioner", "elite", "guardian",
        "commander", "war-chief", "warchief", "hunter of",
    )),
    ("standard", (
        "guard", "soldier", "bandit", "raider", "mercenary", "sellsword",
        "hunter", "watchman", "sentry", "marine", "trooper", "warrior",
        "brawler", "pirate", "smuggler", "enforcer", "wolf", "boar", "hound",
        "constable", "legionary", "swordsman", "archer", "gunman",
    )),
    ("weak", (
        "cutpurse", "pickpocket", "thug", "lackey", "apprentice", "recruit",
        "conscript", "deckhand", "dockworker", "labourer", "laborer", "miner",
        "hooligan", "footpad", "initiate", "stray", "rat", "novice",
    )),
    ("harmless", (
        "beggar", "child", "kid", "infant", "drunk", "drunkard", "invalid",
        "cripple", "elderly", "grandmother", "grandfather", "urchin",
        "peasant", "farmhand", "scribe", "clerk", "servant", "maid", "cook",
        "baker", "weaver", "tailor", "minstrel", "pilgrim", "patient",
        "wounded", "bedridden", "blind", "prisoner", "refugee", "orphan",
    )),
]

_TIER_BY_NPC_TIER = {
    NPCTier.AMBIENT: "weak",
    NPCTier.MINOR: "standard",
    NPCTier.MAJOR: "standard",
}


def infer_danger_tier(npc: NPC) -> str:
    """Keyword fallback for NPCs with no authored tier.

    NEVER returns "boss" — a boss is an authored set-piece, never an accident.
    """
    text = " ".join(filter(None, [
        npc.profession or "", npc.description_physical or "",
        npc.description_personality or "", npc.species or "",
    ])).lower()
    for tier, words in _TIER_WORDS:
        if any(w in text for w in words):
            return tier
    return _TIER_BY_NPC_TIER.get(npc.tier, "standard")
```

Precedence is highest-danger-first so "veteran guard" resolves `dangerous`, not
`standard`. The `harmless` list is last but its words are near-unique, so "beggar"
lands `harmless` — the headline requirement.

### 6.3 DM (in-play promotion, tightly fenced)

`update_npc` gains `danger_tier` and `combat_archetype`. Both are permanent character
facts, not fight knobs:

```python
    if danger_tier is not None:
        if danger_tier == "boss":
            return {"error": (
                "danger_tier 'boss' cannot be set here — a boss is an authored "
                "set-piece. Create the character through prompt_creator_agent with "
                "danger_tier='boss', or set it in the world editor."
            )}
        if danger_tier not in DANGER_TIERS:
            return {"error": f"danger_tier must be one of {list(DANGER_TIERS)}"}
        npc.danger_tier = danger_tier
    if combat_archetype is not None:
        if combat_archetype not in ARCHETYPES:
            return {"error": f"combat_archetype must be one of {list(ARCHETYPES)}"}
        npc.combat_archetype = combat_archetype
```

The structural guarantee against inflation/deflation is not this validation — it is
that `build_combat_start` reads `resolve_danger_tier(npc)` **from the freshly loaded
row**, and `start_combat` has no tier parameter at all. The DM cannot pass a number,
cannot pass a tier, and cannot pass a multiplier. Worst case it edits a row a turn
early, which is a visible, persistent, auditable world change — not a hidden dial.

### 6.4 World editor (human author)

`NPCUpdate` gains `danger_tier: str | None` and `combat_archetype: str | None`;
`GET /api/world/npcs/{id}` returns both; `PUT` applies both (the editor path DOES
allow `boss`). Frontend NPC edit form gets two `<select>`s (§10.2).

---

## 7. Player skill depth

Existing feel to preserve: instant light attack, 230 ms dodge roll with 350 ms
i-frames, hitstop/knockback/screenshake on every connection. Nothing below adds
latency to the light attack — that responsiveness is the game's best quality.

### 7.1 Charged heavy attack (hold J)

The light attack still fires **on keydown, instantly**. The charge grows *underneath*
its recovery, so the player pays nothing for the option.

| moment | ms from J keydown | effect |
|---|---|---|
| press | 0 | light attack fires (unchanged) |
| — | 300 | if J still held: `charging = true`. Move speed ×0.45. `windup` sfx @0.4. Pulsing white outline on the player sprite. |
| — | 700 | `chargeReady = true`. Bright flash, `clash` sfx @0.35. |
| release ≥700 | — | **heavy attack fires** |
| release 300–700 | — | charge aborts. No attack. 120 ms whiff lockout. |
| — | 1600 | auto-release (heavy fires) so nobody holds J forever |

Heavy attack parameters:

| | light | heavy |
|---|---|---|
| damage | `light_damage` | `heavy_damage` |
| arc half-angle | 0.9 rad | 1.15 rad |
| reach (normalized) | 10 | 12 |
| knockback | 6 × `knockMul` | 10 × `knockMul` |
| hitstop | 70 ms | 110 ms |
| screenshake | 5 | 9 |
| cooldown after | 380 ms | 760 ms |
| extra | — | staggers non-boss enemies 300 ms |

The heavy is *lower DPS* than light spam (≈2.05/s vs ≈2.6/s at level 1) and higher
per-swing. That is deliberate: light is what you do to a bandit, heavy is what you do
in a boss's 1100 ms slam-recover window. The move set teaches punish recognition.

### 7.2 Parry (tap K)

| phase | ms from K press | state |
|---|---|---|
| active window | 0 – **150** | any incoming *strike* is parried |
| whiff lag | 150 – 410 | cannot dodge, attack, or parry — you eat what comes |
| cooldown | 0 – 520 | K does nothing |

On a successful parry:

- damage negated entirely; `hitsTaken` and `heartsLost` unchanged
- attacker enters `stagger` for `params.stagger` ms (1200 harmless → 520 boss →
  440 boss phase 2): no movement, no attack, telegraph cleared, tint `0xfff0a0`
- **staggered enemies take ×2 damage** — a heavy into a parry-stagger at level 1 is
  6 damage, the single biggest number a level-1 character can produce
- hitstop 140 ms, screenshake 10, white full-screen flash @0.25 for 90 ms,
  `clash` sfx @1.0
- 120 ms of i-frames granted (so a second enemy's simultaneous swing doesn't punish
  a correct read)

**Parry does NOT work on area attacks** — the `ranged` archetype's ground ring and
the boss slam ignore it entirely. You must leave the circle. This is the one
attack-type read the player has to learn, and the ring telegraph is visually
unmistakable (expanding red circle on the ground vs. red wedge from a body).

Risk/reward is real: 260 ms of whiff lag is longer than a deadly skirmisher's entire
windup, so a panic-parry against a fast enemy converts a dodgeable hit into a
guaranteed one.

### 7.3 Full control surface after v1

| key | action |
|---|---|
| WASD | move |
| Space | dodge roll (230 ms, 350 ms i-frames, 750 ms cd) |
| J tap | light attack |
| J hold → release | charged heavy attack |
| K | parry |

---

## 8. Damage interface with `progression.md`

### 8.1 Contract (this spec ships working defaults; progression may override)

`build_player_state()` gains one key:

```json
"combat": {
  "level": 1,
  "max_hearts": 5,
  "light_damage": 1,
  "heavy_damage": 3,
  "parry_unlocked": true,
  "heavy_unlocked": true
}
```

Produced by a soft-import shim so **enemy tiers ships and plays with no progression
module present**:

```python
# src/services/combat.py

_LOADOUT_DEFAULT = {
    "level": 1, "max_hearts": 5, "light_damage": 1, "heavy_damage": 3,
    "parry_unlocked": True, "heavy_unlocked": True,
}

def get_combat_loadout(player_id: str) -> dict:
    """Player-side combat numbers. Progression owns them; we own the defaults."""
    out = dict(_LOADOUT_DEFAULT)
    try:
        from src.services.progression import get_combat_loadout as _p  # noqa
    except ImportError:
        return out
    try:
        supplied = _p(player_id) or {}
    except Exception:
        return out
    out.update({k: v for k, v in supplied.items() if k in out})
    if "heavy_damage" not in supplied:
        out["heavy_damage"] = out["light_damage"] * 2 + 1
    return out
```

Default heavy formula: **`heavy = light * 2 + 1`**.

Current combat HP is derived, not stored:
`hp = max(1, max_hearts - PENALTY[health_status])` where
`PENALTY = {healthy:0, winded:1, hurt:2, badly_hurt:3, critical:4}`. At
`max_hearts = 5` this reproduces today's `{healthy:5 … critical:1}` map exactly.

Progression must **not** feed anything into enemy stats. There is no code path from
`get_combat_loadout` into `combat_params`. That is the no-scaling guarantee, enforced
by function signature: `combat_params(tier, archetype)` takes no player argument.

### 8.2 Hits-to-kill matrix (the intended power curve)

Reference archetype (`hp_mul = 1.0`). Reference progression curve:
L1 → light 1 / heavy 3; L4 → 2 / 5; L8 → 3 / 7; L12 → 4 / 9.

| tier | HP | dmg to player | L1 light | L4 light | L8 light | L8 heavy | L8 heavy into parry-stagger |
|---|---|---|---|---|---|---|---|
| harmless | 1 | 0 hearts | 1 | 1 | 1 | 1 | 1 |
| weak | 3 | 1 heart | 3 | 2 | 1 | 1 | 1 |
| standard | 5 | 1 heart | 5 | 3 | 2 | 1 | 1 |
| **dangerous** | 8 | **2 hearts** | **8** | 4 | **3** | 2 | 1 |
| deadly | 14 | 2 hearts | 14 | 7 | 5 | 2 | 1 |
| **boss** | 24 | 2 (slam 3) | **24** | 12 | 8 | 4 | 2 |

The `dangerous` row is the requirement stated verbatim: *level 1 = 8 hits and
2-heart mistakes; level 8 = 3 hits.* Multiply by `hp_mul` for the actual archetype:
a dangerous brute is 11 hits at L1, a dangerous skirmisher is 5.

Survivability at the same rungs (mistakes before death, `max_hearts` 5 at healthy):

| tier | mistakes to death @5 hearts | @8 hearts (L8-ish) |
|---|---|---|
| harmless | ∞ | ∞ |
| weak / standard | 5 | 8 |
| dangerous / deadly | 3 | 4 |
| boss (basic 2 / slam 3) | 2 | 3 |

Growth is felt on **both** axes without ever touching an enemy stat: your swings
shorten the fight, your heart pool lengthens your mistakes. The beggar stays 1 HP,
0 damage, forever.

---

## 9. Server-side implementation

### 9.1 `build_combat_start` — enriched payload

```python
def build_combat_start(npc_ids: list[str], player_id: str) -> dict[str, Any]:
    ...
        enemies.append({
            "npc_id": npc.id,
            "name": npc.name,
            "archetype": archetype,          # infer_archetype(npc)
            "tier": tier,                    # resolve_danger_tier(npc)
            "params": combat_params(tier, archetype),
        })
        if tier == "boss" and boss is None:
            boss = {
                "npc_id": npc.id,
                "name": npc.name,
                "title": npc.profession or "",
                "intro_ms": BOSS_PATTERN["intro_ms"],
            }
    ...
    apply_tension_floor("climax" if boss else "high",
                        "boss fight started" if boss else "combat started")

    return {
        "success": True,
        "event": "combat_start",
        "enemies": enemies,
        "boss": boss,                        # None when no boss-tier combatant
        "roster": ", ".join(f"{e['name']} ({e['tier']} {e['archetype']})" for e in enemies),
        "instruction": (...)                 # see §9.5
    }
```

Boss fights floor tension at `climax` (not `high`) so the music layer escalates
immediately without an LLM round-trip.

### 9.2 `apply_combat_result` — hearts, XP, tiers

Request gains one optional field (backward compatible: old clients omit it):

```python
class CombatResultRequest(BaseModel):
    player_id: str
    outcome: str                        # "victory" | "defeat" | "fled"
    hits_taken: int = 0
    hearts_lost: int = 0                # NEW — 0 falls back to hits_taken
    defeated_npc_ids: list[str] = []
```

Why: with 2- and 3-heart hits, "hits taken" understates a mauling. Health severity
now maps from hearts:

```python
_HEARTS_TO_STATUS = {0: None, 1: "winded", 2: "hurt", 3: "badly_hurt"}
# clamped at 3; "defeat" still forces "critical"

severity_units = max(hearts_lost, hits_taken)     # hearts_lost >= hits_taken always
floor = _HEARTS_TO_STATUS.get(min(severity_units, 3))
```

**XP: re-read from the DB, never trust the client.**

*Considered and rejected:* letting the tier ride back on the client's
`combat-ended` payload. It is a browser-editable field that mints XP. The re-read is
one `IN (...)` query inside the transaction we already open — free.

```python
    xp_total = 0
    xp_breakdown = []
    for npc_id in defeated_npc_ids:
        npc = session.get(NPC, npc_id)
        if not npc or npc.status == "dead":
            continue                       # no XP for corpses (anti double-report)
        tier = resolve_danger_tier(npc)    # authoritative: the DB row
        gain = TIER_ROWS[tier]["xp"]
        xp_total += gain
        xp_breakdown.append({"npc_name": npc.name, "tier": tier, "xp": gain})
        npc.status = "dead"
        ... existing death Event ...
    changes["xp_awarded"] = xp_total
    changes["xp_breakdown"] = xp_breakdown
    changes["level_up"] = False
```

Then, outside the session, the progression hand-off (soft import — no hard dep):

```python
    if xp_total:
        try:
            from src.services.progression import award_xp
        except ImportError:
            award_xp = None
        if award_xp:
            try:
                res = award_xp(player_id, xp_total, reason="combat") or {}
                changes["level_up"] = bool(res.get("level_up"))
                changes["new_level"] = res.get("level")
            except Exception as e:
                logger.warning(f"award_xp failed: {e}")
```

`harmless` is worth **0 XP** — killing beggars is not a strategy, and the ladder's
bottom rung stays a moral choice rather than a farm.

### 9.3 `combat_outcome_message` — labels, never numbers

```python
def combat_outcome_message(changes, fled: bool = False) -> str:
    parts = [f"[COMBAT OUTCOME — resolved in real-time by the player, mechanics "
             f"already applied: {changes['outcome']}."]
    if changes.get("xp_breakdown"):
        roster = ", ".join(f"{d['npc_name']} ({d['tier']})" for d in changes["xp_breakdown"])
        parts.append(f"Killed: {roster} (already marked dead — do NOT call kill_npc). "
                     "Match the weight of your aftermath to those words: a 'harmless' "
                     "kill is an ugly little murder; a 'boss' kill is the end of a chapter.")
    if changes.get("health"):
        parts.append(f"Player health is now '{changes['health']}' (already applied — "
                     "do NOT call update_player_health for these wounds).")
    if changes.get("level_up"):
        parts.append("The player has grown from this fight — narrate the FELT sense of "
                     "it (steadier hands, a wound that would have killed them last week). "
                     "Never print a number, a level, or the word 'experience'.")
    parts.append("Narrate the aftermath in the fiction ...")
    return " ".join(parts)
```

### 9.4 `build_player_state` addition

```python
    return {
        ...,
        "tension": tension,
        "combat": get_combat_loadout(player_id),   # NEW
    }
```

### 9.5 `start_combat` instruction text (replaces the current string)

```
Real-time combat has begun ON THE CANVAS — the player is fighting it with their own
reflexes right now. Combatants: {roster}. Write ONE sentence that FREEZE-FRAMES the
instant violence erupts (weapons coming out, the first step forward), pitched to the
weight of those labels — a 'harmless' foe is pitiful, a 'deadly' one is a death
sentence someone else usually receives — and then END your turn immediately.
Absolutely no exchanges of blows, no parries, no hits, no wounds, no outcome of any
kind — anything you write about the fight will contradict what the player actually
does. You will receive a [COMBAT OUTCOME] message when it's over; mechanics (health,
deaths) will already be applied — then you narrate the aftermath.
```

Boss variant, appended when `boss` is not None:

```
This is a BOSS encounter. The engine is playing an entrance for {boss_name} — your
one sentence is the moment before it lands, not the fight. Stop after it.
```

### 9.6 SSE plumbing (`src/web/streaming.py`)

```python
            elif event == "combat_start":
                self._notifications.append({
                    "type": "combat_start",
                    "enemies": output_data.get("enemies") or [],
                    "boss": output_data.get("boss"),          # NEW
                })
```

### 9.7 Exact SSE / REST payloads

**SSE `combat_start`** (`/api/play` stream):

```json
{
  "type": "combat_start",
  "enemies": [
    {
      "npc_id": "9f1c…",
      "name": "Coldiron",
      "archetype": "brute",
      "tier": "dangerous",
      "params": {
        "hp": 11, "damage": 2, "speed": 5.28, "windup": 553,
        "lungeDist": 10, "lungeMs": 198, "recover": 704, "engage": 7,
        "scaleMul": 1.21, "stagger": 750, "knockMul": 0.7,
        "circles": false, "ranged": false, "standoff": 0, "ringRadius": 0,
        "flees": false, "boss": false,
        "tier": "dangerous", "archetype": "brute"
      }
    }
  ],
  "boss": null
}
```

Boss form of the `boss` key:

```json
"boss": { "npc_id": "3a…", "name": "Vharath", "title": "the Ash-Wyrm", "intro_ms": 1600 }
```

**SSE `state`** (post-tool and post-turn) — one new key:

```json
{ "type": "state", "state": { "...": "...", "tension": "climax",
  "combat": { "level": 1, "max_hearts": 5, "light_damage": 1, "heavy_damage": 3,
              "parry_unlocked": true, "heavy_unlocked": true } } }
```

**`POST /api/combat/result`** request:

```json
{ "player_id": "…", "outcome": "victory", "hits_taken": 2,
  "hearts_lost": 4, "defeated_npc_ids": ["9f1c…"] }
```

response:

```json
{
  "success": true,
  "changes": {
    "outcome": "victory",
    "npc_deaths": [{"npc_id": "9f1c…", "npc_name": "Coldiron"}],
    "health": "badly_hurt",
    "xp_awarded": 40,
    "xp_breakdown": [{"npc_name": "Coldiron", "tier": "dangerous", "xp": 40}],
    "level_up": false
  },
  "dm_message": "[COMBAT OUTCOME — …]",
  "state": { "...": "…" }
}
```

**`GET /api/world/npcs/{id}`** / **`PUT /api/world/npcs/{id}`** gain
`"danger_tier"` and `"combat_archetype"` (both `string | null`).

---

## 10. Frontend

Every JS change requires bumping `?v=` on **both** script tags in `index.html`
(`game.js` and `audio.js`) — see the comment at line ~1697.

### 10.1 `static/js/game.js`

| region (current line ≈) | change |
|---|---|
| `setupInput` (180–237) | Track J **edges**: on keydown set `keys['j']=true` and `c.jDownAt = performance.now()` only on the transition (guard with `e.repeat`); on keyup fire `_releaseCharge()`. Add `k` to the tracked keys and call `_tryParry()` on the keydown edge. |
| `static ARCHETYPES` (1604–1611) | Demote to **fallback only**. Keep `test` plus a `_default` row (= today's `skirmisher`). Add a comment: authoritative params arrive in `combat_start.enemies[].params`; this table is only for the Ctrl+K gray-box and for old-server defence. |
| `_playerHpFromHealth` (1615–1619) | Rename `_playerCombatHp()`. Read `window.lastPlayerState?.combat` → `max_hearts` (default 5); `hp = max(1, max_hearts - PENALTY[health])`. |
| `_ensureCombat` (1621–1631) | New state fields: `playerMaxHp` from loadout, `heartsLost: 0`, `charging:false`, `chargeReady:false`, `jDownAt:0`, `chargeLock:0`, `parry:0`, `parryLag:0`, `parryCd:0`, `rings: []`, `boss: null`, `loadout: {...}`, `fizzleT: 0`. |
| `startCombat` (1634–1643) | New signature `startCombat(enemies, boss)`. If `boss`, `await this._bossIntro(boss)` before spawning and store `this.combat.boss = {...}`. Pass `en.params` into `_spawnCombatant`. |
| `_spawnCombatant` (1652–1726) | New signature `(npcId, archetype, name, params)`; `params = params || GameRenderer.ARCHETYPES[archetype] || ._default`. Store `tier`, `vulnMul: 1`, `staggerT: 0`, `comboLeft: 0`, `slamT`, `phase: 1`, `phaseT: 0`, `invuln: false`. Sprite scale uses `params.scaleMul` (bosses come out ~1.5–1.7× — that is the intended silhouette). |
| `_drawEnemyHp` (1728–1739) | Bar width `max(30, h*0.7) * (1 + 0.12 * TIER_INDEX[tier])` and fill colour by tier: harmless `0x9ca3af`, weak `0x84cc16`, standard `0xef4444`, dangerous `0xf97316`, deadly `0xdc2626`, boss (drawn by `_drawBossBar` instead). Teaches the ladder visually at zero cost. |
| `_drawHearts` (1741–1756) | Iterate `playerMaxHp` (may exceed 5). Unchanged otherwise. |
| **new** `_drawBossBar()` | uiLayer child named `boss-bar`: backing rect `min(560, screen.width*0.62) × 16` centred at top (y = 34), border `0x7f1d1d` 2 px, fill `0xef4444` (phase 1) / `0xf59e0b` (phase 2), boss name uppercase 14 px bold letter-spaced above it, `title` in 10 px muted below. Redrawn on damage and on phase change only. |
| **new** `_bossIntro(info)` | Returns a Promise. **Seam owned by `cinematics.md`:** dispatches `new CustomEvent('boss-intro', {detail: {...info, durationMs}})`, then resolves after `info.intro_ms`. Default body draws a minimal centred name card (fade in 300 / hold 1000 / fade out 300) so v1 stands alone; the cinematics layer replaces `GameRenderer.prototype._bossIntro` wholesale. |
| **new** `_spawnGroundRing(nx, ny, radius, windupMs, damage, opts)` | Pushes `{nx, ny, r, t, dur, dmg, ownerId, detonated}` onto `c.rings`. |
| **new** `_updateRings(dt)` | Grows each ring's drawn radius `0 → r` over `dur` (red stroke @0.5, fill @0.10, pulsing), then on `t >= dur` flashes white 1 frame and checks `hypot(player - ring) <= r`: if hit and `iframes <= 0 && dodge <= 0`, apply `dmg` hearts (rings **ignore parry**), knockback away from centre, `hurt` sfx, shake 12. Removes the ring 140 ms later. Shared by `ranged` strikes and boss slams. |
| `_updateCombat` (1841–2085) | The bulk. See the block list below. |
| `_finishCombat` (1804–1816) | Add `heartsLost: c.heartsLost` to the `combat-ended` detail. |
| `_endCombat` (1758–1801) | Also remove `boss-bar` from uiLayer, clear `c.rings` graphics. |

`_updateCombat` new/changed blocks, in tick order:

1. **timers**: decrement `parry`, `parryLag`, `parryCd`, `chargeLock`; per-enemy
   `staggerT`; boss `slamT` and `phaseT`.
2. **charge**: if `keys['j']` held and `now - c.jDownAt > 300` → `charging = true`
   (move speed multiplier applied in the movement block); at `> 700` →
   `chargeReady = true` (one-shot flash + sfx); at `> 1600` → `_releaseCharge()`.
3. **`_releaseCharge()`** (called on keyup or auto): if `chargeReady` → run the
   attack routine with the heavy profile; else clear and set `chargeLock = 120`.
4. **attack routine** (extracted from today's inline `keys['j']` block into
   `_swing(profile)`): profile = `{damage, halfAngle, reach, knock, hitstop, shake,
   cd, staggerOnHit}`. Damage applied is `profile.damage * e.vulnMul`.
5. **parry**: `_tryParry()` on K edge — requires `parryCd <= 0 && parryLag <= 0 &&
   dodge <= 0`; sets `parry = 150`, `parryLag = 410`, `parryCd = 520`, plays a
   90 ms white flash graphic + `clash` sfx @0.5 (a *whiffed* parry still makes the
   sound — audible commitment).
6. **dodge / light attack gating**: both additionally require `parryLag <= 0` and
   `chargeLock <= 0`.
7. **enemy AI**: before the existing state machine, `if (e.staggerT > 0)` →
   skip movement/attack, tint `0xfff0a0`, `e.vulnMul = 2`, clear telegraph;
   on expiry `vulnMul = 1`, `state = 'recover'`, `t = 250`.
8. **`flees` branch** (harmless): in `chase`, move *away* from the player at
   `params.speed`; never transitions to `windup`.
9. **`ranged` branch**: in `chase`, if `dist < standoff` retreat, else if
   `dist > engage` approach, else hold and enter `windup`. On entering `strike`,
   call `_spawnGroundRing(player.x, player.y, ringRadius, 0, damage)` — the ring is
   planted at the player's position **at windup start** (already stored in `e.aim`
   as an absolute point for ranged) with a 0 ms grow (the windup already served as
   the telegraph via a growing ring drawn during `windup`). Implementation: spawn
   the ring on entering `windup` with `dur = params.windup`, and skip the wedge
   telegraph for ranged.
10. **player-hit check** (inside `strike`): before applying damage, `if (c.parry > 0)`
    → parry success path (§7.2) and `return` from the hit; else apply
    `dmg = e.params.damage`, `c.playerHp -= dmg`, `c.heartsLost += dmg`,
    `c.hitsTaken += 1`.
11. **knockback**: multiply existing `kn` by `e.params.knockMul` (bosses = 0).
12. **boss layer** (only when `e.params.boss`):
    - `phase 1 → 2` when `e.hp <= e.maxHp * phase2At`: set `invuln = true`,
      `phaseT = phase_transition_ms`, clear rings/telegraph, shake 14, sfx
      `clash` + `windup`, dispatch `new CustomEvent('boss-phase', {detail:
      {npcId, phase: 2}})`, redraw boss bar in amber. On expiry apply
      `p2_speed_mul`, `p2_windup_mul`, `stagger = p2_stagger`,
      `slamEvery = slam_every_p2`, `comboCount = combo_count`.
    - **slam**: `slamT` counts down from `slam_first_at`, then `slamEvery`. On fire
      (only from `chase`/`recover`, never mid-lunge): `state = 'slam'`,
      `_spawnGroundRing(e.nx, e.ny, slam_radius, slam_windup, slam_damage)`, boss
      freezes; after `slam_windup` → `state = 'recover'`, `t = slam_recover`
      (the designed punish window).
    - **phase-2 combo**: on entering `recover` after a `strike`, if
      `comboLeft > 0` → after `combo_gap` ms go straight back to `strike` with a
      re-aimed vector instead of `recover`. `comboLeft` initialised to
      `combo_count - 1` on each windup in phase 2.
    - boss basic hits use `params.damage` (2); slam uses `slam_damage` (3).
13. **fizzle guard** (`_maybeFizzle`): if every living enemy has `flees` and 4000 ms
    pass with no player attack, call `_finishCombat('fled')`. Without this a
    beggar-only "fight" can never end.
14. **victory**: unchanged.

### 10.2 `static/index.html`

| region (line ≈) | change |
|---|---|
| 1700–1701 | bump `?v=20260724a` → `?v=20260726a` on **both** script tags |
| 2867–2868 | `game.startCombat(payload.enemies, payload.boss)` |
| 2583 | combat placeholder → `'⚔ Fight! Space dodge · J strike (hold: heavy) · K parry'` |
| 2192–2193 (controls overlay) | add rows: `<b>Hold J</b><span>charged heavy strike</span>` and `<b>K</b><span>parry — stagger them, then punish</span>` |
| 2595–2620 (`combat-ended`) | send `hearts_lost: heartsLost || 0` in the POST body |
| ~3905 (NPC edit form) | two new `<select>`s before "Position & Scale": `edit-npc-danger` (blank/auto + 6 tiers) and `edit-npc-archetype` (blank/auto + 4 archetypes) |
| ~3949 (`saveNPC`) | include `danger_tier` and `combat_archetype` in the PUT body (empty string → `null`) |

### 10.3 `static/js/audio.js`

No functional change required in v1 — `combat_start` already forces the `danger`
mood, and boss fights arrive with tension floored to `climax`, which maps to `danger`
too. Boss-specific scoring belongs to `cinematics.md` and hangs off the `boss-intro`
/ `boss-phase` DOM events. (Bump its `?v=` anyway to keep the two files in lockstep.)

---

## 11. Reality-audit adaptation (`src/web/turn_audit.py`)

Two changes; both are about *not* creating false positives on code-path actions.

### 11.1 Suppress health checks on `[COMBAT OUTCOME]` turns

This is a **pre-existing** false positive that XP makes louder: health is applied by
`/api/combat/result` *before* the turn starts, so `state_before == state_after` and
the DM legitimately describing the player's wounds trips `damage_without_tool`.

```python
_CODE_PATH_PREFIXES = ("[COMBAT OUTCOME",)

def audit_turn(turn_text, tools, state_before, state_after, player_input: str = ""):
    ...
    code_path = player_input.strip().startswith(_CODE_PATH_PREFIXES)
    ...
    if (_DAMAGE_CLAIM.search(turn_text) and not code_path
            and "update_player_health" not in succeeded and not health_changed):
        ...
```

Caller: `src/web/server.py` line ≈613 → `audit_turn(..., player_input=request.player_input)`.

### 11.2 New check: numeric mechanics in narration

The new code-side mutation (XP/levels) invites exactly one new failure: the DM
printing numbers. High precision by construction — a *number* next to xp/level is
never legitimate prose.

```python
_MECHANICS_NUMBERS = re.compile(
    r"\b(?:\+?\d+\s*(?:xp|exp|experience\s+points?)"
    r"|level(?:led|ed)?\s*(?:up)?\s*\d+"
    r"|reach(?:ed|es)\s+level\s+\d+"
    r"|you\s+are\s+now\s+level"
    r"|\bhp\b\s*[:\-]?\s*\d+"
    r"|\d+\s*(?:hit\s*points?|damage))\b",
    re.IGNORECASE,
)

    if _MECHANICS_NUMBERS.search(turn_text):
        warnings.append({
            "check": "mechanics_numbers_in_narration",
            "detail": ("Narration printed raw mechanics (experience, levels, hit "
                       "points, damage numbers). The player must never see engine "
                       "numbers — describe the felt change instead."),
        })
```

### 11.3 What is deliberately NOT audited

Tier labels in prose. If the DM writes "the beast is deadly", that is fiction
agreeing with the database, not a mechanical claim. Adding a check would fire on
ordinary adjectives.

---

## 12. DM prompt changes

Replace the **Combat & Danger** block (`dm_orchestrator.py` lines 189–195) with:

```
**3. Combat & Danger:**
- **Fights with named, present NPCs are REAL-TIME: call `start_combat(player_id, npc_ids)`.**
  When violence erupts with NPCs who exist in the database and are at the player's
  location — an ambush, a duel, a brawl that turns lethal — call `start_combat` and
  STOP. The player fights it out on the canvas themselves (dodging, striking,
  parrying). Do not narrate the fight or decide its outcome. One short line for the
  moment violence breaks out, then end your turn.
- **You do not set difficulty. Ever.** Every NPC carries an absolute danger tier —
  harmless, weak, standard, dangerous, deadly, boss — that describes how hard THEY
  are, never how hard the player is. A beggar is harmless whether the player is a
  farmhand or a legend. `start_combat` reads the tier off the character; you cannot
  raise or lower it for a scene, and you must never promise the player an easy or a
  hard fight in mechanical terms. Narrate the threat honestly and let the engine be
  the judge.
- The tier IS a storytelling tool, though: check it with `get_npc` before writing a
  confrontation. A `deadly` opponent should read as a death sentence in your prose;
  a `harmless` one should read as pitiful. When the fiction permanently changes what
  someone is — a drunk reveals himself as a retired champion, a guard captain is
  promoted, a wounded beast recovers — record it with
  `update_npc(npc_id, danger_tier="dangerous")`. That is a permanent character fact,
  not a knob for the current fight, and `boss` can only be set when the character is
  created.
- When the fight ends you'll receive a `[COMBAT OUTCOME]` message. Health changes,
  NPC deaths, and any growth the player earned are ALREADY applied by the engine —
  do not re-apply them, and never print numbers, levels, or experience. Narrate the
  aftermath and apply follow-on consequences (reputation, events, quest updates,
  faction retaliation).
- For danger with no named NPC entity — beasts without database entries, traps,
  falls, environmental peril, off-screen violence — resolve it narratively with the
  SUCCESS & FAILURE rubric: `show_combat_action` for the strikes,
  `update_player_health` for damage.
- Use `kill_npc` only for deaths OUTSIDE real-time combat (assassination, accident,
  execution) — this triggers a death animation.
- Use `update_npc_mood` or `update_npc_relationship` (hostile) immediately when a
  fight starts or ends.
- **Consequences**: After combat, schedule faction retaliation if applicable. Update
  the world.
```

**`tests/test_dm_prompt_tools.py` compliance:** the only backticked identifiers above
matching `TOOL_PREFIXES` are `start_combat`, `get_npc`, `update_npc`,
`update_player_health`, `update_npc_mood`, `update_npc_relationship`, `kill_npc`,
`show_combat_action` — all already in `DM_TOOLS`. **Do not backtick `add_npc`** in
the DM prompt (it lives in `CREATOR_TOOLS`, not `DM_TOOLS`, and would fail the test).

---

## 13. Build checklist (implementation order)

1. **Schema** — add `NPC.danger_tier` (`String(20)`, nullable, no default). Boot an
   existing world (`data/emberfall.db`) and confirm SQLite adds the column and the
   game plays with everything inferring.
2. **`src/services/combat.py` — tables & pure functions.** `DANGER_TIERS`, extend
   `ARCHETYPES` with `"ranged"`, `ARCH_ROWS`, `TIER_ROWS`, `BOSS_PATTERN`,
   `WINDUP_FLOOR_MS`, `_r`, `combat_params`, `_TIER_WORDS`, `infer_danger_tier`,
   `resolve_danger_tier`, `_LOADOUT_DEFAULT`, `get_combat_loadout`. Add
   `"ranged"`-flavoured words to `infer_archetype` (`archer, bow, sling, sniper,
   caster, mage, spitter, gunner, thrower`) so the new archetype is reachable
   without authoring. **Ship tests 1–7 here before touching anything else.**
3. **`build_combat_start`** — emit `tier` / `params` / `boss` / `roster`, climax
   floor for bosses, new instruction text.
4. **`apply_combat_result`** — `hearts_lost`, `_HEARTS_TO_STATUS`, DB-re-read XP,
   `xp_breakdown`, soft `award_xp` hand-off; `combat_outcome_message` rewrite.
5. **`src/web/streaming.py`** — pass `boss` through on `combat_start`.
6. **`src/web/server.py`** — `CombatResultRequest.hearts_lost`;
   `build_player_state` `"combat"` key; `NPCUpdate` +
   `danger_tier`/`combat_archetype` and the PUT/GET handlers;
   `audit_turn(..., player_input=request.player_input)`.
7. **`src/web/turn_audit.py`** — code-path suppression + `mechanics_numbers_in_narration`.
8. **Tools** — `add_npc` (+2 params, menu docstrings), `update_npc` (+2 params with
   the `boss` refusal).
9. **Prompts** — DM Combat & Danger block; creator DANGER TIERS section. Run
   `pytest tests/test_dm_prompt_tools.py` immediately.
10. **`game.js`** — in this order so each step is playable:
    (a) consume `params` from the payload + tier-coloured HP bars;
    (b) heavy attack (charge) and the `_swing(profile)` extraction;
    (c) parry + enemy `stagger` state + `vulnMul`;
    (d) `flees` branch;
    (e) `_spawnGroundRing`/`_updateRings` + the `ranged` branch;
    (f) boss layer: bar, phases, slam, combo, `_bossIntro` seam.
11. **`index.html`** — `?v=` bump, `startCombat(enemies, boss)`, `hearts_lost` in the
    POST, controls overlay rows, NPC editor selects + `saveNPC` fields.
12. **Tests** — the full list in §14.
13. **`CLAUDE.md`** — update the two real-time-combat bullets (Mechanical
    consequences + Scene collision & canvas liveliness) to describe tiers, the
    server-authoritative params table, parry/heavy, and the boss layer.
14. **Manual pass** — seed a world; author one NPC per rung plus one boss; fight each;
    confirm the beggar cannot hurt you, the boss bar/phase/slam read correctly, and a
    parried boss lunge into a charged heavy visibly chunks the bar.

---

## 14. Test list (`pytest`)

### `tests/test_enemy_tiers.py` (new)

| test | asserts |
|---|---|
| `test_combat_params_is_pure_and_deterministic` | two calls with the same `(tier, archetype)` return equal dicts; the signature accepts no player argument (`inspect.signature` has exactly 2 params) — the structural no-scaling guarantee |
| `test_hp_is_monotonic_across_the_ladder` | for each archetype, resolved `hp` is non-decreasing `harmless → boss` |
| `test_damage_is_monotonic_and_absolute` | `damage` non-decreasing across the ladder; `harmless` is exactly 0 |
| `test_standard_tier_reproduces_legacy_numbers` | `standard×brute/skirmisher/stalker` hp == 7/3/4 and windup == 650/340/280 (existing worlds play identically) |
| `test_harmless_dies_in_one_hit_and_flees` | `hp == 1`, `damage == 0`, `flees is True` for all four archetypes |
| `test_boss_hp_ignores_archetype_multiplier` | boss hp == 24 for every archetype; `params["boss"] is True`; `bossPattern` present |
| `test_windup_never_below_reaction_floor` | every tier×archetype `windup >= 220` |
| `test_ranged_params_carry_standoff_and_ring` | `ranged` has `standoff == 14`, `ringRadius == 9`, `lungeDist == 0` |
| `test_infer_danger_tier_beggar_is_harmless` | an NPC with `profession="beggar"` infers `harmless` |
| `test_infer_danger_tier_precedence` | `profession="veteran guard"` infers `dangerous`, not `standard` |
| `test_infer_danger_tier_never_returns_boss` | no input across a fixture list of dragon/god/titan descriptions yields `"boss"` |
| `test_legacy_npc_defaults_by_npc_tier` | `danger_tier=None`, no keywords: AMBIENT→`weak`, MINOR/MAJOR→`standard` |
| `test_explicit_danger_tier_wins` | `npc.danger_tier="harmless"` on a "dragon" description resolves `harmless` |

### `tests/test_combat.py` (extend the existing file — all current tests must still pass)

| test | asserts |
|---|---|
| `test_combat_start_payload_carries_tier_and_params` | `enemies[0]["tier"]`, `["params"]["hp"]`, `["params"]["damage"]` present and match `combat_params` |
| `test_combat_start_boss_sets_boss_key_and_climax_tension` | `result["boss"]["name"]` set, `_tension() == "climax"` |
| `test_combat_start_non_boss_stays_high_tension` | regression: tension `high` for a non-boss roster |
| `test_hearts_lost_maps_health_more_severely_than_hits` | `hits_taken=1, hearts_lost=3` → `badly_hurt` |
| `test_hearts_lost_absent_falls_back_to_hits` | old-client payload (`hearts_lost=0, hits_taken=2`) → `hurt` |
| `test_xp_awarded_uses_db_tier` | NPC row `danger_tier="deadly"` → `xp_awarded == 100` regardless of any client-supplied value |
| `test_xp_for_harmless_is_zero` | killing a `harmless` NPC yields `xp_awarded == 0` and no `award_xp` call |
| `test_xp_not_awarded_twice_for_same_corpse` | calling `apply_combat_result` twice with the same `defeated_npc_ids` awards XP once |
| `test_progression_absent_is_a_noop` | with no `src.services.progression` importable, result succeeds and `level_up is False` |
| `test_award_xp_called_with_total` | monkeypatched `src.services.progression.award_xp` receives the summed total once |
| `test_combat_outcome_message_names_tiers_without_numbers` | message contains `"(dangerous)"` and contains no digit-adjacent `xp`/`level` token |

### `tests/test_turn_audit.py` (extend, or new `test_turn_audit_combat.py`)

| test | asserts |
|---|---|
| `test_combat_outcome_turn_skips_damage_check` | wound-claiming text + `player_input="[COMBAT OUTCOME …]"` + unchanged health → **no** `damage_without_tool` warning |
| `test_normal_turn_still_flags_damage_without_tool` | same text with an ordinary `player_input` → warning present (no regression) |
| `test_xp_numbers_in_narration_flagged` | `"You gain 40 experience."` → `mechanics_numbers_in_narration` |
| `test_level_up_prose_without_numbers_is_clean` | `"Your hands are steadier than they were."` → no warnings |

### `tests/test_dm_prompt_tools.py` (existing — must pass unchanged)

Guards the new prompt block: every backticked tool it names is registered.

### `tests/test_world_simulation.py` / `test_playtest_seed.py` (existing)

Must pass untouched — proof that a world with `danger_tier IS NULL` everywhere still
loads, fights, and resolves.

---

## 15. Integration points with the other four specs

| spec | interface |
|---|---|
| **progression** | Consumes: `src/services/progression.award_xp(player_id, amount, reason) -> {"level_up": bool, "level": int}` and `get_combat_loadout(player_id) -> {"level","max_hearts","light_damage","heavy_damage","parry_unlocked","heavy_unlocked"}`; both **soft-imported** — enemy tiers ships and plays without them. Provides: `TIER_ROWS[tier]["xp"]` as the only combat XP source, and `changes["xp_awarded"] / ["level_up"]` on the combat-result response. **Hard boundary: no player value ever enters `combat_params`.** |
| **tiers (NPC importance)** | `NPCTier` (MAJOR/MINOR/AMBIENT) is *narrative weight*; `danger_tier` is *combat weight*. They are independent (a MAJOR quest-giver is usually `harmless`). `NPCTier` is only used as the last-resort inference default (AMBIENT→`weak`). |
| **campaign** | `danger_tier` is the campaign's difficulty vocabulary: arcs escalate by placing higher rungs, and a `boss`-tier NPC is by definition an arc's terminal beat. The campaign spec should treat "author a boss" as a first-class beat type and can gate boss placement on player level *at authoring time* (which is legal — the enemy's stats still never scale). |
| **canvas** | Sprite scale `params.scaleMul` reaches 1.68 for a boss brute — the canvas spec must ensure oversized sprites depth-sort and shadow correctly. Ground rings render in `particleLayer` (below entities), the boss HP bar in `uiLayer`. Ambient wandering is already suppressed during combat. |
| **cinematics** | Owns the presentation of two emitted triggers: DOM `boss-intro` (`{npcId, name, title, tier, durationMs}`, fired `intro_ms` before spawn) and `boss-phase` (`{npcId, phase: 2}`). Seam: override `GameRenderer.prototype._bossIntro(info) -> Promise`. game.js ships a minimal name-card fallback so v1 is standalone. Music escalation comes free via the `climax` tension floor. |

---

## 16. Open questions (do not block v1)

1. **Does the player ever see a tier word?** Right now it is DM-narrated only. A
   later "assess" affordance (hovering a hostile NPC in combat shows a coloured
   threat pip) would teach the ladder faster but risks reading as a stat screen.
2. **Boss retry loop.** Defeat → `critical` health → the DM narrates capture/rescue.
   A boss you lost to is still standing at full HP; nothing currently persists boss
   damage between attempts. Intentional for v1 (bosses are set-pieces, not
   attrition), but worth revisiting if playtests show it feels cheap.
3. **Multi-boss rosters.** `build_combat_start` takes only the *first* boss-tier NPC
   for the bar/intro. Two bosses at once is untested and probably unreadable.
4. **`heavy` and `parry` unlock gating.** Both default unlocked at level 1. If
   progression wants them as level-up rewards, the flags exist — but a level-1
   player without parry cannot meaningfully learn a boss, so the recommendation is
   to keep them free and let *damage* be the progression axis.
5. **Windup floor of 220 ms** is a guess at the human reaction wall for this
   telegraph size. Needs one live playtest at `deadly×stalker` to confirm it is
   "hard" and not "unfair".
