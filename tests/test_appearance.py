"""Visible equipment: appearance signatures, signature-scoped sprite sets.

Four groups, in the order the system is built:

A. Signatures — pure dict math over an already-loaded Player row. These pin
   the seam with the perception system: `attire` and trinkets must NEVER move
   a signature (ruling A10), or the two systems have silently coupled.
B. Paths and completeness — `base` resolves to the flat legacy directory with
   zero migration (A4), a set is all-or-nothing (never a half-dressed frame),
   and the LRU cap keeps disk growth bounded.
C. Prompts — pure string assertions, no API call. The default arguments must
   reproduce today's prompt and today's paths byte-for-byte.
D. Orchestration — `ensure_body_set` / `ensure_action_set` with a stubbed
   image client: seven calls, 58 files, the right reference at each step.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.config import load_settings
from src.models import NPC, Item, Location, LocationType, Player, get_session
from src.services import appearance
from src.services.asset_manager import AssetManager
from src.services.image_generator import ImageGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _player(**kwargs) -> Player:
    """A detached Player row with explicit fields (SQLAlchemy column defaults
    only fire on flush, so a legacy-shaped row is what we get for free)."""
    fields = {
        "name": "Tester",
        "health_status": "healthy",
        "inventory": [],
        "traits": [],
        "level": None,
        "xp": None,
        "equipment": None,
    }
    fields.update(kwargs)
    return Player(**fields)


def _gear(item_id: str, name: str, slot: str, tier: str, **extra) -> dict:
    return Item(id=item_id, name=name, type=slot, slot=slot, tier=tier, **extra).to_dict()


def _dressed(armor: dict | None = None, weapon: dict | None = None,
             trinket: dict | None = None, **kwargs) -> Player:
    inventory = [item for item in (armor, weapon, trinket) if item]
    equipment = {
        "armor": armor["id"] if armor else None,
        "weapon": weapon["id"] if weapon else None,
        "trinket": trinket["id"] if trinket else None,
    }
    return _player(inventory=inventory, equipment=equipment, **kwargs)


COAT = _gear("watch_coat", "Watch Coat", "armor", "common",
             visual_desc="a boiled-leather Watch coat with the brass lantern badge")
SWORD = _gear("shortsword", "Notched Shortsword", "weapon", "common",
              visual_desc="a notched iron shortsword")


def _png(path: Path, size=(4, 4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (10, 20, 30, 255)).save(path)


# ===========================================================================
# The shared Item seam (built once; the perception system owns `attire`'s
# meaning, this system owns `visual_desc`'s)
# ===========================================================================


def test_appearance_fields_round_trip():
    item = Item.from_dict({
        "id": "watch_coat", "name": "Watch Coat", "type": "armor",
        "slot": "armor", "tier": "common",
        "visual_desc": "a boiled-leather Watch coat with the brass lantern badge",
        "attire": {"style": "common", "uniform_faction_id": "harbor_watch", "concealing": False},
    })
    again = Item.from_dict(item.to_dict())
    assert again.visual_desc == item.visual_desc
    assert again.attire == {"style": "common", "uniform_faction_id": "harbor_watch",
                            "concealing": False}


def test_legacy_item_without_appearance_still_validates():
    item = Item.from_dict({"id": "iron_sword", "name": "Iron Sword", "type": "weapon", "value": 40})
    assert item.visual_desc is None and item.attire is None
    assert item.to_dict()["visual_desc"] is None
    assert item.to_dict()["attire"] is None


# ===========================================================================
# Group A — signatures (pure, no I/O)
# ===========================================================================


def test_base_signature_for_empty_loadout():
    """`equipment=NULL` is the legacy world: it renders exactly what it does
    today, from exactly the files it uses today."""
    payload = appearance.appearance_payload(_player())
    assert payload == {
        "body_sig": "base",
        "weapon_sig": "unarmed",
        "action_sig": "base-unarmed",
        "version": appearance.APPEARANCE_VERSION,
    }


def test_signature_is_deterministic():
    first = appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))
    second = appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))
    third = appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))
    assert first == second == third


def test_signature_changes_with_visual_desc():
    """Re-authoring what an item looks like regenerates its art."""
    repainted = dict(COAT, visual_desc="a soot-black Watch coat, badge torn off")
    assert (appearance.body_signature(_dressed(armor=COAT))
            != appearance.body_signature(_dressed(armor=repainted)))


def test_signature_changes_with_item_id():
    """Two items that read the same must not share a directory."""
    twin = dict(COAT, id="watch_coat_2")
    assert (appearance.body_signature(_dressed(armor=COAT))
            != appearance.body_signature(_dressed(armor=twin)))


def test_signature_ignores_attire_and_trinket():
    """THE SEAM GUARD (ruling A10).

    `attire` is social metadata owned by the perception system and a trinket is
    invisible at sprite scale. If either ever moves a signature, the two
    systems have coupled and every social edit starts costing seven image
    calls.
    """
    plain = _dressed(armor=COAT, weapon=SWORD)
    baseline = appearance.appearance_payload(plain)

    for attire in (
        {"style": "noble", "uniform_faction_id": None, "concealing": False},
        {"style": "filthy", "uniform_faction_id": "harbor_watch", "concealing": True},
    ):
        dressed = _dressed(armor=dict(COAT, attire=attire), weapon=SWORD)
        assert appearance.appearance_payload(dressed) == baseline

    charm = _gear("charm", "Ash Charm", "trinket", "common", visual_desc="a bone charm")
    assert appearance.appearance_payload(
        _dressed(armor=COAT, weapon=SWORD, trinket=charm)
    ) == baseline


def test_weapon_only_moves_action_sig():
    """Changing weapons costs ONE image call — the 58-file half is untouched."""
    armed = appearance.appearance_payload(_dressed(weapon=SWORD))
    assert armed["body_sig"] == "base"
    assert armed["weapon_sig"] != "unarmed"
    assert armed["action_sig"] == f"base-{armed['weapon_sig']}"


def test_armor_moves_body_and_action_sig():
    worn = appearance.appearance_payload(_dressed(armor=COAT))
    assert worn["body_sig"] != "base"
    assert worn["weapon_sig"] == "unarmed"
    assert worn["action_sig"] == f"{worn['body_sig']}-unarmed"


def test_swapping_weapons_reuses_the_body_set():
    """The point of two orthogonal axes (A1)."""
    other = _gear("ashblade", "Ashblade", "weapon", "common", visual_desc="a grey ashwood blade")
    first = appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))
    second = appearance.appearance_payload(_dressed(armor=COAT, weapon=other))
    assert first["body_sig"] == second["body_sig"]
    assert first["weapon_sig"] != second["weapon_sig"]
    assert first["action_sig"] != second["action_sig"]


def test_legacy_typed_weapon_produces_weapon_sig():
    """`{"type": "weapon"}` with no slot is repaired by `mechanize_item`, so an
    old world's Iron Sword shows up in the swing with nothing backfilled."""
    legacy = {"id": "iron_sword", "name": "Iron Sword", "type": "weapon",
              "value": 30, "quantity": 1}
    p = _player(inventory=[legacy], equipment={"weapon": "iron_sword"})
    assert appearance.weapon_signature(p) != "unarmed"


