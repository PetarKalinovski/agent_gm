# Handoff — Forge DM re-architecture and the playtest loop

Date: 2026-10-05. Committed on branch `hybrid-dm-playtest-loop` (not yet merged into `master`): 66 modified files and 85 new ones, about +19.7k / −1.5k lines in tracked files plus the new modules. Everything under `data/` (playtest DBs, transcripts, logs) is git-ignored.

Final full suite: **3006 passed, 0 failed** (`uv run pytest tests -q -p no:cacheprovider --ignore=tests/js`, about 10 min).

---

## 1. What was asked, and the verdict

The original ask was to make the game a fully fledged experience with a strong gameplay loop, to play-test it (the AI included), and to answer an architecture question: were the sub-agents and tools over-built, and would a `claude -p` / markdown-files / skills design be better?

**Verdict: hybrid.** The user chose it ("Hybrid (Recommended)") over going fully tool-less with markdown as the source of truth.

- **One DM agent.** Claude Sonnet 5 via OpenRouter, reasoning off, prompt-cached (3 breakpoints).
- **One tool: `roll_check`.** Dice must land before the prose.
- **Everything else is inline in the streamed reply:**
  - prose, in second person;
  - speaker lines, `Name (tone): "words"`, which become a voiced bubble and portrait;
  - hidden `@command` lines that `src/services/commands.py` validates and applies (about 38 verbs).
- **The DM's rules are markdown:** `src/agents/prompts/dm.md` (about 39k chars) and `commands.md` (about 11k). `tests/test_dm_prompt_tools.py` keeps them consistent with the code: every taught verb exists, every verb is documented, and no seed names appear.
- **Context is assembled in code** (`dm_orchestrator._build_context` + `src/services/scene_brief.py`), so the DM needs no read tools.
- **Memory is markdown stored in the world's `.db`** (`MemoryDoc` rows, `src/services/memory_files.py`): per-NPC documents, chronicle, DM notes, journal and heard names. Backing up the `.db` backs up everything. `uv run main.py memory-export|memory-import --db <world>.db` round-trips them to files.
- **Mechanics stay in SQL tables.** Markdown as truth would have broken the HUD, combat, the shop and the gates.

**Measured, before → after:**

| | Before | After |
|---|---|---|
| Opening | 7.5 min | ~11–17 s |
| Typical turn | 38–85 s, 4–8 sequential model calls | median ~6–7 s, one call (two with a roll) |
| First text on screen | — | ~2.4–2.7 s |

Errored turns in the last three AI playtest rounds (63 turns each): 0.

---

## 2. How the game is evaluated (the loop used all session)

1. **AI playtest fleet.**
   - **Servers:** three sandbox servers on fresh seeded worlds:
     `FORGE_TTS=0 .venv/Scripts/python.exe scripts/sandbox_server.py --seed --db data/pt_XX.db --port 1210N`
   - **Players:** three `scripts/autoplay.py` runs (a cheap model with a persona plays 20 turns through `/api/play`; fights are resolved by script):
     - Bram, a hot-headed ex-soldier;
     - Tamsin, a pickpocket;
     - Wenna, a curious folklorist.
   - **Output:** `data/playtests/<run>/transcript.jsonl` and `report.md`.
   - **Command lines:** the exact launch lines are in the appendix.
2. **Critic workflow.** Six agents (3 sessions × a fun lens and a correctness lens) compare each session with the previous round and return scores, what got fixed, and evidence-backed issues.
3. **Fix workflow.** Five parallel workstreams, each an implementer followed by an adversarial reviewer. Every regex detector is replayed over every transcript before it ships.
4. **Lead pass.** Fold the reviewers' prompt suggestions into `dm.md` / `commands.md`, fix the leftover concerns, add a "The loop, round N" block to `CLAUDE.md`, and run the full suite.

**Critic fun scores (1–10), same three personas each round:**

| Round | Bram | Tamsin | Wenna | Notes |
|---|---|---|---|---|
| 1 | ~4.8 avg | | | "radio drama": 0 fights in 63 turns |
| 3 | 6.6 avg | | | every session finished a quest and reached level 2 |
| 4 | 6.5 | 6.5 | 6 | |
| 5 | 7 | 5 | 6.5 | |
| 6 | 4 | 6 | 7 | a clock bug barred the gate |
| 7 | 6.5 | 7 | 5 | phantom-proof loop |
| 8 | 4.5 | 6 | 6 | the world bible forced third person |
| 9 | 6.5 | 5.5 | 6 | correctness 6 / 6 / 6 |

