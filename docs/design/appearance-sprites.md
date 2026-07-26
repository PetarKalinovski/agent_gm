# Visible Equipment — Appearance-Keyed Sprite Regeneration

**Status:** implementation-ready design. Extends `00-integration.md`; where that
document rules, it wins. Nothing here contradicts it.
**User requirement (verbatim intent):** *"I want the new weapons and armor to show
up. Weapons when attacking, and armor all of the time. Like equipped stuff should
show."*

**One-line summary:** the player's sprite set is keyed by a deterministic
**appearance signature** derived from equipped gear; sprites live in
signature-scoped directories; the sprite endpoint resolves the signature
server-side so `game.js` keeps asking for `id + direction`; a signature that has
no art yet generates in the background exactly like every other asset in this
repo, and the player keeps wearing the old set until the new one is **completely**
on disk.

**Headline properties**

| Property | How |
|---|---|
| Zero DB migration | No new columns, no new tables. The only schema delta is the two **shared pydantic `Item` fields** frozen by the brief. Legacy worlds resolve to signature `base` and play byte-identically. |
| Zero new LLM round-trips on any fast path | The DM turn is untouched. `state.appearance` is one sha1 over ≤200 chars of already-loaded row data. All image work is `spawn_generation` fire-and-forget, 404-triggered, or scene-load-triggered. |
| Never a naked frame | Texture swaps are **atomic per axis** and only happen when the *whole* set for that signature exists on disk. Generation failure = stay on the current set, forever if need be. |
| Style consistency | Variant idles are generated **from the base idle** (face/body identity anchor); walk/dodge/attack strips are generated **from the variant idle**, one strip per call — the outfit rides in through the *reference image*, not through prose. |
| Bounded cost | Armor and weapon are **orthogonal axes** (A + W sets, never A × W for the expensive half). One in-flight variant body set process-wide. Per-player LRU cap. Kill switch in config. |

---

## 0. Scope

**In v1:** the *player* sprite set reflects equipped **armor** (idles, walk cycles,
dodge roll) and equipped **weapon** (attack cycle only).

**Out of v1, recorded in §13:** NPC gear visibility, trinket visibility,
sheathed weapons in idle/walk, faction-uniform recolouring, portrait
regeneration.

---

## 1. Rulings

These are decisions, not options. Where a sibling spec (the `Item.visual_desc` /
`Item.attire` seam owner) touches the same code, the boundary is stated.

| # | Ruling |
|---|---|
| **A1** | **Two orthogonal axes, never a product for the expensive half.** `body_sig` (from equipped **armor**) owns 4 idles + 48 walk frames + 6 dodge frames. `weapon_sig` (from equipped **weapon**) contributes only to the attack cycle. Growth is `A + W`, not `A × W`, for the 58-file half. |
| **A2** | **The attack cycle is keyed on the PAIR.** `action_sig = f"{body_sig}-{weapon_sig}"`. An attack strip is generated *from the variant right idle* (so the armour is right) *with the weapon named in the prompt* (so the sword is right). One API call per realized pair; a player realizes ~one pair per gear change, never the full matrix. |
| **A3** | **Dodge belongs to the BODY axis.** A dodge roll shows the outfit, not the weapon. `ensure_action_sprites` is split accordingly (§7.3). |
| **A4** | **`base` is the legacy signature.** `body_sig == "base"` and `action_sig == "base-unarmed"` resolve to today's flat `sprites/{id}_*.png` paths. Existing files keep working with zero migration and zero rename. |
| **A5** | **The signature is computed in a NEW module, `src/services/appearance.py`, not in `progression.combat_stats`.** `state.combat` is a frozen merged interface (00-integration R3) consumed by the combat loop; cosmetics do not belong in it. `appearance_payload(player)` is called next to `combat_stats(p)` in `build_player_state` and obeys the same contract: **pure dict math over an already-loaded row, never a query, never a disk touch.** |
| **A6** | **`state.appearance` carries WANTED signatures only — never readiness.** Readiness requires ~58 `stat()` calls; `build_player_state` runs after every world-write tool. Readiness lives exclusively on the two endpoints that already do disk work (`/api/assets/appearance/{player_id}`, `/api/assets/location/{id}`), memoized per `(character_id, sig)`. |
| **A7** | **`?a=<sig>` is STRICT; its absence is LENIENT.** With `?a=`, the endpoint serves exactly that signature's file or 404s (and spawns generation). Without it, the endpoint resolves the player's *serving* signature (the wanted one if complete, else `base`). This kills the "did I get the variant or the fallback?" ambiguity and makes `?a=` a real cache-buster. |
| **A8** | **The outfit enters the walk/dodge/attack prompts through the REFERENCE IMAGE, not through text.** The existing walk prompt already says *"clothing, colors, face and proportions matching the reference image EXACTLY (do not redesign or simplify the outfit)"*. Point it at the variant idle and it is already correct. `_WALK_CHOREOGRAPHY` and the walk-strip prompt are **not edited**. Only two prompts change: the new outfit-variant idle prompt (§6.1) and one sentence of the action-cycle prompt (§6.3). Minimum churn on machinery that currently works. |
| **A9** | **Variant generation never writes to the database.** `Player.sprite_base_path` keeps pointing at the **base** front idle — it is the identity anchor every variant is generated from. Variants are disk-only artefacts. |
| **A10** | **`attire` never enters a sprite prompt or a signature.** `Item.attire` (`style` / `uniform_faction_id` / `concealing`) is *social* metadata owned by the sibling spec. Anything visually load-bearing about a hooded cloak is already in its `visual_desc`. Asserted by `test_signature_ignores_attire_and_trinket` — this is the seam guard that keeps the two systems from coupling. |
| **A11** | **This spec makes NO change to `src/web/turn_audit.py` or `src/web/pending_actions.py`.** `equip_note` already tells the DM *"They now wear the Harbor Mail."*; appearance is a rendering consequence of a ledger entry that already exists. If the ledger line grows a look-and-feel clause, that is the sibling spec's edit, in the sibling spec's chunk. |
| **A12** | **Cached per signature, LRU-capped per player.** A signature's art is never regenerated once complete. Past `appearance.max_variants_per_player` (default 8), the least-recently-used variant directory *for that player* is deleted before the new one generates. `base` and the currently-serving signature are never evicted. |

---

## 2. The signature scheme

### 2.1 Module: `src/services/appearance.py` (new)

```python
"""Appearance signatures: which sprite set the player's gear entitles them to.

Pure functions over an already-loaded Player row. No queries, no disk, no
network — this runs inside `build_player_state`, which runs after every
world-write tool.

Two axes, deliberately orthogonal (00-integration extension, ruling A1):
  body   <- equipped armor  -> idles, walk cycles, dodge cycle   (58 files)
  weapon <- equipped weapon -> attack cycle only                  (6 files)
The attack cycle is keyed on the PAIR (A2) because it must show both.
"""

import hashlib
from typing import Any

from src.services import progression

# Bump to invalidate EVERY generated variant (prompt machinery changed).
# Folded into the hash input, so old directories simply stop being addressed.
APPEARANCE_VERSION = 1

BASE_BODY_SIG = "base"
BASE_WEAPON_SIG = "unarmed"
BASE_ACTION_SIG = f"{BASE_BODY_SIG}-{BASE_WEAPON_SIG}"

# Fallback nouns when an item carries neither visual_desc nor a usable name
_SLOT_NOUN = {"weapon": "a plain weapon", "armor": "plain travelling clothes"}
```