def test_over_tier_cap_item_does_not_change_signature():
    """Art must match mechanics: gear the player cannot use is not worn."""
    master = _gear("master_plate", "Master Plate", "armor", "master",
                   visual_desc="mirror-bright plate")
    p = _dressed(armor=master, level=1)
    assert appearance.body_signature(p) == "base"


def test_appearance_version_folds_into_hash(monkeypatch):
    before = appearance.body_signature(_dressed(armor=COAT))
    monkeypatch.setattr(appearance, "APPEARANCE_VERSION", appearance.APPEARANCE_VERSION + 1)
    assert appearance.body_signature(_dressed(armor=COAT)) != before


def test_appearance_payload_is_pure(monkeypatch):
    """It runs after every world-write tool — it may never issue a query."""
    import src.models

    def _explode(*args, **kwargs):
        raise AssertionError("appearance_payload must never open a session")

    monkeypatch.setattr(src.models, "get_session", _explode)
    assert appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))["body_sig"] != "base"


def test_disabled_config_forces_base(monkeypatch):
    """One switch, one place: full gear equipped still renders as `base`."""
    monkeypatch.setattr(load_settings().appearance, "enabled", False)
    fully_kitted = _dressed(armor=COAT, weapon=SWORD)
    assert appearance.appearance_payload(fully_kitted) == appearance.base_payload()


def test_visual_phrase_fallback_chain():
    assert appearance.visual_phrase(COAT, "armor") == COAT["visual_desc"]
    named = dict(COAT, visual_desc=None)
    assert appearance.visual_phrase(named, "armor") == "Watch Coat"
    anonymous = dict(COAT, visual_desc="   ", name="")
    assert appearance.visual_phrase(anonymous, "armor") == "plain travelling clothes"
    assert appearance.visual_phrase({"name": ""}, "weapon") == "a plain weapon"
    assert appearance.visual_phrase(None, "armor") is None


def test_visual_phrase_clamps_purple_prose():
    rambling = dict(COAT, visual_desc="a coat " + "of terrible portent " * 40)
    phrase = appearance.visual_phrase(rambling, "armor")
    assert len(phrase) == appearance.VISUAL_PHRASE_MAX


def test_signatures_are_safe_directory_names():
    payload = appearance.appearance_payload(_dressed(armor=COAT, weapon=SWORD))
    for sig in (payload["body_sig"], payload["action_sig"], "base", "base-unarmed"):
        assert appearance.is_valid_signature(sig)


