"""AI in-betweening for walk cycles via RIFE (rife-ncnn-vulkan).

The animation-studio split: the image model draws the KEY poses (6 per
cycle), RIFE draws the in-betweens — doubling to 12 frames locally, in
seconds, at zero API cost.

The binary is fetched once into data/tools/ (not vendored: ~40 MB). If RIFE
is unavailable (download blocked, no Vulkan device), interpolate_cycle
still honors its contract by duplicating keys — callers always get 2n
frames, so the on-disk cycle is always complete and asset checks never
trigger a paid regeneration loop.
"""

import io
import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

RIFE_RELEASE_URL = (
    "https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
    "20221029/rife-ncnn-vulkan-20221029-windows.zip"
)
RIFE_DIR = Path("data/tools/rife-ncnn-vulkan")
RIFE_MODEL = "rife-v4.6"

_rife_exe: Path | None = None
_rife_checked = False


def ensure_rife() -> Path | None:
    """Return the rife executable, downloading it on first use. None if unavailable."""
    global _rife_exe, _rife_checked
    if _rife_checked:
        return _rife_exe
    _rife_checked = True

    exe = RIFE_DIR / "rife-ncnn-vulkan.exe"
    if exe.exists():
        _rife_exe = exe
        return exe

    try:
        logger.info("Downloading rife-ncnn-vulkan (one-time, ~40 MB)...")
        RIFE_DIR.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            data = client.get(RIFE_RELEASE_URL).raise_for_status().content
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(td)
            # The zip contains one versioned top-level directory
            extracted = next(Path(td).iterdir())
            if RIFE_DIR.exists():
                shutil.rmtree(RIFE_DIR)
            shutil.move(str(extracted), str(RIFE_DIR))
        if exe.exists():
            _rife_exe = exe
            logger.info(f"rife-ncnn-vulkan installed at {RIFE_DIR}")
    except Exception as e:
        logger.warning(f"Could not install rife-ncnn-vulkan ({e}); walk cycles stay at key frames")
    return _rife_exe


def _run_rife(exe: Path, a: Path, b: Path, out: Path) -> None:
    subprocess.run(
        [str(exe), "-0", str(a), "-1", str(b), "-o", str(out),
         "-m", str(exe.parent / RIFE_MODEL)],
        check=True, capture_output=True, timeout=120,
    )


def _interpolate_pair(exe: Path, a: Image.Image, b: Image.Image, workdir: Path, tag: str) -> Image.Image:
    """Midpoint frame between two RGBA sprites, alpha-safe.

    RIFE works on RGB, so color (composited on neutral gray) and the alpha
    channel (as grayscale RGB) are interpolated separately and recombined.
    """
    def rgb_of(img: Image.Image) -> Image.Image:
        bg = Image.new("RGBA", img.size, (128, 128, 128, 255))
        bg.alpha_composite(img)
        return bg.convert("RGB")

    def alpha_of(img: Image.Image) -> Image.Image:
        return Image.merge("RGB", [img.getchannel("A")] * 3)

    mids: list[Image.Image] = []
    for kind, fa, fb in (("rgb", rgb_of(a), rgb_of(b)), ("alpha", alpha_of(a), alpha_of(b))):
        pa, pb, po = (workdir / f"{tag}_{kind}_{s}.png" for s in ("a", "b", "mid"))
        fa.save(pa)
        fb.save(pb)
        _run_rife(exe, pa, pb, po)
        mids.append(Image.open(po))

    mid = mids[0].convert("RGBA")
    mid.putalpha(mids[1].convert("L"))
    return mid


def interpolate_cycle(keys: list[Image.Image]) -> list[Image.Image]:
    """Double a looping cycle: n keys -> 2n frames (key, in-between, key, ...).

    The last in-between wraps from the final key back to the first so the
    loop stays seamless. Falls back to duplicating keys if RIFE is
    unavailable or errors — the frame-count contract always holds.
    """
    keys = [k.convert("RGBA") for k in keys]
    size = keys[0].size
    keys = [k if k.size == size else k.resize(size) for k in keys]

    exe = ensure_rife()
    if exe is None:
        return [f for k in keys for f in (k, k.copy())]

    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        for i, key in enumerate(keys):
            nxt = keys[(i + 1) % len(keys)]
            try:
                mid = _interpolate_pair(exe, key, nxt, workdir, f"p{i}")
            except Exception as e:
                logger.warning(f"RIFE failed on pair {i} ({e}); duplicating key")
                mid = key.copy()
            frames.append(key)
            frames.append(mid)
    return frames