```python
def visual_phrase(item: dict | None, slot: str) -> str | None:
    """The concrete phrase that goes into an image prompt for this item.

    Fallback chain (frozen seam): visual_desc -> name -> slot noun.
    Trimmed and clamped to 180 chars — a paragraph in an image prompt buys
    nothing and destabilises the strip.
    """
```

```python
def _axis_sig(prefix: str, item: dict | None, slot: str, empty: str) -> str:
    """'base'/'unarmed' when the slot is empty, else prefix + 8 hex.

    Hash input is EXACTLY what reaches the prompt plus the item identity:
        f"{APPEARANCE_VERSION}|{item_id}|{visual_phrase.lower()}"
    so re-authoring an item's visual_desc regenerates its art, two different
    items never share a directory, and bumping APPEARANCE_VERSION invalidates
    the world.
    """
    if not item:
        return empty
    raw = "|".join((
        str(APPEARANCE_VERSION),
        str(progression.item_key(item)),
        (visual_phrase(item, slot) or "").strip().lower(),
    ))
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def body_signature(player) -> str:      # 'base' | 'a3f9c1b2'
def weapon_signature(player) -> str:    # 'unarmed' | 'w77bb0111'
def action_signature(player) -> str:    # f"{body}-{weapon}"

def appearance_payload(player) -> dict[str, Any]:
    """What rides on `state.appearance`. WANTED signatures only (A6)."""
    body = body_signature(player)
    weapon = weapon_signature(player)
    return {
        "body_sig": body,
        "weapon_sig": weapon,
        "action_sig": f"{body}-{weapon}",
        "version": APPEARANCE_VERSION,
    }
```

Both axis functions read the loadout through `progression.resolve_loadout(p)` —
already pure, already handles NULL `equipment`, already drops pointers to items
above the gear-tier cap and to items no longer in the bag. **Legacy repair comes
free:** an old world's `{"type": "weapon", "name": "Iron Sword"}` is repaired by
`progression.mechanize_item` into `slot=weapon, tier=common`, so it equips, so it
gets a `weapon_sig`, so it shows up in the attack cycle. Nothing to backfill.

### 2.2 Worked examples

| Loadout | body_sig | weapon_sig | action_sig |
|---|---|---|---|
| nothing equipped (legacy row, `equipment=NULL`) | `base` | `unarmed` | `base-unarmed` |
| notched iron shortsword only | `base` | `w9c14ab02` | `base-w9c14ab02` |
| boiled-leather Watch coat only | `a3f9c1b2` | `unarmed` | `a3f9c1b2-unarmed` |
| both | `a3f9c1b2` | `w9c14ab02` | `a3f9c1b2-w9c14ab02` |
| coat + a *different* sword | `a3f9c1b2` (unchanged — 58 files reused) | `w51e0dd7` | `a3f9c1b2-w51e0dd7` (1 new call) |
| trinket swapped, `attire.style` edited | `a3f9c1b2` (unchanged) | unchanged | unchanged |

The fifth row is the point of A1: changing weapons costs **one** image call.

---

## 3. Schema delta

### 3.1 Shared seam (build EXACTLY ONCE — coordinate with the sibling spec)

`src/models/item.py`, pydantic only, no DB:

```python
    # --- Appearance (optional; legacy items have neither) ---
    visual_desc: str | None = Field(
        None,
        description="Short concrete visual phrase for image prompts, e.g. "
                    "'a boiled-leather Watch coat with the brass lantern badge'",
    )
    attire: dict | None = Field(
        None,
        description="ARMOR ONLY: {'style': noble|fine|common|rough|filthy, "
                    "'uniform_faction_id': str|null, 'concealing': bool}",
    )
```