def test_traversal_signatures_are_refused():
    for hostile in ("../../etc", "a/b", "base-unarmed-extra", "", None, "UPPER"):
        assert not appearance.is_valid_signature(hostile)


# ===========================================================================
# Group B — paths and completeness
# ===========================================================================


@pytest.fixture()
def manager(tmp_path, monkeypatch) -> AssetManager:
    """An AssetManager rooted in a temp directory (never the repo's data/)."""
    monkeypatch.chdir(tmp_path)
    return AssetManager("testworld")


def _fill_body_set(manager: AssetManager, character_id: str, sig: str, omit: str | None = None):
    directory = manager.sprite_dir(sig)
    for name in manager.body_frame_names(character_id):
        if name == omit:
            continue
        _png(directory / name)


def _fill_action_set(manager: AssetManager, character_id: str, sig: str, count: int = 6):
    directory = manager.sprite_dir(sig)
    for name in manager.action_frame_names(character_id)[:count]:
        _png(directory / name)


def test_sprite_dir_base_is_flat_legacy_path(manager):
    """A4: existing files keep working with zero migration and zero rename."""
    flat = manager.assets_dir / "sprites"
    assert manager.sprite_dir("base") == flat
    assert manager.sprite_dir("base-unarmed") == flat


def test_sprite_dir_variant_shape(manager):
    assert manager.sprite_dir("a3f9c1b2") == manager.assets_dir / "sprites" / "variants" / "a3f9c1b2"
    assert (manager.sprite_dir("a3f9c1b2-w9c14ab0")
            == manager.assets_dir / "sprites" / "variants" / "a3f9c1b2-w9c14ab0")


def test_sprite_dir_refuses_path_traversal(manager):
    """`?a=` is client-supplied and gets joined onto a path."""
    with pytest.raises(ValueError):
        manager.sprite_dir("../../../etc")


def test_body_set_is_58_frames(manager):
    names = manager.body_frame_names("pc")
    assert len(names) == 58 == 4 + 4 * 12 + 6
    assert len(set(names)) == 58


def test_body_set_complete_requires_every_frame(manager):
    """All-or-nothing: this is what keeps a player from idling in mail and
    walking in a shirt."""
    missing = "pc_right_walk7.png"
    _fill_body_set(manager, "pc", "a3f9c1b2", omit=missing)
    assert manager.body_set_complete("pc", "a3f9c1b2") is False
    _png(manager.sprite_dir("a3f9c1b2") / missing)
    assert manager.body_set_complete("pc", "a3f9c1b2") is True


def test_zero_byte_frame_does_not_count(manager):
    _fill_body_set(manager, "pc", "a3f9c1b2", omit="pc_front.png")
    (manager.sprite_dir("a3f9c1b2") / "pc_front.png").write_bytes(b"")
    assert manager.body_set_complete("pc", "a3f9c1b2") is False


def test_action_set_complete_requires_six_frames(manager):
    _fill_action_set(manager, "pc", "a3f9c1b2-w9c14ab0", count=5)
    assert manager.action_set_complete("pc", "a3f9c1b2-w9c14ab0") is False
    _fill_action_set(manager, "pc", "a3f9c1b2-w9c14ab0", count=6)
    assert manager.action_set_complete("pc", "a3f9c1b2-w9c14ab0") is True


def test_serving_falls_back_to_base_when_incomplete(manager):
    _fill_body_set(manager, "pc", "a3f9c1b2", omit="pc_dodge6.png")
    assert manager.serving_body_sig("pc", "a3f9c1b2") == "base"
    _png(manager.sprite_dir("a3f9c1b2") / "pc_dodge6.png")
    assert manager.serving_body_sig("pc", "a3f9c1b2") == "a3f9c1b2"


def test_serving_action_falls_back_to_base_pair(manager):
    assert manager.serving_action_sig("pc", "a3f9c1b2-w9c14ab0") == "base-unarmed"
    _fill_action_set(manager, "pc", "a3f9c1b2-w9c14ab0")
    assert manager.serving_action_sig("pc", "a3f9c1b2-w9c14ab0") == "a3f9c1b2-w9c14ab0"


def test_serving_returns_base_without_checking_base(manager):
    """A4: the flat path is the legacy self-healing one — an individual
    missing file 404s and spawns exactly as it does today."""
    assert not list((manager.assets_dir / "sprites").glob("*.png"))
    assert manager.serving_body_sig("pc", "base") == "base"
    assert manager.serving_action_sig("pc", "base-unarmed") == "base-unarmed"


def test_invalid_signature_is_never_complete(manager):
    assert manager.body_set_complete("pc", "../escape") is False
    assert manager.serving_body_sig("pc", "../escape") == "base"


