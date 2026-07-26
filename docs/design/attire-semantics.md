# Attire Semantics — perception, recognition, disguise, dress codes

**Status:** implementation-ready design. Extends `00-integration.md`; where that document rules, it wins.
**Frozen shared seam consumed here:** `Item.visual_desc: str | None` and `Item.attire: dict | None` (armor-slot only, keys `style` / `uniform_faction_id` / `concealing`, style menu exactly `noble|fine|common|rough|filthy`). This spec owns the *semantics* of `attire`; the sibling spec owns `visual_desc` prompts and image use. Both fields ride the same two tool-parameter edits (§9.4) — one shared edit, coordinated in the build order.

**The user requirement, restated as a contract:**

> Strangers react to **what you are wearing**. Acquaintances react to **who you are**. Those are two different facts, computed separately by code, and the DM is told both.

---

## 0. Rulings (the design decisions, made)

| # | Decision | Why |
|---|---|---|
| **A1** | Perception lives in a **new module `src/services/perception.py`**, not inside `progression.py`. | `progression.py`'s stated contract is "every progression NUMBER lives here — nothing else may hold one." Perception holds no numbers; it holds *social semantics*. It is also a leaf consumed by five different subsystems (travel, DM context, NPC context, state, checks) — a small many-consumer module must not be buried inside a big one. Import direction is one-way: `perception → progression`, never back. |
| **A2** | **Zero new DM tools.** The whole system is context + gates + one optional kwarg on an existing tool. | Every new tool is a new way for the DM to lie. Attire is a *fact about the world*, not an action; facts belong in context and gates, not in the tool menu. `tests/test_dm_prompt_tools.py` needs no `TOOL_PREFIXES` change. |
| **A3** | Style is derived **only** from the equipped armor's `attire`. No armor → `common`. Armor with no `attire` → derived from gear tier. | One source of truth, always non-NULL, always explainable to the player ("take the coat off and they stop saluting"). A NULL style would force every consumer to branch. |
| **A4** | Dress codes are enforced **inside `check_route`** — the existing shared choke point — and ride the existing `requirements` channel. | `move_player`, `travel_now`, and the canvas door `locked` flag all already read `check_route`. One insertion point gives all three, plus the frontend needs **zero** new blocking logic. |
| **A5** | Recognition is **never rolled automatically.** Code computes `contested` and hands the DM the exact `roll_check` call to make; the DM makes it when it matters. Resolutions stick for the game-day via a process-local memo. | Auto-rolling every turn is both a latency tax and a fiction disaster (twelve recognition rolls per tavern visit). Code decides *whether it is even possible*; the DM decides *when the moment arrives*. |
| **A6** | **No new audit check.** Instead, two phrases are added to the existing `_ARRIVAL_CLAIM` regex. | The only mechanically-checkable failure ("narrated getting in without getting in") is already `arrival_without_move`. R10 in the integration doc merged two overlapping checks for exactly this reason; adding a third near-duplicate would repeat the mistake. Everything else about attire is social judgment, which is precisely what the DM is *for*. |
| **A7** | The world-derived check modifier is computed by `perception`, passed **into** `roll_skill_check` as an optional keyword, not queried inside it. | `progression.roll_skill_check(p, …)` takes an already-loaded row and issues no queries; the `roll_check` tool already has a session open. Keeping it that way avoids a `progression → perception → progression` import cycle and preserves the module's purity contract. **At most one factor ever applies** — situational modifiers do not stack. |
| **A8** | `attire` on a non-armor item is **dropped with a log line**, never an error. | The DM authoring `attire` on a sword is a flavour mistake, not a rules violation. Refusing would cost a turn; dropping costs nothing. |

---

## 1. Schema delta

### 1.1 New nullable column

| Table | Column | Type | Notes |
|---|---|---|---|
| `locations` | `dress_code` | JSON NULL | `{"min_style": str|null, "banned_styles": [str], "reason": str}`. NULL = anyone may enter (every existing world). |

```python
# src/models/location.py — inside class Location, after `objects`
    # What this place refuses at the door. NULL = no standards.
    # {"min_style": "fine"|null, "banned_styles": ["filthy","concealing"],
    #  "reason": "<one sentence in the house's voice>"}
    # Normalized by src/services/perception.normalize_dress_code.
    dress_code: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

`_migrate_missing_columns` adds it with `DEFAULT NULL`; `data/emberfall.db` plays unchanged.

### 1.2 Pydantic-only (the frozen seam, already agreed)

```python
# src/models/item.py — inside class Item, after `effect_tag`
    visual_desc: str | None = Field(None, description="Short concrete visual phrase for image prompts")
    attire: dict | None = Field(None, description="ARMOR ONLY: {'style','uniform_faction_id','concealing'}")
```

No validator on the model — validation is `perception.normalize_attire`, applied at every authoring boundary (§9.4). The model stays a dumb container, consistent with `slot`/`tier`/`effect_tag`.

### 1.3 Nothing else

No new tables. No NPC column. No relationship column. Recognition state is derived from the `npc_relationships` rows that already exist, plus a process-local memo (§4.4).

---

## 2. Vocabulary (fixed menus — nothing may be added at runtime)

| Menu | Values | Owner |
|---|---|---|
| attire style | `filthy rough common fine noble` (ascending rank 0-4) | `perception.STYLES` |
| dress-code token | the five styles **+** `concealing` | `perception.DRESS_TOKENS` |
| familiarity | `stranger acquainted close` | `perception.FAMILIARITY` |
| recognition verdict | `known unknown contested` | `perception.VERDICTS` |
| check factor | `dark_or_weather filthy_in_fine_company hooded_and_watched wearing_their_colours` | `perception.CHECK_FACTORS` |

Everything mechanical about these labels (ranks, DCs, which factor beats which) is a table in `perception.py`. The DM never types any of these words and never sees the ordering.

---

## 3. `src/services/perception.py` — the pure layer

Module docstring (draft):

```python
"""How the world READS the player, and who it thinks they are.

Two facts that must never be confused:

  APPEARANCE — what strangers see. Derived entirely from the equipped armor's
  `attire` dict plus a code-owned default. A famous knight in a farmhand's
  coat is a farmhand.

  IDENTITY — who they actually are. Derived from whether an NPC has an
  NPCRelationship row with them. Clothes do not change it; a hood can only
  make it *contested*.

Reputation, faction reactions, and the player's history apply when the player
is RECOGNISED, or when a uniform claims the identity on their behalf — never
merely because the player happens to be who they are.

Numbers live in `progression.py`; this module holds no numbers except the
style rank and the two recognition difficulties, both of which are label
tables. Every function here is either pure (takes an already-loaded row) or a
thin session wrapper named `*_for`.
"""
```

### 3.1 Tables

```python
STYLES = ("filthy", "rough", "common", "fine", "noble")   # ascending
STYLE_RANK = {s: i for i, s in enumerate(STYLES)}
DEFAULT_STYLE = "common"
CONCEALING_TOKEN = "concealing"
DRESS_TOKENS = STYLES + (CONCEALING_TOKEN,)

