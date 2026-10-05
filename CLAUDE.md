# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Forge is a text-based RPG with an LLM dungeon master: one DM agent (Strands Agents SDK + LiteLLM) that writes the story, voices every NPC, and changes the world through inline `@commands` the engine validates and applies. Code owns every number; the LLM narrates.

## Commands

```bash
# Install dependencies (uses UV package manager)
uv sync

# Start the game (CLI)
uv run main.py play

# Start web frontend (localhost:12000)
uv run main.py web

# Run tests
uv run pytest

# Create a test world
uv run main.py seed

# Reset/clear the world
uv run main.py clear

# Custom database path
uv run main.py play --db path/to/game.db
```

**Environment:** Requires `OPENROUTER_API_KEY` environment variable.
Optional: `SUNO_API_KEY` (music palette generation), `ELEVENLABS_API_KEY`
(NPC dialogue TTS) — audio degrades gracefully without them.

## Architecture

### The DM: one agent, one reply, one tool (hybrid architecture, 2026-10-04)

The DM is a SINGLE agent (Claude Sonnet 5 via OpenRouter, reasoning off,
`config/agents.yaml`). No sub-agents: the NPC agent and creator agent were
folded in (a turn used to take 4-8 sequential model calls and 38-85 s; the
opening took 7.5 min). Its only tool is `roll_check` (dice must land before
prose). Everything else is written INLINE in its streamed reply:

- **Prose** — streamed to the player as written (second person).
- **Speaker lines** — `Name (tone): "words"` alone on a line → voiced speech
  events with portrait + bubble (`src/web/dialogue_markup.py`). The DM voices
  every NPC itself from dossiers; there is no `speak`/`prompt_npc_agent`.
- **`@command` lines** — hidden from the player, parsed and applied by
  `src/services/commands.py` (`CommandRunner`, ~37 verbs: `@move`, `@time`,
  `@give`, `@coin`, `@trust`, `@fight`, `@quest-start/done/new`,
  `@new-place/@new-npc`, `@note`, `@plan`, ...) through the existing engine
  functions in `src/tools/world_write/*`. Failures and notable engine answers
  go into the next turn's context ("HOW YOUR LAST COMMANDS LANDED"). The
  `complete` SSE event lists each command's outcome.

The DM's instructions are markdown: `src/agents/prompts/dm.md` (rules) +
`src/agents/prompts/commands.md` (command reference), loaded on every agent
build. `tests/test_dm_prompt_tools.py` enforces that every taught `@verb`
exists, every verb is documented, and no retired tool is named.

Each turn's context is assembled in code (`dm_orchestrator._build_context`):
the player block (coin, gear, pack), quests (custom steps flagged, rewards
shown as paid by `@quest-done`), the lead, **the scene brief**
(`src/services/scene_brief.py`: exits with BLOCKED reasons, a dossier per
NPC present — voice, wants, trust, recognition, memory file, offerable
quests, trust-gated secrets, would-join verdict, priced stock — plus known
people/places and a price list), the chronicle, DM notes, world tick,
canvas ledger, campaign directives, audit feedback. The DM needs no read
tools.

**Memory is markdown, stored in the world's .db** (`src/services/memory_files.py`,
`MemoryDoc` rows keyed by player id + doc name, so the .db is the whole save:
a backup, copy or restore carries every memory with its world): one document
per NPC per character (`npc:<id>`; Notes / Recent / Earlier, auto-folded),
`chronicle` (turns trimmed from history, summarized in the background),
`dm_notes` (`@plan`), `journal`, `heard_names`. Mechanics stay in the DB's
own tables (trust/disposition gates live on `NPCRelationship`).
`uv run main.py memory-export --db <world>.db` writes them out as files
(`data/memory/<world>/players/<pc>/…`) to read or fix; `memory-import` reads
them back. A pre-database folder in that layout is copied in on its
character's first touch (never over a stored doc) and marked `.in-database`.
`src/services/dialogue.py` resolves speaker labels to NPCs and files each
turn's dialogue into those documents.

History (`src/agents/turn_window.py`): each turn's big context block is
compacted after the turn; the window trims in steps (30 → 15 turns) into the
chronicle. Prompt caching (`CachingLiteLLMModel` in `src/agents/base.py`)
sets 3 Anthropic cache breakpoints — measured ~all of the prompt cached.

Measured after the change: opening 17 s (was 7.5 min), typical turn 9-15 s
with first text ~2.5 s, one model call (two when a roll is needed).

World generation still uses separate agents:
- WorldForge - Master world generator
- Faction/Location/NPC/History generators - Domain-specific generation

Systems added from the AI-playtest rounds (2026-10-04):
- **Encounters** (`src/services/encounters.py`): the world strikes first. Rolled
  before each DM turn and on canvas arrival (`/api/travel` returns `ambush`);
  only beasts on arrival, anyone hostile once the player lingers; night and
  tension raise the odds; cooldown after any fight; never the campaign
  villain/successor. The DM gets an `[AMBUSH — ALREADY HAPPENING]` directive.
  `@fight` also holds the villain out of group fights before act 3. An
  unanswered combat roster is re-issued at most twice, then breaks off
  (no soft-lock).
- **Strangers** (`src/services/dialogue.py`): an NPC the player has not met
  is shown under `role_label(npc)` ("Ash-Cloaked Man") in dossiers, chips,
  speech display names, combat rosters and body labels. `NameGate` (per
  turn): a name counts as introduced only once someone SAYS it in a speaker
  line; labels show the heard part ("Vex", not "Vex Marrow"); unmet names in
  prose are rewritten to "the <role>" mid-stream; heard words persist in
  `heard_names`. Beat/event delivery and the audit read the DM's real
  names (`dm_turn_text`); the player sees the masked text. `@trust`/`@reveal`
  on a stranger wait for the reply's end and apply only if they were
  introduced; `@journal` entries are masked the same way. Lines voicing the
  player are dropped. Audit checks: `unintroduced_name`, `voiced_the_player`,
  `unknown_named_person`, `prose_before_roll`. Villains/bosses get no
  talk chip until met. `note_confessions` marks a trust-gated secret told in
  plain dialogue as revealed and journals it.
- **Journal** (`@journal`, `@note <player>`; `memory_files.journal`;
  `GET /api/journal/{id}`; 📖 HUD chip): the clues the character has learned.
