"""interpolate_cycle must always honor the 2n frame contract.

If it ever returned fewer frames, the on-disk walk cycle would be
incomplete and asset checks would trigger paid regenerations forever.
"""

from PIL import Image

from src.services import frame_interpolator


def _keys(n=6, size=(32, 32)):
    return [Image.new("RGBA", size, (i * 20, 100, 100, 255)) for i in range(n)]


def test_doubles_frames_without_rife(monkeypatch):
    monkeypatch.setattr(frame_interpolator, "ensure_rife", lambda: None)
    frames = frame_interpolator.interpolate_cycle(_keys())
    assert len(frames) == 12
    # Keys sit at even indices, duplicates follow them
    for i in range(6):
        assert frames[2 * i].tobytes() == frames[2 * i + 1].tobytes()


def test_rife_pair_failure_falls_back_per_pair(monkeypatch):
    monkeypatch.setattr(frame_interpolator, "ensure_rife", lambda: frame_interpolator.RIFE_DIR / "missing.exe")

    def boom(*args, **kwargs):
        raise RuntimeError("no vulkan device")

    monkeypatch.setattr(frame_interpolator, "_interpolate_pair", boom)
    frames = frame_interpolator.interpolate_cycle(_keys())
    assert len(frames) == 12


def test_mixed_sizes_are_normalized(monkeypatch):
    monkeypatch.setattr(frame_interpolator, "ensure_rife", lambda: None)
    keys = _keys(6, (32, 32))
    keys[3] = Image.new("RGBA", (48, 48), (0, 0, 0, 255))
    frames = frame_interpolator.interpolate_cycle(keys)
    assert all(f.size == (32, 32) for f in frames)