# Armor with no authored attire still has to read as SOMETHING.
STYLE_BY_TIER = {"crude": "rough", "common": "common", "fine": "fine",
                 "superior": "fine", "master": "noble", "legendary": "noble"}

# The one-line human string a stranger would use. Never shown to the player
# verbatim — it goes to the DM and to the character panel.
STYLE_READ_AS = {
    "filthy": "someone sleeping rough",
    "rough":  "someone who works with their hands",
    "common": "an ordinary townsperson",
    "fine":   "someone with money",
    "noble":  "someone of rank",
}
CONCEALED_READ_AS = "someone who does not want to be seen"

FAMILIARITY = ("stranger", "acquainted", "close")
VERDICTS = ("known", "unknown", "contested")

# How hard it is to stay unrecognised by someone who has met you. Words from
# progression.DIFFICULTY_DC — code picks them so the DM cannot sandbag.
RECOGNITION_DIFFICULTY = {"acquainted": "tricky", "close": "hard"}
RECOGNITION_SKILL = "stealth"

CLOSE_TRUST = 70          # trust_level at or above this reads as "close"
CLOSE_MOMENTS = 3         # ...or this many key_moments

READ_AS_MAX = 90          # chars; truncated on a word boundary
```

### 3.2 Normalizers (the authoring boundary)

```python
def normalize_attire(raw: Any, *, slot: str | None = None) -> dict | None:
    """Clamp an authored attire dict to the fixed menu, or None.

    Returns None (and logs once) when `slot` is anything but "armor" — the
    frozen seam says attire exists on armor and nowhere else. Unknown style
    words fall back to DEFAULT_STYLE rather than erroring: a garbled label
    must never cost a turn.
    """
    # -> {"style": <one of STYLES>, "uniform_faction_id": str|None,
    #     "concealing": bool}


def normalize_dress_code(raw: Any) -> dict | None:
    """Clamp an authored dress code, or None when nothing survives.

    - min_style: one of STYLES, else None
    - banned_styles: deduped subset of DRESS_TOKENS, order-stable
    - reason: str, stripped, capped at 200 chars, digits NOT stripped (a
      house rule may legitimately say "after the ninth bell")
    A dict with neither a min_style nor a banned_styles entry returns None —
    an empty gate is worse than no gate, because the DM would still see it.
    """
```

### 3.3 Reading the player

```python
def attire_of(item: dict | None) -> dict | None:
    """The normalized attire of an inventory entry, armor-slot only.

    Mechanizes through `progression.mechanize_item` first, so a legacy
    `type:"armor"` item is eligible too.
    """


def perception_profile(player, *, faction_name: str | None = None) -> dict:
    """How this player READS to a stranger. Pure — no queries.

    Returns:
        {"style": "filthy|rough|common|fine|noble",
         "uniform_faction_id": str | None,
         "uniform_faction_name": str | None,     # only when caller resolved it
         "concealing": bool,
         "visual_desc": str | None,              # from the armor item
         "read_as": str,                         # <= READ_AS_MAX chars
         "source": "attire" | "tier" | "bare"}
    """
```

Resolution order, exactly:

1. `armor = progression.resolve_loadout(player)["armor"]`.
2. No armor → `{style: "common", uniform: None, concealing: False, visual_desc: None, source: "bare"}`.
3. Armor with `attire` → normalized attire, `source: "attire"`.
4. Armor without `attire` → `style = STYLE_BY_TIER[tier_label]`, no uniform, not concealing, `source: "tier"`.
5. `visual_desc` always taken from the armor item when present (any source).

`read_as` assembly (fixed templates, no LLM, deterministic):

```
base   = CONCEALED_READ_AS                       if concealing
       = f"a member of the {faction_name}"       elif faction_name
       = f"someone in unfamiliar colours"        elif uniform_faction_id (name unresolved)
       = STYLE_READ_AS[style]                    otherwise
read_as = f"{base} — {visual_desc}" if visual_desc else base
```

then truncated to `READ_AS_MAX` on a word boundary with `"…"`.

Worked examples:

| Equipped armor | `read_as` |
|---|---|
| *(nothing)* | `an ordinary townsperson` |
| Watch Coat, attire `{common, watch_id, false}`, visual_desc set | `a member of the Lantern Watch — a boiled-leather Watch coat with the brass lantern badge` |
| Ashfall Hood, attire `{rough, null, true}` | `someone who does not want to be seen — a soot-grey hood that swallows the face` |
| Mucker's Oilskin, attire `{filthy, null, false}` | `someone sleeping rough — a stable oilskin gone black with muck` |
| Superior plate, **no** attire | `someone with money` *(tier fallback)* |

```python
def profile_for(session, player) -> dict:
    """perception_profile + the uniform faction's NAME resolved (one query,
    only when a uniform is actually worn)."""
```

### 3.4 Dress verdict (pure)

```python
def dress_verdict(profile: dict, dress_code: dict | None) -> dict:
    """{"allowed": bool, "failed": "" | "min_style" | "banned",
        "token": str, "reason": str, "requirement": str}

    `requirement` is the single human string that rides the existing
    Connection.requirements channel, so every consumer that joins
    requirements into a message keeps working untouched.
    """