- **Authored objects**: the brief's "Things here" lists each scene object and,
  for the DM only, what it reads/holds; `@interact <thing>` resolves it
  through the same path as the canvas's [E].
- **Starting kit** (`src/services/starting_kit.py`): every new character gets
  a crude knife (equipped) and a healing draught on Q. The seed's market has
  a merchant (Hesk Bramble).
- Command guards: quest rewards and bed fees are never paid twice; a paid
  room IS `@rest` (one night per reply); `@rest` walks to an adjacent inn;
  `@move` into a whole-town node is refused; `@move <person>` goes where they
  are; `@take` is refused as ambiguous (`@remove` / `@give`); DM-made quest
  XP scale is clamped by its steps.

- **The loop, round 4** (2026-10-04):
  - *Names on screen*: `dialogue.player_facing_*` rewrites everything the
    client shows outside the reply (lead, quest log, chips, toasts, campaign
    panel, party) at the serialization boundary, so people appear only as far
    as their names were said; the DM context keeps real names. A `talk_to`
    step ticks only when the player addressed that NPC (`dialogue._talked_to`).
  - *Offers*: an `[OFFER THIS REPLY]` line (`dm_orchestrator`) binds the DM
    to pitch an authored follow-on job, with its pay; `@quest-done` thanks the
    giver with trust once (`paid_trust`); `reward_unnarrated` audits payoffs
    nobody saw. Play-triggered next-act beats pull ahead when only clock beats
    remain (never act 3); a stalled beat re-fires once at its place.
  - *Proof*: `PROOF HERE:` / `LEFT BEHIND:` brief lines and a lead (+ interact
    chip) that points at the proof object; handing the real item to the giver
    also ticks the custom steps before it. The engine journals reads/finds,
    clue `@give`s and beat-delivery lines (`memory_files.journal_interaction`);
    coin words are clues, never gear; `@give` of what an authored Thing holds
    resolves through the Thing; `@new-npc … | status: dead|missing`.
  - *Risk*: `@move` charges the road's time beyond the turn floor (a `@time`
    after it is de-duplicated); quiet streaks get a `[COMPLICATION]`
    (`src/services/complications.py`); audits `wound_without_fight`,
    `partial_without_cost`, `item_handed_without_command`; a fight chip; world
    sim events stay at real places and inside the truths; the campaign author
    sees the world's scheduled events. Memory lives in each test's own
    temp DB; the autouse fixture in `tests/conftest.py` points the export /
    legacy folder at `tmp_path`.

