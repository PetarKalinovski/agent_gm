"""The Emberfall playtest seed builds a world that exercises every system."""

import re

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import (
    NPC,
    Connection,
    DMState,
    Event,
    Faction,
    Location,
    Player,
    Quest,
    QuestStatus,
    WorldBible,
    WorldClock,
    get_session,
    reset_engine,
)


@pytest.fixture()
def playtest_db(tmp_path):
    db_path = str(tmp_path / "emberfall.db")
    reset_engine()
    create_playtest_world(db_path)
    yield db_path
    reset_engine()


def test_seed_is_idempotent(playtest_db, capsys):
    with get_session() as session:
        before = session.query(Location).count()
    # Same path again: must detect the existing world and change nothing
    create_playtest_world(playtest_db)
    with get_session() as session:
        assert session.query(Location).count() == before
    assert "already exists" in capsys.readouterr().out


def test_world_shape(playtest_db):
    with get_session() as session:
        assert session.query(WorldBible).count() == 1
        bible = session.query(WorldBible).first()
        # Music style + character-creation prefill both read the bible
        assert bible.genre == "fantasy"
        assert bible.pc_suggested_name and bible.pc_suggested_description

        # The town, the square, the Lantern, the Hollow, and two corners of the
        # square the story keeps walking to (fleet6-wenna: the stable yard
        # fence and the gatehouse, toured on no-op moves)
        assert session.query(Location).count() == 6
        # 8 + Hesk Bramble, the market's counter (2026-10-04: nothing was for sale)
        assert session.query(NPC).count() == 10
        tobin = session.query(NPC).filter_by(name="Tobin").one()   # the letter's dead carter
        assert tobin.status == "dead" and tobin.current_location_id is None
        # No pre-made player: character creation (and sprite/walk generation)
        # is part of the playtest
        assert session.query(Player).count() == 0

        # Start scene is visited so the game opens there
        square = session.query(Location).filter_by(name="Market Square").one()
        assert square.visited and square.discovered
        # Obstacles unset -> auto-detection runs after background generation
        assert square.obstacles is None

        # Three wanderers share the start scene (Brann, Piper, Hesk); Sorrel
        # is at his horses in the Stable Yard (fleet7-bram T6-T10)
        assert session.query(NPC).filter_by(current_location_id=square.id).count() == 3

        # The tier ladder has a face at both ends: harmless townsfolk, weak
        # wolves, dangerous named fighters, and exactly one authored boss
        tiers = {n.name: n.danger_tier for n in session.query(NPC).all()}
        assert tiers["Piper Quillow"] == "harmless"
        assert tiers["Vex Marrow"] == "dangerous"
        bosses = [n for n in session.query(NPC).all() if n.danger_tier == "boss"]
        assert len(bosses) == 1 and bosses[0].boss_title

        # Every seeded location carries canvas objects (E-to-interact surface)
        for loc_name in ("Market Square", "The Drowned Lantern", "Wolf's Hollow"):
            loc = session.query(Location).filter_by(name=loc_name).one()
            # 1-3 authored things to touch; a bed (kind 'rest') is the engine's own pool
            authored = [o for o in loc.objects if o.get('kind') in ('take', 'read', 'open', 'search')]
            assert loc.objects and 1 <= len(authored) <= 3


def test_locked_connection_and_quest(playtest_db):
    with get_session() as session:
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
        locked = session.query(Connection).filter_by(to_location_id=hollow.id).one()
        assert locked.requirements, "north road must test move_player requirement blocking"
        # ...from the gatehouse that guards it (fleet7-tamsin T9: a bare dead end)
        assert locked.from_location.name == "North Gatehouse"

        # Deliberately three now: the main quest and two follow-on jobs
        # (fleet4: after the turn-in there was no work left to pick up)
        assert session.query(Quest).count() == 3
        quest = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        assert quest.status == QuestStatus.NOT_STARTED
        assert quest.rewards.get("currency") == 60
        assert quest.rewards.get("items") and quest.rewards.get("reputation")
        brann = session.query(NPC).filter_by(name="Captain Brann Coldiron").one()
        assert quest.assigned_by_npc_id == brann.id