def test_lru_eviction_respects_cap_and_keeps(manager, monkeypatch):
    monkeypatch.setattr(load_settings().appearance, "max_variants_per_player", 8)
    sigs = [f"var{i:05d}" for i in range(9)]
    for index, sig in enumerate(sigs):
        _fill_body_set(manager, "pc", sig)
        for frame in manager.sprite_dir(sig).glob("pc_*.png"):
            import os
            os.utime(frame, (1_700_000_000 + index, 1_700_000_000 + index))

    manager._evict_lru_variants("pc", keep={sigs[-1], sigs[-2]})

    assert not manager.sprite_dir(sigs[0]).exists()          # oldest went
    for sig in sigs[1:]:
        assert manager.sprite_dir(sig).is_dir(), sig
    assert (manager.assets_dir / "sprites" / "variants").is_dir()


def test_lru_eviction_never_touches_wanted_or_serving(manager, monkeypatch):
    monkeypatch.setattr(load_settings().appearance, "max_variants_per_player", 2)
    import os

    for index, sig in enumerate(("oldkeep0", "middle00", "newest00")):
        _fill_body_set(manager, "pc", sig)
        for frame in manager.sprite_dir(sig).glob("pc_*.png"):
            os.utime(frame, (1_700_000_000 + index, 1_700_000_000 + index))

    # "oldkeep0" is the oldest but it is what we are serving right now
    manager._evict_lru_variants("pc", keep={"oldkeep0", "newest00"})
    assert manager.sprite_dir("oldkeep0").is_dir()
    assert not manager.sprite_dir("middle00").exists()


def test_eviction_only_removes_this_players_frames(manager, monkeypatch):
    """Directories are signature-keyed, not player-keyed — two players in the
    same coat share one, and neither may delete the other's pixels."""
    monkeypatch.setattr(load_settings().appearance, "max_variants_per_player", 1)
    _fill_body_set(manager, "pc", "shared00")
    _fill_body_set(manager, "npc", "shared00")
    _fill_body_set(manager, "pc", "keeper00")

    manager._evict_lru_variants("pc", keep={"keeper00"})
    assert not (manager.sprite_dir("shared00") / "pc_front.png").exists()
    assert (manager.sprite_dir("shared00") / "npc_front.png").exists()


def test_completeness_memo_is_dropped_on_eviction(manager, monkeypatch):
    monkeypatch.setattr(load_settings().appearance, "max_variants_per_player", 1)
    _fill_body_set(manager, "pc", "doomed00")
    _fill_body_set(manager, "pc", "keeper00")
    assert manager.body_set_complete("pc", "doomed00") is True
    assert ("body", "pc", "doomed00") in manager._complete

    manager._evict_lru_variants("pc", keep={"keeper00"})
    assert ("body", "pc", "doomed00") not in manager._complete
    assert manager.body_set_complete("pc", "doomed00") is False


def test_clear_cache_drops_the_memo(manager):
    _fill_body_set(manager, "pc", "a3f9c1b2")
    assert manager.body_set_complete("pc", "a3f9c1b2") is True
    manager.clear_cache("sprites")
    assert manager.body_set_complete("pc", "a3f9c1b2") is False


# --- the readiness payload -------------------------------------------------


def test_appearance_status_shape_for_a_legacy_player(manager, db):
    with get_session() as session:
        session.add(Player(id="pc", name="Legacy", level=None, xp=None, equipment=None))
        session.commit()

    status = manager.appearance_status("pc")
    assert set(status) == {
        "player_id", "body_sig", "weapon_sig", "action_sig", "version",
        "body_ready", "action_ready", "serving_body", "serving_action",
        "generating", "failed",
    }
    assert status["body_sig"] == status["serving_body"] == "base"
    assert status["action_sig"] == status["serving_action"] == "base-unarmed"
    assert status["body_ready"] is True and status["action_ready"] is True
    assert status["generating"] is False and status["failed"] is False


def test_appearance_status_reports_a_pending_variant(manager, db):
    with get_session() as session:
        p = Player(id="pc", name="Dressed", level=1,
                   inventory=[COAT], equipment={"armor": COAT["id"]})
        session.add(p)
        session.commit()
        body_sig = appearance.body_signature(p)

    spawned = []
    manager.spawn_generation = lambda key, factory: spawned.append(key) or True

    async def _poll():                     # the endpoint is async: loop present
        return manager.appearance_status("pc")

    status = asyncio.run(_poll())
    assert status["body_sig"] == body_sig
    assert status["body_ready"] is False
    assert status["serving_body"] == "base"            # keep wearing the old set
    # Armour moves BOTH axes: the attack strip has to show the new coat too
    assert spawned == [f"body:pc:{body_sig}", f"action:pc:{body_sig}-unarmed"]

    _fill_body_set(manager, "pc", body_sig)
    ready = manager.appearance_status("pc", spawn=False)
    assert ready["body_ready"] is True
    assert ready["serving_body"] == body_sig