```

Rules, in order:

1. `dress_code` is None/normalizes to None → allowed.
2. `profile["concealing"]` and `"concealing" in banned_styles` → blocked, `failed="banned"`, `token="concealing"`.
3. `profile["style"] in banned_styles` → blocked, `failed="banned"`, `token=style`.
4. `min_style` set and `STYLE_RANK[style] < STYLE_RANK[min_style]` → blocked, `failed="min_style"`.
5. Otherwise allowed.

`requirement` string template:

```
f"{reason} (the player currently reads as {read_as_short})"
```

where `read_as_short` is the `base` half of `read_as` (before the em-dash) so the requirement line stays short in the DM's message.

Example: `"Maera keeps a warm room and a clean floor: no covered faces, and nobody who has been sleeping in the mudflats. (the player currently reads as someone who does not want to be seen)"`

### 3.5 Where every entry point runs

| Caller | Function | Cost |
|---|---|---|
| `build_player_state` | `profile_for` | +1 query (faction name) only when a uniform is worn |
| `DMOrchestrator._build_context` | `appearance_lines(player_id)` | 1 session, ≤4 indexed reads |
| `travel.check_route` | `dress_verdict` | 0 queries (profile passed in by caller) |
| `agents_as_tools.prompt_npc_agent` | `npc_recognition_block` | 1 session, 2 indexed reads — already inside the existing UoW block |
| `world_write/progression.roll_check` | `situational_factor` | ≤4 indexed reads, inside the session it already opens |
| `POST /api/player/{id}/equip` | `profile_for` ×2 (before/after) | inside the session it already opens |

**No new LLM round trip anywhere.** No path in this spec calls a model.

---

## 4. Recognition

### 4.1 Familiarity (pure-ish, one query)

```python
def familiarity(session, player_id: str, npc_id: str) -> str:
    """'stranger' | 'acquainted' | 'close'.

    - no NPCRelationship row                       -> "stranger"
    - row, trust >= CLOSE_TRUST or >= CLOSE_MOMENTS key moments -> "close"
    - row otherwise                                -> "acquainted"

    The row is the ONLY signal. It is written by `_record_npc_exchange` on
    every real exchange, which is exactly the definition we want: they know
    you because you have actually spoken.
    """
```

### 4.2 The verdict

```python
def recognition(session, player, npc, *, profile: dict | None = None,
                day: int | None = None) -> dict:
    """Does this NPC know who they are looking at?

    Returns:
        {"familiarity": "stranger|acquainted|close",
         "verdict": "known" | "unknown" | "contested",
         "identity_known": bool,          # do they know WHO this is?
         "assumed_faction_id": str | None,# what the CLOTHES claim
         "assumed_faction_name": str | None,
         "reputation_visible": bool,      # == identity_known
         "check": {"skill": "stealth", "difficulty": "tricky",
                   "about_npc_id": "<id>"} | None,
         "line": "<the briefing sentence>"}
    """
```

Truth table — the whole system in nine cells:

| | `concealing = False` | `concealing = True` |
|---|---|---|
| **stranger** | `unknown` · identity_known **False** | `unknown` · identity_known **False** — *no roll, ever* |
| **acquainted** | `known` · identity_known **True** | `contested` · check `stealth`/`tricky` |
| **close** | `known` · identity_known **True** | `contested` · check `stealth`/`hard` |

Independent of every cell above: `assumed_faction_id = profile["uniform_faction_id"]`. **A Watch uniform makes strangers treat you as Watch regardless of who you are, and it does so even when `identity_known` is False.** That is the structural separation the requirement asks for: identity flows down the left column, appearance flows in from the profile, and they never touch.

`reputation_visible` gates the player's *history and standing*. When it is False, the NPC-facing block explicitly forbids using the name or referring to shared history (§6.2). It does **not** strip the relationship data from the NPC agent's prompt — that data is the NPC's memory, and they need it the instant recognition lands. Suppression is an instruction on top of code-computed truth, which is the only workable shape without gutting NPC memory.

### 4.3 The trigger — when the contested roll actually happens

**Code never rolls.** `recognition` returns `verdict: "contested"` plus the exact call to make. The DM makes it when the fiction forces the question, per the prompt rule (§7):

- the NPC addresses the player, or the player addresses them
- the player is asked to show a face, to sign, to pay, to pass
- the scene otherwise puts them under a light

The DM calls `roll_check(player_id, "stealth", "<difficulty from the context line>", about_npc_id="<npc_id>")`. Outcome mapping is the standard five-label ladder — the DM narrates it as usual. `critical_failure` is the interesting one: recognised *and* they say the name out loud in front of the room.

### 4.4 Making the answer stick (process-local memo)

Re-litigating recognition on every message of one conversation is the failure mode that would kill this feature. The memo prevents it:

```python
# (player_id, npc_id) -> {"day": int, "recognized": bool}
_recognition_memo: dict[tuple[str, str], dict] = {}
_memo_lock = threading.Lock()

def remember_recognition(player_id: str, npc_id: str, *, recognized: bool, day: int) -> None
def recalled_recognition(player_id: str, npc_id: str, day: int) -> bool | None   # None = unresolved/stale
def clear_recognition(player_id: str | None = None) -> None                       # world switch
```

- Written by the `roll_check` tool when `about_npc_id` is set and `skill == "stealth"`: `recognized = outcome in ("failure", "critical_failure")` — the player rolled *to stay hidden*, so a failed roll means they were made.
- Read by `recognition()`: a live memo overrides `contested` with `known`/`unknown`.
- Scoped to the game **day** (`WorldClock.day`). A new day means a new hood and a fresh question.
- Process-local, exactly like `pending_actions`. If the process restarts mid-scene, the worst case is one extra roll. `clear_recognition` is called next to `pending_actions.clear` wherever that fires (world switch, player deletion).

---

## 5. Dress-code gates — the same code path as `Connection.requirements`

### 5.1 `check_route` signature change (additive, back-compatible)

```python
def check_route(session, from_location_id: str | None, destination_id: str,
                player=None) -> dict[str, Any]:
    """...
    player: the Player row, when the caller has one. Dress codes are only
        evaluated when it is supplied — a route question with no player is a
        question about the map, not about a person. This keeps
        tests/test_travel_gate.py passing byte-for-byte.
    """
```

Return shape gains one value in an existing field: `reason` becomes `"" | "route" | "gate" | "dress"`.

Insertion, after the existing rule-1/rule-2 block computes `requirements` / `blocked` / `reason`:

```python
    # Rule 3: the destination's own standards. Evaluated on the DESTINATION
    # regardless of which route was taken — a dress code is a door policy,
    # not a road condition — and only when we know who is asking.
    verdict = {"allowed": True}
    if player is not None and getattr(destination, "dress_code", None):
        verdict = perception.dress_verdict(
            perception.perception_profile(player),
            perception.normalize_dress_code(destination.dress_code),
        )
    if not verdict["allowed"]:
        requirements = list(requirements) + [verdict["requirement"]]
        reason = reason or "dress"          # route/gate keep precedence
        blocked = True