def test_the_proof_is_the_hollows_whistle_handed_over(playtest_db):
    """fleet4-tamsin/-wenna: 'Bring proof back' was a talk_to, so it ticked on
    hearsay and the whistle stayed in the bracken. It is a delivery of the
    very item the Hollow's 'Cracked bone whistle' yields."""
    with get_session() as session:
        quest = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        brann = session.query(NPC).filter_by(name="Captain Brann Coldiron").one()
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
    proof = quest.objectives[-1]
    assert proof["kind"] == "deliver" and proof["target_id"] == brann.id
    whistle = next(o for o in hollow.objects if o["label"] == "Cracked bone whistle")
    assert proof["item_name"] == whistle["payload"]["item"]["name"] == "Cracked Bone Whistle"


def test_the_reward_charm_lands_as_a_worn_trinket(playtest_db):
    """All three fleet4 sessions got the charm as inert misc. Authored as a
    common trinket, it arrives equippable and goes on into the empty slot."""
    from src.tools.world_write.quests import _complete_quest

    with get_session() as session:
        square = session.query(Location).filter_by(name="Market Square").one()
        player = Player(name="Bram", current_location_id=square.id, currency=0)
        session.add(player)
        session.flush()
        quest = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        quest.status = QuestStatus.ACTIVE
        _complete_quest(session, quest, player)
        session.commit()
        charm = next(i for i in player.inventory if i["name"] == "Wolf-Tooth Charm")
        assert charm["slot"] == "trinket" and charm["tier"] == "common"
        assert "brass wire" in charm["description"]
        assert player.equipment.get("trinket") == charm["id"]


def test_follow_on_quests_point_at_real_people_and_props(playtest_db):
    """Two jobs a player can pick up after the first turn-in, from their
    givers' own dossiers: Maera's lock-gated satchel letter, Piper's howls.
    Every target is a real seed row; every item is one the world can yield."""
    with get_session() as session:
        npcs = {n.name: n.id for n in session.query(NPC).all()}
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        follow_ons = session.query(Quest).filter(Quest.title != "Teeth on the North Road").all()
        satchel = next(o for o in tavern.objects if o["label"] == "Lost traveler's satchel")
        givers = {q.title: q.assigned_by_npc_id for q in follow_ons}
        assert givers == {"A Letter for a Buried Man": npcs["Maera Duskwell"],
                          "Counting the Howls": npcs["Piper Quillow"]}
        for quest in follow_ons:
            assert quest.status == QuestStatus.NOT_STARTED and quest.scale == "job"
            for step in quest.objectives:
                if step.get("target_id"):
                    assert step["target_id"] in npcs.values(), step
            # The last step closes the loop with someone, never a free tick
            assert quest.objectives[-1]["kind"] in ("deliver", "talk_to")
        letter = next(q for q in follow_ons if q.title == "A Letter for a Buried Man")
        assert satchel["payload"].get("lock")              # asking Maera IS the first step
        assert {s.get("item_name") for s in letter.objectives if s.get("item_name")} == {
            satchel["payload"]["item"]["name"]}


def test_quest_text_never_restates_what_an_unopened_thing_holds(playtest_db):
    """The DM paraphrases quest text in prose; a quest that restated the
    satchel letter's own words ("addressed to a man the town buried") made
    that prose read to the audit as the unopened satchel leaking."""
    from src.services.scene_objects import normalize_objects
    from src.web.turn_audit import _object_contents_leaked

    with get_session() as session:
        objects = [o for loc in session.query(Location).all() for o in normalize_objects(loc.objects)]
        texts = [(q.title, t) for q in session.query(Quest).all()
                 for t in [q.title, q.description] + [o["text"] for o in q.objectives]]
    for title, text in texts:
        leaks = _object_contents_leaked(text, "", objects)
        assert not leaks, f"{title}: {text!r} restates {[(o['label'], tell) for o, tell in leaks]}"