**Honest read:** the floor rose a lot. Bram, Tamsin and Wenna now reliably reach the dungeon, fight, find the proof, and keep names, the ledger and point of view straight. But fun has plateaued around 6. Each round fixes the last round's problems and surfaces new structural ones, and the same persona swings 4↔7 between rounds, so single rounds are noisy.

---

## 3. What fix round 9 changed (not yet playtested)

Everything below is in the code and tested, but **no playtest has run on it yet**. Round 10 was deliberately not run.

- **Story spine** (`src/services/campaign.py`):
  - **Place binding.** A beat binds to the place its directive opens on ("At Wolf's Hollow, …"), or to the place whose authored Things its keywords name, and waits there. This fixes the act-1 discovery being told in the square as a false memory. New audit: `memory_of_unvisited_place`.
  - **Finds and entrances.** A find never retires a beat that brings on someone not yet met; it makes the beat due now, with THE FIND IS THE CUE and ENTRANCE notes. This fixes the antagonist who never appeared.
  - **"If asked … admits X" beats** count only the admission's words as delivery, and the player's question gets a binding `[THE PLAYER ASKED — … ADMITS IT NOW]` line.
  - **Spacing.** A scene at the player's place with its people present skips beat spacing.
  - **Story beasts.** `@fight` on a story beast before its scene fights a packmate stand-in, so the act-3 wolves survive.
  - **Offstage cast.** The villain's people (the successor, faction members never met) are OFFSTAGE until their first beat: not on the canvas, no chips, and audit `cast_before_their_scene`.
- **The world's clock** (`world_tick.py`, `stakes.py`, `complications.py`):
  - **Events at the player's feet.** An event firing where the player stands is "HAPPENING WHERE YOU STAND", also on combat-aftermath turns, and names the job step it answers. Denials and long-past mentions don't count as delivery (`told_at_hand`).
  - **Waiting clock.** It never contradicts a fresh consequence, and it pauses while the player works the story's own side job.
  - **Missing people.** The engine registers the missing person and leaves a findable trace.
  - **Complications.** A summons needs real news. Pushes hold while the player talks with their current quest step's person, which the lead tightened to "spoke in the last two replies".
  - **Watchers** are lookouts of the villain's faction, with world-style names.
  - **Distances.** Exits give travel time in words; new audit `distance_misstated`.
- **Agency:**
  - **No unchosen rolls.** `roll_check` refuses a roll on a danger this very reply introduced, so the player chooses first.
  - **Shown first.** A beast that meets a leaving player is shown first (`[DANGER ON THE WAY OUT`, no travel line that reply).
  - **Health after fights.** `[COMBAT OUTCOME]` always states health, and a worse `@health` in the aftermath is refused.
  - **Consumables.** `@use` needs the player's own reach.
  - **Odds.** Rolls get a 10% odds floor and an odds word below 40%.
  - **New roll triggers.** Bluffs and haggles are now detected.
  - **Look vs take.** `@interact` take needs a reach verb; otherwise it's a look.
  - **Lost gear.** Audit `lost_gear`.
- **Rewards:**
  - **Step XP.** Each met step pays a slice of the quest's XP; the total is unchanged (`Quest.step_xp_paid`).
  - **Trophies.** Beast bodies leave a trophy (fang, pelt) the merchant buys.
  - **Payout in the lead.** The lead shows what a job pays.
  - **Kindness.** A gift to a present person adds +5 trust, once per person per game-day, capped at 70, with a toast showing only heard names.
  - **Persona hook.** It's due on reply 3, then every 5th.
  - **No spoilers.** An unfound proof step shows as "Bring proof back to …" until the item is held.
- **Identity:**
  - **Borrowed names.** A speaker label that borrows a name in use goes out under the prose's role and never teleports story cast, merchants, or anyone seen elsewhere that day. This is the "Hesk the driver" bug.
  - **Walk-off.** It never falls back to the wrong pronoun.
  - **Departures** told after the fact now move the person.
  - **Invented suspects.** Audit `invented_suspect`, plus a brief line offering the authored stranger (`src/services/suspects.py`).
  - **Menus.** Closing menus are dropped.
  - **Journal.** Custom steps and evidence lines are journaled.
  - **Held roads.** A pass checked on the way is noted.
- **Prompt.** About 25 new rules for the above in `dm.md` and `commands.md`.

Rounds 4–8 are summarised in `CLAUDE.md` under "The loop, round 4 … round 9".

