"""Playtest world seed: Emberfall.

A small hand-built world where every recently-landed system has a designed
test surface:

- Music palette (Suno)      -> WorldBible genre/tone drives distinct mood styles
- NPC TTS voices            -> five NPCs spanning the voice-pool tag space
                               (old gruff male, warm middle female, bright young
                               female, friendly young male, menacing male)
- Obstacle auto-detection   -> Market Square (stalls/crates/well) outdoors,
                               The Drowned Lantern (tables/barrels/hearth) indoors
- Walk-cycle filmstrips     -> no Player seeded; character creation is prefilled
                               from pc_suggested_* and generates fresh sprites
- NPC wandering             -> three NPCs share the starting scene
- Day/night tint            -> clock starts at 17:30 (evening amber); night at 21:00
- Scheduled events          -> "The Evening Howl" fires at 20:00 on day 1
- Tension / danger music    -> Wolf's Hollow + the Ashen Fangs arc in DMState
- Connection requirements   -> the North Gate route is locked behind a gate pass
- Quest activation/rewards  -> "Teeth on the North Road" (NOT_STARTED, has
                               currency/item/reputation rewards)
- Combat gray-box           -> Ctrl+K anywhere; Vex Marrow at Wolf's Hollow is
                               the natural in-fiction fight

Run with: uv run main.py playtest   (creates data/emberfall.db)
"""

from src.models import (
    Connection,
    DMState,
    Event,
    Faction,
    Location,
    LocationType,
    NPC,
    NPCTier,
    Quest,
    QuestStatus,
    WorldBible,
    WorldClock,
    get_session,
    init_db,
)

PLAYTEST_DB_PATH = "data/emberfall.db"