def test_appearance_status_reports_failure(manager, db):
    import time

    with get_session() as session:
        p = Player(id="pc", name="Dressed", level=1,
                   inventory=[COAT], equipment={"armor": COAT["id"]})
        session.add(p)
        session.commit()
        body_sig = appearance.body_signature(p)

    manager._failures[manager.body_key("pc", body_sig)] = time.monotonic()
    status = manager.appearance_status("pc")
    assert status["failed"] is True
    assert status["generating"] is False
    assert status["serving_body"] == "base"            # the game plays on


def test_no_player_row_reads_as_base(manager, db):
    assert manager.appearance_status("ghost")["body_sig"] == "base"


# ===========================================================================
# Group C — prompts (pure strings, no API call)
# ===========================================================================


@pytest.fixture()
def generator(tmp_path, monkeypatch) -> ImageGenerator:
    monkeypatch.chdir(tmp_path)
    return ImageGenerator("testworld")


CHARACTER = SimpleNamespace(id="pc", name="Rook", description_physical="tall, ash-scarred")
BIBLE = SimpleNamespace(visual_style="grimy ink-wash", color_palette=["soot", "brass"])


def test_outfit_prompt_names_the_visual_desc(generator):
    prompt = generator._build_outfit_idle_prompt(CHARACTER, BIBLE, "front", COAT["visual_desc"])
    assert COAT["visual_desc"] in prompt
    assert "ONLY ONE CHARACTER, ONE POSE" in prompt
    assert "Solid bright green background (#00FF00)" in prompt
    assert "grimy ink-wash" in prompt
    assert "front-facing view, looking at viewer" in prompt


def test_outfit_prompt_forbids_weapons_and_redesign(generator):
    prompt = generator._build_outfit_idle_prompt(CHARACTER, BIBLE, "right", "a mail hauberk")
    flowed = " ".join(prompt.split())     # the prompt is hard-wrapped
    assert "Do not redesign the face." in flowed
    assert "Do not change their proportions or their height." in flowed
    assert "Do not add a weapon, a prop, a backdrop, or a companion." in flowed
    assert "right side profile view" in prompt


def test_default_action_prompt_is_byte_identical_to_legacy(generator):
    """The two prompt changes must not move a single existing character."""
    n = ImageGenerator.ACTION_KEYFRAME_COUNT
    legacy = f"""The attached image shows the character "Rook" standing idle, from a 2D RPG.
Draw a sprite sheet: this EXACT character performing a {n}-frame attack animation.

Format: a single horizontal filmstrip of {n} frames. Divide the image into {n} equal
vertical columns; place exactly one full-body pose centered in each column, with
clear empty space between poses (poses never touch the image edges).

The motion must be dynamic and clearly readable, like classic action-game animation:
{ImageGenerator._ACTION_CHOREOGRAPHY["attack"]}

Every frame: the SAME character design — clothing, colors, face and proportions
matching the reference image EXACTLY (do not redesign or simplify the outfit),
identical art style (grimy ink-wash), identical scale, one shared ground line.
If the character carries a weapon in the reference, they use that weapon;
otherwise they fight bare-handed.
But every frame is a DIFFERENT pose from the choreography above — no two frames in
the strip may repeat the same pose.
The ENTIRE canvas must be one continuous solid white background from edge to edge —
no black areas, no cards, no panels, no grid lines, no frame borders, no numbers,
no text, no shadows."""
    assert generator._build_action_cycle_prompt(CHARACTER, BIBLE, "attack") == legacy


def test_attack_prompt_names_the_weapon(generator):
    prompt = generator._build_action_cycle_prompt(
        CHARACTER, BIBLE, "attack", "a notched iron shortsword"
    )
    assert "The character is striking with a notched iron shortsword." in prompt
    assert "DRAWN AND IN HAND in" in prompt
    assert "never disappears" in prompt
    assert "otherwise they fight bare-handed." not in prompt


def test_attack_prompt_says_barehanded_when_unarmed(generator):
    prompt = generator._build_action_cycle_prompt(CHARACTER, BIBLE, "attack", "")
    assert "The character fights BARE-HANDED." in prompt
    assert "striking with" not in prompt
    assert "If the character carries a weapon in the reference" not in prompt


def test_dodge_prompt_never_mentions_a_weapon(generator):
    """A3: a roll shows the outfit, not the sword."""
    prompt = generator._build_action_cycle_prompt(CHARACTER, BIBLE, "dodge")
    assert "dodge-roll" in prompt
    assert SWORD["visual_desc"] not in prompt
    assert "striking with" not in prompt


def test_unknown_action_still_raises(generator):
    with pytest.raises(ValueError):
        generator._build_action_cycle_prompt(CHARACTER, BIBLE, "moonwalk")