```

Precedence is deliberate: if a road already needs a key **and** the hall needs a coat, the player needs both, `requirements` carries both strings, and `reason` names the harder-to-explain blocker (`route`/`gate`) so the existing messages stay truthful. `reason == "dress"` fires only when the dress code is the sole blocker.

`perception_profile` is called with the *unresolved* faction name here (no extra query on the travel hot path); `read_as_short` never needs it, because a dress code cares about style and concealment, not about which colours.

### 5.2 Call-site updates (four lines total)

| File | Change |
|---|---|
| `src/tools/world_write/player.py` `move_player` | `check_route(session, player.current_location_id, destination_id, player)` |
| `src/services/travel.py` `travel_now` | `check_route(session, origin_id, destination_id, player)` — `player` is already loaded three lines above |
| `src/web/server.py` `/api/assets/location/{id}` | load the player row once in the existing `with get_session() as db:` block, pass it into the per-exit `check_route` — dress-locked doors then render as `locked: true` for free |
| everything else | unchanged (`player` defaults to `None`) |

### 5.3 `move_player`'s third blocked message

```python
            elif route["reason"] == "dress":
                message = (
                    f"{destination.name} will not take them dressed as they are: "
                    f"{', '.join(str(r) for r in requirements)} "
                    "You decide whether that stands. If they talk their way past, "
                    "bribe the door, borrow a coat, or are simply known here, call "
                    "move_player again with requirements_met=True. Otherwise narrate "
                    "the refusal — and let it cost them something."
                )
```

`requirements_met=True` is the **only** override, and it is the same override the DM already knows. There is no dress-specific escape hatch and no new parameter.

### 5.4 Frontend: nothing to build

- Canvas doors already read `exits[].locked` from `/api/assets/location/{id}` → a dress-locked door renders locked, and `game.js` dispatches `canvas-travel` with `locked: true` → `index.html` sends `"I try to get through to X."` to the DM. Existing code, existing wiring.
- `POST /api/travel` already returns `409 {blocked, requirements, destination_name, chat_prompt}` and `index.html` already routes `chat_prompt` into chat.
- **One optional line** in the `/api/travel` blocked payload: pass `"reason": result.get("reason")` through so a future UI can differentiate. No client change in v1.

### 5.5 Seeded example (playtest world)

`The Drowned Lantern` gets:

```python
dress_code=normalize_dress_code({
    "min_style": None,
    "banned_styles": ["concealing", "filthy"],
    "reason": ("Maera keeps a warm room and a clean floor: no covered faces "
               "under her roof, and nobody who has been sleeping in the mudflats."),
}),
```

It demonstrates both rules on one row, needs no new location (so `test_playtest_seed.py` grows by assertions, not by restructuring), and it hangs off the seed's existing ash-signed-ledger mystery — Maera has a *reason* to hate hoods.

Three garments seeded so the whole matrix is reachable on day one, all through `inventory_notable` (a `transfer_item` away — scene objects are stripped of `slot`/`tier` by `normalize_objects`, so gear cannot enter that way):

| NPC | Item | slot/tier | attire |
|---|---|---|---|
| Captain Brann Coldiron | **Lantern Watch Coat** | armor / common | `{"style": "common", "uniform_faction_id": <watch.id>, "concealing": false}` |
| Vex Marrow (Ashen Fangs scout) | **Ashfall Hood** | armor / common | `{"style": "rough", "uniform_faction_id": null, "concealing": true}` |
| Sorrel Tack (stable hand) | **Mucker's Oilskin** | armor / crude | `{"style": "filthy", "uniform_faction_id": null, "concealing": false}` |

Each gets a `visual_desc` (the sibling spec's field, authored here because the seed is one file): `"a boiled-leather Watch coat with the brass lantern badge"`, `"a soot-grey hood that swallows the face"`, `"a stable oilskin gone black with muck"`.

The resulting day-one loop, with no LLM in it: buy the hood → the Lantern's door locks → the DM adjudicates; wear the Watch coat → strangers salute, Brann does not.

---

## 6. Context shaping

### 6.1 DM context — the APPEARANCE lines

Current player briefing in `DMOrchestrator._build_context`:

```
- Player health: {health}
- Player level: {level}
- Player carries: {gear}
- Player inventory: {items}
```

Insert **immediately after `Player carries:`** (it is the same subject — what is on their body):

```
- Player reads as: {read_as}
{risk lines, 0-2 of them, each already prefixed with "- "}
```

```python
def appearance_lines(player_id: str) -> list[str]:
    """The 1-3 context lines describing how the player reads right now.

    Line 1 is always present. Lines 2-3 fire only when there is a live social
    risk in the room, so a plainly-dressed player in an empty street costs one
    line and nothing else.
    """
```

Line 1, always:

```
- Player reads as: a member of the Lantern Watch — a boiled-leather Watch coat with the brass lantern badge
```

Risk line **U** (uniform + a member of that faction is present):

```
- They are wearing Lantern Watch colours and Lantern Watch are standing here (Brann Coldiron). Strangers will treat them as Watch. If someone with standing to know decides the coat is not theirs, that is a crime, not a misunderstanding.
```

Risk line **U-lite** (uniform, no member of that faction present):

```
- They are wearing Lantern Watch colours: strangers here will assume they are Watch, and act accordingly.
```

Risk line **C** (concealing + at least one present NPC who has met them; names up to two, "and others" beyond):

```
- Their face is covered. Maera Duskwell has met them before and may see past it. If Maera looks at them closely — if she speaks to them, or they speak to her — call roll_check(player_id, "stealth", "tricky", about_npc_id="<maera_id>") BEFORE deciding whether she names them.
```

Risk line **C** is suppressed when `recalled_recognition` already has an answer for that NPC today; instead:

```
- Their face is covered, and Maera Duskwell has already placed them today. She knows exactly who is under the hood.
```

Cap: at most two risk lines (U/U-lite and C are mutually compatible; nothing else exists). Everything is code-generated from fixed templates — no LLM, ~4 indexed reads inside `_build_context`, which already runs a world tick and a campaign tick.

### 6.2 `prompt_npc_agent` — the recognition block

Computed in code inside the existing `with unit_of_work() as uow:` block (which already loads the NPC and the relationship — one extra read for the player row), and appended to `agent._scene_context`, which lands in the NPC's **system prompt**. System prompt, not the narrative context blob: this is the NPC's standing truth about who they are looking at, and it must not be diluted by a long DM briefing.

```python
def npc_recognition_block(session, player, npc, *, day: int | None = None) -> str:
    """The '**Who you see**' block for the NPC's system prompt. Fixed
    templates only — the engine decided this, and the NPC does not get a
    vote."""
