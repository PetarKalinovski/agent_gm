"""Filmstrip slicing and frame validation for generated sprite cycles.

The image model draws six poses on a white strip but never exactly on the
six-column grid, and decorates the strip with lines and stray marks. These
tests pin the behaviours that keep a bad drawing from becoming a bad frame:

- poses are found by the gaps between them, wherever they landed
- a pose that straddles a grid wall (fallback path) is flagged `clipped`
- small disconnected alpha islands are dropped after background removal
- a clipped or underweight frame is replaced, never shipped
"""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from src.services.image_generator import ImageGenerator


def _generator(tmp_path) -> ImageGenerator:
    gen = ImageGenerator.__new__(ImageGenerator)
    gen.assets_dir = tmp_path
    return gen


def _strip(width: int, height: int, blobs: list[tuple[int, int, int, int]],
           *, ground_line: bool = False, grid_lines: bool = False,
           stray: tuple[int, int] | None = None) -> bytes:
    """A white strip with dark rectangles (poses) and optional decorations."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in blobs:
        d.rectangle((x0, y0, x1, y1), fill=(40, 40, 60))
    if ground_line:
        d.line((0, height - 30, width, height - 30), fill=(0, 0, 0), width=3)
    if grid_lines:
        cell = width // 6
        for i in range(1, 6):
            d.line((i * cell, 0, i * cell, height), fill=(0, 0, 0), width=2)
    if stray:
        d.ellipse((stray[0], stray[1], stray[0] + 8, stray[1] + 8), fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dark_mass(cell: Image.Image) -> int:
    return int((np.asarray(cell.convert("L")) <= 240).sum())


# --------------------------------------------------------------------------
# Gap segmentation
# --------------------------------------------------------------------------

def test_gap_slicing_finds_poses_off_the_grid(tmp_path):
    """Six poses, one deliberately straddling the 3rd/4th grid wall, come
    back whole — with a ground line, grid lines and a stray mark present."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = []
    for i in range(6):
        cx = i * cell + cell // 2
        if i == 3:
            cx = 3 * cell + 4          # straddles the wall between cells 3 and 4
        blobs.append((cx - 40, 60, cx + 40, 340))
    data = _strip(w, h, blobs, ground_line=True, grid_lines=True, stray=(30, 30))

    cells = gen._slice_walk_strip(data, 6)

    assert len(cells) == 6
    masses = [_dark_mass(c) for c in cells]
    expected = 81 * 281
    for m in masses:
        # whole pose (not a half) and no grid line / ground line inside
        assert expected * 0.95 <= m <= expected * 1.15
    assert all(c.info.get("clipped") is False for c in cells)