def test_default_args_reproduce_legacy_paths(generator):
    """`sig="base"` must land on today's flat filenames, unchanged."""
    assert generator.sprite_rel("base", "pc_right_walk7.png") == "sprites/pc_right_walk7.png"
    assert generator.sprite_rel("base-unarmed", "pc_attack3.png") == "sprites/pc_attack3.png"
    assert (generator.sprite_rel("a3f9c1b2", "pc_front.png")
            == "sprites/variants/a3f9c1b2/pc_front.png")
    with pytest.raises(ValueError):
        generator.sprite_rel("../oops", "pc_front.png")


def test_mirror_walk_cycle_honours_the_signature(generator):
    for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
        _png(generator.sprite_path("a3f9c1b2", f"pc_right_walk{frame}.png"), size=(6, 4))
    paths = generator.mirror_walk_cycle("pc", "right", "left", sig="a3f9c1b2")
    assert len(paths) == ImageGenerator.WALK_FRAME_COUNT
    assert all("variants" in p and "a3f9c1b2" in p for p in paths.values())
    assert generator.sprite_path("a3f9c1b2", "pc_left_walk1.png").exists()
    assert not generator.sprite_path("base", "pc_left_walk1.png").exists()


# ===========================================================================
# Group D — orchestration (stubbed image client; NO api calls, ever)
# ===========================================================================


@pytest.fixture()
def stub_images(monkeypatch, manager):
    """Replace the three generating methods with file-writing fakes.

    Everything below this line is orchestration: which reference goes into
    which call, how many calls, which directory the frames land in.
    """
    calls: list[dict] = []

    async def fake_idle(self, character, world_bible, direction, reference_image, outfit_desc, sig):
        calls.append({"kind": "idle", "direction": direction, "sig": sig,
                      "outfit": outfit_desc, "reference": reference_image})
        dest = self.sprite_path(sig, f"{character.id}_{direction}.png")
        _png(dest)
        return str(dest)

    async def fake_walk(self, character, world_bible, direction, reference_image,
                        sig=appearance.BASE_BODY_SIG):
        calls.append({"kind": "walk", "direction": direction, "sig": sig,
                      "reference": reference_image})
        paths = {}
        for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
            dest = self.sprite_path(sig, f"{character.id}_{direction}_walk{frame}.png")
            _png(dest)
            paths[f"{direction}_walk{frame}"] = str(dest)
        return paths

    async def fake_action(self, character, world_bible, action, reference_image, *,
                          weapon_desc=None, sig=appearance.BASE_ACTION_SIG):
        calls.append({"kind": action, "sig": sig, "weapon_desc": weapon_desc,
                      "reference": reference_image})
        paths = {}
        for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1):
            dest = self.sprite_path(sig, f"{character.id}_{action}{frame}.png")
            _png(dest)
            paths[f"{action}{frame}"] = str(dest)
        return paths

    monkeypatch.setattr(ImageGenerator, "generate_outfit_variant_idle", fake_idle)
    monkeypatch.setattr(ImageGenerator, "generate_walk_cycle", fake_walk)
    monkeypatch.setattr(ImageGenerator, "generate_action_cycle", fake_action)
    return calls


def _seed_base_sprites(manager: AssetManager, character_id: str, walk: bool = True) -> None:
    """The 52 flat legacy frames, so `ensure_all_sprites_generated` short-circuits."""
    flat = manager.sprite_dir("base")
    for direction in AssetManager.DIRECTIONS:
        _png(flat / f"{character_id}_{direction}.png", size=(8, 8))
        if walk:
            for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
                _png(flat / f"{character_id}_{direction}_walk{frame}.png")


def _seed_dressed_player(armor: dict | None = COAT, weapon: dict | None = None) -> str:
    with get_session() as session:
        inventory = [item for item in (armor, weapon) if item]
        p = Player(id="pc", name="Rook", level=1, inventory=inventory,
                   equipment={"armor": armor["id"] if armor else None,
                              "weapon": weapon["id"] if weapon else None,
                              "trinket": None})
        session.add(p)
        session.commit()
    return "pc"


def test_ensure_body_set_builds_58_files_in_seven_calls(manager, db, stub_images):
    _seed_dressed_player()
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))

    paths = asyncio.run(manager.ensure_body_set("pc", sig))

    assert len([c for c in stub_images if c["sig"] == sig]) == 7
    assert sorted(c["kind"] for c in stub_images if c["sig"] == sig) == [
        "dodge", "idle", "idle", "idle", "walk", "walk", "walk",
    ]
    assert manager.body_set_complete("pc", sig) is True
    directory = manager.sprite_dir(sig)
    assert len(list(directory.glob("pc_*.png"))) == 58
    assert set(paths) >= {"front", "back", "left", "right", "left_walk12", "dodge6"}