```

Four variants, verbatim drafts:

**Stranger, no uniform**

```
**Who you see (the engine decided this — it is not yours to change):**
You do not know this person. What you see: an ordinary townsperson.
You do not know their name, their history, or their reputation. Do not use a name you were never told, and do not react to anything they have done elsewhere. Judge them on what is in front of you.
```

**Stranger, uniform**

```
**Who you see (the engine decided this — it is not yours to change):**
You do not know this person. What you see: a member of the Lantern Watch — a boiled-leather Watch coat with the brass lantern badge.
They are wearing Lantern Watch colours, so treat them as Lantern Watch: give them what you would give the Watch, refuse them what you would refuse the Watch. You do not know their name. If you have reason to doubt the coat, you may doubt it out loud — but you do not know.
```

**Known (acquainted or close, not concealed)**

```
**Who you see (the engine decided this — it is not yours to change):**
You know this person: Rook Fennick. You recognise them on sight, whatever they are wearing. Everything in your memory of them applies.
What they have on: a member of the Lantern Watch — a boiled-leather Watch coat with the brass lantern badge. You may absolutely have an opinion about that.
```

That last sentence is the whole feature in one line: an acquaintance who sees you in a Watch coat *knows it is a costume*, and the NPC agent is told it may say so.

**Contested (concealed, has met them)**

```
**Who you see (the engine decided this — it is not yours to change):**
This person's face is covered: someone who does not want to be seen — a soot-grey hood that swallows the face.
You have met them before, but you have NOT placed them. Unless the narrator tells you the hood came off, do not use their name, do not refer to anything you have done together, and do not act on your history with them. You may be uneasy. You may ask them to show their face.
```

The `context` parameter the DM passes is untouched — it still flows into `narrative_context` exactly as today. The recognition block is additive and code-owned, so the DM cannot forget it, water it down, or contradict it.

---

## 7. DM prompt touch — ONE new subsection

Appended to `### GUIDELINES FOR SPECIFIC SITUATIONS` as **7**, after `6. Cinematic Moments`. **No other section of the prompt is edited.** Every backticked tool (`roll_check`, `move_player`, `update_player_reputation`, `create_event`, `start_combat`) is already registered in `DM_TOOLS`, so `tests/test_dm_prompt_tools.py` passes with no `TOOL_PREFIXES` change.

```
**7. What They Are Wearing (the world reads clothes, not character sheets):**
- Your context carries a `Player reads as:` line. That is what STRANGERS see, computed by the
  engine from what the character actually has on their body. Trust it over your memory of who
  they are. A famous knight in a farmhand's coat is a farmhand until somebody recognises them,
  and a hero in ordinary clothes is nobody at all.
- **Uniforms make claims.** When the line says they are wearing a faction's colours, strangers
  treat them as that faction — doors open, orders are expected to land, that faction's enemies
  react accordingly — whether or not they have any right to the coat. Play the assumption, not
  the truth.
- **A false uniform is a crime, not a costume.** If someone with standing to know — an officer
  of that faction, anyone who has served with the real thing — decides the coat is stolen, the
  world answers: `update_player_reputation` against that faction, `create_event` so it sticks,
  and `start_combat` if they would rather arrest than argue. Never hand-wave the unmasking.
- **People who know them see through clothes.** An NPC the player has actually spoken to knows
  them on sight; a hood does not change that. When a hood IS in play and an acquaintance is
  present, your context names the exact check to make. Call `roll_check` with the skill and
  difficulty it gives you — and pass `about_npc_id` so the answer holds for the rest of the
  scene — BEFORE you decide whether they get named. Do not decide it yourself, and do not roll
  it again because you disliked the answer.
- **Doors have standards.** Some places will not admit certain kinds of dress. The engine blocks
  the way and hands you the house's own reason. That refusal is yours to run: let them argue,
  bribe, borrow a coat, or find the yard door. If you decide they get in, call `move_player`
  with requirements_met=True — otherwise they did not get in.
- Never say the engine's words. Do not tell the player their "style", do not name a check, do
  not mention advantage or difficulty. Say what the doorman's face does.
```

### 7.1 Creator prompt — one new section

Inserted after `## SCENE OBJECTS` in `src/agents/creation_agent.py`:

```
## DRESS CODES (only where a doorman would really turn someone away)

A location may carry a `dress_code`. Most must not. A road, a market, a
dockhands' tavern has no standards. Author one only for a place whose whole
character is that it keeps people out: a banking hall, an officers' club, a
temple sanctum, a private box at the races.

dress_code = {"min_style": "<filthy|rough|common|fine|noble>" or null,
              "banned_styles": ["<filthy|rough|common|fine|noble|concealing>", ...],
              "reason": "<one sentence in the house's own voice>"}

- "min_style" is the LOWEST way of dressing they will admit, ranked poorest
  first: filthy < rough < common < fine < noble. Leave it null unless the
  place genuinely requires money to walk in. "fine" is already exclusive;
  "noble" shuts out nearly everyone alive.
- "banned_styles" turns specific kinds away regardless of rank. "filthy" is
  the ordinary one. "concealing" bans covered faces — hoods, masks, veils —
  and is the single most interesting rule you can write, because it forces a
  choice on anyone travelling hidden.
- "reason" is what the person on the door actually says. One sentence, in
  their voice. No mechanics, no menus, no numbers.

The engine enforces it: a player dressed wrong cannot walk in, and the DM has
to decide whether they talk their way past. You are authoring a wall — make it
a wall worth talking to.
```

---

## 8. World-derived check modifiers

### 8.1 The table (four rows — deliberately small)

Evaluated **in this order; the first match wins; at most one factor ever applies.** Disadvantages precede the uniform advantage so a filthy or hooded impostor gets no free help.