def test_maeras_leave_opens_the_satchel_and_the_letter_step_ticks(playtest_db):
    """The follow-on's first step is the Lantern's LOCKED satchel, and a
    lock is the DM's call: `@interact … | through` is how its fiction opens
    one. Without it the letter could arrive only by an invented @give that
    left the satchel 'holding' it — and 'unseen' — for good."""
    from src.services.commands import CommandRunner
    from src.services.scene_objects import normalize_objects
    from src.tools.world_write.quests import activate_quest

    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        quest = session.query(Quest).filter_by(title="A Letter for a Buried Man").one()
        player = Player(name="Bram", current_location_id=tavern.id)
        session.add(player)
        session.commit()
        player_id, quest_id, tavern_id = player.id, quest.id, tavern.id
    activate_quest(quest_id)

    r = CommandRunner(player_id)
    shut = r.run("@interact Lost traveler's satchel")
    assert not shut.ok and "write @interact Lost traveler's satchel | through" in shut.message
    opened = r.run("@interact Lost traveler's satchel | through")
    assert opened.ok and "found Water-Stained Letter" in opened.message
    with get_session() as session:
        pack = {i["name"]: i for i in session.get(Player, player_id).inventory}
        assert pack["Water-Stained Letter"]["type"] == "quest_item"
        assert [o["done"] for o in session.get(Quest, quest_id).objectives] == [True, False, False]
        objects = normalize_objects(session.get(Location, tavern_id).objects)
        assert next(o for o in objects if o["label"] == "Lost traveler's satchel")["consumed"]
    # ...and it is in the pack once: the same reply's @give is dropped quietly
    # (round 10, fleet10-bram T15: no failure booked for a pickup that happened)
    again = r.run("@give Water-Stained Letter")
    assert again.ok and again.result.get("skipped") == "already_found" and not again.message
    with get_session() as session:
        assert [i["name"] for i in session.get(Player, player_id).inventory].count("Water-Stained Letter") == 1


def test_the_lantern_keeps_a_door_policy(playtest_db):
    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        code = tavern.dress_code
        assert code["banned_styles"] == ["concealing", "filthy"]
        assert code["min_style"] is None
        assert "Maera keeps a warm room" in code["reason"]

        # Everywhere else stays open to anyone — a dress code is rare on purpose
        others = session.query(Location).filter(Location.id != tavern.id).all()
        assert all(loc.dress_code is None for loc in others)


def test_three_garments_cover_the_attire_matrix(playtest_db):
    """Uniform, concealing hood and filthy oilskin, one NPC each."""
    with get_session() as session:
        watch_id = session.query(Faction).filter_by(name="The Lantern Watch").one().id
        shelves = {
            npc.name: (npc.inventory_notable or [])
            for npc in session.query(NPC).all()
        }

    coat = shelves["Captain Brann Coldiron"][0]
    hood = shelves["Vex Marrow"][0]
    oilskin = shelves["Sorrel Tack"][0]

    for garment in (coat, hood, oilskin):
        assert garment["slot"] == "armor" and garment["tier"] in ("crude", "common")
        assert garment["visual_desc"]
        assert set(garment["attire"]) == {"style", "uniform_faction_id", "concealing"}

    assert coat["attire"] == {"style": "common", "uniform_faction_id": watch_id,
                              "concealing": False}
    assert hood["attire"]["concealing"] is True
    assert hood["attire"]["uniform_faction_id"] is None
    assert oilskin["attire"]["style"] == "filthy"


def test_the_seeded_matrix_actually_reaches_the_door(playtest_db):
    """Hood -> refused, oilskin -> refused, Watch coat -> welcome."""
    from src.services import perception
    from src.services import progression as prog

    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        code = tavern.dress_code
        shelves = {npc.name: (npc.inventory_notable or [])
                   for npc in session.query(NPC).all()}

    def _verdict(garment):
        player = Player(name="Rook", level=10, inventory=[garment],
                        equipment={"armor": prog.item_key(garment)})
        return perception.dress_verdict(perception.perception_profile(player), code)

    assert _verdict(shelves["Vex Marrow"][0])["token"] == "concealing"
    assert _verdict(shelves["Sorrel Tack"][0])["token"] == "filthy"
    assert _verdict(shelves["Captain Brann Coldiron"][0])["allowed"] is True


def test_clock_and_scheduled_events(playtest_db):
    with get_session() as session:
        clock = session.query(WorldClock).one()
        assert clock.get_time_of_day() == "afternoon"

        events = session.query(Event).all()
        assert len(events) == 2
        for event in events:
            assert not event.narrated_to_player
            # Strictly in the future relative to the seeded clock
            assert (event.scheduled_day, event.scheduled_hour) > (clock.day, clock.hour)

        assert session.query(DMState).one().tension == "low"