def test_variant_idles_chain_through_the_variant_front(manager, db, stub_images):
    """Face drift is the failure mode: back and right must be drawn from the
    VARIANT front, never from the base one (which still wears the old coat)."""
    _seed_dressed_player()
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))

    asyncio.run(manager.ensure_body_set("pc", sig))

    base_front = (manager.sprite_dir("base") / "pc_front.png").read_bytes()
    variant_front = (manager.sprite_dir(sig) / "pc_front.png").read_bytes()
    idles = {c["direction"]: c for c in stub_images if c["kind"] == "idle"}
    assert idles["front"]["reference"] == base_front
    assert idles["back"]["reference"] == variant_front
    assert idles["right"]["reference"] == variant_front
    assert all(c["outfit"] == COAT["visual_desc"] for c in idles.values())


def test_dodge_is_on_the_body_axis_and_never_named_a_weapon(manager, db, stub_images):
    """A3 — and the dodge strip lands in the BODY directory, not the pair's."""
    _seed_dressed_player(weapon=SWORD)
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))

    asyncio.run(manager.ensure_body_set("pc", sig))

    dodge = [c for c in stub_images if c["kind"] == "dodge"]
    assert len(dodge) == 1
    assert dodge[0]["sig"] == sig
    assert dodge[0]["weapon_desc"] is None
    assert (manager.sprite_dir(sig) / "pc_dodge1.png").exists()


def test_ensure_body_set_reuses_frames_already_on_disk(manager, db, stub_images):
    """A killed process leaves a half-written directory; the completed frames
    inside it are re-derived, not re-billed."""
    _seed_dressed_player()
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))
    _png(manager.sprite_dir(sig) / "pc_front.png")
    for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
        _png(manager.sprite_dir(sig) / f"pc_front_walk{frame}.png")

    asyncio.run(manager.ensure_body_set("pc", sig))

    kinds = [(c["kind"], c.get("direction")) for c in stub_images if c["sig"] == sig]
    assert ("idle", "front") not in kinds
    assert ("walk", "front") not in kinds
    assert len(kinds) == 5


def test_complete_body_set_costs_nothing(manager, db, stub_images):
    _seed_dressed_player()
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))
    _fill_body_set(manager, "pc", sig)

    asyncio.run(manager.ensure_body_set("pc", sig))
    assert stub_images == []


def test_base_body_set_generates_the_dodge_strip_only(manager, db, stub_images):
    """`sig == 'base'` delegates to today's path plus the base dodge strip."""
    _seed_dressed_player(armor=None)
    _seed_base_sprites(manager, "pc")

    asyncio.run(manager.ensure_body_set("pc", "base"))

    assert [c["kind"] for c in stub_images] == ["dodge"]
    assert stub_images[0]["sig"] == "base"
    assert (manager.sprite_dir("base") / "pc_dodge1.png").exists()


def test_stale_body_signature_is_refused(manager, db, stub_images):
    """Painting today's coat into yesterday's directory would poison that
    cache forever — a variant is never generated for a signature the gear no
    longer asks for."""
    _seed_dressed_player()
    _seed_base_sprites(manager, "pc")
    with pytest.raises(ValueError, match="Stale body signature"):
        asyncio.run(manager.ensure_body_set("pc", "deadbeef"))
    assert stub_images == []


def test_ensure_action_set_names_the_weapon_and_uses_the_variant_idle(manager, db, stub_images):
    _seed_dressed_player(armor=COAT, weapon=SWORD)
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        p = session.get(Player, "pc")
        body_sig, action_sig = appearance.body_signature(p), appearance.action_signature(p)
    _fill_body_set(manager, "pc", body_sig)

    paths = asyncio.run(manager.ensure_action_set("pc", action_sig))

    assert len(stub_images) == 1
    call = stub_images[0]
    assert call["kind"] == "attack"
    assert call["sig"] == action_sig
    assert call["weapon_desc"] == SWORD["visual_desc"]
    assert call["reference"] == (manager.sprite_dir(body_sig) / "pc_right.png").read_bytes()
    assert len(paths) == 6
    assert manager.action_set_complete("pc", action_sig) is True


def test_armored_but_unarmed_pair_says_barehanded(manager, db, stub_images):
    _seed_dressed_player(armor=COAT)
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        p = session.get(Player, "pc")
        body_sig, action_sig = appearance.body_signature(p), appearance.action_signature(p)
    _fill_body_set(manager, "pc", body_sig)

    asyncio.run(manager.ensure_action_set("pc", action_sig))
    assert stub_images[0]["weapon_desc"] == ""


def test_action_reference_falls_back_down_the_ladder(manager, db, stub_images):
    """Variant right -> variant front -> base right -> base front."""
    _seed_dressed_player(armor=COAT, weapon=SWORD)
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        p = session.get(Player, "pc")
        body_sig, action_sig = appearance.body_signature(p), appearance.action_signature(p)

    asyncio.run(manager.ensure_action_set("pc", action_sig))
    assert stub_images[0]["reference"] == (manager.sprite_dir("base") / "pc_right.png").read_bytes()