Authoring surface (the `visual_desc` half — the `attire` half is the sibling's):
`create_item_template`, `add_to_inventory`, `stock_merchant` each gain a
`visual_desc: str | None = None` parameter with this docstring line:

> `visual_desc`: One short concrete phrase describing what this looks like
> when worn or held — *"a notched iron shortsword"*, *"a boiled-leather Watch
> coat with the brass lantern badge"*. The engine draws it. Describe the
> object, not the wearer; no sentences, no stats, no adjectives about how it
> feels to use.

`normalize_objects` (scene objects) **strips `visual_desc` and `attire`** exactly
as it already strips `slot`/`tier`/`effect_tag` — scene loot is plain stuff
(00-integration §1.3).

### 3.2 Everything else

**No DB delta.** No new column, no new table, no `_migrate_missing_columns`
entry. A world booted from `data/emberfall.db` with `equipment=NULL` produces
`{"body_sig": "base", "weapon_sig": "unarmed", ...}` and is served the exact
files it is served today.

### 3.3 Config

`src/config.py`:

```python
class AppearanceConfig(BaseModel):
    """Visible-equipment sprite variants."""
    enabled: bool = True                 # kill switch: everyone renders as `base`
    max_variants_per_player: int = 8      # LRU cap on variant body directories
    body_concurrency: int = 1             # simultaneous variant BODY sets, process-wide
    action_concurrency: int = 2           # simultaneous variant ATTACK strips
```

added to `Settings` as `appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)`,
plus the mirrored block in `config/settings.yaml`.

When `enabled` is false: `appearance_payload` returns the base signatures
regardless of gear, and nothing else in the pipeline ever fires. One switch,
one place.

---

## 4. File and cache scheme

### 4.1 Layout

```
data/assets/{world}/sprites/
    {id}_front.png  {id}_back.png  {id}_left.png  {id}_right.png       <- base idles
    {id}_{dir}_walk1..12.png                                            <- base walks
    {id}_dodge1..6.png                                                  <- base dodge (body axis)
    {id}_attack1..6.png                                                 <- base attack (action axis)
    variants/
        a3f9c1b2/                       <- one BODY signature
            {id}_front.png … {id}_right.png
            {id}_{dir}_walk1..12.png
            {id}_dodge1..6.png
        a3f9c1b2-w9c14ab02/             <- one ACTION signature (the pair)
            {id}_attack1..6.png
```

Directories are keyed by signature, **not** by player — files inside carry the
character id, so two players wearing the same coat share a directory and never
share pixels.

### 4.2 The one path resolver

`src/services/asset_manager.py`:

```python
    def sprite_dir(self, sig: str) -> Path:
        """Directory holding one signature's frames.

        'base' and 'base-unarmed' resolve to the FLAT legacy sprites dir — that
        is the entire migration story (A4).
        """
        if sig in (appearance.BASE_BODY_SIG, appearance.BASE_ACTION_SIG):
            return self.assets_dir / "sprites"
        return self.assets_dir / "sprites" / "variants" / sig
```

`ImageGenerator` gets the mirror helper so generated frames land in the right
place without every call site building paths:

```python
    def sprite_rel(self, sig: str, filename: str) -> str:
        """Relative path for `_save_image`: 'sprites/x.png' or
        'sprites/variants/<sig>/x.png'."""
```

### 4.3 Set membership and completeness

| Set | Signature axis | Files | API calls to build |
|---|---|---|---|
| **body** | `body_sig` | 4 idles + 4×12 walk + 6 dodge = **58** | **7** (front, back, right idles; 3 walk strips; 1 dodge strip). Left idle and left walk are mirrors — free. |
| **action** | `action_sig` | 6 attack frames | **1** |

```python
    def body_set_complete(self, character_id: str, sig: str) -> bool
    def action_set_complete(self, character_id: str, sig: str) -> bool
```

Both memoized in `self._complete: set[tuple[str, str, str]]` — completeness is
monotonic (files only appear), the memo entry is dropped only by LRU eviction.
Cost after the first check: a set lookup.

```python
    def serving_body_sig(self, character_id: str, wanted: str) -> str:
        """`wanted` if its 58 files are all there, else 'base'.

        'base' is returned WITHOUT a completeness check: the flat path is the
        legacy self-healing one — an individual missing file 404s and spawns
        exactly as it does today.
        """

    def serving_action_sig(self, character_id: str, wanted: str) -> str
```

**Readiness reported to the client** (`body_ready`) is `True` whenever
`wanted == "base"` (nothing to wait for) or `body_set_complete(id, wanted)`.
Same shape for `action_ready`.

---

## 5. Server surface

### 5.1 `state.appearance` — new key on `build_player_state`

`src/web/server.py`, inside the existing `with get_session() as db:` block that
already computes `progress` / `equipment` / `combat`:

```python
        appearance = appearance_mod.appearance_payload(p) if p else dict(_NO_PLAYER_APPEARANCE)
```

and in the returned dict, immediately after `"combat": combat,`:

```python
        "appearance": appearance,
```

with the module-level constant next to `_NO_PLAYER_EQUIPMENT`:

```python
_NO_PLAYER_APPEARANCE = {
    "body_sig": "base", "weapon_sig": "unarmed",
    "action_sig": "base-unarmed", "version": appearance_mod.APPEARANCE_VERSION,
}
```

Because `/api/travel`, `/api/interact`, `/api/player/{id}/equip|unequip|
use-consumable|quick-slot` and every SSE `state` event all funnel through
`build_player_state`, the appearance signature reaches the client on every path
for free.

### 5.2 `GET /api/assets/sprite/{character_type}/{character_id}/{direction}` — changed

Signature: `async def get_sprite(character_type: str, character_id: str, direction: str, a: str | None = None)`.

```
resolve which set this frame belongs to:
    ^attack\d+$                          -> ACTION axis
    ^dodge\d+$                           -> BODY axis
    ^(front|back|left|right)(_walk\d+)?$  -> BODY axis
    anything else                        -> BODY axis (unchanged legacy behaviour)

npc  : ignore `a` entirely — NPCs are always 'base' in v1 (§13).
player:
    if a is not None:                    # STRICT (A7)
        sig = a
    else:                                # LENIENT
        sig = serving_<axis>_sig(id, wanted_<axis>_sig_from_db(id))

path = sprite_dir(sig) / f"{character_id}_{direction}.png"
if path exists and non-empty -> FileResponse
else:
    spawn_generation(f"{axis}:{character_id}:{sig}", …)   # dedup key gains the sig
    return 404 {"status": "generating", "sig": sig}
```

`wanted_body_sig_from_db(player_id)` is one indexed `Player` read; it happens
only on the **lenient** path, which the client uses only during the initial
load of a scene (§9.2). Steady-state rendering always passes `?a=`, so the
hot path is a `Path.exists()` and a `FileResponse` — identical to today.

**Dedup keys** (`spawn_generation`) change shape everywhere, once:

| Today | New |
|---|---|
| `sprites:{id}` | `body:{id}:{sig}` |
| `actions:{id}` | `action:{id}:{sig}` |

One in-flight task per `(character, signature, axis)`. Call sites:
`get_sprite`, `get_location_assets_fast`, and the new equip pre-warm.

### 5.3 `GET /api/assets/appearance/{player_id}` — NEW

The readiness poll. One request answers both axes.

```json
{
  "player_id": "pc_1",
  "body_sig": "a3f9c1b2",
  "weapon_sig": "w9c14ab02",
  "action_sig": "a3f9c1b2-w9c14ab02",
  "version": 1,
  "body_ready": false,
  "action_ready": true,
  "serving_body": "base",
  "serving_action": "a3f9c1b2-w9c14ab02",
  "generating": true,
  "failed": false
}
```

- `generating` — `asset_manager.is_generating` on either axis key.
- `failed` — `_recently_failed` on either key (the existing 300 s cooldown).
  The client stops polling immediately on `failed && !generating` instead of
  burning its retry budget.
- Requesting this endpoint **also spawns** any missing set (same dedup keys), so
  a client that reconnects mid-generation restarts a task that a server restart
  dropped. Idempotent by construction.

Handler lives beside `get_sprite` in `server.py`; the disk work lives in
`AssetManager.appearance_status(player_id) -> dict`.

### 5.4 `GET /api/assets/location/{location_id}` — one new key

The response gains a top-level `"appearance"` block with the **same shape as
§5.3**. `get_location_assets_fast` already loads the player row and already
does disk checks, so this costs one dict build.

Why: it removes an extra round-trip on scene load. `game.js` learns *what to
load right now* (`serving_body`/`serving_action`, guaranteed on disk) and *what
to aim for* (`body_sig`/`weapon_sig`) in the fetch it already makes.

`get_location_assets_fast` also changes its player-sprite pending check from

```python
        if not self._check_all_sprites_exist(player_id, include_walk=True):
            spawn_generation(f"sprites:{player_id}", …)
```

to the signature-aware form:

```python
        wanted = appearance.body_signature(player_row)
        if not self.body_set_complete(player_id, wanted):
            if self.spawn_generation(f"body:{player_id}:{wanted}",
                                     lambda: self.ensure_body_set(player_id, wanted)):
                pending = True
```

`pending` therefore already drives the existing 6-second scene re-poll — a
player who reloads mid-generation gets the variant when it lands, with no new
client code. (The in-session swap still uses §5.3, which is finer-grained and
does not rebuild the scene.)

### 5.5 `POST /api/player/{id}/equip` and `/unequip` — pre-warm

After `db.commit()` and after the existing `pending_actions.record_action(...)`,
before the return:

```python
    _prewarm_appearance(player_id)      # fire-and-forget, returns immediately
```

```python
def _prewarm_appearance(player_id: str) -> None:
    """Start background generation for the player's new look.

    Adds no latency: two `spawn_generation` calls, both deduplicated, both
    returning immediately. Every other path (DM auto-equip, quest rewards,
    campaign ops) self-heals through the 404 trigger — this is a head start,
    not a requirement.
    """
```

No other endpoint and **no tool** changes. `add_to_inventory`'s `auto_equip`
path deliberately does not pre-warm: tools have no asset-manager handle and no
event loop guarantee, and the state event that follows the tool carries the new
signature, which makes the client request it, which 404s, which spawns the
generation. One mechanism, arrived at from two directions.

---

## 6. Prompt construction

Only **two** prompts change. Both are extracted into pure builder methods so
they can be asserted in tests without an API call.

### 6.1 NEW — `_build_outfit_idle_prompt` (the only genuinely new prompt)

Owner: `src/services/image_generator.py`.

```python
    def _build_outfit_idle_prompt(
        self, character, world_bible, direction: str, outfit_desc: str
    ) -> str:
```

```
The attached image shows the character "{name}" from a 2D RPG. That is exactly
who this is: same face, same hair, same skin tone, same build, same age, same
art style.

Draw the SAME character, now WEARING: {outfit_desc}

The clothing is the ONLY thing that changes. Ignore any clothing described
elsewhere — the garment above replaces what they are wearing in the reference.
Do not redesign the face. Do not change their proportions or their height. Do
not add a weapon, a prop, a backdrop, or a companion.

Pose: standing idle, {view_desc}
- Full body visible from head to feet
- SAME scale and framing as the reference image: the character fills the same
  share of the canvas, feet at the same height
- Solid bright green background (#00FF00) for chroma key removal
- ONLY ONE CHARACTER, ONE POSE. No sprite sheet, no alternate views, no
  turnaround, no duplicates
- No shadows on the ground, no text, no labels, clean edges for cutting out
- Style: {visual_style}
```

`{view_desc}` reuses the existing `direction_map`.
`{outfit_desc}` is `appearance.visual_phrase(armor_item, "armor")`.

**Reference-image chain (the style-consistency rule this repo learned the hard
way):**

```
base front idle  --(outfit prompt)-->  variant front idle
variant front idle --(outfit prompt)-->  variant back idle
variant front idle --(outfit prompt)-->  variant right idle
variant right idle --mirror-->           variant left idle
```

The variant front idle — not the base one — is the reference for back and
right. Generating `back` from the *base* front would hand the model the old
outfit as ground truth and it would faithfully redraw it. Chaining through the
variant front is the only way the garment propagates around the character.

### 6.2 UNCHANGED — walk and dodge strips (A8)

`generate_walk_cycle` and `generate_action_cycle("dodge", …)` are called with
the **variant** idle as `reference_image` and are otherwise untouched. Their
existing text already carries the contract:

> *Every frame: the SAME character design — clothing, colors, face and
> proportions matching the reference image EXACTLY (do not redesign or simplify
> the outfit)…*

Point that sentence at a coat and it draws a coat, in all 6 keyframes of one
strip, in one call — which is precisely the property (frames generated together
stay consistent) that makes this whole design affordable. `_WALK_CHOREOGRAPHY`
is not edited. The RIFE interpolation, artifact-immune slicing, ghost/sliver
repair, and feet-centroid normalization all apply unchanged.

### 6.3 CHANGED — one sentence of the action-cycle prompt

`generate_action_cycle` gains a keyword argument:

```python
    async def generate_action_cycle(
        self, character, world_bible, action: str, reference_image: bytes,
        *, weapon_desc: str | None = None, sig: str = "base",
    ) -> dict[str, str]:
```

Today's prompt contains:

```
If the character carries a weapon in the reference, they use that weapon;
otherwise they fight bare-handed.
```

That line is replaced by `{weapon_line}`, built by a pure helper
`_weapon_line(weapon_desc)`:

**Armed** (`weapon_desc` given):

```
The character is striking with {weapon_desc}. The weapon is DRAWN AND IN HAND in
every single frame — cocked back at the hip in frames 1-2, sweeping through the
arc in frames 3-4, settling back to guard in frames 5-6. Draw the same weapon at
the same size, shape and colour in every frame; it never disappears, never
changes into a different weapon, and is never sheathed.
```

**Unarmed** (`weapon_desc` is None):

```
The character fights BARE-HANDED. No weapon in any frame — fists, forearms and
elbows only.
```

The dodge cycle passes `weapon_desc=None` always (A3) and keeps today's exact
string, so its output is unchanged for existing characters.

Everything else about `generate_action_cycle` is untouched: 6 keys, 21:9 strip,
one call, no RIFE, nearest-neighbour repair, one regeneration on an unusable
strip.

### 6.4 Prompt-side risks, handled

| Risk | Handling |
|---|---|
| Model drifts the face when changing the outfit | Reference chain (§6.1) + three explicit "do not redesign the face / proportions / height" clauses. Idles are one character per call, the format the model is most reliable at. |
| Weapon flickers between attack frames | The strip is one call (frames drawn together) + the "same weapon, same size, shape and colour, never disappears" clause + existing broken-frame repair + one automatic regeneration. |
| The reference idle already shows a weapon and the player switched weapons | Variant idles never contain a weapon (`Do not add a weapon` in §6.1) and base idles are authored from `Player.description`. If a base idle happens to show one, the armed `{weapon_line}` names the correct weapon explicitly and wins; this is a cosmetic edge, not a correctness one. |
| `visual_desc` is a paragraph of purple prose | `visual_phrase` clamps to 180 chars; the tool docstring says "one short concrete phrase". |
| `visual_desc` is missing (legacy item) | Falls back to the item **name** ("Iron Sword" → *striking with Iron Sword*), then the slot noun. Always draws something plausible; never blocks. |

---

## 7. Generation pipeline

### 7.1 `ImageGenerator` — new/changed methods

```python
    async def generate_outfit_variant_idle(
        self, character, world_bible, direction: str,
        reference_image: bytes, outfit_desc: str, sig: str,
    ) -> str:
        """One variant idle. Same cutout + `_cutout_failed` + one-retry logic as
        `generate_character_sprite_with_reference`; saves to
        sprites/variants/{sig}/{id}_{direction}.png."""
```

Changed, each gaining `sig: str = "base"` used only to pick the output
directory via `sprite_rel` (default preserves today's behaviour byte-for-byte):

- `generate_walk_cycle(..., sig="base")`
- `generate_action_cycle(..., weapon_desc=None, sig="base")`
- `mirror_walk_cycle(character_id, source, target, sig="base")`
- `upgrade_walk_cycle(character_id, direction, sig="base")`

New pure builders (test surface): `_build_outfit_idle_prompt`, `_weapon_line`.

### 7.2 `AssetManager` — the orchestration

```python
    async def ensure_body_set(self, player_id: str, sig: str) -> dict[str, str]:
        """Generate the 58-file body set for one signature. 7 API calls.

        sig == 'base' delegates to today's `ensure_all_sprites_generated` +
        the base dodge strip — one code path, no special-casing downstream.

        Ordering (the base front idle is the identity anchor, A9):
          0. If the BASE set is missing, build it first — a variant has nothing
             to be consistent WITH otherwise.
          1. LRU-evict if this player is at `max_variants_per_player` (A12).
          2. variant front idle   <- base front idle
          3. variant back, right  <- variant front idle   (asyncio.gather)
          4. variant left         <- mirror(variant right)
          5. walk strips front/back/right <- matching variant idles (gather)
          6. variant left walk    <- mirror(variant right walk)
          7. dodge strip          <- variant right idle
        Held behind `self._body_gate` (asyncio.Semaphore, config
        `appearance.body_concurrency`, default 1) so a player fiddling with the
        character panel cannot fire five sets at once. The BASE set is NOT
        gated — first-run experience never queues behind a cosmetic variant.
        """

    async def ensure_action_set(self, player_id: str, sig: str) -> dict[str, str]:
        """Generate the 6-frame attack strip for one action signature. 1 call.

        Reference = the RIGHT idle of the pair's body signature (falling back to
        that body's front idle, then base right, then base front — the same
        ladder `ensure_action_sprites` uses today). Weapon phrase comes from the
        equipped weapon's `visual_desc`. Behind `self._action_gate` (default 2).
        """

    def appearance_status(self, player_id: str) -> dict[str, Any]:
        """The §5.3 payload. The only place readiness touches disk."""

    def _evict_lru_variants(self, player_id: str, keep: set[str]) -> None:
        """Delete this player's least-recently-used variant directories down to
        the cap. Never evicts `base`, never evicts anything in `keep` (the
        currently-wanted and currently-serving signatures). Drops the matching
        completeness memo entries."""
```

### 7.3 `ensure_action_sprites` split (A3)

Today one function generates `("attack", "dodge")` for players. It becomes:

```python
    async def ensure_attack_sprites(self, character_id, character_type, sig="base")
        # attack only — players AND npcs; npcs are always sig='base' in v1
```

and the dodge strip moves into `ensure_body_set` step 7. Backward compatibility:
existing `{id}_dodge{n}.png` files at base are untouched and still served;
`body_set_complete("base")` is never consulted (A4/§4.3 — base is served
unconditionally), so a world with idles+walks but no dodge behaves exactly as
today, with the dodge 404 spawning `body:{id}:base`.

The 404 router in `get_sprite` (§5.2) is what makes this split invisible: the
frame name decides the axis, so `dodge3.png` asks for a body-set build and
`attack3.png` asks for an action-set build. No client knowledge required.

### 7.4 Cost accounting

| Event | API image calls | Wall clock (background) |
|---|---|---|
| First-ever player (unchanged) | 7 (+1 attack lazily) | ~2–3 min |
| Equip first armor | 7 + 1 attack | ~2–3 min |
| Swap weapon (any armor) | **1** | ~20–40 s |
| Swap back to previously worn armor | **0** (cached forever, A12) | instant |
| Unequip everything | **0** (`base` is already on disk) | instant |

A campaign in which the player owns 3 armors and 4 weapons and wears every
combination costs `3×7 + 12×1 = 33` calls *spread across the whole campaign*,
against `3×4×7 = 84` if the axes were coupled. That is A1 paying for itself.

---

## 8. Lifecycle

### 8.1 The state machine (per axis, per player)

```
        equip / unequip / reward / 404 on a variant frame
                        |
                        v
    [WANTED != SERVING] --spawn_generation(dedup)--> [GENERATING]
                        |                                 |
                        |                    success      |     failure
                        |                        |        |        |
                        |                        v        |        v
                        +----------------> [COMPLETE]     |   [COOLDOWN 300s]
                                                 |        |        |
                        client poll sees ready --+        |        |
                                                 v        |        v
                                         [SWAPPED, atomic]|  stay on SERVING
                                                          |  (client stops polling)
```

**Invariants**

1. The client renders a set that is **completely on disk** — always. There is no
   state in which some frames are the coat and some are the shirt.
2. A generation failure is *silent to the player*: they keep their current look.
   It is logged, and it is visible in devtools via `failed: true`.
3. The wanted signature changing mid-generation does not cancel anything (the
   in-flight set may still be worn later and is cached forever); it just starts a
   second, differently-keyed task, serialized by the semaphore.
4. Nothing in this pipeline can block, slow, or fail a DM turn. Every entry point
   is `spawn_generation`, which returns a bool immediately.

### 8.2 Server restart

In-flight tasks die with the process (as they do today for every asset). The
next `/api/assets/location` load or `/api/assets/appearance` poll re-spawns them,
because both endpoints spawn on a missing set. Half-written directories are
harmless: `body_set_complete` is all-or-nothing, so a partial directory reads as
incomplete and is regenerated (the completed frames inside it are re-derived,
not re-billed — `generate_all_sprites_for_character` already skips existing
files, and `ensure_body_set` uses the same "if it exists, reuse it" checks per
frame).

---

## 9. Frontend

Three files, the repo's usual single-owner discipline. `audio.js` is untouched.

### 9.1 `index.html` — two lines plus the cache-bust

In `applyPlayerState(state)`, next to the existing `setTimeOfDay` / `setWeather`
calls:

```js
            // Visible equipment: the engine names the sprite set, the renderer
            // swaps to it when its art is completely on disk
            if (game && game.setAppearance) game.setAppearance(state.appearance);
```

Cache-bust both script tags to one value (00-integration R13):

```html
    <script src="/static/js/game.js?v=20260726a"></script>
    <script src="/static/js/audio.js?v=20260726a"></script>
```

Nothing else. The equip/unequip/inventory buttons already call
`applyPlayerState(data.state)` with the endpoint's response, so a click on
"Equip" starts the whole chain with no new handler.

### 9.2 `game.js` — where textures are loaded today, and what changes

New renderer state, set in the constructor and reset in `loadLocation`:

```js
        this.appearance = {
            wantedBody: 'base', wantedAction: 'base-unarmed',
            loadedBody: null,   loadedAction: null,
            poll: null, pollTries: 0,
        };
```

**(a) `loadLocation`** — store the block the scene fetch now carries:

```js
            this.appearance.wantedBody = (data.appearance || {}).body_sig || 'base';
            this.appearance.wantedAction = (data.appearance || {}).action_sig || 'base-unarmed';
            const servingBody = (data.appearance || {}).serving_body || 'base';
            const servingAction = (data.appearance || {}).serving_action || 'base-unarmed';
```

**(b) `loadPlayer(playerData)`** → `await this.preloadAllSprites(servingBody, servingAction)`
(the serving signatures are guaranteed present on disk — the initial load never
shows a placeholder it could have avoided).

**(c) `preloadAllSprites(bodySig, actionSig)`** — the only structural change is
that every URL gains `?a=`:

```js
    async preloadAllSprites(bodySig = 'base', actionSig = 'base-unarmed') {
        const base = `/api/assets/sprite/player/${this.playerId}`;
        const b = `?a=${encodeURIComponent(bodySig)}`;
        const a = `?a=${encodeURIComponent(actionSig)}`;
        // attack frames -> ${base}/attack${f}.png${a}
        // dodge  frames -> ${base}/dodge${f}.png${b}      <-- body axis (A3)
        // idles         -> ${base}/${dir}.png${b}
        // walks         -> ${base}/${dir}_walk${f}.png${b}
        …
        this.appearance.loadedBody = bodySig;
        this.appearance.loadedAction = actionSig;
    }
```

Frame counts are **unchanged**: `walkFrameCount` stays 12, action strips stay 6,
`_getWalkCycle`, `applyAnimationFrame`, `_startPlayerAction` and the mirroring
rule (`mirror = dir === 'left'`) are all untouched. A signature is a different
URL string, which is a different `PIXI.Assets` cache entry — that is the whole
invalidation mechanism.

`updatePlayerSprite(direction)` and `preloadWalkSprites(direction)` (the
single-direction lazy paths) append `?a=${this.appearance.loadedBody}` so they
can never pull a frame from a different set than the one on screen.

**(d) NEW `setAppearance(block)`** — called from `applyPlayerState`:

```js
    /** The engine's wanted look. Does NOT touch textures — it only decides
     *  whether we need to start watching for a new set to finish. */
    setAppearance(block) {
        if (!block || !this.player) return;
        const body = block.body_sig || 'base';
        const action = block.action_sig || 'base-unarmed';
        if (body === this.appearance.wantedBody && action === this.appearance.wantedAction) return;
        this.appearance.wantedBody = body;
        this.appearance.wantedAction = action;
        this.appearance.pollTries = 0;
        this._startAppearancePoll();
    }
```

**(e) NEW `_startAppearancePoll()`** — `GET /api/assets/appearance/{playerId}`
every 5 s, at most 48 times (~4 min, comfortably past a 7-call set). Stops on:
both axes loaded; `failed && !generating`; the wanted signature changing (a new
poll replaces it); scene teardown. **No polling in steady state** — the timer
only exists between a gear change and the swap.

**(f) NEW `_swapBody(sig)` / `_swapAction(sig)`** — the atomic swap:

```js
    async _swapBody(sig) {
        // Load EVERYTHING into throwaway maps first
        const idles = {}, walks = {}, dodge = {};
        try { …load 4 + 48 + 6 with ?a=sig, Promise.all… }
        catch (e) { return false; }               // keep the old set, retry next tick
        if (this.playerAction) { this._pendingSwap = …; return false; }  // never mid-swing
        this.player.sprites = idles;
        this.player.walkSprites = walks;
        this.player.actionSprites.dodge = dodge;
        this.appearance.loadedBody = sig;
        this.applyAnimationFrame();
        PIXI.Assets.unload(oldUrls);              // bounded texture memory across swaps
        return true;
    }
```

Assignment happens only after **every** texture in the set resolved. A single
failed frame discards the whole batch and leaves the player dressed as they
were. `_swapAction` is the same shape for 6 attack frames.

**Mid-fight rule:** a swap is deferred while `this.playerAction` is animating
(one guard, applied on the next frame after the action ends). Combat stats are
already snapshotted at `_ensureCombat` and are unaffected — appearance is
strictly cosmetic and never touches `this.combat.stats`.

**(g) `_spawnCombatant`** — NPC attack frames keep loading from
`/api/assets/sprite/npc/${npcId}/attack${f}.png` with **no** `?a=`. NPCs are
`base` in v1 and the server ignores the parameter for them (§13).

### 9.3 What the player sees

1. Opens the character panel mid-game, clicks **Equip** on the Harbor Mail.
2. The HUD updates instantly (the endpoint's `state` response) — armour hearts,
   the equipped-item badge, the ledger note for the DM. **Zero added latency.**
3. Their sprite still wears the old coat. Nothing flickers, nothing blanks.
4. ~2 minutes later, between one frame and the next, they are wearing the mail —
   idling, walking in all four directions, and dodge-rolling in it.
5. The next fight, they swing the new sword because the attack strip for that
   pair finished ~40 s after the body set.
6. If generation failed, step 4 never happens and nothing else is different.

---

## 10. Latency, audit, and ledger boundaries

**Fast paths, measured against what they cost today:**

| Path | Added cost |
|---|---|
| DM turn (`/api/play`) | one `appearance_payload` per `build_player_state` call: one `resolve_loadout` (already computed for `combat_stats`) + up to two sha1 hashes over <200 bytes. No query, no disk, no network. |
| `POST /api/player/{id}/equip` | two `spawn_generation` calls (dict lookup + `asyncio.create_task`), returns immediately. |
| `GET /api/assets/sprite/...?a=` (steady state) | zero — one `Path.exists()` on a differently-named file. |
| `GET /api/assets/sprite/...` (no `?a=`, scene load only) | one indexed `Player` read. |
| `GET /api/assets/location/{id}` | one dict build; the completeness checks are memoized after first use. |
| `/api/assets/appearance/{id}` poll | exists only between a gear change and the swap; ≤48 requests, each a memoized set lookup. |
| Canvas frame loop | zero. Frame counts, animation timing, and `applyAnimationFrame` are unchanged. |

**No new LLM round-trip anywhere.** The image model is called only from
background tasks that the player never waits on.

**REALITY AUDIT / pending-actions (A11):** untouched. Appearance is a rendering
consequence of `Player.equipment`, which the equip endpoint already records in
the ledger via `pending_actions.equip_note`. There is no appearance claim a DM
could make that the audit should check, and no appearance mutation the audit
could false-positive on. If the DM says *"the Watch coat sits stiff on your
shoulders"*, that is prose about a fact the ledger already delivered.

---

## 11. Build checklist

Ordered. Steps 1–6 are backend and independently testable; 7–9 are the frontend
wave with the usual one-owner-per-file rule.

**Shared seam (do exactly once — coordinate with the sibling spec)**

1. `src/models/item.py`: add `visual_desc` and `attire` (§3.1 verbatim). Add
   `visual_desc` to `create_item_template`, `add_to_inventory`, `stock_merchant`
   with the docstring line from §3.1. Strip both fields in
   `scene_objects.normalize_objects`. Round-trip test: `Item.from_dict(
   item.to_dict())` preserves both; a legacy dict without them still validates.

**Backend**

2. **`src/services/appearance.py` (new).** `APPEARANCE_VERSION`, the three
   base-signature constants, `visual_phrase`, `_axis_sig`, `body_signature`,
   `weapon_signature`, `action_signature`, `appearance_payload`. Honour
   `settings.appearance.enabled` (returns base signatures when off). Ship with
   `tests/test_appearance_sprites.py` §12 group A — **write these first**, they
   pin the seam.
3. **`src/config.py` + `config/settings.yaml`:** `AppearanceConfig` (§3.3).
4. **`src/services/image_generator.py`:** `sprite_rel`; `sig=` parameter on
   `generate_walk_cycle`, `generate_action_cycle`, `mirror_walk_cycle`,
   `upgrade_walk_cycle`; `weapon_desc=` on `generate_action_cycle` with the
   `_weapon_line` swap (§6.3); new `_build_outfit_idle_prompt` +
   `generate_outfit_variant_idle` (§6.1). **Default arguments must reproduce
   today's paths and today's strings byte-for-byte** — assert it (§12 group D).
5. **`src/services/asset_manager.py`:** `sprite_dir`, `body_set_complete`,
   `action_set_complete` (+ the memo), `serving_body_sig`, `serving_action_sig`,
   `ensure_body_set`, `ensure_action_set`, `appearance_status`,
   `_evict_lru_variants`, the two semaphores; split `ensure_action_sprites` into
   `ensure_attack_sprites` + the dodge step inside `ensure_body_set` (§7.3);
   re-key `get_location_assets_fast`'s player-sprite spawn (§5.4).
6. **`src/web/server.py`:** `_NO_PLAYER_APPEARANCE` + the `appearance` key on
   `build_player_state` (§5.1); `a: str | None = None` and the axis router on
   `get_sprite` (§5.2); new `GET /api/assets/appearance/{player_id}` (§5.3); the
   `appearance` block on `/api/assets/location/{id}` (§5.4); `_prewarm_appearance`
   wired into equip and unequip (§5.5).

**Frontend**

7. **`src/web/static/js/game.js`** (sole owner): appearance state block;
   `loadLocation` stores it; `loadPlayer` passes serving signatures;
   `preloadAllSprites(bodySig, actionSig)`; `?a=` on `updatePlayerSprite` and
   `preloadWalkSprites`; new `setAppearance`, `_startAppearancePoll`,
   `_stopAppearancePoll`, `_swapBody`, `_swapAction`, the mid-action deferral
   guard, `PIXI.Assets.unload` of the retired URLs.
8. **`src/web/static/index.html`** (sole owner): the `setAppearance` line in
   `applyPlayerState`; both `?v=` tags to `20260726a`.
9. **Closure:** CLAUDE.md updates (Services layer: appearance signatures +
   variant directories; Data storage: the `sprites/variants/{sig}/` layout; Web
   API: the new endpoint and the `?a=` contract); full `uv run pytest`; the
   manual pass in §15; confirm the cache-buster is identical on both tags.

**File-conflict note:** steps 2–5 touch disjoint files. Step 6 owns `server.py`
alone and merges last in the backend wave. Steps 7 and 8 are the usual
frozen-interface pair — the interface between them is exactly
`GameRenderer.setAppearance(block)` and the `state.appearance` shape in §5.1.

---

## 12. Test plan (pytest)

### `tests/test_appearance_sprites.py` (new)

**Group A — signatures (pure, no I/O)**

| Test | Asserts |
|---|---|
| `test_base_signature_for_empty_loadout` | `equipment=NULL` → `("base", "unarmed", "base-unarmed")`, version 1 |
| `test_signature_is_deterministic` | same player row, two calls, and a second freshly-constructed row → identical strings |
| `test_signature_changes_with_visual_desc` | editing `visual_desc` moves the sig |
| `test_signature_changes_with_item_id` | two items, same `visual_desc`, different id → different sigs |
| `test_signature_ignores_attire_and_trinket` | **the seam guard (A10)** — changing `attire.style`, `attire.uniform_faction_id`, `attire.concealing`, or the equipped trinket leaves all three sigs identical |
| `test_weapon_only_moves_action_sig` | equipping a weapon leaves `body_sig == "base"` |
| `test_armor_moves_body_and_action_sig` | equipping armor moves both; the 58-file half is keyed on armor alone |
| `test_legacy_typed_weapon_produces_weapon_sig` | `{"type": "weapon", "name": "Iron Sword"}` (no `slot`) → non-`unarmed` weapon sig, via `mechanize_item` repair |
| `test_over_tier_cap_item_does_not_change_signature` | `resolve_loadout` drops it → sig stays base (art must match mechanics) |
| `test_appearance_version_folds_into_hash` | monkeypatched `APPEARANCE_VERSION` changes the sig |
| `test_appearance_payload_is_pure` | monkeypatch `get_session` to raise; `appearance_payload` still returns |
| `test_disabled_config_forces_base` | `appearance.enabled = False` → base signatures with full gear equipped |
| `test_visual_phrase_fallback_chain` | `visual_desc` → `name` → slot noun; 180-char clamp |

**Group B — paths and completeness (tmp asset dir)**

| Test | Asserts |
|---|---|
| `test_sprite_dir_base_is_flat_legacy_path` | `sprite_dir("base")` and `sprite_dir("base-unarmed")` == `sprites/` |
| `test_sprite_dir_variant_shape` | `sprites/variants/a3f9c1b2` |
| `test_body_set_complete_requires_every_frame` | 57 of 58 files → False; 58 → True |
| `test_action_set_complete_requires_six_frames` | 5 → False, 6 → True |
| `test_serving_falls_back_to_base_when_incomplete` | partial variant dir → `serving_body_sig` returns `"base"` |
| `test_serving_returns_base_without_checking_base` | an empty flat sprites dir still serves `"base"` (A4) |
| `test_completeness_memo_is_dropped_on_eviction` | LRU eviction clears the memo entry |
| `test_lru_eviction_respects_cap_and_keeps` | 9 variant dirs at cap 8 → oldest deleted; `base`, wanted and serving never deleted |

**Group C — endpoints (FastAPI `TestClient`, monkeypatched asset dir + `spawn_generation`)**

| Test | Asserts |
|---|---|
| `test_sprite_strict_sig_404s_and_spawns` | `?a=<missing>` → 404 + a spawn keyed `body:{id}:<sig>` |
| `test_sprite_lenient_falls_back_to_base` | no `?a=`, incomplete variant → base bytes, 200 |
| `test_sprite_axis_router` | `attack3` spawns `action:…`, `dodge3` and `right_walk7` spawn `body:…` |
| `test_dedup_key_includes_signature` | two different sigs → two tasks; same sig twice → one |
| `test_npc_sprites_ignore_sig_param` | `?a=x` on an NPC serves the flat file |
| `test_appearance_endpoint_shape` | all 10 keys present; `body_ready` True when wanted == base |
| `test_appearance_endpoint_reports_failure` | after a recorded failure → `failed: true, generating: false` |
| `test_location_assets_carry_appearance` | the block is present with `serving_*` keys |
| `test_equip_prewarms_both_axes` | `POST /equip` records two spawn keys and returns without awaiting them |
| `test_state_payload_carries_appearance` | added to `test_integration_overhaul.py::test_state_payload_superset` — a legacy DB yields the base block |

**Group D — prompts (pure string assertions, no API call)**

| Test | Asserts |
|---|---|
| `test_outfit_prompt_names_the_visual_desc` | the phrase appears verbatim; "ONLY ONE CHARACTER" and the green-screen line survive |
| `test_outfit_prompt_forbids_weapons_and_redesign` | the "Do not add a weapon" and "do not redesign the face" clauses are present |
| `test_attack_prompt_names_the_weapon` | `_weapon_line("a notched iron shortsword")` appears inside the built action prompt |
| `test_attack_prompt_says_barehanded_when_unarmed` | the unarmed line appears and no weapon phrasing does |
| `test_dodge_prompt_never_mentions_a_weapon` | dodge is weapon-free regardless of the equipped weapon (A3) |
| `test_default_args_reproduce_legacy_paths` | `generate_walk_cycle(..., sig="base")` saves to `sprites/{id}_{dir}_walk{n}.png` |

### Existing tests that must pass **unmodified**

`test_equipment.py`, `test_progression.py`, `test_inventory.py`,
`test_playtest_seed.py`, `test_world_simulation.py`, `test_combat.py`,
`test_turn_audit.py`, `test_pending_actions.py`, `test_frame_interpolator.py` —
collectively the proof that a NULL-everything world is unchanged and that this
system touched neither mechanics nor the audit.

---

## 13. V1 vs later

### In v1

- Player armor → idles (4), walk cycles (4 × 12), dodge cycle (6).
- Player weapon → attack cycle (6), keyed on the (armor, weapon) pair.
- Signature-scoped directories; `base` is the legacy flat path.
- Strict `?a=` / lenient default resolution on the existing sprite endpoint.
- `GET /api/assets/appearance/{player_id}` readiness poll; `appearance` block on
  the scene fetch and on every `state` payload.
- Equip/unequip pre-warm; 404-triggered lazy generation everywhere else.
- Atomic per-axis texture swap; never a partial set; failure keeps the old look.
- LRU cap, concurrency gate, config kill switch.

### Later (recorded, deliberately not built)

| Deferred | Why not v1 |
|---|---|
| **NPC gear visibility** | Explicitly out of scope. NPCs have one front idle and no equipment model; giving them one means a `NPC.equipment` column, a creator-agent authoring surface, and N× the image spend. The `?a=` parameter is already ignored for `character_type == "npc"`, so the seam exists when it is wanted. |
| **Trinkets** | A ring or amulet is invisible at sprite scale. Adding it to `body_sig` would triple the 58-file cost for pixels nobody can see. |
| **Sheathed weapon in idle/walk** | Would couple the axes (A1) and multiply the expensive half by the number of weapons. Revisit only with a cheaper compositing pipeline. |
| **Faction-uniform recolouring** | `attire.uniform_faction_id` is where this eventually pays off (a Watch coat that is *visibly* Watch colours) — but it is the sibling system's data and belongs with whatever renders faction identity. |
| **Portrait regeneration on gear change** | Portraits are head-and-shoulders; armour barely reads, and portraits are the most identity-sensitive asset in the game. |
| **Damaged/bloodied appearance by health status** | A third axis. Tempting, cheap-looking, and a combinatorial trap; do it as a tint or an overlay, not as a signature. |
| **Player-facing "generating your new look" indicator** | The swap is meant to be quiet. If playtests say the wait feels broken, add a small spinner on the character panel's equipped row — one CSS class, driven by the existing `generating` flag. |
| **Cross-player sharing of variant art** | Directories are already signature-keyed (not player-keyed) so the layout supports it, but the files are per-character by design: two people in the same coat still have different faces. |
| **Weapon trails / VFX per weapon tier** | Pure client work, no art pipeline; a natural follow-up once tiers are visible. |

---

## 14. Risk register

| Risk | Detail | Mitigation |
|---|---|---|
| **Half-dressed frames** | The single most visible failure mode: idle in mail, walking in a shirt. | Atomic per-axis swap; `body_set_complete` is all-or-nothing; the client assigns only after `Promise.all` resolves; `?a=` is strict so a stray frame can never come from another set. Tests: `test_body_set_complete_requires_every_frame`, `test_serving_falls_back_to_base_when_incomplete`. |
| **Face drift across variants** | The model redraws the character while redressing them. | Reference chain anchored on the base front idle; three explicit do-not-redesign clauses; back/right chained from the *variant* front so the outfit propagates without re-anchoring identity. Visual check in §15. |
| **Weapon flicker inside the attack strip** | Model drops or changes the weapon between frames. | One strip per call; explicit "same weapon, same size, shape and colour, never disappears"; existing broken-frame repair + one automatic regeneration. |
| **Image spend** | 7 calls per armor swap is real money. | Orthogonal axes (A1) — the common action, swapping weapons, costs 1 call. Cached forever per signature. `body_concurrency=1`. LRU cap 8. `appearance.enabled=false` kills it outright. Cost table in §7.4. |
| **Disk growth** | 58 PNGs per variant. | LRU eviction at 8 variants per player; `clear_cache("sprites")` already removes the whole tree including `variants/`. |
| **`build_player_state` slowdown** | It runs after every world-write tool. | A6: the payload is pure, disk-free, query-free — one sha1 per equipped axis. Asserted by `test_appearance_payload_is_pure`. |
| **Poll storm** | A naive readiness check per frame or per sprite. | One endpoint, one 5 s timer, only between a gear change and the swap, capped at 48 tries, cancelled on `failed`. Zero requests in steady state. |
| **Texture memory across many swaps** | PIXI caches per URL, so every signature the player ever wore stays resident. | `PIXI.Assets.unload(oldUrls)` after each successful swap. |
| **Mid-fight visual pop** | Texture map replaced mid-swing. | Swap deferred while `this.playerAction` is active. Combat stats are snapshotted at `_ensureCombat` and never read from the appearance path. |
| **Stale client vs new sprite URLs** | An old `game.js` requests without `?a=` and gets the lenient resolution — the correct look, just without cache-busting precision. Graceful by construction. | Single `?v=` bump on both tags (R13). |
| **Two specs editing the same `Item` fields** | The shared seam. | §3.1 is written to be built exactly once; A10 and A11 name the boundaries (attire never enters a prompt or a signature; this spec never edits the ledger or the audit). `test_signature_ignores_attire_and_trinket` fails loudly if the boundary is crossed. |
| **A world with no `GEMINI_API_KEY`** | Variant generation always fails. | Existing 300 s failure cooldown + `failed` flag; the player permanently renders as `base`, which is exactly today's game. |

---

## 15. Manual verification pass

1. Boot an existing world (`data/emberfall.db`). Player renders identically;
   `state.appearance` is the base block; **no variant directory is created**.
2. `add_to_inventory` a weapon with a `visual_desc`; it auto-equips (existing
   rule). The state event shows a new `action_sig`, `body_sig` stays `base`.
   Within ~40 s, `sprites/variants/base-w…/` holds 6 attack frames. Start a
   fight: the swing shows the weapon. Idles and walks are unchanged.
3. Equip armor with a `visual_desc`. HUD updates instantly. The sprite keeps the
   old look. `/api/assets/appearance/{id}` reports `generating: true`. After
   ~2–3 min the whole set swaps at once: idle in all four directions, walk in
   all four directions, dodge-roll — all in the new armour, same face.
4. Attack again: the attack strip for the *new pair* has regenerated (armour +
   weapon both correct).
5. Unequip everything: the swap back to `base` is instant (0 calls, cached).
6. Re-equip the first armour: instant (cached forever).
7. Reload the page mid-generation: the scene loads with the *serving* set (no
   placeholder, no naked frame) and swaps when the poll reports ready.
8. Set `GEMINI_API_KEY` to garbage, equip a third armour: nothing visibly
   changes, `failed: true` appears in the appearance endpoint, the client stops
   polling, and the game plays on.
9. `appearance.enabled: false` in settings, restart: full gear equipped, base
   signatures, no generation, no variant directories.
10. `uv run pytest` green, including every legacy suite listed in §12.