---

## 4. Decisions the user may want to revisit

1. **Gift trust is +5** (`standing.py`). One gift won't open a secret 10 points away; a won roll covers the rest. Raise it to 10 if one kindness should be enough.
2. **Sealed truths can still come out early at trust ≥ 60** through the dossier's "Would now confess" plus `@reveal`. If sealed truths should always wait for their scene, filter those secrets out of "Would now confess".
3. **The seed starts at 15:00.** A reviewer moved it from 14:00 so the 20:00 howl still lands in a 20-turn session that stays in town.
4. **Odds word.** Any roll below a 40% chance carries "against the odds" or "a long shot". At levels 1–3 that includes plain "hard" rolls, so the prose will say it often.
5. **Haggling.** A won haggle has no engine effect, because quest rewards are fixed in the DB; only the narration changes.
6. **Keep-in-place is strict.** A story-cast person voiced away from their DB spot is never auto-moved; the DB lags one turn until the DM writes `@npc-move`. This was chosen over the risk of teleporting a quest-giver.
7. **Defaults from earlier rounds:**
   - an encounter fights a weak stand-in from the pack, never the story's own cast;
   - the campaign author's `delay` deadlines are not honoured.

---

## 5. Known limitations and debt

- **Much per-player engine state lives only in process memory, and a server restart forgets it.** Affected:
  - the job offer book and credited work (`job_offers.py`);
  - persona-hook counters (`scene_brief._PERSONA_REPLIES`);
  - repeated-detail history (`voice_tics.py`);
  - seen-today and borrowed-label records (`dialogue._SEEN`, `_BORROWED`);
  - beats a find made due (`campaign._FOUND_DUE`);
  - cleared places (`world_tick._cleared`, `_gone_in`);
  - audit memories (`turn_audit._ARRIVED_LAST`, `_HURT_REPLIES`);
  - complication and encounter counters.

  This is fine for playtests and wrong for real saves. **This is the biggest piece of debt.** The fix is one nullable JSON column (or a small `EngineState` table) keyed by player, written at the end of each turn.
- **The detectors are regexes, replayed over about 600 transcript turns, all from one seed world** (Emberfall). They are precise on that corpus. They are very likely over-fitted to its vocabulary: wolves, howls, the gate pass, the Captain.
- **History reload differs from the live stream in a few places:**
  - a split long line shows as one bubble;
  - a borrowed speaker label resolves to the real person's name and portrait.
- **The NameGate counts a borrowed vocative** ("Say it plain, Hesk") as hearing the real person's name.
- **A night-edge scout** spawned while another complication is open is not caught by the "danger introduced this reply" refusal.
- **Places two or more hops away have no distance words in the brief**; only exits do. The audit catches errors after the fact.
- **Watchers** don't yet report back to change the world later.
- **Unfound-proof rewording** covers active quests only; a job still being offered shows its steps as written.
- **Prompt size.** `dm.md` grew from about 31k to 39k chars over rounds 7–9, and many rules are now also enforced in code. A pruning pass, dropping rules the engine already enforces, would cut tokens and contradictions.

---

## 6. Recommended next steps, in order

1. **Merge** `hybrid-dm-playtest-loop` into `master` (or open a PR) once reviewed.
2. **Playtest a freshly generated world** (WorldForge), not the seed. This is the real test of whether the engine generalises or has been tuned to Emberfall.
3. **Persist the in-memory engine state** (section 5).
4. **Human playtest in the browser.** The AI fleet never presses keys, so the canvas, combat, the shop panel, the toasts and the new people re-sync (`state.present_ids` → `game.syncNpcs`) have only been exercised by tests.
5. **Run round 10 on the seed** to measure fix round 9 (see the appendix).
6. **Replace the riskiest regex detectors with a cheap classifier call.** Candidates: intent to take, intent to leave, a reported departure, "the player asked about X". Possibly a single structured pass over the player's input each turn, which would make the detectors generalise to other worlds.
7. **Prune `dm.md`** of rules the engine now enforces.

---

## 7. Map of the new code