def test_the_north_road_is_open_by_day_and_the_howl_still_lands_in_a_session(playtest_db):
    """fleet9 (all three): the seed opened at 17:30, the 20:00 howl barred the
    north gate ten replies in and the pass is void after dark, so no session
    of 21 replies saw Wolf's Hollow. The day opens early enough for the road,
    and the howl still comes inside a 21-reply session even at the 15-minute
    turn floor (fleet9's three spent exactly 5h15m in 21: from 14:00 that
    ended at 19:15, without it)."""
    from src.services.travel import barred_tonight
    from src.services.world_tick import MIN_TURN_HOURS

    with get_session() as session:
        clock = session.query(WorldClock).one()
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
        road = session.query(Connection).filter(Connection.to_location_id == hollow.id).one()
        assert road.requirements, "the pass still gates it"
        assert barred_tonight(road) is False
        howl = session.query(Event).filter_by(name="The Evening Howl").one()
        hours_to_howl = howl.scheduled_hour - (clock.hour + (clock.minute or 0) / 60)
        assert hours_to_howl >= 5                           # the Hollow and back by day
        assert hours_to_howl / MIN_TURN_HOURS <= 20         # at the floor, by T20 of T0-T20
        square = session.query(Location).filter_by(name="Market Square").one()
        assert "dusk." not in square.description and "light goes blue" not in square.description


def test_the_stable_yard_and_the_gatehouse_are_places_off_the_square(playtest_db):
    """fleet6-wenna T6/T9/T11: the prose toured the stable yard fence and the
    gatehouse on three no-op `@move Market Square`s, and the canvas never
    left the square. The player never starts there: the opening is staged on
    the square. The yard is Sorrel's (fleet7-bram T6-T10: a nameless "Stable
    Boy" voiced there while he stood in the square)."""
    from src.services.commands import CommandRunner

    with get_session() as session:
        square = session.query(Location).filter_by(name="Market Square").one()
        for name in ("Stable Yard", "North Gatehouse"):
            place = session.query(Location).filter_by(name=name).one()
            assert place.parent_id == square.id and place.discovered
            road = session.query(Connection).filter_by(from_location_id=square.id, to_location_id=place.id).one()
            assert road.bidirectional and road.discovered and not road.requirements
            people = [n.name for n in session.query(NPC).filter_by(current_location_id=place.id).all()]
            assert people == (["Sorrel Tack"] if name == "Stable Yard" else [])
        sorrel = session.query(NPC).filter_by(name="Sorrel Tack").one()
        assert sorrel.home_location_id == sorrel.current_location_id   # he goes back there
        player = Player(name="Wenna Cole", current_location_id=square.id)
        session.add(player)
        session.commit()
        player_id, square_id = player.id, square.id

    for name in ("Stable Yard", "North Gatehouse"):
        r = CommandRunner(player_id)
        assert r.run(f"@move {name}").ok
        with get_session() as session:
            assert session.get(Player, player_id).current_location.name == name
        assert r.run("@move Market Square").ok
    with get_session() as session:
        assert session.get(Player, player_id).current_location_id == square_id


def test_the_north_road_leaves_from_the_gatehouse(playtest_db):
    """fleet7-tamsin T9: `@move North Gatehouse | through` put her in a bare
    dead end and the road it guards ran from the square, so the climb to the
    Hollow lived only in prose. The square's way north is the gatehouse; the
    gatehouse's door onto the road is the locked one, and a held pass opens it."""
    from src.services.commands import CommandRunner
    from src.services.lead import next_hop
    from src.tools.world_read.locations import get_available_destinations

    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        player = Player(name="Bram Holloway", current_location_id=places["Market Square"])
        session.add(player)
        session.commit()
        player_id = player.id
        assert next_hop(session, player, places["Wolf's Hollow"]) == places["North Gatehouse"]
    assert "Wolf's Hollow" not in [d["name"] for d in get_available_destinations(places["Market Square"])]
    road = next(d for d in get_available_destinations(places["North Gatehouse"]) if d["name"] == "Wolf's Hollow")
    assert "gate pass" in road["requirements"][0]

    r = CommandRunner(player_id)
    assert not r.run("@move Wolf's Hollow").ok                  # no way round the gate
    assert r.run("@move North Gatehouse").ok
    assert not r.run("@move Wolf's Hollow").ok                  # the gate, no pass
    with get_session() as session:
        session.get(Player, player_id).inventory = [{"id": "gate_pass", "name": "Gate Pass", "type": "key"}]
        session.commit()
    assert r.run("@move Wolf's Hollow").ok
    with get_session() as session:
        assert session.get(Player, player_id).current_location_id == places["Wolf's Hollow"]


