"""NPC dialogue TTS with dual backend, ported from the telltale project.

Backends, tried in order per line:
1. Qwen3-TTS — local voice-cloning server. Used only when the server is
   reachable AND the NPC has a reference clip at
   data/assets/{world}/voices/refs/{npc_id}.wav|.mp3 (drop clips there
   manually to give an NPC a cloned voice).
2. ElevenLabs — cloud TTS using the NPC's voice_id. NPCs without one get
   a voice auto-assigned from the configured pool by matching pool tags
   against the NPC's description/voice_pattern (stable: persisted on the
   NPC row).

Generated lines are cached as data/assets/{world}/voices/{audio_id}.mp3|.wav
where audio_id = sha1(npc_id|text|tone) — replaying a cached line is free.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path

import httpx

from src.config import load_settings
from src.models import NPC, get_session

logger = logging.getLogger(__name__)

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"

# speak() tone -> ElevenLabs voice settings (derived from telltale's
# emotion table, rekeyed to the tones the DM's speak tool documents)
TONE_SETTINGS: dict[str, dict[str, float]] = {
    "normal": {"stability": 0.5, "similarity_boost": 0.75},
    "whispered": {"stability": 0.7, "similarity_boost": 0.9},
    "shouted": {"stability": 0.25, "similarity_boost": 0.7},
    "nervous": {"stability": 0.3, "similarity_boost": 0.75},
    "angry": {"stability": 0.3, "similarity_boost": 0.7},
    "friendly": {"stability": 0.35, "similarity_boost": 0.8},
    "suspicious": {"stability": 0.6, "similarity_boost": 0.7},
}
DEFAULT_TONE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}

_FEMALE_WORDS = {"she", "her", "hers", "woman", "female", "girl", "lady", "matron",
                 "mother", "sister", "daughter", "queen", "priestess", "duchess", "mistress"}
_MALE_WORDS = {"he", "him", "his", "man", "male", "boy", "lord", "king", "father",
               "brother", "son", "duke", "master", "sir"}
_OLD_WORDS = {"old", "elder", "elderly", "aged", "ancient", "grey", "gray", "wizened",
              "veteran", "grizzled", "weathered"}
_YOUNG_WORDS = {"young", "youth", "youthful", "teen", "boyish", "girlish", "fresh"}


def compute_audio_id(npc_id: str, text: str, tone: str) -> str:
    return hashlib.sha1(f"{npc_id}|{text}|{tone}".encode()).hexdigest()[:20]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


class VoiceGenerator:
    """Generates and caches TTS voice lines for one world."""

    QWEN3_HEALTH_TTL = 300.0  # re-check the local server every 5 minutes

    def __init__(self, world_name: str):
        self.world_name = world_name
        self.voices_dir = Path("data/assets") / world_name / "voices"
        self.refs_dir = self.voices_dir / "refs"
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        settings = load_settings()
        self.audio_cfg = settings.audio
        self.api_key = os.environ.get(self.audio_cfg.elevenlabs_key_env, "")
        self._qwen3_ok: bool | None = None
        self._qwen3_checked_at = 0.0

    def available(self) -> bool:
        """Any TTS backend configured at all?"""
        return bool(self.api_key) or self._qwen3_ref_possible()

    def _qwen3_ref_possible(self) -> bool:
        try:
            return any(self.refs_dir.iterdir())
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Cache lookup
    # ------------------------------------------------------------------

    def line_path(self, audio_id: str) -> Path | None:
        for ext in (".mp3", ".wav"):
            p = self.voices_dir / f"{audio_id}{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def _ref_path(self, npc_id: str) -> Path | None:
        for ext in (".wav", ".mp3"):
            p = self.refs_dir / f"{npc_id}{ext}"
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    # Voice assignment
    # ------------------------------------------------------------------

    def pick_voice(self, npc: NPC) -> str:
        """Pick a pool voice by tag match against the NPC's text fields.

        Deterministic for a given NPC (hash tiebreak) so re-assignment
        after a cleared column stays stable.
        """
        pool = self.audio_cfg.voice_pool
        if not pool:
            return ""
        desc = _words(" ".join(filter(None, [
            npc.description_physical, npc.description_personality,
            npc.voice_pattern, npc.profession, npc.species,
        ])))

        def score(entry) -> int:
            tags = _words(entry.tags)
            s = 0
            if "female" in tags and desc & _FEMALE_WORDS:
                s += 10
            if "male" in tags and desc & _MALE_WORDS:
                s += 10
            if "old" in tags and desc & _OLD_WORDS:
                s += 3
            if "young" in tags and desc & _YOUNG_WORDS:
                s += 3
            # quality-word overlap (raspy, warm, gruff, noble, ...)
            s += len((tags - {"male", "female", "old", "young", "middle"}) & desc) * 2
            return s

        best = max(score(e) for e in pool)
        candidates = [e for e in pool if score(e) == best]
        idx = int(hashlib.sha1(npc.id.encode()).hexdigest(), 16) % len(candidates)
        return candidates[idx].voice_id

    def ensure_voice_assigned(self, npc_id: str) -> str | None:
        """Return the NPC's voice_id, assigning and persisting one if missing."""
        with get_session() as db:
            npc = db.query(NPC).filter(NPC.id == npc_id).first()
            if not npc:
                return None
            if npc.voice_id:
                return npc.voice_id
            voice_id = self.pick_voice(npc)
            if voice_id:
                npc.voice_id = voice_id
                db.commit()
                logger.info(f"Assigned voice {voice_id} to NPC {npc.name}")
            return voice_id or None

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    async def _qwen3_available(self, client: httpx.AsyncClient) -> bool:
        now = time.monotonic()
        if self._qwen3_ok is not None and now - self._qwen3_checked_at < self.QWEN3_HEALTH_TTL:
            return self._qwen3_ok
        self._qwen3_checked_at = now
        try:
            resp = await client.get(f"{self.audio_cfg.qwen3_tts_url}/health", timeout=2)
            self._qwen3_ok = resp.status_code == 200
        except Exception:
            self._qwen3_ok = False
        return self._qwen3_ok

    async def _generate_qwen3(self, client: httpx.AsyncClient, ref_path: Path, text: str) -> bytes | None:
        try:
            resp = await client.post(
                f"{self.audio_cfg.qwen3_tts_url}/generate_direct",
                json={
                    "reference_audio": str(ref_path.resolve()),
                    "reference_text": "",
                    "text": text,
                    "language": "english",
                },
                timeout=600,
            )
            if resp.status_code == 200:
                return resp.content
            logger.warning(f"Qwen3-TTS error {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"Qwen3-TTS request failed: {str(e)[:100]}")
        return None

    async def _generate_elevenlabs(
        self, client: httpx.AsyncClient, voice_id: str, text: str, tone: str
    ) -> bytes | None:
        if not self.api_key:
            return None
        settings = TONE_SETTINGS.get(tone, DEFAULT_TONE_SETTINGS)
        delays = [1, 3, 5]
        for attempt in range(3):
            try:
                resp = await client.post(
                    f"{ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}",
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": self.api_key},
                    json={
                        "text": text,
                        "model_id": self.audio_cfg.elevenlabs_model,
                        "voice_settings": settings,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(delays[attempt])
                else:
                    logger.warning(f"ElevenLabs TTS failed after 3 attempts: {str(e)[:120]}")
        return None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def generate_line(self, npc_id: str, text: str, tone: str, audio_id: str) -> str | None:
        """Generate (or return cached) TTS for one dialogue line.

        Returns the file path, or None if no backend could produce audio.
        """
        cached = self.line_path(audio_id)
        if cached:
            return str(cached)

        text = text.strip()
        if not text or len(text) > self.audio_cfg.max_tts_chars:
            return None

        async with httpx.AsyncClient() as client:
            # Local cloning first: free and unlimited when set up
            ref = self._ref_path(npc_id)
            if ref and await self._qwen3_available(client):
                audio = await self._generate_qwen3(client, ref, text)
                if audio:
                    dest = self.voices_dir / f"{audio_id}.wav"
                    dest.write_bytes(audio)
                    return str(dest)

            voice_id = await asyncio.to_thread(self.ensure_voice_assigned, npc_id)
            if not voice_id:
                return None
            audio = await self._generate_elevenlabs(client, voice_id, text, tone)
            if audio:
                dest = self.voices_dir / f"{audio_id}.mp3"
                dest.write_bytes(audio)
                return str(dest)

        return None