- **The loop, round 5** (2026-10-05):
  - *Who knows whom*: `NPCRelationship.name_known` (the player knows their
    name) and `knows_player_name` (they know the player's) are separate from
    the row itself (= they have spoken; trust lives there). NULL = an old row,
    known both ways. A name heard from anyone (`heard_names`) marks the row;
    `turn_audit.player_name_said` + `dialogue.note_player_name_heard` decide
    who learned the player's name; `name_before_introduction` keys on it. A
    bare trade-word label ("Keeper") resolves to the one present person of
    that trade (`dialogue._by_trade`); `@trust`/`@reveal` on a stranger land
    at once on a stranger row.
  - *Spine*: next-act pull-ahead only on triggers play made true after the
    act armed (`Campaign.pull_held`); a later act's quests are never offered
    (`campaign.later_act_quest_ids` — offer directive and dossier); delivery
    evidence ignores names and campaign-common words and waits out a failed
    roll; a delivered beat retires its scheduled twin event (CONTINUITY
    line); hook beats arrive as `STORY HOOK — SHOW IT, THE PLAYER CHOOSES`
    and hand over nothing until taken up (`_settle_hook`).
  - *Stakes* (`src/services/stakes.py`, `roll_intent.py`): a partial/failure
    whose price the DM never wrote is charged by the engine next turn; a
    failed roll blocks `@step-done` on that step; a risky typed act gets a
    `[THIS REPLY: call roll_check FIRST…]` line; a stalled main quest's clock
    produces `[WHILE YOU WAITED]` consequences; unanswered complications come
    back as `[COMPLICATION — UNANSWERED]` tied to a real person; night at the
    town's edge can bring a lone beast scout; audits `roll_without_attempt`,
    `healed_without_command`.
  - *Ledger*: pay up front is booked against the giver's job
    (`commands._advanced_quest`) and `@quest-done` pays the rest; a reward is
    never paid twice within two replies; documents carry canonical `reads`
    text (the brief quotes it, the audit checks it); the brief's
    `Coming up:` line marks scheduled events that have not happened (told
    early → the clock jumps, `world_tick._told_early`); `@move` to where the
    player already is fails with a hint; the seed gained Stable Yard and
    North Gatehouse.

- **The loop, round 6** (2026-10-05):
  - *Travel*: `travel_intent.py` reads where the player said they go and adds
    a binding `[THIS REPLY: the player goes to X — @move …]` line (or "the
    way is shut", or "make it with @new-place"); an `[OFFER THIS REPLY]` waits
    on such a turn. A pass in the pack opens the road its requirement names
    (`travel.held_pass`) — except at night: a gated road whose requirement
    or a fired event at its end says it bars after dark is shut 20:00-06:00
    whatever the player holds (`travel.barred_tonight`; `| through` still
    can). Prose that walks on past the reply's `@move` to a neighbouring
    place it names and arrives at (or shows by its Things) moves the player
    when the way is open (`server._carried_on_to`, warning
    `story_carried_on`), else warns `arrived_past_the_way`.
  - *Clock*: told-early needs a happening verb and ignores time-until /
    denied clauses (`world_tick.told_happening`); a jumped event shows the DM
    a one-time **HAPPENED** block. The waiting clock resets while the way in
    is shut to the player (`lead.way_in_blocked`) and pauses for a night
    closure. A lead about a scheduled future event gives way to a present
    one, or says when ("Tomorrow morning: …", no chip) (`lead.beat_due_later`).
  - *Jobs* (`src/services/job_offers.py`, in memory): a pitched job the player
    takes in words or by setting out on its first step gets a
    `[START THIS JOB]` line; the engine starts it after the reply if the DM
    did not, crediting steps done since the pitch. `@quest-done` and
    `@step-done` refuse a place never stood at or an item never held;
    `@interact` take/search refuses what the player did not reach for.
    Audits `refused_but_narrated`, `carried_not_held`. Rewards read "(theirs
    to keep — …)".
  - *Names*: the player's name is cut from the lines of speakers who never
    heard it (bubble, TTS, memory, history replay — `dialogue.cut_player_name`);
    a Thing's read text counts as heard at once; trade synonyms for bare
    labels; `@new-npc … | status: dead` is refused when the world already
    has that lost person, and the brief lists "People the story has lost"
    (the seed's dead carter Tobin has a row). The `[OPENING]` names
    strangers by role label, real name bracketed as unknown.
  - *Spine*: a beat tied to a place (lead target, or a directive "when they
    reach X") waits there and parks when the player walks off; elsewhere a
    deadline arrives as WHERE IT HAPPENS news. Clock beats whose scheduled
    twin already happened come due as its aftermath, and an "as scheduled"
    beat retires the event it told; earlier-act beats a later beat
    superseded are skipped. Encounters never send the story's cast
    (`campaign.story_cast_ids`; a weak stand-in of the pack springs the
    ambush) and hold once after a reply that ended on a choice. roll_intent
    knows spoken threats, flight and facing a beast down; gifts no longer
    reset the complication count. A finale is never skipped for waiting.

- **The loop, round 7** (2026-10-05):
  - *Proof & ledger*: a refused `@give`/`@interact`/`@hand` of an authored
    thing lying elsewhere arms a one-time `[SETTLE THIS FIRST]` line at the
    top of the next context (`src/services/settle.py`); refusals name where it
    lies and offer a lesser clue under its own name; `carried_not_held` /
    `refused_but_narrated` read "it" after a refusal (denials and repairs
    exempt, `turn_audit._moves_on`). `[COMBAT OUTCOME]` lists what each body
    carries. A same-reply duplicate `@coin`/`@give`/`@remove` and an `x0`
    count are refused; a payout repeated within two replies is refused when
    its reason names the job or giver; audit `advance_bought`.
    `[PAY THIS REPLY]` when every step is done and the giver is present;
    `[PAID WHILE APART]` for a payout made away from them.
  - *Spine*: beat delivery reads place names, `@interact` reads and
    second-stage keywords; beats that script the player are hooks (forge
    rule 8); a proof hand-over never ticks a "find out" step; an impossible
    `quest_status` cue comes due; an empty tracker takes the next act's lead
    (`_starved` pull-ahead). The villain is OFFSTAGE before the first beat
    that brings them on (`campaign.villain_offstage`: dossier line, audit,
    and `offstage_ids` keeps them off the canvas and the state's people).
    `SEALED_KNOWERS_LINE`; a beat that fires as the player leaves goes with
    them (`with_departure`).
  - *Travel*: the parser rejects "going to <verb>"; if the reply ignores the
    travel line the engine walks the player after it (`server._walk_as_said`,
    unless the prose held them short); people the prose walks along follow
    (`_walked_along`); `@quest-start` in the reply that pitched the job is
    refused (`commands._not_taken_yet`); a road barred for the night opens
    only on a won roll or a bribe this reply or the last (`travel.night_why`).
  - *Stakes*: a won roll holds the danger it faced down for two rolls (no
    ambush cancels it); complications wait out a vigil
    (`complications.keeping_vigil`); a repeated roll of one act returns the
    first result; junk-reason rolls refused; a willing person told by a fired
    beat is not rolled on (`stakes.beat_tells_it`); wounds make athletics and
    stealth harder; on roll-first turns prose before the roll is held and
    dropped.
  - *Voice*: `MetaSentenceFilter` drops planning/apology sentences (also on
    history reload); item names, shop and interact payloads go through the
    NameGate; person lines carry a look and pronoun; "Coming up" gives the
    next event's hour; "Already told to the player" in the brief; the
    player's name cut handles possessives, emphasis and tag questions.

- **The loop, round 8** (2026-10-05):
  - *Point of view*: the world bible's own point-of-view words never reach
    the DM (`world_bible.dm_narration_style`: always second person — a seed
    "Third person" style had turned a whole session third person);
    `MetaSentenceFilter` rewrites fiction that names the player into "you"
    (`dialogue_markup.to_second_person`, audit `third_person`) and drops
    planning that names them; recaps ("…is filed away", "…yours to choose")
    and a closing "What do you do?" are dropped.
  - *Reaching for things* (`src/services/intents.py`): binding lines when the
    player takes what was held out (`@give`, else the engine gives it after
    the reply — `taken_fallback`), asks for a room (`@rest`; audit
    `room_without_rest`) or to read a Thing (`@interact`); a look-only
    `@interact` reveals without taking; a take put back is undone; a quest
    item a present NPC carries moves out of their hands on `@give`
    (`WHO CARRIES WHAT THE QUESTS ASK FOR`); a role-only voice heard twice
    gets a binding `@new-npc` line. Audits `handed_aloud_warning`, slid keepsakes.
  - *Danger*: facing a beast without a blow is a roll-first stand-off (also
    unnamed, when a beast is in the room: `encounters.beast_here`); a win
    sends it home (`encounters.send_off`), a loss provokes it; `[PAY THIS
    REPLY]` waits out danger; a theft is staged as a choice and answered
    only when the player saw the cost; a barred night lists the ways still
    open (bribe, climb, bed); five talk-only turns bring a closing choice
    (`complications._talk_push`); people a fired event clears go indoors
    and return at first light (`world_tick.clear_places`).
  - *Loop*: the seed starts at 15:00 (road open by day, the howl still lands);
    a job counts as offered only when pitched (`job_offers.pitched`); work
    done on an unoffered job is credited in a later `[OFFER THIS REPLY]`;
    offers carry their steps and where the first one's work is; report-backs
    add something new; a persona-hook line ("What this character wants");
    bought upgrades go on automatically (never over a disguise).
  - *Secrets*: speaker lines that say a SEALED truth are cut as they stream
    (`campaign.seal_line`, audit `sealed_truth_cut`); a plain question gets
    no social roll; one-word sealed slips, the dead told as alive, borrowed
    names, altered reads and misnamed rewards are audited; "Knows:" and
    "Names in use" brief lines.
  - *Voice*: walk-along/walk-off read more phrasings (`server._along_ids`,
    `_left_ids`); speaker lines over 60 words split into two bubbles; an
    "Already used — vary" line (`src/services/voice_tics.py`, never the
    plot's own things); the canvas re-syncs who stands here after the
    engine moved someone (`state.present_ids` → `game.syncNpcs`).

- **The loop, round 9** (2026-10-05):
  - *Spine* (`campaign.py`): a beat binds to the place its directive opens on
    ("At X,") or whose authored Things its keywords name, and waits there
    (the not-delivered nag is held while the player is away; audit
    `memory_of_unvisited_place`); a find never retires a beat that brings
    on someone not yet met — it makes it due now (THE FIND IS THE CUE +
    ENTRANCE notes); "if asked … admits X" beats count only the admission's
    words and get `[THE PLAYER ASKED — … ADMITS IT NOW]` (`asked_note`); a
    place scene whose people are present skips beat spacing; `@fight` on a
    story beast before its beat fights a packmate stand-in; the villain's
    people (successor, faction members never met) are OFFSTAGE until their
    first beat (`offstage_ids`, no chips, audit `cast_before_their_scene`).
  - *Clock*: an event firing where the player stands is "HAPPENING WHERE YOU
    STAND" (also on [COMBAT OUTCOME] turns) and names the job step it answers;
    denials and long-past mentions are not delivery (`world_tick.told_at_hand`);
    the waiting clock never contradicts a fresh consequence, pauses while
    the player works the story's own side job, and registers a missing
    person with a findable trace; summonses need real news; pushes hold
    while the player talks with their step's person (spoke in the last two
    replies) or an armed beat's person is here; watchers are lookouts of the
    villain faction (`world_name` names); exits give travel time in words
    (audit `distance_misstated`).
  - *Agency*: roll_check refuses a roll on a danger this reply introduced
    (`encounters.introduced_now`); a beast met by a leaving player is shown
    first (`[DANGER ON THE WAY OUT`, no travel line); `[COMBAT OUTCOME]`
    always states health and a worse `@health` after it is refused; `@use`
    needs the player's own reach; rolls have a 10% odds floor and an odds
    word below 40%; bluff/haggle patterns; `@interact` take needs a reach
    verb (else a look); audit `lost_gear`.
  - *Reward*: each met step pays a slice of the quest's XP (total unchanged,
    `Quest.step_xp_paid`); beast bodies leave a trophy a merchant buys; the
    lead shows what a job pays; a gift to a present person adds trust once a
    day (`NPCRelationship.last_gift_day`, cap 70) with a toast; the persona
    hook is due on reply 3 then every 5th; an unfound proof step shows as
    "Bring proof back to …" until held (`dialogue.unfound_steps`).
  - *Identity*: a speaker label borrowing a name in use goes out under the
    prose's role (audit `name_in_use_borrowed`) and never moves story cast,
    merchants or anyone seen elsewhere today; walk-off never falls back to
    the wrong pronoun; departures told after the fact move people; audit
    `invented_suspect` + the brief offers the authored stranger
    (`src/services/suspects.py`); closing menus dropped; custom steps and
    evidence lines journaled; held-road checks noted.

**AI playtester**: `scripts/autoplay.py` — a cheap player model plays a
character through `/api/play` (fights resolved via `/api/combat/result`),
logging per-turn latency, commands, failures and audit warnings to
`data/playtests/<run>/` (`transcript.jsonl`, `report.md`). Run several in
parallel on separate sandbox servers (`scripts/sandbox_server.py --seed --db
data/x.db --port N`; `FORGE_TTS=0` silences TTS for a process).

World generation uses separate agents:
- WorldForge - Master world generator
- Faction/Location/NPC/History generators - Domain-specific generation

### Mechanical consequences (code enforces, LLM narrates)

- **Time**: every turn applies a minimum time cost if the DM forgot
  `advance_time` (`enforce_minimum_time_cost` in `src/services/world_tick.py`);
  the clock tracks minutes so fractional costs accumulate.
- **Events**: scheduled events fire deterministically on the world tick;
  fired-but-unnarrated events are re-surfaced to the DM for up to 3 turns.
- **Tension** (`src/services/tension.py`): health loss floors it
  (winded/hurt→rising, badly_hurt→high, critical→climax), combat floors it
  to high, sustained calm decays it one level at a time (climax 2 / high 3 /
  rising 4 calm turns), and low→rising auto-escalates after 8 quiet turns.
  DM-authored `update_dm_state` changes are respected and reset decay.
- **Quests**: `activate_quest`/`update_quest_status` sync `Player.active_quests`
  and apply `Quest.rewards` (currency/items/reputation) transactionally; both
  emit structured quest events (frontend banners + triumph music stinger).
- **Objectives** (`src/services/objectives.py`): `Quest.objectives` entries
  are objects `{text, kind, target_id, item_name, done}` with a fixed `kind`
  menu (`go_to`/`talk_to`/`obtain`/`kill`/`deliver`/`custom`). The ENGINE
  ticks every kind but `custom` from its own paths (`move_player` arrival,
  `prompt_npc_agent`, `add_to_inventory`/`transfer_item`, `kill_npc`/
  `apply_combat_result`/campaign `npc_status`), emits `objective_done` SSE
  events (also `objectives_done` on the travel/interact/combat-result REST
  responses), writes an `Objective met` ledger line, and exempts the same-turn
  audit via `pop_audit_tools`. `normalize_objectives` is the whole
  backward-compat story: strings still load as `custom` (a legacy `[DONE]`
  prefix is honoured), names resolve to ids, and `done` is STICKY across a
  DM rewrite. All-done never completes a quest — the DM is told and calls
  `update_quest_status`. `create_quest` takes `scale`. Quests are
  world-global; the player's `active_quests` list is only an ordering hint.
- **The Lead** (`src/services/lead.py`): the one code-chosen next step —
  the newest active quest's first undone objective, else the earliest
  armed/stalled beat's player-facing `StoryBeat.lead` (+ `lead_target`,
  authored spoiler-safe by campaign_forge, resolved at persist). Rides
  `state.lead` with a resolved `target` (`here`) and a BFS
  `route_next_hop_location_id` over discovered connections/hierarchy that
  prunes roads `check_route` blocks unless the locked road IS the target.
  Client: a `#lead-tracker` line under the HUD and canvas markers
  (`game._updateLeadMarkers`: NPC halo, door glow + chevron, rim arrow along
  the exit's map `position`). DM context gets objectives (`✓`/`◦`) and a
  `[PLAYER'S LEAD]` block — or an order to create one when it is empty.
- **Suggested actions** (`src/services/suggestions.py`): 2-4 code-derived
  chips on `state.suggestions` (lead first, people here, exits, objects,
  "Look around"); the client routes door/object chips through `canvas-travel`
  / `canvas-interact` (no DM turn) and talk chips through `npc-interact`.
- **Opening turn** (`src/services/opening.py`, `POST /api/opening/{id}`): a
  character with no DM history gets a title card (campaign title/act, else
  world name/tagline — cinematic source `opening` bypasses rarity but is
  ledgered), the Location row as the scene, and an auto-sent `[OPENING]`
  directive (premise + act-1 + the lead, never a beat directive) that must
  END by handing over a lead. The endpoint waits up to 45 s for a `pending`
  campaign. History replay skips `[OPENING]`; `PUT /api/world/players/{id}`
  also runs `ensure_campaign`.
- **Travel**: `move_player` blocks on `Connection.requirements` until the DM
  confirms them with `requirements_met=True`; hierarchy hops cannot bypass a
  destination whose every authored entry route is requirement-locked.
- **Attire semantics** (`src/services/perception.py`): what the player WEARS
  decides how strangers read them, separately from who they are. The equipped
  armor's `attire` label set (`style` from a five-word menu, optional
  `uniform_faction_id`, `concealing`) produces a code-owned perception profile
  (`state.appearance.read_as`, a `Player reads as:` context line, a
  character-panel row). Strangers react to the clothes — a faction uniform
  makes them treat the wearer as that faction regardless of identity — while
  NPCs with an `NPCRelationship` row know the player on sight; a concealed
  face makes an acquaintance's recognition *contested*, resolved by a
  DM-called `roll_check` (never rolled automatically) whose answer sticks for
  the game-day. `Location.dress_code` (`min_style` / `banned_styles` incl.
  `concealing` / `reason`) is enforced inside `travel.check_route`, so it
  blocks canvas doors, `/api/travel` (blocked 409 carries `reason: "dress"`),
  and `move_player` through one path, overridable only with
  `requirements_met=True`. `roll_check` also layers ONE code-computed
  situational factor (night/weather stealth, filthy-in-fine-company,
  hooded-and-watched, wearing-their-colours) over the DM's modifier and
  reports it back as a phrase, never a number.
- **Progression** (`src/services/progression.py`): every number lives here —
  XP thresholds (levels 1-10), quest XP by `Quest.scale` label
  (errand/job/mission/saga), gear tier tables (crude→legendary: weapon damage,
  armor hearts, trinket i-frames, catalog prices), consumables, level scalars.
  XP is granted ONLY by code (quest completion via `_complete_quest`, combat
  kills via DB tier re-read); `rewards["xp"]` authored by the DM is stripped.
  `combat_stats(player)` produces the `state.combat` dict game.js runs on;
  level-1-unarmed is test-pinned byte-identical to the pre-overhaul game.
  Equipment = pointers into inventory (`Player.equipment`); equip/unequip/
  quick-slot/use-consumable are REST endpoints, no DM turn.
- **Skill checks** (`roll_check` tool): risky non-combat actions are decided
  by a code-rolled d20 vs difficulty label (easy/tricky/hard/desperate, ±
  advantage/disadvantage) — the DM picks words, narrates the outcome label,
  never sees or prints numbers. The audit flags risky-action resolutions
  narrated without a check (`risky_action_without_check`).
- **Enemy tiers** (`src/services/combat.py`): ABSOLUTE danger ladder
  (harmless/weak/standard/dangerous/deadly/boss × brute/skirmisher/stalker/
  ranged) — `combat_params(tier, archetype)` takes no player argument, so a
  beggar stays trivial forever and growth is legible. `NPC.danger_tier` is
  assigned by the creator agent (never `boss` — that's authored-only via the
  world editor); NULL infers from keywords. `standard` row == legacy values
  (test-pinned). Tier XP 0/5/15/40/100/300 awarded by `/api/combat/result`.
- **Real-time combat** (`src/services/combat.py` + `start_combat` tool): the
  DM names present NPCs; the frontend fights them Hades-style with
  server-resolved params riding the `combat_start` event (client table is
  fallback only). Player: light (J), heavy (hold J), parry (K), dodge
  (Space), potion (Q). Bosses: named HP bar, phase 2 at 50%, ring slam,
  lunge combo, once-ever `boss_intro` cinematic, client-side kill cam.
  `POST /api/combat/result` applies outcomes in code (hearts_lost/healed
  severity, defeat→critical, NPC deaths + world event, tier XP + level-ups)
  BEFORE the DM narrates the aftermath via an auto-sent `[COMBAT OUTCOME]`
  turn.
- **Campaign spine** (`src/services/campaign.py` + `campaign_forge` agent):
  generated at player creation from the existing roster — fixed truths and
  9-15 story beats across 3 acts live in DB tables the DM can read
  (`get_campaign`) but never write. Beats fire deterministically in
  `run_campaign_tick` (called inside `_build_context`, pure SQLite) on
  time/location/quest/level triggers with hard fallback deadlines; ops
  (move/kill NPC, quest flips, faction shifts) execute in code, then the DM
  gets a MUST-DELIVER directive + already-applied ledger. Deadline beats
  carry met AND missed consequences — blown clocks change the world. HUD
  shows act progress + countdown chips from `state.campaign`.
- **Canvas-first play** (`src/services/travel.py`, `scene_exits.py`,
  `scene_objects.py`, `src/services/barks.py`): unlocked doors travel via
  `POST /api/travel` (same code path as `move_player`, applies time) and
  objects resolve via `POST /api/interact` (take/read/open/search, fixed
  menu, numbers stripped at authoring) — no DM turn. Ambient bark pools per
  location (`GET /api/barks/{id}`, fast model, cached on Location) drive
  proximity speech bubbles between turns. Everything the engine did lands in
  the DM's next-turn context as a `[SINCE YOUR LAST TURN]` block
  (`src/web/pending_actions.py`) whose `tool_equivalents` feed the audit's
  `code_actions` so engine mutations never false-positive.
- **Cinematics** (`src/services/cinematics.py`): fixed vocabulary
  (title_card/boss_intro/kill_cam/vision/weather) triggered by label from
  campaign beats (`CAMPAIGN_CINEMATIC_MAP`), auto (boss combat), or the
  cooldown-guarded `play_cinematic` DM tool; a DB-backed rarity ledger
  (per-kind cooldowns, daily/session caps, once-ever boss intros) keeps them
  special. Rejections carry no `event` key + a say-nothing instruction.
  `DMState.weather` rides the state event as a persistent scene modifier.
- **Turn integrity** (`src/web/server.py` play_sse, `dialogue_markup.py`,
  `turn_filters.py`, `turn_audit.py`): the reply stream runs CodeFenceFilter
  → DialogueMarkupFilter (ordered `text` / `speech` / `command` segments;
  commands applied as their line ends, with live state/quest/combat events)
  → JunkLineFilter → MetaSentenceFilter (drops the model's planning and
  bookkeeping: "Let me…", "the player", ids) → DialogueDeduper →
  SentenceDeduper. The post-turn audit diffs narration claims (quests,
  damage, items, arrivals, consequences, consumables drunk without `@use`,
  quoted dialogue inside prose, named speakers who are not where the player
  is) against the commands and tools that actually succeeded, attaches
  warnings to `complete`, and injects them into the next turn as a
  `[REALITY AUDIT]` block. Unspoken quoted NPC lines still become
  `synthesized` speech events. Stored reasoning blocks are stripped from
  history on load, so the DM model is a config knob
  (`FORGE_MODEL_<AGENT>=provider/model` overrides per process).
- **Combat continuity**: a `combat_start`/`cinematic` that arrives while the
  client is loading a new scene is queued and fired when the scene is
  playable; the server remembers the last unanswered roster and re-issues
  it on the next turn instead of running the DM.
- **Routes**: `move_player(requirements_met=True)` records the player on
  `Connection.cleared_for`; `check_route` treats that road as open for them
  both ways, so the canvas walks it without another DM turn. Every move
  (tool or canvas) lands the player just inside the door they came through.
  Doors slide along their wall to open floor (`scene_exits._slide_clear`).
- **World sim**: `sim_move_npc` refuses moves onto a player's location or of
  any campaign-staged NPC; the opening day is claimed but never simulated.
- **Campaign author**: stale `pending` rows (a restart mid-generation)
  regenerate after 5 min; model JSON is repaired (fences, comments, trailing
  commas, truncation, wrong closers) before failing, raw replies saved under
  `data/debug/`. Beats carry a player-facing `lead`/`lead_target`.
- **Loot** (`src/services/loot.py`): a death (`kill_npc`, `apply_combat_result`)
  leaves the NPC's `inventory_notable` + a tier coin drop
  (`progression.TIER_COIN`/`coin_drop`) on the location as a `body` scene
  object where they fell (`defeated_positions` from the client). The player
  searches it on the canvas (`[E] Search`, `/api/interact`); items keep their
  mechanics (`_normalize_loot_item` round-trips the Item model). Bodies live in
  their own pool (`MAX_BODIES_PER_LOCATION`, oldest evicted) and can only enter
  with `src == "engine"` through `normalize_objects(trusted=True)`; every LLM/
  editor write is `trusted=False` and cannot author a body or `items`/`coin`.
  Ledger kind `loot`; the combat-result response carries `bodies` so the
  client sparkles them without a reload.
- **Rest** (`src/services/rest.py`): `Location.rest` (`{kind: inn|camp, price,
  quality}`, NULL = nowhere safe; a `rest` scene object with no spec is a
  makeshift camp). `rest_now` charges first, heals by quality
  (`progression.REST_QUALITY` steps up the health ladder), moves the clock to
  `MORNING_HOUR`, ledgers a "DAY N" title card (source `auto`), and writes a
  `Rested` ledger line ordering the DM to open on the morning. Three doors,
  one service: `POST /api/rest`, the `rest` DM tool, `[E] Rest` on a bed.
  Deliberately no campaign/world tick inside — both run in the next turn's
  context build; the day tick is backgrounded by the endpoint.
- **Shop** (`GET /api/shop/{npc_id}`, `POST .../buy|sell`, Trade button + HUD
  panel, "Trade with X" chip): merchant stock priced through
  `standing.price_for`; selling pays `sell_price` (buyback fraction) — the
  old `transfer_item` never paid. `NPC.currency` NULL = bottomless till.
  Equipped and quest items are never sold. Ledger kind `shop`.
- **Standing** (`src/services/standing.py`, the ONLY copy of every threshold):
  reputation bands (honoured…hostile) × NPC disposition drive
  `price_multiplier` (clamped); `check_route` refuses a location whose
  `controlling_faction_id` hates the player (`reason: "standing"`, precedence
  route > gate > dress > standing, never recorded as a cleared road);
  `prompt_npc_agent` returns a code-authored, voiced refusal (no model call)
  when the NPC is hostile and trust < `REFUSAL_TRUST`; `reveal_secret` (now in
  `NPC_TOOLS`) needs `SECRET_TRUST`.
- **Party** (`src/services/party.py`): `Player.party_members` is now moved
  by the engine. `join_party`/`leave_party` are shared by the DM and the NPC
  agent (`NPC_TOOLS`); joining is gated in code (alive, present, room under
  `MAX_PARTY`, and `standing.join_refusal` — hostile never, warm always,
  else `JOIN_TRUST`). `move_player` carries members through the same door in
  the same transaction (`follow` + `settle` beside the arrival door);
  `sim_move_npc` refuses members, the DM's `move_npc` elsewhere parts them,
  a campaign `move_npc` op parts them, and `sync` (run in `_build_context`)
  drops the dead/moved with a `Party change:` context line. `start_combat`
  refuses a member as an enemy and adds `allies` (fit members at their OWN
  tier) to the roster; the client fights them (`_updateAllies`, enemies pick
  targets through `_enemyTarget` so a solo fight is unchanged); a downed
  companion is `NPC.condition = "wounded"` (never killed by code), reported
  via `downed_allies` on `/api/combat/result`, excluded from fights until
  `rest_now` heals the party. Scene rows carry `companion`/`condition`;
  `state.party` drives the HUD chip/panel and the canvas follow + ring;
  a "come along" chip appears when the gate would say yes.
- **Stalled beats**: a fired beat the DM fails to narrate three times becomes
  `stalled` (never silently delivered): it stops blocking, its lead goes on
  the tracker, it is delivered when its keywords or `met_condition` show up,
  and after `STALLED_SKIP_DAYS` it is `skipped` (un-seals nothing) so the act
  can close.

### World Mutation

The DM changes the world only through `@command` lines (see above), which
`src/services/commands.py` maps onto the engine functions:
- `src/tools/world_write/` - Mutate world state (move player, advance time, etc.);
  `creation.py` holds `create_location`/`create_npc` (refuse duplicates)
- `src/tools/world_read/` - Read helpers (the scene brief and REST endpoints use them)
- `src/tools/narration.py`, `src/tools/agents_as_tools.py` - legacy (the CLI/world
  forge paths; no longer given to the DM)

### Data Models

SQLAlchemy models in `src/models/`:
- **WorldBible** - World lore, genre, tone, visual style, PC guidelines
- **Player/NPC** - Character state with NPC tiers (MAJOR/MINOR/AMBIENT)
- **Location** - Hierarchical locations with connection graph
- **Connection** - Travel routes between locations
- **Faction** - Factions with relationship tracking
- **WorldClock** - Game time simulation
- **NPCRelationship** - Player-NPC memory (trust, secrets, mood, messages)
- **Quest** - Quest definitions and tracking (+ `scale` XP label)
- **Campaign/StoryBeat** - Per-PC authored story spine (fixed truths, acts,
  deterministic beat triggers) — no write tool exists for these
- **Cinematic** - Cinematic rarity ledger + deferred queue
- Player carries `level`/`xp`/`equipment`/`quick_slot_item_id`; NPC carries
  `danger_tier`/`boss_title`; Location carries `objects`/`ambient_barks`;
  DMState carries `weather` — all nullable, NULL plays as the legacy game

No migration tool — schema evolution uses nullable columns with `DEFAULT NULL` so SQLite auto-adds them to existing DBs.

### Services Layer

`src/services/` handles asset generation and caching:
- **AssetManager** (`asset_manager.py`) - Orchestrates sprite/portrait/background generation with DB-backed caching. Entry point for all asset requests.
- **ImageGenerator** (`image_generator.py`) - Generates sprites, portraits, and location backgrounds via LLM image APIs. Filmstrips are sliced by ink blobs, not a fixed grid (`_erase_lines` → `_cells_from_blobs`: touching poses split at their thinnest column, rows read row-major so a 2×3 grid is fine, debris attached to the nearest pose, foreign pixels painted out; grid slicing is the last resort and flags wall-touching cells). Raw strips are kept under `debug/strips/`. Animals get a four-legged attack/dodge choreography (`_is_beast`). The normalized canvas is as wide as its widest frame. Walk animations are 6-frame cycles generated as a single filmstrip per direction (front/back/right; left is mirrored from right — image models have a left-to-right walking bias and flip requested left-facing strips, so we generate the direction they're biased toward; the left idle is likewise a mirror of the right idle so it matches the mirrored walk frames) — one call per direction keeps frames style-consistent, then the strip is sliced (artifact-immune: full-span rows/columns like grid lines and letterbox cards are cleared before the content bbox), background-removed, validated (ghost/sliver cells are replaced by their phase partner; a mostly-broken strip regenerates once), and normalized to the idle sprite's character-height ratio and baseline with feet-centroid horizontal anchoring so frames render at identical size with feet planted. Combat action cycles (`generate_action_cycle`: attack/dodge, 6 keys, right-facing, mirrored client-side) reuse the same machinery but skip RIFE (attacks read through snap, not smoothness) and repair broken frames from the nearest neighbor instead of the phase partner (actions are arcs, not loops). Action frames generate lazily: a 404 on `{id}_attack1.png` triggers `ensure_action_sprites` in the background.
- **ReferenceImageSearch** (`reference_search.py`) - Searches Bing Images for reference photos of known-IP characters/locations. Uses entity-ID-based caching (`references/{entity_id}.png`). Controlled by `reference_search_query` field on NPC/Location models — `NULL` means no web search.
- **MusicGenerator** (`music_generator.py`) - Per-world mood palette (explore/tension/danger/somber/night/triumph) via the Suno API; style prompts built from the WorldBible. Cached as `music/{mood}.mp3`; `GET /api/assets/music/manifest` triggers background generation and reports status.
- **VoiceGenerator** (`voice_generator.py`) - NPC dialogue TTS: local Qwen3-TTS voice cloning when a reference clip exists at `voices/refs/{npc_id}.wav`, else ElevenLabs using `NPC.voice_id` (auto-assigned from the `audio.voice_pool` by tag-matching NPC descriptions). Lines cached as `voices/{sha1(npc|text|tone)}.mp3`.

Asset pipeline: `reference_search` finds web images → `image_generator` creates assets using reference + text description → `asset_manager` caches results to disk and DB.

Appearance variants (`src/services/appearance.py`): the player's sprite set is keyed by deterministic signatures over equipped gear — `body_sig` (armor → idles/walks/dodge, `sprites/variants/{sig}/`) and `action_sig` (armor+weapon pair → attack strip); `base`/`base-unarmed` resolve to the flat legacy dir with zero migration. `state.appearance` carries the WANTED signatures (+ `read_as`); readiness lives on `GET /api/assets/appearance/{player_id}` and the scene fetch (`serving_*` = complete on disk). The sprite endpoint takes `?a=<sig>` (strict when present, lenient serving-signature resolution when absent); equip/unequip pre-warm generation and the client swaps textures atomically per axis only when a whole set exists — never a half-dressed frame.

### Scene collision & canvas liveliness

- **Collision**: `Location.obstacles` holds polygons (normalized 0-100).
  Auto-detected from the background image at generation time via the vision
  model (`ImageGenerator.detect_obstacles`; boxes are reduced to bottom-slice
  footprints since characters walk behind the upper part of objects in the
  3/4 view). Re-runnable via `POST /api/world/locations/{id}/detect-obstacles`.
  In-game editor: Ctrl+E then C — click vertices, Enter closes, right-click
  deletes, G auto-detects. Frontend blocks movement by point-in-polygon.
  NOTE: `gemini-2.5-flash` is sunset (404); `vision_model` is `gemini-3.5-flash`.
- **Ambient NPC wandering** (client-side only, not persisted): alive NPCs
  stroll between walkable points near their DB position, pause near the
  player; drag-editing an NPC rebases its wander home.
- **Day/night tint**: the frontend tints the world container from the state
  event's `time_of_day` (evening amber, night blue, etc.).
- **Real-time combat**: DM-initiated via `start_combat` → `combat_start` SSE
  → `GameRenderer.startCombat` spawns present NPCs as combatants using their
  real sprites (ambient bodies hidden meanwhile), driven by the fixed
  archetype table `GameRenderer.ARCHETYPES` (brute/skirmisher/stalker;
  stalkers circle before committing). Player combat HP derives from health
  status — wounded characters fight weaker. Space = dodge-roll with i-frames,
  J = melee arc; chase → telegraph → lunge → recover AI; hitstop, knockback,
  screenshake, exits lock during combat. Outcome → `combat-ended` event →
  `POST /api/combat/result` (code applies health/deaths) → auto-sent
  `[COMBAT OUTCOME]` turn for the aftermath. Ctrl+K still spawns a gray-box
  test enemy (client-only, resets on defeat).

### Audio / cinematic event flow

`speak()` and `describe_location()` return an `"event"` key in their tool-result
dicts; `ToolUsageTracker._parse_tool_output_for_events` (same mechanism as
`npc_death`) converts these into structured SSE events: `speech`
(npc_name/text/tone/action, enriched server-side with npc_id + a TTS
`audio_id`), `scene` (location/time_of_day/atmosphere), quest lifecycle
events (`quest_activated`/`quest_completed`/`quest_failed`),
`combat_start`, `check_result` (outcome label, never numbers), `xp_gain` +
`level_up` (fan-out of any tool result carrying a `progression` block —
order: quest_completed → xp_gain → level_up), `cinematic` (a nested
`cinematic` key on ANY tool result emits BEFORE the tool's own event, so a
boss intro precedes `combat_start`), and `campaign_beat`/`campaign_act`/
`campaign_resolved` (drained server-side around the stream). `state` events
stream mid-turn after every successful world-write tool AND post-turn,
carrying `tension` plus level/xp/equipment/`combat` stats/`campaign`
summary/`weather`. Combat XP rides the `/api/combat/result` REST response,
never SSE. The frontend `ForgeAudio`
class (`static/js/audio.js`) maps tension/time/death to a music mood,
crossfades palette tracks, plays a one-shot triumph stinger on quest
completion (reverts to the world mood; danger interrupts), and plays voice
clips sequentially (polling `/api/assets/voice/{audio_id}`), ducking music
during speech and dispatching `forge-voice-start/end` for speech-bubble sync.
Combat SFX: seven pre-generated one-shots (ElevenLabs sound-generation,
committed under `static/sfx/`) play via `ForgeAudio.playSfx` (WebAudio —
overlapping plays, pitch jitter, per-name throttle, "Effects" volume slider);
game.js triggers swing/hit/hurt/roll/death/windup/clash from the combat loop.
Canvas feedback: state diffs spawn floating text over the player
(gold/health/items), speech events show bubbles above NPC sprites, and
`prompt_npc_agent` tool pills carry `npc_id` so the chat shows "<NPC>
considers…" with their portrait during the slow sub-agent hop. Resuming a
world with history shows a "Previously…" recap (`GET /api/recap/{player_id}`,
fast model).

### Data Storage

- **Databases**: `data/{world_name}.db` (SQLite)
- **Assets**: `data/assets/{world_name}/` with subdirectories:
  - `sprites/` - Character sprites (`{id}_{direction}.png`, `{id}_{direction}_walk{frame}.png`)
  - `portraits/` - NPC dialogue portraits
  - `locations/` - Location background images
  - `references/` - Reference images keyed by entity ID (`{entity_id}.png`, `.miss` markers for failed searches)

### Web API

`src/web/server.py` is a FastAPI server with:
- **Game endpoints** - SSE-streamed play (`/api/play`), session info, chat history
- **Canvas play** - `POST /api/travel` + `POST /api/interact` (code-resolved,
  no DM turn; responses embed the full state), `GET /api/barks/{location_id}`
- **Progression** - `POST /api/player/{id}/equip|unequip|quick-slot|use-consumable`
- **Campaign/cinematics** - `GET /api/campaign/{player_id}` (spoiler-safe;
  `?spoilers=1` debug), `POST .../generate`, `GET /api/cinematics/recent`
- **Asset endpoints** - On-demand sprite/portrait/background generation, reference image upload/get/delete
- **World CRUD** - Full REST API for all entity types (NPCs, locations, factions, quests, connections, players, world bible)
- **World management** - List/select/create worlds, WorldForge streaming generation
- **Canvas services** - `POST /api/rest`, `GET/POST /api/shop/{npc_id}[/buy|/sell]`, `POST /api/opening/{player_id}`
- **Frontend** - Single-file SPA at `src/web/static/index.html`

### Key Patterns

1. **Lazy Generation**: NPCs and locations created on-demand when players explore
2. **Persistent NPC Memory**: Uses `SemanticSummarizingConversationManager` for compressed conversation history
3. **Time Costs**: Actions consume game time (configured in `config/settings.yaml`)
4. **Database Sessions**: Always use `get_session()` context manager

## Configuration

- `config/agents.yaml` - LLM models per agent (format: `provider/model-name`)
- `config/settings.yaml` - Game settings, time costs, database path

## Entry Points

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point with subcommands |
| `src/game/session.py` | Game loop, player creation |
| `src/agents/dm_orchestrator.py` | Main DM agent |
| `src/web/server.py` | FastAPI web server |

## Key Dependencies

- `strands-agents[litellm]` - Multi-agent orchestration
- `sqlalchemy` - Database ORM
- `rich` - CLI formatting
- `fastapi` + `uvicorn` - Web frontend
- `sentence-transformers` - Semantic embeddings for NPC memory