def create_playtest_world(db_path: str = PLAYTEST_DB_PATH) -> None:
    """Create the Emberfall playtest world.

    Args:
        db_path: Path to the database file.
    """
    init_db(db_path)

    with get_session() as session:
        if session.query(WorldBible).first():
            print("Playtest world already exists. Delete the db file to reseed.")
            return

        # ----- World bible -------------------------------------------------
        bible = WorldBible(
            name="Emberfall",
            tagline="Warm hearths against a cold dusk.",
            genre="fantasy",
            sub_genres=["dark folk tale", "frontier"],
            tone="Cozy but with teeth — lantern-lit warmth pressed thin against "
                 "a wild, listening dark. Danger is real but human-scaled.",
            themes=["community under strain", "what the dark hides", "small courage"],
            time_period="A generation after the old kingdom's roads stopped being patrolled.",
            setting_description=(
                "Emberfall is the last market town before the northern hills. "
                "Everything south arrives by cart along one road, and lately the "
                "carts arrive late, light, or not at all. At dusk the town lights "
                "its lanterns all at once — a tradition, and lately a precaution."
            ),
            technology_level="Medieval: steel, oil lanterns, cart trade. No gunpowder.",
            magic_system="Folk magic only — charms, wardings, and half-true remedies. No fireballs.",
            rules=[
                "Wolves do not behave like this naturally; someone is behind it",
                "The town gates close after the evening howl",
                "Coin is scarce; barter is common",
                "No high magic, no monsters bigger than a bear",
            ],
            current_situation=(
                "Wolves have grown bold and strange around the north road. The "
                "weekly supply cart from Greywater is a day late. The Lantern "
                "Watch is stretched thin and Captain Coldiron is hiding how bad "
                "it really is."
            ),
            major_conflicts=["The Ashen Fangs are strangling the north road, using trained wolves as cover"],
            faction_overview=(
                "The Lantern Watch keeps the peace with too few people. The "
                "Ashen Fangs, a bandit pack in the hills, run with wolves that "
                "answer to whistles."
            ),
            narration_style="Third person, close and sensory. Firelight, cold air, small sounds.",
            dialogue_style="Plain and regional for townsfolk; clipped for the Watch; unhurried menace for the Fangs.",
            violence_level="moderate",
            excluded_elements=["high magic", "undead", "royalty plots", "time travel"],
            naming_conventions={
                "humans": "Earthy English-adjacent (Brann, Maera, Sorrel)",
                "places": "Compound nature words (Emberfall, Greywater, Wolf's Hollow)",
            },
            visual_style=(
                "Painterly storybook realism. Warm amber lantern light against "
                "deep blue dusk. Visible brushwork, soft edges, strong silhouettes."
            ),
            color_palette=["lantern amber", "dusk blue", "birch white", "ash gray", "ember red"],
            pc_guidelines="An outsider with a mundane reason to be here and no special powers.",
            pc_starting_situation=(
                "You are a courier. You arrived at dusk with one letter left to "
                "deliver — addressed to a man who, you are told, was buried three "
                "weeks ago. Your return cart doesn't leave until the Greywater "
                "cart arrives, and it is late."
            ),
            pc_suggested_name="Rook Fennick",
            pc_suggested_description=(
                "A lean road-worn courier in a patched slate-blue traveling coat, "
                "mud-spattered boots, a leather satchel on a cross-body strap, and "
                "short dark hair flattened by weather. Early twenties, quick eyes, "
                "a paper-cut scar on one thumb."
            ),
            pc_suggested_background=(
                "Fennicks carry letters; that's the family line and the family "
                "curse. Rook took the northern circuit because it paid double, "
                "and nobody mentioned why."
            ),
        )
        session.add(bible)

        # ----- Factions ----------------------------------------------------
        watch = Faction(
            name="The Lantern Watch",
            ideology="Keep the lanterns lit and the road open",
            methods=["patrols", "gate control", "volunteer musters"],
            aesthetic="Boiled leather, brass lantern badges, oil-stained gloves",
            power_level=40,
            resources={"economic": 20, "influence": 55, "military": 35},
            goals_short=["Keep the north road open", "Find out what changed the wolves"],
            goals_long=["Restore the Greywater cart schedule"],
            leadership={"leader_name": "Captain Brann Coldiron", "structure_type": "captaincy"},
            secrets=["The Watch is down to eleven able bodies; the roster board lists twenty"],
        )
        session.add(watch)

        fangs = Faction(
            name="The Ashen Fangs",
            ideology="The road pays or the road closes",
            methods=["wolf-handling", "cart raids", "intimidation", "a fence in town"],
            aesthetic="Ash-gray cloaks, bone whistles, wolf-tooth trophies",
            power_level=45,
            resources={"economic": 30, "influence": 15, "military": 50},
            goals_short=["Choke the Greywater road until the town pays tribute"],
            goals_long=["Unknown — the Fangs are being paid in minted city silver by someone"],
            leadership={"leader_name": "Unknown — spoken of only as 'the Whistler'", "structure_type": "pack"},
            secrets=[
                "The wolves are trained; they answer to bone whistles",
                "The pack is funded by minted silver from a Greywater counting-house",
            ],
        )
        session.add(fangs)
        session.flush()

        # ----- Locations ---------------------------------------------------
        town = Location(
            name="Emberfall",
            type=LocationType.TOWN,
            depth=0,
            description=(
                "A palisaded market town at the foot of the northern hills, "
                "known for lighting every lantern in town at the same moment "
                "each dusk."
            ),
            atmosphere_tags=["close-knit", "wary", "lantern-lit"],
            economic_function="trade_hub",
            population_level="moderate",
            current_state="peaceful",
            controlling_faction_id=watch.id,
            genre_type="fantasy",
            is_map_container=True,
        )
        session.add(town)
        session.flush()

        # Start scene. Description is deliberately full of discrete, footprintable
        # objects so obstacle auto-detection has real work to do.
        square = Location(
            name="Market Square",
            type=LocationType.DISTRICT,
            parent_id=town.id,
            depth=1,
            position_x=50,
            position_y=50,
            description=(
                "Emberfall's cobbled market square at dusk. A round stone well "
                "with a peaked wooden roof stands at the center. Market stalls "
                "with canvas awnings line the edges, their tables still laid with "
                "root vegetables and wool. Stacked crates and barrels crowd the "
                "gaps between stalls. A notice board stands near the north side, "
                "and iron lantern posts are being lit one by one as the light "
                "goes blue."
            ),
            atmosphere_tags=["dusky", "busy", "lantern-lit", "watchful"],
            economic_function="trade",
            population_level="moderate",
            current_state="peaceful",
            controlling_faction_id=watch.id,
            genre_type="fantasy",
            visited=True,
            discovered=True,
        )
        session.add(square)
        session.flush()

        tavern = Location(
            name="The Drowned Lantern",
            type=LocationType.BUILDING,
            parent_id=square.id,
            depth=2,
            position_x=28,
            position_y=58,
            description=(
                "A low-beamed tavern interior lit by a broad stone hearth. Long "
                "trestle tables with benches fill the middle of the room, ale "
                "barrels are racked along the back wall, and a heavy oak bar "
                "counter runs down one side. The sign outside shows a lantern "
                "underwater, still lit — nobody agrees on what that means."
            ),
            atmosphere_tags=["warm", "smoky", "talkative", "safe"],
            economic_function="entertainment",
            current_state="peaceful",
            genre_type="fantasy",
            discovered=True,
        )
        session.add(tavern)
        session.flush()

        hollow = Location(
            name="Wolf's Hollow",
            type=LocationType.POI,
            parent_id=town.id,
            depth=1,
            position_x=52,
            position_y=18,
            description=(
                "A birch hollow half a mile up the north road, white trunks "
                "crowding around a ruined shepherd's hut with a collapsed roof. "
                "Gnawed bones are scattered near a cold fire ring, and the "
                "carcass of a supply crate lies split open in the bracken. It is "
                "very quiet, in the way a room is quiet when something in it is "
                "holding its breath."
            ),
            atmosphere_tags=["dangerous", "silent", "cold", "watched"],
            current_state="peaceful",
            genre_type="fantasy",
            discovered=True,
        )
        session.add(hollow)
        session.flush()

        # ----- Connections ---------------------------------------------------
        session.add(Connection(
            from_location_id=square.id,
            to_location_id=tavern.id,
            travel_type="walk",
            travel_time_hours=0.1,
            description="The tavern door opens straight onto the square.",
            bidirectional=True,
            discovered=True,
        ))

        # Requirements-locked route: tests move_player blocking until the DM
        # confirms requirements_met=True (Brann can issue a gate pass).
        session.add(Connection(
            from_location_id=square.id,
            to_location_id=hollow.id,
            travel_type="walk",
            travel_time_hours=0.75,
            description=(
                "The north road out of town, through the North Gate and up into "
                "the birches. The gate is barred after the evening howl."
            ),
            requirements=[
                "The North Gate must be open, or the player needs a gate pass "
                "from Captain Brann Coldiron (gates are barred after the evening howl)"
            ],
            bidirectional=True,
            discovered=True,
        ))

        # ----- NPCs ----------------------------------------------------------
        # Descriptions deliberately carry the gender/age/quality words the
        # voice-pool auto-assignment matches on (old gruff / warm refined /
        # young bright / young friendly / menacing).
        brann = NPC(
            name="Captain Brann Coldiron",
            tier=NPCTier.MAJOR,
            species="human",
            age=61,
            profession="Captain of the Lantern Watch",
            faction_id=watch.id,
            current_location_id=square.id,
            home_location_id=square.id,
            position_x=68,
            position_y=42,
            description_physical=(
                "An old, weathered veteran with a gray beard cropped short, a "
                "rough scar through one white eyebrow, and a brass lantern badge "
                "on a boiled-leather coat. He stands like his knees hurt and his "
                "spine refuses to admit it."
            ),
            description_personality=(
                "Gruff, deliberate, and honest right up to the line where honesty "
                "would cause panic. Counts his people twice. Hates owing favors."
            ),
            voice_pattern=(
                "Low, gravelly, unhurried. Short declarative sentences. Calls "
                "everyone 'courier', 'keeper', by trade rather than name until "
                "they've earned otherwise. 'Road's shut. Ask me why.'"
            ),
            goals=[
                "Keep the north road open without losing anyone",
                "Find out what changed the wolves before the town finds out how bad it is",
            ],
            secrets=[
                "The Watch roster is a lie — eleven able bodies, not twenty",
                "He found a bone whistle near the last raided cart and told no one",
            ],
            current_mood="grim",
        )
        session.add(brann)

        maera = NPC(
            name="Maera Duskwell",
            tier=NPCTier.MINOR,
            species="human",
            age=49,
            profession="Keeper of The Drowned Lantern",
            current_location_id=tavern.id,
            home_location_id=tavern.id,
            position_x=48,
            position_y=44,
            description_physical=(
                "A middle-aged woman with a warm, smooth manner, dark hair "
                "pinned with a copper clasp, and a refined way of pouring that "
                "suggests she once kept a finer house than this one."
            ),
            description_personality=(
                "Gracious, noble in bearing, and quietly transactional. Every "
                "kindness is remembered in a ledger only she can read."
            ),
            voice_pattern=(
                "Warm and level, never raised. Answers questions with better "
                "questions. 'And who told you that, love?'"
            ),
            goals=["Keep the tavern full and the peace unbroken under her roof"],
            secrets=[
                "She buys 'wolf-taken' goods cheap from a fence for the Ashen Fangs",
                "The dead man the player's letter is addressed to drank here his last night",
            ],
            current_mood="composed",
        )
        session.add(maera)

        piper = NPC(
            name="Piper Quillow",
            tier=NPCTier.MINOR,
            species="human",
            age=19,
            profession="Herbalist's apprentice",
            current_location_id=square.id,
            home_location_id=square.id,
            position_x=30,
            position_y=60,
            description_physical=(
                "A young woman with bright, emotional eyes, chapped hands "
                "stained green at the fingertips, and a basket of sweetroot and "
                "dried yarrow on one hip."
            ),
            description_personality=(
                "Talks too fast, notices everything, chews sweetroot when "
                "nervous — which is most of the time lately."
            ),
            voice_pattern=(
                "Quick and breathless, sentences that outrun themselves. 'The "
                "howls are wrong — not hungry-wrong, drilled-wrong, like they're "
                "answering something—'"
            ),
            goals=["Get someone with authority to listen to her about the howls"],
            secrets=["She has been charting the howls by night and direction; the pattern repeats every third night"],
            current_mood="anxious",
        )
        session.add(piper)

        sorrel = NPC(
            name="Sorrel Tack",
            tier=NPCTier.AMBIENT,
            species="human",
            age=16,
            profession="Stable hand",
            current_location_id=square.id,
            home_location_id=square.id,
            position_x=55,
            position_y=72,
            description_physical=(
                "A friendly young man all elbows and straw-colored hair, warm "
                "brown eyes, a halter rope coiled over one shoulder."
            ),
            description_personality=(
                "Cheerful, guileless, names every horse after weather. Wants "
                "desperately to be taken seriously by the Watch."
            ),
            voice_pattern="Eager and open. 'Mister! Hey, mister — you came up the north road, didn't you?'",
            goals=["Join the Lantern Watch when he's of age"],
            secrets=["He saw a man in an ash-gray cloak crouched by the north wall two nights ago, and told no one because no one asks Sorrel"],
            current_mood="excited",
        )
        session.add(sorrel)

        vex = NPC(
            name="Vex Marrow",
            tier=NPCTier.MINOR,
            species="human",
            age=34,
            profession="Ashen Fangs scout",
            faction_id=fangs.id,
            current_location_id=hollow.id,
            home_location_id=hollow.id,
            position_x=58,
            position_y=48,
            description_physical=(
                "A lean, menacing man in an ash-gray cloak, a necklace of wolf "
                "teeth, and a bone whistle on a cord. Moves like he's already "
                "decided where you'll fall."
            ),
            description_personality=(
                "Intense, patient, professionally cruel. Talks to buy time for "
                "the wolves to circle. Not loyal to the Fangs — loyal to the pay."
            ),
            voice_pattern=(
                "Soft, amused menace. Never hurried. 'Long way from the "
                "lanterns, courier.'"
            ),
            goals=["Keep travelers off the north road", "Get paid"],
            secrets=["His pay is minted Greywater silver — he can name the counting-house on it"],
            current_mood="watchful",
        )
        session.add(vex)
        session.flush()

        # ----- Quest (NOT_STARTED: tests activate_quest + reward application) --
        session.add(Quest(
            title="Teeth on the North Road",
            description=(
                "Captain Coldiron wants eyes he can spare: walk up to Wolf's "
                "Hollow, find out what has the wolves acting drilled instead of "
                "wild, and bring back proof before the next Greywater cart is due."
            ),
            status=QuestStatus.NOT_STARTED,
            objectives=[
                "Get Captain Coldiron's gate pass for the north road",
                "Find out what is organizing the wolves at Wolf's Hollow",
                "Bring proof back to Captain Coldiron",
            ],
            rewards={
                "currency": 60,
                "items": ["Wolf-Tooth Charm"],
                "reputation": {watch.id: 10},
            },
            assigned_by_npc_id=brann.id,
        ))

        # ----- Scheduled events (fire deterministically on the world tick) ----
        session.add(Event(
            name="The Evening Howl",
            description=(
                "At the stroke of eight, howling rises from the northern hills — "
                "layered, answering itself, closer than last night. The North "
                "Gate is barred early and the square empties fast."
            ),
            event_type="meso",
            scheduled_day=1,
            scheduled_hour=20,
            factions_involved=[fangs.id],
            locations_involved=[square.id, hollow.id],
            npcs_involved=[vex.id],
            consequences=["North Gate barred for the night", "Townsfolk clear the square"],
            player_visible=True,
        ))

        session.add(Event(
            name="The Greywater Cart Arrives Short",
            description=(
                "The overdue supply cart finally limps through the South Gate "
                "half-empty, one horse lathered and lame, the driver repeating "
                "that the birches on the north road had eyes in them."
            ),
            event_type="meso",
            scheduled_day=2,
            scheduled_hour=9,
            factions_involved=[fangs.id, watch.id],
            locations_involved=[square.id],
            consequences=["Flour and lamp oil ration talk begins", "Pressure on the Watch to act"],
            player_visible=True,
        ))

        # ----- Clock & DM state ------------------------------------------------
        # 17:30 = evening (amber tint immediately); night tint lands at 21:00.
        # The Evening Howl is 2.5 game-hours out — a few conversations away.
        session.add(WorldClock(day=1, hour=17, minute=30))

        session.add(DMState(
            current_arc=(
                "Someone is using trained wolves to strangle the north road. "
                "The player's undeliverable letter is the loose thread."
            ),
            planned_beats=[
                "The letter's recipient is dead — Maera knew him and deflects",
                "Coldiron offers the Wolf's Hollow job (activate the seeded quest)",
                "The Evening Howl fires at 20:00 and empties the square",
                "A gate pass unlocks the north road connection",
                "Vex Marrow at the Hollow — talk, or the wolves circle",
            ],
            active_threats=["Ashen Fangs choking the north road with trained wolves"],
            world_pressures=["Town supplies thin if carts keep failing", "Watch is at half strength and hiding it"],
            tension="low",
        ))

        session.commit()

        print("Emberfall playtest world created.")
        print()
        print("Playtest checklist (systems -> where to poke them):")
        print("  Music palette   First click in-world starts Suno generation; tracks fade in (2-5 min each).")
        print("  Day/night tint  It's 17:30 — evening amber now, night blue after 21:00.")
        print("  Walk cycles     Character creation is prefilled (Rook Fennick); sprites + 6-frame strips generate.")
        print("  Obstacles       Market Square bg auto-detects stalls/crates/well; Ctrl+E, C to inspect, G to redo.")
        print("  NPC wandering   Brann, Piper, and Sorrel share the square; watch them stroll and pause near you.")
        print("  TTS voices      Talk to Brann (old gruff) then Piper (young bright) — distinct voices, music ducks.")
        print("  Scheduled event 'The Evening Howl' fires at 20:00 day 1 (2.5 game-hours out).")
        print("  Locked travel   North road to Wolf's Hollow needs a gate pass from Brann.")
        print("  Quest flow      'Teeth on the North Road' is seeded NOT_STARTED with rewards; Brann offers it.")
        print("  Combat          Ctrl+K anywhere for the gray-box; Vex Marrow at the Hollow is the story fight.")