def test_ground_line_with_a_soft_fringe_does_not_join_the_poses(tmp_path):
    """The live failure: a drawn ground line runs under all six poses. Its
    dark core is a full-width row, its anti-aliased fringe is not — and the
    fringe alone was enough to make every column look occupied. The band
    must be removed whole and painted out of the frames."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = [(i * cell + 60, 60, i * cell + cell - 60, 340) for i in range(6)]
    img = Image.open(io.BytesIO(_strip(w, h, blobs)))
    d = ImageDraw.Draw(img)
    d.line((0, 345, w, 345), fill=(215, 215, 215), width=14)  # fringe
    d.line((0, 345, w, 345), fill=(0, 0, 0), width=4)         # core
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    cells = gen._slice_walk_strip(buf.getvalue(), 6)

    assert len(cells) == 6
    for c in cells:
        assert c.info.get("clipped") is False
        arr = np.asarray(c.convert("L"))
        # the line band is gone from the frame pixels, not just the mask
        assert not (arr[-20:, :] <= 240).all(axis=1).any()


def test_slanted_ground_line_still_yields_six_poses(tmp_path):
    """A slanted thin line never fills 80% of any row, but leaves a few
    pixels in every column. Column occupancy is a pixel count, not `any`."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = [(i * cell + 60, 60, i * cell + cell - 60, 340) for i in range(6)]
    img = Image.open(io.BytesIO(_strip(w, h, blobs)))
    ImageDraw.Draw(img).line((0, 350, w, 380), fill=(0, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    cells = gen._slice_walk_strip(buf.getvalue(), 6)

    assert len(cells) == 6
    assert all(c.info.get("clipped") is False for c in cells)


def test_gap_slicing_joins_a_detached_weapon_tip(tmp_path):
    """A few pixels of gap between a body and its weapon tip is one pose."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = []
    for i in range(6):
        cx = i * cell + cell // 2
        blobs.append((cx - 40, 60, cx + 40, 340))
    blobs.append((5 * cell + cell // 2 + 40 + 6, 100, 5 * cell + cell // 2 + 40 + 30, 120))
    data = _strip(w, h, blobs)

    cells = gen._slice_walk_strip(data, 6)

    assert len(cells) == 6
    assert cells[5].width > cells[0].width  # the tip rode along with pose 6


def test_touching_poses_fall_back_to_the_grid_and_flag_clipping(tmp_path):
    """No gaps between poses → grid slicing; the pose crossing a wall is
    `clipped` on both sides, the well-centred ones are not."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    # Pose A spills over the wall between cells 1 and 2 and pose B starts
    # right where A ends (touching): gap slicing sees five runs, not six.
    blobs = [
        (0 * cell + 60, 60, 0 * cell + cell - 60, 340),
        (1 * cell + 60, 60, 2 * cell + 30, 340),           # A, crosses the wall
        (2 * cell + 30, 60, 2 * cell + cell - 60, 340),    # B, touches A
        (3 * cell + 60, 60, 3 * cell + cell - 60, 340),
        (4 * cell + 60, 60, 4 * cell + cell - 60, 340),
        (5 * cell + 60, 60, 5 * cell + cell - 60, 340),
    ]
    data = _strip(w, h, blobs)

    cells = gen._slice_walk_strip(data, 6)

    assert len(cells) == 6
    flags = [c.info.get("clipped") for c in cells]
    assert flags == [False, True, True, False, False, False]


def test_five_poses_split_one_and_flag_both_halves(tmp_path):
    """The model drew five poses. Blob slicing still hands back six cells
    — the widest pose split down the middle — but flags both halves as
    cut so validation replaces them instead of shipping half-bodies."""
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = [(i * cell + 60, 60, i * cell + cell - 60, 340) for i in range(5)]

    cells = gen._slice_walk_strip(_strip(w, h, blobs), 6)

    assert len(cells) == 6
    assert sum(1 for c in cells if c.info.get("clipped")) == 2


def test_grid_fallback_raises_on_an_empty_cell(tmp_path):
    gen = _generator(tmp_path)
    w, h = 1200, 400
    cell = w // 6
    blobs = [(i * cell + 60, 60, i * cell + cell - 60, 340) for i in range(5)]
    img = Image.open(io.BytesIO(_strip(w, h, blobs))).convert("RGB")
    mask = np.asarray(img.convert("L")) <= 240
    with pytest.raises(ValueError, match="empty"):
        gen._cells_from_grid(img, mask, 6)


# --------------------------------------------------------------------------
# Component filter
# --------------------------------------------------------------------------

def _rgba_with_islands() -> Image.Image:
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((60, 20, 140, 180), fill=(200, 50, 50, 255))   # body
    d.rectangle((10, 170, 20, 180), fill=(255, 255, 255, 255))  # debris (tiny)
    d.rectangle((150, 30, 190, 60), fill=(90, 90, 200, 255))    # a shield-sized part
    return img


def test_component_filter_drops_debris_and_keeps_big_parts():
    cleaned = ImageGenerator._keep_main_components(_rgba_with_islands())
    alpha = np.asarray(cleaned.getchannel("A"))
    assert alpha[175, 15] == 0          # debris gone
    assert alpha[100, 100] == 255       # body intact
    assert alpha[45, 170] == 255        # 41×31 part (~10% of body) kept


def test_component_filter_is_identity_for_a_single_component():
    img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 40, 40), fill=(1, 2, 3, 255))
    out = ImageGenerator._keep_main_components(img)
    assert np.array_equal(np.asarray(out), np.asarray(img))


# --------------------------------------------------------------------------
# Validation: clipped / underweight frames are replaced
# --------------------------------------------------------------------------

