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
- Day/night tint            -> clock starts at 15:00 (afternoon); evening amber at
                               17:00, night at 21:00
- Scheduled events          -> "The Evening Howl" fires at 20:00 on day 1
- Tension / danger music    -> Wolf's Hollow + the Ashen Fangs arc in DMState
- Connection requirements   -> the north road (North Gatehouse -> Wolf's Hollow)
                               is locked behind a gate pass
- Quest activation/rewards  -> "Teeth on the North Road" (NOT_STARTED, has
                               currency/trinket/reputation rewards; its proof is
                               the Hollow's whistle, delivered); two follow-on
                               jobs from Maera and Piper
- Combat gray-box           -> Ctrl+K anywhere; Vex Marrow at Wolf's Hollow is
                               the natural in-fiction fight
- Enemy tier ladder         -> harmless townsfolk, weak wolves, dangerous
                               named fighters (Vex/Brann), one authored boss
                               (Aldric Vane, "The Whistler of Wolf's Hollow")
- Canvas objects            -> 3 per location (read/take/open/search spread;
                               the tavern satchel is lock-gated to chat; its
                               letter carries its own text, `reads`)
- Places off the square     -> Stable Yard (Sorrel's, at his horses) and North
                               Gatehouse (the north road leaves from it), so the
                               yard fence and the Watch post are somewhere to @move
- Quest XP                  -> "Teeth on the North Road" is scale="mission"
- Attire & dress codes      -> The Drowned Lantern bans hoods and mudflat
                               filth at the door; three garments on three
                               NPCs cover the whole matrix (Watch uniform,
                               concealing hood, filthy oilskin)

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
from src.services import progression as prog
from src.services.perception import normalize_attire, normalize_dress_code
from src.services.rest import normalize_rest
from src.services.scene_objects import normalize_objects

PLAYTEST_DB_PATH = "data/emberfall.db"


def _ware(
    name: str,
    slot: str | None = None,
    tier: str | None = None,
    *,
    description: str,
    effect_tag: str | None = None,
    visual_desc: str | None = None,
    quantity: int = 1,
) -> dict:
    """One thing on a merchant's table — a weapon, a trinket, a draught —
    priced by the catalog like everything the engine sells."""
    item = {
        "id": prog._slug(name),
        "name": name,
        "type": "consumable" if effect_tag else ("armor" if slot == "armor" else ("weapon" if slot == "weapon" else "misc")),
        "description": description,
        "effects": {},
        "stackable": bool(effect_tag),
        "quantity": quantity,
        "slot": slot,
        "tier": tier,
        "effect_tag": effect_tag,
        "visual_desc": visual_desc,
        "attire": None,
    }
    item["value"] = prog.catalog_price(item) or 0
    return item


# The main quest's first step is this pass, and the Captain carries it: the
# brief says who holds it and `@give Gate Pass` takes it out of his keeping.
# fleet9-wenna T1-T6: with no holder, the DM sent her to "get my pass at the
# gatehouse", a watchman there sent her back, and the Captain handed it over
# himself — three turns of walking for an item that never reached the pack.
# A clue or key, never stock: the counter does not sell it.
_GATE_PASS = {
    "id": "gate_pass",
    "name": "Gate Pass",
    "type": "quest_item",
    "value": 0,
    "description": "Stamped leather under the Watch's brass lantern seal. The North Gate opens to it by daylight.",
    "effects": {},
    "stackable": False,
    "quantity": 1,
}


def _garment(
    name: str,
    tier: str,
    style: str,
    *,
    description: str,
    visual_desc: str,
    uniform_faction_id: str | None = None,
    concealing: bool = False,
) -> dict:
    """One wearable seeded onto an NPC's shelf, a `transfer_item` away.

    Scene objects are stripped of slot/tier by `normalize_objects`, so gear
    reaches the player through NPC stock — which is also where the fiction
    wants it (buy the hood from Vex, get the coat off Brann).
    """
    return {
        "id": prog._slug(name),
        "name": name,
        "type": "armor",
        "value": prog.TIER_PRICE["armor"][prog.TIER_LABELS.index(tier)],
        "description": description,
        "effects": {},
        "stackable": False,
        "quantity": 1,
        "slot": "armor",
        "tier": tier,
        "effect_tag": None,
        "visual_desc": visual_desc,
        "attire": normalize_attire(
            {"style": style, "uniform_faction_id": uniform_faction_id,
             "concealing": concealing},
            slot="armor",
        ),
    }


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
            narration_style="Second person, close and sensory. Firelight, cold air, small sounds.",
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
                "You are a courier. You arrived this afternoon with one letter left to "
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
            # No hour in it: the day opens at 15:00 now (see the clock below),
            # and "lantern posts being lit as the light goes blue" was read
            # back to the player at every hour
            description=(
                "Emberfall's cobbled market square. A round stone well "
                "with a peaked wooden roof stands at the center. Market stalls "
                "with canvas awnings line the edges, their tables still laid with "
                "root vegetables and wool. Stacked crates and barrels crowd the "
                "gaps between stalls. A notice board stands near the north side, "
                "and iron lantern posts wait at the corners for the town's dusk "
                "lighting."
            ),
            atmosphere_tags=["chill", "busy", "lantern-lit", "watchful"],
            economic_function="trade",
            population_level="moderate",
            current_state="peaceful",
            controlling_faction_id=watch.id,
            genre_type="fantasy",
            visited=True,
            discovered=True,
            # Canvas interactables (E to touch — resolved in code, no DM turn)
            objects=normalize_objects([
                {
                    "kind": "read",
                    "label": "Notice board",
                    "x": 62, "y": 32,
                    "payload": {"text": (
                        "TAKEN BY WOLVES — cart, driver unharmed, cargo gone, "
                        "north road, third such this month. The Watch pays for "
                        "true accounts of the howls. — Capt. B. Coldiron.\n\n"
                        "Beneath it, smaller: 'Sweetroot for nerves. Ask Piper.'"
                    )},
                },
                {
                    "kind": "take",
                    "label": "Dropped sweetroot bundle",
                    "x": 34, "y": 66,
                    "payload": {"item": {
                        "name": "Sweetroot Bundle",
                        "type": "consumable",
                        "description": "Chewing it steadies the hands. It smells of the herb stall.",
                        "value": 2,
                    }},
                },
                {
                    "kind": "search",
                    "label": "Abandoned market crate",
                    "x": 78, "y": 70,
                    "payload": {"item": {
                        "name": "Coil of Hemp Rope",
                        "type": "misc",
                        "description": "Twenty feet, cart-rated, smells of onions.",
                        "value": 4,
                    }},
                },
            ]),
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
            # Both dress rules on one door: Maera has a reason to hate hoods
            # (the ash-signed ledger entry), and she keeps a clean floor.
            dress_code=normalize_dress_code({
                "min_style": None,
                "banned_styles": ["concealing", "filthy"],
                "reason": ("Maera keeps a warm room and a clean floor: no covered faces "
                           "under her roof, and nobody who has been sleeping in the mudflats."),
            }),
            objects=normalize_objects([
                {
                    "kind": "read",
                    "label": "Guest ledger",
                    "x": 74, "y": 40,
                    "payload": {"text": (
                        "The ledger's last full page is three weeks old. One name "
                        "is entered twice that night — once in the keeper's neat "
                        "hand, once again below in a shakier one, as if the man "
                        "signed himself in a second time on his way out. The "
                        "second entry is smudged with ash."
                    )},
                },
                {
                    "kind": "take",
                    "label": "Iron hearth poker",
                    "x": 22, "y": 36,
                    "payload": {"item": {
                        "name": "Iron Hearth Poker",
                        "type": "weapon",
                        "description": "Heavy, fire-blacked, better than bare hands.",
                        "value": 6,
                    }},
                },
                {
                    "kind": "open",
                    "label": "Lost traveler's satchel",
                    "x": 52, "y": 68,
                    "payload": {
                        "lock": "Maera keeps whatever's lost under her roof; ask her before you go through it",
                        "item": {
                            "name": "Water-Stained Letter",
                            "type": "quest_item",
                            "description": "Addressed to a man the town buried three weeks ago.",
                            # Its own words, so nobody writes them for it:
                            # fleet6-wenna T17's DM invented the letter and
                            # spilled three sealed truths. A dead carter's
                            # plain worry, a name, a debt — no clue the
                            # campaign authors (the drinking debt fits
                            # Maera's secret: his last night was here)
                            "reads": (
                                "Tobin — your last letter frightened me. If the north road is as "
                                "bad as you say, sell the cart and come home by the south road "
                                "before the frost. Settle what you owe at the Lantern first; nine "
                                "coin is not worth a name for leaving debts. The children ask "
                                "after you every market day. — Nell"
                            ),
                        },
                    },
                },
                {
                    "kind": "rest",
                    "label": "Upstairs bed",
                    "x": 86, "y": 30,
                    "payload": {"text": "A narrow bed under the eaves; the sheets smell of woodsmoke."},
                },
            ]),
            # The one place in Emberfall a courier can sleep: eight coin, a good bed
            rest=normalize_rest({"kind": "inn", "price": 8, "quality": "good"}),
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
            objects=normalize_objects([
                {
                    "kind": "read",
                    "label": "Scratches on the hut wall",
                    "x": 28, "y": 38,
                    "payload": {"text": (
                        "Tally marks gouged into the doorframe, grouped in "
                        "threes. Under them, one word, carved deep and old: "
                        "SING."
                    )},
                },
                {
                    "kind": "take",
                    "label": "Cracked bone whistle",
                    "x": 60, "y": 74,
                    "payload": {"item": {
                        "name": "Cracked Bone Whistle",
                        "type": "quest_item",
                        # What the eye sees, nothing more. It once read "The same
                        # make as the one Captain Coldiron never mentions": the
                        # pack showed an unheard name and hinted at his secret,
                        # and the DM invented a twin whistle to explain it
                        # (fleet8-tamsin, fleet8-bram T7)
                        "description": (
                            "Split down the barrel — silent now. Old bone worn "
                            "smooth by a thumb; finer work than any shepherd's."
                        ),
                    }},
                },
                {
                    "kind": "search",
                    "label": "Split supply crate",
                    "x": 80, "y": 56,
                    "payload": {"item": {
                        "name": "Greywater Silver Piece",
                        "type": "misc",
                        "description": "Freshly minted. The counting-house stamp is still sharp.",
                        "value": 12,
                    }},
                },
            ]),
        )
        session.add(hollow)
        session.flush()

        # Corners of the square the story keeps walking to. fleet6-wenna toured
        # the stable yard fence (T6-T8) and the gatehouse (T19-T20) on three
        # no-op `@move Market Square`s; the canvas never left the square. The
        # player never starts here: the opening is staged on the square with
        # its people (Sorrel keeps the yard; the north road leaves the gatehouse)
        stable_yard = Location(
            name="Stable Yard",
            type=LocationType.POI,
            parent_id=square.id,
            depth=2,
            position_x=74,
            position_y=56,
            description=(
                "A muddy yard behind the market stalls: a row of horse stalls under a "
                "sagging roof, a stone water trough, and the north paddock fence where "
                "the horses stand with their ears turned toward the hills."
            ),
            atmosphere_tags=["muddy", "horse-warm", "quiet"],
            current_state="peaceful",
            genre_type="fantasy",
            discovered=True,
        )
        gatehouse = Location(
            name="North Gatehouse",
            type=LocationType.BUILDING,
            parent_id=square.id,
            depth=2,
            position_x=64,
            position_y=36,
            description=(
                "The Watch's post beside the North Gate: a cramped stone room with a "
                "brazier, a bench for whoever has the night watch, the gate's iron bar "
                "leaning by the door, and a slit window onto the north road."
            ),
            atmosphere_tags=["cramped", "cold", "watchful"],
            current_state="peaceful",
            controlling_faction_id=watch.id,
            genre_type="fantasy",
            discovered=True,
        )
        session.add_all([stable_yard, gatehouse])
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
        for corner, how in ((stable_yard, "Through the gap between the last stalls, behind the market."),
                            (gatehouse, "Across the square to the Watch's post at the North Gate.")):
            session.add(Connection(
                from_location_id=square.id,
                to_location_id=corner.id,
                travel_type="walk",
                travel_time_hours=0.05,
                description=how,
                bidirectional=True,
                discovered=True,
            ))

        # Requirements-locked route: tests move_player blocking until the DM
        # confirms requirements_met=True (Brann can issue a gate pass). It
        # runs from the gatehouse it is guarded by: fleet7-tamsin T9 `@move
        # North Gatehouse | through` put her in a bare dead end, and the road
        # beyond lived only in prose. From the square the way is the gatehouse.
        session.add(Connection(
            from_location_id=gatehouse.id,
            to_location_id=hollow.id,
            travel_type="walk",
            travel_time_hours=0.75,
            description=(
                "The north road out through the North Gate and up into the "
                "birches. The gate is barred after the evening howl."
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
                # No example trades: the DM copied 'carter' onto every player,
                # a folklorist investigating a dead carter included (fleet5-wenna
                # T0/T2/T10, every round before it)
                "Low, gravelly, unhurried. Short declarative sentences. Calls a "
                "stranger by the trade they look to have — their own, never a "
                "stock word — rather than by name until they've earned otherwise. "
                "'Road's shut. Ask me why.'"
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
            danger_tier="dangerous",
            combat_archetype="brute",
            # Wear it and strangers salute; Brann himself knows exactly who you
            # are and has an opinion about the coat.
            inventory_notable=[_garment(
                "Lantern Watch Coat", "common", "common",
                uniform_faction_id=watch.id,
                description="Watch issue. Nobody hands one of these over lightly.",
                visual_desc="a boiled-leather Watch coat with the brass lantern badge",
            ), dict(_GATE_PASS)],
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
                "The carter found dead on the north road three weeks ago drank here his last night",
            ],
            current_mood="composed",
            danger_tier="harmless",
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
            danger_tier="harmless",
        )
        session.add(piper)

        # At his horses: fleet7-bram T6-T10 the DM voiced a nameless "Stable
        # Boy" in the Stable Yard while Sorrel stood in the square, and Piper's
        # "ask Sorrel" step could never tick
        sorrel = NPC(
            name="Sorrel Tack",
            tier=NPCTier.AMBIENT,
            species="human",
            age=16,
            profession="Stable hand",
            current_location_id=stable_yard.id,
            home_location_id=stable_yard.id,
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
            # No "mister": copied onto every player, women too (fleet5-tamsin
            # T5, fleet5-wenna T6 "Mister — missus, sorry"). Not even as a
            # "never": a word in the dossier is a word in the DM's mouth
            voice_pattern=("Eager and open; hails a stranger with a plain 'Hey —', never a title, "
                           "and asks the next question before the last one's answered."),
            goals=["Join the Lantern Watch when he's of age"],
            secrets=["He saw a man in an ash-gray cloak crouched by the north wall two nights ago, and told no one because no one asks Sorrel"],
            current_mood="excited",
            danger_tier="harmless",
            # The cheap end of the ladder: warm, free, and banned at the tavern
            inventory_notable=[_garment(
                "Mucker's Oilskin", "crude", "filthy",
                description="A stable spare, stiff with muck. Its owner would part with it for a kind word.",
                visual_desc="a stable oilskin gone black with muck",
            )],
        )
        session.add(sorrel)

        # The market's one real counter. Playtest 2026-10-04: a market square
        # with nothing for sale meant a new character could never arm
        # themselves, and every fight was fought bare-handed.
        hesk = NPC(
            name="Hesk Bramble",
            tier=NPCTier.MINOR,
            species="human",
            age=58,
            profession="Hawker of blades, tools and remedies",
            current_location_id=square.id,
            home_location_id=square.id,
            position_x=28,
            position_y=60,
            description_physical=(
                "A stooped, wiry hawker with a leather apron hung with knives and "
                "twine, a grey stubble beard, and a fur hat pulled low; his stall "
                "is a cart under a patched red awning."
            ),
            description_personality=(
                "Shrewd, talkative, honest about prices and dishonest about "
                "everything else. Sells to anyone; remembers who paid."
            ),
            voice_pattern="Fast patter, prices first. 'Ten for the knife and it'll outlast you.'",
            goals=["Sell out before the north road closes for good", "Get his cousin's cart back from the wolves"],
            secrets=["He buys from the same fence Maera does, and knows the goods come off dead carters"],
            current_mood="hopeful",
            danger_tier="weak",
            currency=140,
            inventory_notable=[
                _ware("Gutting Knife", "weapon", "crude",
                      description="Short, ugly, sharp. It has opened more fish than men.",
                      visual_desc="a short single-edged gutting knife"),
                _ware("Woodsman's Hatchet", "weapon", "common",
                      description="A good iron head on an ash haft.",
                      visual_desc="a short iron hatchet on an ash haft"),
                _ware("Padded Jerkin", "armor", "common",
                      description="Quilted wool over boiled leather. Warm, and it turns a tooth.",
                      visual_desc="a quilted brown jerkin over boiled leather"),
                _ware("Willow-Bark Draught", effect_tag="heal_light", quantity=3,
                      description="Bitter, cloudy, and it works."),
                _ware("Lamp-Oil Flask", "trinket", "crude",
                      description="Thrown, it makes a wolf think twice.",
                      visual_desc="a stoppered clay flask of lamp oil on a cord"),
            ],
        )
        session.add(hesk)

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
                "Soft, amused menace. Never hurried. Names people for what they "
                "carry. 'Long way from the lanterns.'"
            ),
            goals=["Keep travelers off the north road", "Get paid"],
            secrets=["His pay is minted Greywater silver — he can name the counting-house on it"],
            current_mood="watchful",
            danger_tier="dangerous",
            combat_archetype="stalker",
            # The hood is the interesting purchase: it locks the Lantern's door
            # and makes every acquaintance's recognition contested.
            inventory_notable=[_garment(
                "Ashfall Hood", "common", "rough",
                concealing=True,
                description="Soot-dark wool, cut deep. Nobody who wears one wants to be counted.",
                visual_desc="a soot-grey hood that swallows the face",
            )],
        )
        session.add(vex)

        # The campaign-scale threat: the man the wolves answer to. Boss tier
        # is authored-only — he exists so the tier ladder tops out in-fiction
        # and the campaign generator has a villain to cast.
        whistler = NPC(
            name="Aldric Vane",
            tier=NPCTier.MAJOR,
            species="human",
            age=44,
            profession="Ashen Fangs wolf-caller",
            faction_id=fangs.id,
            current_location_id=hollow.id,
            home_location_id=hollow.id,
            position_x=30,
            position_y=30,
            description_physical=(
                "A broad, still man in a wolf-pelt mantle, ash rubbed into his "
                "beard, a row of bone whistles strung across his chest like "
                "medals. The wolves sit when he stands."
            ),
            description_personality=(
                "Patient as winter. Believes the town's lanterns made people "
                "soft, and the hills are only taking back their due. Speaks to "
                "wolves more gently than to men."
            ),
            voice_pattern=(
                "Slow, resonant, almost kind. 'The gate closes at the howl. "
                "Ever ask who taught them when to sing?'"
            ),
            goals=[
                "Strangle the north road until Emberfall pays the Fangs' toll",
                "Keep his wolves alive — they are the only pack he trusts",
            ],
            secrets=[
                "He was a Lantern Watch sergeant once, drummed out by Coldiron's predecessor",
                "The third-night howl pattern is a muster call — he is counting his pack",
            ],
            current_mood="patient",
            danger_tier="boss",
            combat_archetype="brute",
            boss_title="The Whistler of Wolf's Hollow",
        )
        session.add(whistler)

        # Weak-tier pack: fights the player can win at level 1, and the tier
        # ladder's bottom rungs made visible next to the boss at its top.
        for wolf_name, wolf_arch, wx, wy in (
            ("Hollow Wolf", "skirmisher", 44, 62),
            ("Gray-Flank Wolf", "stalker", 70, 40),
        ):
            session.add(NPC(
                name=wolf_name,
                tier=NPCTier.AMBIENT,
                species="wolf",
                profession="wolf of the drilled pack",
                faction_id=fangs.id,
                current_location_id=hollow.id,
                home_location_id=hollow.id,
                position_x=wx,
                position_y=wy,
                description_physical=(
                    "A rangy gray wolf, ribs showing, moving with an unnatural "
                    "patience — it watches like something taught it to wait."
                ),
                description_personality="Drilled, not wild. Holds its distance until called.",
                goals=["Answer the whistle"],
                current_mood="watchful",
                danger_tier="weak",
                combat_archetype=wolf_arch,
            ))
        # The dead carter the town talks about, as a person: he lived only in
        # Nell's letter, so the DM named him itself ("Fenwick", fleet7-wenna
        # T1-T2; "Hollis", round 5) and made a second dead carter. With a row,
        # "People the story has lost" gives every telling his name.
        session.add(NPC(
            name="Tobin",
            tier=NPCTier.AMBIENT,
            species="human",
            profession="Carter",
            status="dead",
            description_physical="A Greywater carter, weathered and broad, buried three weeks ago.",
            description_personality="Worried, by his wife's letter; owed the Lantern nine coin.",
            danger_tier="harmless",
        ))
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
            # Structured, so the ENGINE ticks them (getting the pass, reaching
            # the Hollow, reporting back). As plain strings they loaded as
            # "custom" steps only the DM could tick, and a playtest carried
            # the pass for an hour under "Get Captain Coldiron's gate pass"
            # Arriving is not discovering: reaching the Hollow is its own step
            # (a marker to follow), finding out is the DM's custom step
            # The proof is a real thing handed over, and it is the Hollow's own
            # authored whistle. As a talk_to it ticked on hearsay (fleet4
            # tamsin T10, wenna T13) and the whistle stayed in the bracken.
            # Its words never name it: "Bring the cracked bone whistle back"
            # sat on the quest log from T1 and the Captain said "Find the
            # whistle" before any witness had (fleet10-bram T1, -tamsin T2).
            # The engine still ticks it on the item; the DM's quest block
            # names it, and the brief says nobody does until it is found
            objectives=[
                {"text": "Get Captain Coldiron's gate pass for the north road",
                 "kind": "obtain", "item_name": "Gate Pass"},
                {"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": hollow.id},
                {"text": "Find out what is organizing the wolves", "kind": "custom"},
                {"text": "Bring proof back to Captain Coldiron",
                 "kind": "deliver", "item_name": "Cracked Bone Whistle", "target_id": brann.id},
            ],
            # The charm is gear, said the way @give says it: as a bare name it
            # landed as inert misc in all three fleet4 sessions
            rewards={
                "currency": 60,
                "items": [{
                    "name": "Wolf-Tooth Charm", "kind": "trinket", "tier": "common",
                    "about": ("A wolf's fang bound in brass wire on a Watch cord. Watch "
                              "patrols know it on sight, and wolves give its wearer half a "
                              "breath more room."),
                }],
                "reputation": {watch.id: 10},
            },
            assigned_by_npc_id=brann.id,
            scale="mission",
        ))

        # ----- Follow-on work (NOT_STARTED): a quest turned in must leave the
        # player something to pick up. fleet4-tamsin/-wenna: after the payout,
        # nine turns with no quest and no NPC with work to offer. Each is
        # built from its giver's own dossier and the world's authored props.
        session.add(Quest(
            title="A Letter for a Buried Man",
            # Says what the job is, never what the letter says: the satchel's
            # payload stays the reveal, and prose that paraphrased a quest
            # restating it ("addressed to a man the town buried") would read
            # to the audit as the unopened satchel's contents leaking
            description=(
                "Maera has kept a lost traveler's satchel under her roof since before "
                "the roads went bad, and wants it gone before anyone asks whose it was. "
                "Something in it was never delivered. The Watch keeps the affairs of "
                "the dead."
            ),
            status=QuestStatus.NOT_STARTED,
            # The satchel in the Lantern is lock-gated to asking her: when she
            # says yes the DM writes `@interact Lost traveler's satchel |
            # through`, the engine hands over the letter, and the obtain ticks
            objectives=[
                {"text": "Take the letter from the lost traveler's satchel, with Maera's leave",
                 "kind": "obtain", "item_name": "Water-Stained Letter"},
                {"text": "Learn who the letter was meant for, and how he died", "kind": "custom"},
                {"text": "Put the letter in Captain Coldiron's hands",
                 "kind": "deliver", "item_name": "Water-Stained Letter", "target_id": brann.id},
            ],
            rewards={"currency": 20, "reputation": {watch.id: 5}},
            assigned_by_npc_id=maera.id,
            scale="job",
        ))
        session.add(Quest(
            title="Counting the Howls",
            description=(
                "Piper swears the howls keep a schedule, and nobody with a badge will "
                "listen to an herbalist's apprentice. Find who else is awake when they "
                "start, stand out the next one, mark where it rises, and bring her what "
                "you heard."
            ),
            status=QuestStatus.NOT_STARTED,
            # The last step goes back to Piper (her own talk_to waits for the
            # steps before it); a talk_to Coldiron would tick the first time
            # the player spoke to him, done or not
            objectives=[
                {"text": "Ask Sorrel Tack what the stable yard hears after dark",
                 "kind": "talk_to", "target_id": sorrel.id},
                {"text": "Stand out the next howl and mark where it rises", "kind": "custom"},
                {"text": "Bring what you heard back to Piper", "kind": "talk_to", "target_id": piper.id},
            ],
            rewards={
                "currency": 10,
                "items": [{
                    "name": "Yarrow Tincture", "kind": "potion",
                    "about": "An herbalist's own: green, bitter, and it closes a cut faster than it has any right to.",
                }],
            },
            assigned_by_npc_id=piper.id,
            scale="job",
        ))

        # ----- Scheduled events (fire deterministically on the world tick) ----
        # The howl is at eight in every mouth; its text once said the gate was
        # barred "early", and the DM told it shutting "well ahead of its usual
        # hour" at 20:15 (fleet5-wenna T10)
        session.add(Event(
            name="The Evening Howl",
            description=(
                "At the stroke of eight, howling rises from the northern hills — "
                "layered, answering itself, closer than last night. The North "
                "Gate is barred behind it and the square empties fast."
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
        # 15:00 = afternoon; evening amber at 17:00, night blue at 21:00. It
        # opened at 17:30 until fleet9: the 20:00 howl barred the north gate
        # (and the pass is void after dark) ten replies in, so no session of
        # 21 reached Wolf's Hollow and the main quest stalled on its first
        # step (fleet9-tamsin, -wenna). Five game-hours of open road: the
        # Hollow is ~4 replies away by day (pass, gatehouse, 45 minutes of
        # road). The howl still lands inside a 21-reply session: the fleet
        # runs that did not sleep spent 5h06m-6h28m in 21 replies (fleet9's
        # town-bound three exactly the 15-minute floor, 5h15m), so from 14:00
        # a town-bound session ended at 19:15 without it — the set piece the
        # critics called the best scene of fleet7 and fleet9-bram, and a step
        # of "Counting the Howls".
        session.add(WorldClock(day=1, hour=15, minute=0))

        session.add(DMState(
            current_arc=(
                "Someone is using trained wolves to strangle the north road. "
                "A carter dead on the north road three weeks ago is the loose thread."
            ),
            planned_beats=[
                "The dead carter drank at the Lantern — Maera knew him and deflects",
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
        print("  Day/night tint  It's 15:00 — afternoon now, evening amber at 17:00, night blue after 21:00.")
        print("  Walk cycles     Character creation is prefilled (Rook Fennick); sprites + 6-frame strips generate.")
        print("  Obstacles       Market Square bg auto-detects stalls/crates/well; Ctrl+E, C to inspect, G to redo.")
        print("  NPC wandering   Brann, Piper, and Hesk share the square; watch them stroll and pause near you.")
        print("  TTS voices      Talk to Brann (old gruff) then Piper (young bright) — distinct voices, music ducks.")
        print("  Scheduled event 'The Evening Howl' fires at 20:00 day 1 (5 game-hours out).")
        print("  Locked travel   North road from the gatehouse to Wolf's Hollow needs a gate pass from Brann.")
        print("  Quest flow      'Teeth on the North Road' is seeded NOT_STARTED with rewards; Brann offers it.")
        print("                  Follow-ons: Maera's satchel letter, Piper's howl count.")
        print("  Combat          Ctrl+K anywhere for the gray-box; Vex Marrow at the Hollow is the story fight.")
        print("  Attire          Buy Vex's Ashfall Hood -> the Lantern's door refuses you; wear Brann's")
        print("                  Watch Coat -> strangers treat you as the Watch, Brann does not.")