| # | id | skills | condition (all code-read) | effect | `because` (what the DM is told) |
|---|---|---|---|---|---|
| 1 | `dark_or_weather` | `stealth` | `clock.time_of_day == "night"` **or** `DMState.weather in {fog, rain, snow, ash}` | advantage | `the dark and the weather are covering them` |
| 2 | `filthy_in_fine_company` | `persuasion` | `style in {filthy, rough}` **and** the room is fine: location `dress_code.min_style` ranks ≥ `fine`, **or** `atmosphere_tags` ∩ `{wealthy, opulent, noble, refined}` | disadvantage | `they are dressed like something the tide left, in a room that notices` |
| 3 | `hooded_and_watched` | `persuasion` | `concealing` **and** ≥1 NPC present | disadvantage | `nobody trusts a face they cannot see` |
| 4 | `wearing_their_colours` | `persuasion`, `cunning` | `uniform_faction_id` set **and** (location `controlling_faction_id` matches **or** any present NPC's `faction_id` matches) | advantage | `they are wearing the colours of the people they are dealing with` |

`athletics` and `perception` deliberately get no rows in v1: weather-blinded perception is tempting but it fights the `dark_or_weather` stealth row in the same scene, and a player who is *both* helped and hurt by the same fog is a confusing table. One-directional, four rows, done.

```python
def situational_factor(session, player, skill: str, *, profile=None,
                       location=None, npcs_present=None) -> dict | None:
    """{"factor": <id>, "effect": "advantage"|"disadvantage",
        "because": "<short phrase>"} or None.

    Reads: WorldClock (1), DMState (1), Location (1), NPCs at location (1).
    Runs only inside roll_check — never on a per-turn path.
    """
```

### 8.2 Layering onto the DM's modifier

```python
# src/services/perception.py
def combine_modifier(dm_modifier: str, factor_effect: str | None) -> str:
    """The DM's word and the world's word, resolved. Never stacks: two
    advantages are still one advantage, and opposites cancel to none."""
    if not factor_effect:
        return dm_modifier
    if dm_modifier == "none":
        return factor_effect
    if dm_modifier == factor_effect:
        return dm_modifier
    return "none"
```

`roll_skill_check` gains **one optional keyword-only parameter**:

```python
def roll_skill_check(p, skill, difficulty, modifier="none", *,
                     situation: dict | None = None) -> dict[str, Any]:
```

- Validation of `skill`/`difficulty`/`modifier` is unchanged (an invalid DM word still errors — a coerced word is a number the DM did not choose).
- **Who calls `combine_modifier`:** the `roll_check` **tool**, not `roll_skill_check`. `combine_modifier` lives in `perception`, and `progression` must never import `perception` (A1). So the tool builds the full situation dict and hands it down:

  ```python
  # src/tools/world_write/progression.py, inside roll_check's session
  factor = perception.situational_factor(session, player, skill, ...)   # dict | None
  situation = None
  if factor:
      situation = {**factor,
                   "effective": perception.combine_modifier(modifier, factor["effect"])}
  result = prog.roll_skill_check(player, skill, difficulty, modifier, situation=situation)
  ```

  `roll_skill_check` then does exactly one new thing: `effective = (situation or {}).get("effective") or modifier`, and rolls with `effective` instead of `modifier`. Six lines in `progression`, no cycle, no duplicated table.
- Returns `"modifier": modifier` unchanged (the DM's own word) plus `"situation": situation | None`; `_debug` gains `"effective_modifier"`.

**Legacy guarantee:** `situation=None` produces byte-identical behaviour to today under a seeded RNG. Test-pinned (`test_check_situation.py::test_no_factor_matches_legacy_roll`).

### 8.3 How the DM is told

`roll_check` tool result gains one optional key and one instruction suffix — **a label and a phrase, never the word "advantage", never a number:**

```python
    if situation:
        result["situation"] = {"factor": situation["factor"], "because": situation["because"]}
        result["instruction"] += (
            f" The world was with them: {situation['because']} — let that show in how it goes."
            if situation["effect"] == "advantage" else
            f" The world was against them: {situation['because']} — let that show in how it goes."
        )
```

The `effect` word is withheld from the DM on purpose: it is a mechanical term from the menu, and if the DM can read it, sooner or later it prints "you had the advantage here". The `because` phrase carries everything narration needs.

The SSE `check_result` event is **unchanged** — outcome and skill, no numbers, no factor. The factor is a narration aid, not a HUD element.

### 8.4 `about_npc_id` — the recognition binding

```python
@tool
def roll_check(player_id, skill, difficulty, modifier="none", reason="",
               about_npc_id: str = "") -> dict[str, Any]:
```

Docstring addition:

```
        about_npc_id: Only for a recognition check your context asked for —
            the NPC who might be placing the player. Passing it makes the
            answer stick for the rest of the day, so you never have to ask
            twice in one scene.
```

Behaviour: when `about_npc_id` is set **and** `skill == "stealth"`, the tool calls `perception.remember_recognition(player_id, about_npc_id, recognized=outcome in ("failure", "critical_failure"), day=clock.day)` after the roll resolves. Any other skill ignores the parameter (logged at debug). This is additive to the frozen `00-integration.md` §2.3 signature; it introduces no new tool and no new menu word.

---

## 9. Everything else that changes

### 9.1 `build_player_state` → `state.appearance`

```python
    try:
        with get_session() as db:
            p = db.query(Player).filter(Player.id == player_id).first()
            appearance = perception.profile_for(db, p) if p else None
    except Exception as e:
        logger.debug(f"Appearance unavailable: {e}")
        appearance = None
```

added to the returned dict as `"appearance": appearance`. Shape is §3.3's profile verbatim. Null-safe: a client that does not know the key ignores it.

### 9.2 Character panel row (index.html, `renderHudPanel`, `character` branch)

One row, inserted after the `trinket` slot rows and before the quick-slot row:

```js
    `<div class="hud-item"><span class="muted">reads as</span><span>${
        esc((s.appearance && s.appearance.read_as) || 'an ordinary townsperson')}</span></div>` +
```

That is the entire frontend delta. **`game.js` and `audio.js` are untouched.** One `?v=` bump on both script tags per the R13 rule (both tags, same value, once).

### 9.3 `pending_actions.equip_note` — the consequence rides the existing ledger

Equipping is already a ledger kind (`equip`). Extend the note so the DM learns the *social* consequence, not just the item:

```python
def equip_note(name: str, slot: str = "weapon", *, automatic: bool = False,
               reads_as: str | None = None) -> str:
    """...
    reads_as: the player's NEW `read_as` string, passed ONLY when equipping
        this piece actually changed how they read. Armor only; a swap between
        two plain coats says nothing and adds nothing.
    """
```

Appends `f" To strangers they now read as {reads_as}."` when supplied. The `/api/player/{id}/equip` endpoint computes `profile_for` before and after inside the session it already holds, and passes `reads_as` only when `read_as` changed. `POST .../unequip` does the same. No new ledger kind, no new `tool_equivalents` entry (equipping still runs no world-write tool).

### 9.4 Authoring boundaries for `attire` (shared edit with the sibling spec)

Both fields of the frozen seam land in the same two tool signatures. **This is the one file both designs touch — build it once, in the order given in §10.**

| Tool | Added params | Validation |
|---|---|---|
| `add_to_inventory` (`world_write/player.py`) | `visual_desc: str | None = None`, `attire: dict | None = None` | `attire` → `perception.normalize_attire(raw, slot=mechanized_slot)`; dropped + logged on non-armor (A8) |
| `stock_merchant` (`world_write/progression.py`) | per-entry `"visual_desc"`, `"attire"` on `kind: "armor"` entries | same normalizer inside `_prepare_entry`; unknown style words clamp, they do not refuse the stock line |
| `add_location` / `update_location` (`world_write/locations.py`) | `dress_code: dict | None = None` | `perception.normalize_dress_code` |
| `PUT /api/world/locations/{id}` (`LocationUpdate`) | `dress_code: dict | None = None` | same normalizer |
| `PUT /api/world/npcs/{id}` | — | unchanged |

Docstring menu for `attire` (in both tools, identical wording):

```
        attire: ARMOR ONLY. How this garment makes its wearer READ to strangers:
            {"style": "noble"|"fine"|"common"|"rough"|"filthy",
             "uniform_faction_id": "<faction id>" or null,
             "concealing": true|false}
            Set uniform_faction_id when the piece is a faction's recognisable
            colours — strangers will then treat the wearer as that faction,
            with everything that implies. Set concealing for hoods, masks and
            veils: strangers cannot identify the wearer at all, and people who
            know them have to work for it. The engine owns what each word does.
```

### 9.5 Turn audit — regex extension only (A6)

`_ARRIVAL_CLAIM` in `src/web/turn_audit.py` gains the two phrases dress-code scenes actually produce:

```python
_ARRIVAL_CLAIM = re.compile(
    r"\byou (?:arrive (?:at|in)|reach the|step into|pass through the gates? of"
    r"|are waved (?:through|in|inside)|are let (?:in|inside|through))\b",
    re.IGNORECASE,
)
```

No new check, no new suppression, no `code_actions` change — a dress-code refusal mutates nothing, so there is nothing to record in the ledger and nothing to suppress. When the DM overrides with `requirements_met=True`, `move_player` succeeds and the existing check clears normally. The `[REALITY AUDIT]` feedback loop needs no new text.

---

## 10. Build checklist (numbered, in order)

1. **`src/models/location.py`** — add `dress_code` column. Boot `data/emberfall.db`, confirm the `[migrate]` line. *(files: `location.py`)*
2. **`src/models/item.py`** — add `visual_desc` + `attire` (the frozen seam). **Coordinate: whichever spec builds first lands both fields; the other rebases.** *(files: `item.py`)*
3. **`src/services/perception.py`** — the whole pure layer: tables, `normalize_attire`, `normalize_dress_code`, `attire_of`, `perception_profile`, `profile_for`, `dress_verdict`, `combine_modifier`, `read_as` assembly. No callers yet. *(new file + `tests/test_perception.py`)*
4. **`perception.py` part 2** — `familiarity`, `recognition`, the memo (`remember_recognition` / `recalled_recognition` / `clear_recognition`), `npc_recognition_block`. *(same file + `tests/test_recognition.py`)*
5. **`perception.py` part 3** — `CHECK_FACTORS`, `situational_factor`, `appearance_lines`. *(same file + `tests/test_check_situation.py`)*
6. **Dress gate** — `check_route(…, player=None)` + rule 3; `move_player` third branch + call-site; `travel_now` call-site; `/api/assets/location/{id}` call-site. `tests/test_travel_gate.py` must still pass **unmodified**. *(files: `services/travel.py`, `tools/world_write/player.py`, `web/server.py` + `tests/test_dress_code.py`)*
7. **Check modifiers** — `roll_skill_check(*, situation=None)`; `roll_check` tool computes the factor, combines, passes it, adds `situation` to the result and the instruction suffix, adds `about_npc_id` and the memo write. *(files: `services/progression.py`, `tools/world_write/progression.py`)*
8. **State + HUD** — `build_player_state` `appearance` key; `renderHudPanel` "reads as" row; one `?v=` bump on both script tags. *(files: `web/server.py`, `web/static/index.html`)*
9. **DM context** — `_build_context` inserts `appearance_lines` after `Player carries:`. *(file: `agents/dm_orchestrator.py`)*
10. **NPC context** — `prompt_npc_agent` computes `npc_recognition_block` in the existing UoW block and appends it to `agent._scene_context`. *(file: `tools/agents_as_tools.py`)*
11. **Prompts** — DM GUIDELINES subsection **7**; creator `## DRESS CODES` section. *(files: `agents/dm_orchestrator.py`, `agents/creation_agent.py`)*
12. **Authoring params** — `add_to_inventory`, `stock_merchant`, `add_location`, `update_location`, `LocationUpdate` (§9.4). *(files: `tools/world_write/player.py`, `progression.py`, `locations.py`, `web/server.py`)*
13. **Ledger** — `equip_note(reads_as=…)` + the equip/unequip endpoints computing before/after. *(files: `web/pending_actions.py`, `web/server.py`)*
14. **Audit** — the two `_ARRIVAL_CLAIM` phrases. *(file: `web/turn_audit.py`)*
15. **Seed** — the Drowned Lantern dress code + three garments on Brann / Vex / Sorrel. *(file: `src/data/playtest_seed.py`)*
16. **Closure** — `clear_recognition` beside every `pending_actions.clear` site; CLAUDE.md bullet; full pytest; manual pass (§12).

File-conflict note against the shipped overhaul's hotspots: steps 6, 8, 12, 13 all touch `src/web/server.py` — they are **serialized under one owner**, exactly as B3 was. Step 2 is the shared seam with the sibling spec. Everything else is disjoint.

---

## 11. Test plan (pytest)

**New files**

| File | Coverage |
|---|---|
| `tests/test_perception.py` | no armor → `common` / `"an ordinary townsperson"`; tier fallback table (crude→rough … legendary→noble); authored attire wins over tier; `read_as` templates for all four branches; `visual_desc` composition + `READ_AS_MAX` word-boundary truncation; `normalize_attire` clamps unknown style → `common`, strips attire from non-armor, coerces `concealing` to bool; `normalize_dress_code` drops unknown tokens, returns None on an empty gate, caps `reason`; `dress_verdict` rank ordering incl. equal-rank allow, `concealing` ban, banned-before-min precedence |
| `tests/test_recognition.py` | the full 3×2 familiarity/concealing truth table; `familiarity` from trust ≥70 and from ≥3 key moments; `identity_known` False for a concealed acquaintance; **`assumed_faction_id` set independently of `identity_known`** (the structural-separation test); `reputation_visible == identity_known`; memo write/read/day-staleness/`clear_recognition`; **`recognition()` never touches the RNG** (assert `progression._RNG` state unchanged) |
| `tests/test_dress_code.py` | `check_route(player=None)` never evaluates a dress code (legacy parity); dress-only block → `reason == "dress"`; route requirement + dress code → both strings in `requirements`, `reason == "route"`; `move_player` blocked then `requirements_met=True` succeeds; `travel_now` blocked payload shape; exits enrichment marks a dress-locked door `locked: true`; a NULL `dress_code` location is never blocked |
| `tests/test_check_situation.py` | **`situation=None` is byte-identical to today under a seeded RNG**; each of the four factors fires on its condition and only for its skills; at most one factor ever returned; ordering (filthy/hooded beat uniform); `combine_modifier` truth table (none/same/opposite); tool result carries `factor` + `because` and **never** the words "advantage"/"disadvantage"/a number; `about_npc_id` writes the memo only for `stealth` |

**Extended files**

| File | Added |
|---|---|
| `tests/test_turn_audit.py` | "you are waved through" / "you are let in" fire `arrival_without_move`; ordinary prose ("you are waved at from across the square") stays clean |
| `tests/test_pending_actions.py` | `equip_note(reads_as=…)` appends the reading; omitted → note byte-identical to today |
| `tests/test_playtest_seed.py` | the Drowned Lantern carries a normalized `dress_code`; the three garments exist with valid `attire`; Brann's coat's `uniform_faction_id` resolves to the Lantern Watch |
| `tests/test_schema_overhaul.py` | `locations.dress_code` exists and defaults NULL; `Item(attire=…)` round-trips through `to_dict`/`from_dict` |
| `tests/test_integration_overhaul.py` | `test_uniform_changes_stranger_reading` — equip the Watch coat → `state.appearance.read_as` changes, `_build_context` emits the U line, `npc_recognition_block` for an unmet NPC says "treat them as Lantern Watch"; `test_hood_blocks_the_lantern` — equip the hood → `/api/travel` to the tavern 409s with the house's reason, `move_player(requirements_met=True)` gets in |

**Must pass unmodified** (the proof that a NULL-everything world plays exactly as today): `test_travel_gate.py`, `test_canvas_travel.py`, `test_roll_check.py`, `test_equipment.py`, `test_inventory.py`, `test_dm_prompt_tools.py`, `test_playtest_seed.py`'s existing assertions.

**Budget test:** `test_perception.py::test_appearance_lines_query_budget` — `appearance_lines` on a seeded DB issues ≤ 4 queries and runs < 20 ms (it sits on the per-turn hot path).

---

## 12. Manual pass (end to end, playtest world)

1. Fresh character, nothing equipped → character panel reads **"reads as: an ordinary townsperson"**; the Drowned Lantern door is open.
2. Buy the **Ashfall Hood** from Vex, equip from the inventory panel → the panel line changes; the `[SINCE YOUR LAST TURN]` block tells the DM the new reading.
3. Walk to the tavern door on the canvas → it is **locked**; the click routes `"I try to get through to The Drowned Lantern."` into chat; the DM narrates Maera's rule (it has her reason, verbatim, in its context).
4. Argue with the DM. If it lets you in, verify `move_player` actually succeeded (location changed in the HUD) — if it narrated entry without the call, `arrival_without_move` fires in the audit warnings on the `complete` event.
5. With the hood on, talk to **Maera** (an acquaintance) → the NPC agent must not use the player's name. Verify the DM was handed the `roll_check(..., "stealth", "tricky", about_npc_id=…)` line, calls it, and does not call it again later in the same scene.
6. Buy/receive the **Lantern Watch Coat**, equip it, stand in Market Square with Brann present → the DM context carries the U risk line; a stranger NPC's recognition block says "treat them as Lantern Watch"; Brann's block says he knows exactly who this is and may have an opinion about the coat.
7. Attempt a persuasion in Watch colours on a Watch-controlled scene → `roll_check` result carries `situation.factor == "wearing_their_colours"`; narration reflects it without printing the word.
8. Equip the **Mucker's Oilskin**, try the tavern → blocked on `filthy`; try a persuasion in a "wealthy"-tagged room → `filthy_in_fine_company` disadvantage.
9. `git stash` the world: boot `data/emberfall.db` (a legacy DB) → every appearance is `common`, no door is dress-locked, `roll_check` results carry no `situation` key.

---

## 13. V1 vs later

**In v1 (everything above).** Notably: no new DM tool, no new SSE event, no new audit check, no `game.js` change, no LLM call anywhere in the feature.

**Deliberately deferred**

| Deferred | Why it waits |
|---|---|
| **NPCs having attire the player reads** (a beggar reads as filthy *to you*) | Doubles the surface for a payoff the DM's prose already delivers; the player has no HUD for it. |
| **Disguise as durable world state** ("someone in Watch colours was seen at the docks") | Needs an events/rumour design of its own; the memo is process-local on purpose. |
| **An impersonation crime ledger** (repeat offences escalating automatically) | Code cannot judge "with standing to know"; the DM's `create_event` + `update_player_reputation` is the honest v1. |
| **A real Watchman spotting a fake coat by uniform detail** | Would need a per-faction counterfeit-quality axis on `attire`; the DM already has `roll_check(perception)` for exactly this. |
| **Attire affecting prices** | Merchant pricing is `TIER_PRICE`, code-owned and deliberately flat; a haggling multiplier belongs to the deferred sell-back economy. |
| **Style decay** (clothes getting filthy over time / after a fight) | Needs a per-item condition field and a tick; the fiction currently handles it, and the DM can hand over a filthy coat. |
| **Cross-session recognition persistence** | A DB column plus a "who clears it" policy; the day-scoped memo covers the actual scene problem. |
| **Health in the reading** ("and badly hurt") | Real, cheap, and tempting — but it belongs with a broader "visible condition" pass, not inside attire. |

---

## 14. CLAUDE.md delta (one bullet, under *Mechanical consequences*)

```
- **Attire semantics** (`src/services/perception.py`): what the player WEARS
  decides how strangers read them, separately from who they are. The equipped
  armor's `attire` label set (`style` from a five-word menu, optional
  `uniform_faction_id`, `concealing`) produces a code-owned perception profile
  (`state.appearance`, a `Player reads as:` context line, a character-panel
  row). Strangers react to the clothes — a faction uniform makes them treat
  the wearer as that faction regardless of identity — while NPCs with an
  `NPCRelationship` row know the player on sight; a concealed face makes an
  acquaintance's recognition *contested*, resolved by a DM-called `roll_check`
  (never rolled automatically) whose answer sticks for the game-day.
  `Location.dress_code` (`min_style` / `banned_styles` incl. `concealing` /
  `reason`) is enforced inside `travel.check_route`, so it blocks canvas doors,
  `/api/travel`, and `move_player` through one path, overridable only with
  `requirements_met=True`. `roll_check` also layers ONE code-computed
  situational factor (night/weather stealth, filthy-in-fine-company,
  hooded-and-watched, wearing-their-colours) over the DM's modifier and reports
  it back as a phrase, never a number.
```