def test_stale_action_signature_is_refused(manager, db, stub_images):
    _seed_dressed_player(weapon=SWORD)
    _seed_base_sprites(manager, "pc")
    with pytest.raises(ValueError, match="Stale action signature"):
        asyncio.run(manager.ensure_action_set("pc", "base-w0000000"))
    assert stub_images == []


def test_npc_attack_stays_on_the_base_signature(manager, db, stub_images):
    with get_session() as session:
        session.add(NPC(id="guard", name="Harbor Guard"))
        session.commit()
    _png(manager.sprite_dir("base") / "guard_front.png", size=(8, 8))

    paths = asyncio.run(manager.ensure_attack_sprites("guard", "npc"))

    assert stub_images[0]["sig"] == "base-unarmed"
    assert stub_images[0]["weapon_desc"] is None       # legacy prompt, unchanged
    assert all("variants" not in p for p in paths.values())


def test_legacy_action_shim_still_makes_attack_and_dodge_at_base(manager, db, stub_images):
    """`ensure_action_sprites` keeps working byte-identically until the sprite
    endpoint learns the axis router."""
    _seed_dressed_player(armor=None)
    _seed_base_sprites(manager, "pc")

    paths = asyncio.run(manager.ensure_action_sprites("pc", "player"))

    assert sorted(c["kind"] for c in stub_images) == ["attack", "dodge"]
    assert {c["sig"] for c in stub_images} == {"base-unarmed", "base"}
    assert {c["weapon_desc"] for c in stub_images} == {None}
    for frame in range(1, 7):
        assert (manager.sprite_dir("base") / f"pc_attack{frame}.png").exists()
        assert (manager.sprite_dir("base") / f"pc_dodge{frame}.png").exists()
    assert set(paths) == {f"attack{i}" for i in range(1, 7)} | {f"dodge{i}" for i in range(1, 7)}


def test_missing_idle_refuses_instead_of_guessing(manager, db, stub_images):
    _seed_dressed_player(armor=None)
    with pytest.raises(ValueError, match="No idle sprite"):
        asyncio.run(manager.ensure_attack_sprites("pc", "player"))


# --- the scene fetch -------------------------------------------------------


def _seed_scene(manager: AssetManager, armor: dict | None) -> str:
    with get_session() as session:
        session.add(Location(id="square", name="Harbor Square", type=LocationType.DISTRICT))
        session.add(Player(
            id="pc", name="Rook", level=1, current_location_id="square",
            inventory=[armor] if armor else [],
            equipment={"armor": armor["id"] if armor else None, "weapon": None, "trinket": None},
        ))
        session.commit()
    return "square"


def test_location_assets_carry_the_appearance_block(manager, db):
    _seed_scene(manager, COAT)
    _seed_base_sprites(manager, "pc")
    spawned = []
    manager.spawn_generation = lambda key, factory: spawned.append(key) or True

    assets = asyncio.run(manager.get_location_assets_fast("square", "pc"))

    block = assets["appearance"]
    assert block["body_sig"] != "base"
    assert block["serving_body"] == "base"             # the variant is not built yet
    assert block["body_ready"] is False
    assert block["action_ready"] is False              # the coat shows in the swing too
    assert assets["pending"] is True
    assert f"body:pc:{block['body_sig']}" in spawned
    # The scene is served the SERVING set — never a half-dressed frame
    assert assets["player"]["sprite_path"].endswith("pc_front.png")
    assert "variants" not in assets["player"]["sprite_path"]


def test_location_assets_serve_a_finished_variant(manager, db):
    _seed_scene(manager, COAT)
    _seed_base_sprites(manager, "pc")
    with get_session() as session:
        sig = appearance.body_signature(session.get(Player, "pc"))
    _fill_body_set(manager, "pc", sig)
    spawned = []
    manager.spawn_generation = lambda key, factory: spawned.append(key) or True

    assets = asyncio.run(manager.get_location_assets_fast("square", "pc"))

    assert assets["appearance"]["serving_body"] == sig
    assert assets["appearance"]["body_ready"] is True
    assert sig in assets["player"]["sprite_path"]
    assert not [key for key in spawned if key.startswith("body:")]


def test_legacy_scene_spawns_the_legacy_way(manager, db):
    """A NULL-everything world: no variant directory is created, and a world
    with idles+walks but no dodge does not start paying for one."""
    _seed_scene(manager, None)
    _seed_base_sprites(manager, "pc")
    spawned = []
    manager.spawn_generation = lambda key, factory: spawned.append(key) or True

    assets = asyncio.run(manager.get_location_assets_fast("square", "pc"))

    assert assets["appearance"]["body_sig"] == "base"
    assert assets["appearance"]["body_ready"] is True
    assert assets["appearance"]["action_ready"] is True
    # Only the missing background is pending — no sprite work of any kind
    assert spawned == ["bg:square"]
    assert not (manager.assets_dir / "sprites" / "variants").exists()