| Area | Files |
|---|---|
| The reply and its commands | `src/agents/dm_orchestrator.py`, `src/agents/prompts/dm.md`, `commands.md`, `src/services/commands.py`, `src/agents/turn_window.py` |
| Stream filters and audit | `src/web/dialogue_markup.py` (speaker lines, MetaSentenceFilter, second-person rewrite, menus), `src/web/turn_filters.py`, `src/web/turn_audit.py`, `src/web/server.py` (`play_sse`, engine fallbacks: walk, take, walk-along, walk-off) |
| Context | `src/services/scene_brief.py`, `src/services/memory_files.py`, `src/models/memory.py` |
| Names and people | `src/services/dialogue.py` (NameGate, speaker resolution, player-facing masking), `src/services/suspects.py`, `src/services/voice_tics.py` |
| Binding one-line directives | `roll_intent.py`, `travel_intent.py`, `job_offers.py`, `settle.py`, `intents.py` (all in `src/services/`) |
| Story and world pressure | `campaign.py`, `world_tick.py`, `stakes.py`, `complications.py`, `encounters.py`, `spawn.py` |
| Player progress | `progression.py`, `loot.py`, `lead.py`, `objectives.py`, `starting_kit.py` |
| Seed world | `src/data/playtest_seed.py` (Emberfall: the Captain, Piper, Sorrel, Hesk, Wolf's Hollow) |
| Playtest tooling | `scripts/autoplay.py`, `scripts/sandbox_server.py`, `scripts/browser_driver.py` / `browser_cmd.py` |
| Canvas | `src/web/static/js/game.js` (`syncNpcs`, `_addNpc`), `src/web/static/index.html` |

---

## Appendix — running a playtest round

Pick unused DB names (`pt_aa`–`pt_ac` were round 9). Free ports 12101–12103 first.

```bash
cd agent_gm
export FORGE_TTS=0
for pair in "12101 pt_ad" "12102 pt_ae" "12103 pt_af"; do set -- $pair
  nohup .venv/Scripts/python.exe scripts/sandbox_server.py --seed --db data/$2.db --port $1 \
    > data/pt_$1.out 2> data/pt_$1.err & done

.venv/Scripts/python.exe scripts/autoplay.py --base http://127.0.0.1:12101 --db data/pt_ad.db \
  --new "Bram Holloway" --looks "A broad, scarred man in his forties in a battered brigandine and a soldier's grey cloak, close-cropped grey hair, a crooked nose, knuckles wrapped in leather." \
  --background "Twenty years in a border company that was disbanded without pay. He came north for work and has no patience for officers." \
  --persona "Bram Holloway, a hot-headed ex-soldier who settles things with his fists, distrusts the Watch and anyone in a uniform, and picks fights he might not win. Blunt, sardonic, loyal to the downtrodden." \
  --turns 20 --out fleet11-bram > data/fleet11_bram.log 2>&1 &
.venv/Scripts/python.exe scripts/autoplay.py --base http://127.0.0.1:12102 --db data/pt_ae.db \
  --new "Tamsin Rook" --looks "A slight, quick woman in her twenties in a fitted green velvet doublet gone shabby at the cuffs, a black silk scarf, rings on every finger, dark curls pinned up with a knitting needle." \
  --background "A card-sharp and pickpocket run out of three cities. She came north because nobody here has heard of her yet." \
  --persona "Tamsin Rook, a charming liar and pickpocket who wants money above everything. Flirts, bluffs, steals when nobody is looking, sells what she steals, and talks her way out of trouble — or tries to." \
  --turns 20 --out fleet11-tamsin > data/fleet11_tamsin.log 2>&1 &

.venv/Scripts/python.exe scripts/autoplay.py --base http://127.0.0.1:12103 --db data/pt_af.db \
  --new "Wenna Cole" --looks "A tall, bookish woman in her fifties in a mustard wool coat with ink-stained cuffs, spectacles on a chain, grey hair in a loose bun, a leather satchel stuffed with notebooks." \
  --background "A collector of folk tales who walks from town to town writing down what people remember. Kind, nosy, and braver than she looks." \
  --persona "Wenna Cole, a curious collector of old stories who talks to everyone, asks about the past and local legends, gives gifts, tries odd things (feeding a wolf, visiting places that are not on the map, asking to see the cellar), and remembers everything people tell her." \
  --turns 20 --out fleet11-wenna > data/fleet11_wenna.log 2>&1 &
wait
```

**Critics:** for each run, give one agent the run's `report.md` plus `transcript.jsonl`, the previous round's `report.md` for the same persona, `dm.md` / `commands.md`, and a list of what changed. Ask for fun scores (before and now), what got fixed with evidence, and 4–10 issues with turn evidence and a fix idea. Run this once with a fun lens and once with a correctness lens.

The server logs (`data/pt_1210N.err`) hold the engine's decisions, which is useful evidence for the critics.