def _white_to_alpha(png: bytes) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    arr = np.array(img)
    white = (arr[..., :3] > 240).all(axis=-1)
    arr[..., 3] = np.where(white, 0, 255)
    out = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(out, format="PNG")
    return out.getvalue()


def _idle_png() -> bytes:
    img = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((110, 30, 190, 270), fill=(40, 40, 60, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _cell(mass_w: int, clipped: bool) -> Image.Image:
    img = Image.new("RGB", (mass_w + 20, 300), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((10, 20, 10 + mass_w, 280), fill=(40, 40, 60))
    img.info["clipped"] = clipped
    return img


def test_clipped_cell_is_replaced_by_its_neighbour(tmp_path, monkeypatch):
    gen = _generator(tmp_path)
    monkeypatch.setattr(gen, "_remove_background", _white_to_alpha)
    cells = [_cell(80, False) for _ in range(6)]
    cells[3] = _cell(80, True)  # same mass, but flagged as straddling a wall

    frames = gen._normalize_walk_frames(cells, _idle_png(), partner_repair=False)

    assert len(frames) == 6
    a3 = np.asarray(frames[3].getchannel("A"))
    a2 = np.asarray(frames[2].getchannel("A"))
    assert np.array_equal(a3, a2)  # donor = nearest intact neighbour


def test_half_character_is_underweight_and_replaced(tmp_path, monkeypatch):
    """A frame at 45% of the median mass (the live half-body case) is
    broken under the tightened threshold."""
    gen = _generator(tmp_path)
    monkeypatch.setattr(gen, "_remove_background", _white_to_alpha)
    cells = [_cell(80, False) for _ in range(6)]
    cells[2] = _cell(36, False)

    frames = gen._normalize_walk_frames(cells, _idle_png(), partner_repair=True)

    mass = [int((np.asarray(f.getchannel("A")) > 128).sum()) for f in frames]
    assert max(mass) - min(mass) < max(mass) * 0.02  # all frames full weight now


def test_debug_strip_is_saved_beside_the_assets(tmp_path):
    gen = _generator(tmp_path)
    gen._save_debug_strip("attack", "pc-1", b"\x89PNG-not-really")
    saved = list((tmp_path / "debug" / "strips").glob("pc-1_attack_*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG-not-really"


def test_motion_score_separates_walking_from_standing():
    still = [Image.new("RGBA", (50, 50), (0, 0, 0, 255)) for _ in range(6)]
    assert ImageGenerator._motion_score(still) == pytest.approx(0.0)

    moving = []
    for i in range(6):
        img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
        x = 5 + i * 7
        ImageDraw.Draw(img).rectangle((x, 0, x + 10, 49), fill=(0, 0, 0, 255))
        moving.append(img)
    assert ImageGenerator._motion_score(moving) > 0.3


# --------------------------------------------------------------------------
# Beasts attack like beasts
# --------------------------------------------------------------------------

from types import SimpleNamespace


def _character(**kw):
    base = {"id": "x", "name": "", "species": None, "description_physical": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def test_species_and_name_decide_beasthood():
    assert ImageGenerator._is_beast(_character(name="Hollow Wolf"))
    assert ImageGenerator._is_beast(_character(name="Grey Thing", species="dire wolf"))
    assert ImageGenerator._is_beast(_character(name="Scar", description_physical="A lean grey wolf with a torn ear"))
    assert not ImageGenerator._is_beast(_character(name="Vex Marrow", species="human",
                                                    description_physical="wolf-tooth trophies on his belt"))
    assert not ImageGenerator._is_beast(_character(name="Captain Brann Coldiron"))
    assert not ImageGenerator._is_beast(_character(name="Wolfgang the Smith", species="human"))


def test_beast_prompt_keeps_it_on_four_legs(tmp_path):
    gen = _generator(tmp_path)
    bible = SimpleNamespace(visual_style="storybook")
    wolf = gen._build_action_cycle_prompt(_character(name="Hollow Wolf"), bible, "attack")
    man = gen._build_action_cycle_prompt(_character(name="Vex Marrow", species="human"), bible, "attack",
                                         weapon_desc="a notched hunting knife")
    assert "ON ALL FOURS" in wolf and "teeth and claws" in wolf
    assert "weapon arm" not in wolf
    assert "ON ALL FOURS" not in man and "hunting knife" in man