def test_the_satchel_letter_has_its_own_words_and_no_campaign_clue(playtest_db):
    """fleet6-wenna T17: the letter's payload said only who it was addressed
    to, and the DM wrote its contents — three sealed truths. Its own words
    now: a dead carter's worry, a name, a debt. Short enough for one journal
    line, and none of the clues the campaign is built on."""
    from src.services.scene_objects import normalize_objects

    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        satchel = next(o for o in normalize_objects(tavern.objects) if o["label"] == "Lost traveler's satchel")
    reads = satchel["payload"]["item"]["reads"]
    assert reads.startswith("Tobin —") and len(reads) <= 300
    lowered = reads.lower()
    for clue in ("silver", "roster", "toll", "whistle", "fence", "eleven", "counting", "fang", "vane", "watch"):
        assert clue not in lowered, clue


def _seed_items(session):
    """Every item the seed can put in a pack: scene finds, shelves, rewards."""
    from src.services.scene_objects import normalize_objects

    for loc in session.query(Location).all():
        for obj in normalize_objects(loc.objects):
            item = (obj.get("payload") or {}).get("item")
            if isinstance(item, dict):
                yield f"{loc.name}/{obj['label']}", item
    for npc in session.query(NPC).all():
        for item in npc.inventory_notable or []:
            if isinstance(item, dict):
                yield f"{npc.name}'s shelf", item
    for quest in session.query(Quest).all():
        for item in (quest.rewards or {}).get("items") or []:
            if isinstance(item, dict):
                yield f"{quest.title} reward", item


_SECRECY = re.compile(r"\bnever (?:mentions?|speaks? of|talks? about|says?)\b|\btold no one\b|\bsecret|"
                      r"\bhidden from\b|\bkeeps? quiet\b", re.I)
_CONTENT = re.compile(r"[a-z][a-z'’-]{3,}")
_COMMON = frozenset({"that", "this", "with", "from", "than", "they", "them", "their", "have", "been", "once",
                     "what", "when", "into", "over", "only", "still", "every", "never"})


def _content_words(text: str) -> set[str]:
    return {w.strip("'’-") for w in _CONTENT.findall(text.lower())} - _COMMON


def test_no_item_text_names_a_person_or_carries_a_secret(playtest_db):
    """fleet8-tamsin: the whistle in the pack read "The same make as the one
    Captain Coldiron never mentions." — a name nobody had said aloud, and a
    hint at his trust-gated secret (fleet8-bram T7: the DM invented a twin
    whistle to explain it). An item says what the eye sees. A document's own
    words ("reads") may name people — reading one is how a name is learned —
    but never a secret."""
    from src.services.dialogue import is_beast, name_words

    with get_session() as session:
        npcs = session.query(NPC).all()
        people = {w for n in npcs if not is_beast(n) for w in name_words(n.name)}
        secrets = [(n.name, s) for n in npcs for s in (n.secrets or [])]
        items = list(_seed_items(session))
        # What every place shows openly ("the north road") is nobody's secret
        public = {w for loc in session.query(Location).all()
                  for w in _content_words(f"{loc.name} {loc.description or ''}")}
    assert len(items) >= 12
    for where, item in items:
        shown = " ".join(str(item.get(k) or "") for k in ("name", "description", "about"))
        for word in people:
            assert not re.search(rf"(?<![\w'’-]){re.escape(word)}(?![\w-])", shown), (where, word, shown)
        assert not re.search(r"\bCaptain\b", shown), (where, shown)
        assert not _SECRECY.search(shown), (where, shown)
        # Sharing a fact's words with a secret (beyond the item's own name:
        # the silver piece IS minted silver) is telling it
        own = _content_words(str(item.get("name") or ""))
        for text in (shown, str(item.get("reads") or "")):
            words = _content_words(text) - own - public
            for who, secret in secrets:
                shared = words & _content_words(secret)
                assert len(shared) < 3, (where, who, sorted(shared))
