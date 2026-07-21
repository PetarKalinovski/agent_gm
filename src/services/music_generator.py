"""Per-world music palette generation via the Suno API (sunoapi.org).

Ported from the telltale project's music generator, adapted to Forge:
async (httpx + asyncio), file-based caching keyed by mood instead of DB
rows, and style prompts built from the WorldBible so each world gets a
soundtrack matching its genre and tone.

One looping track is generated per mood. The frontend crossfades between
them based on tension / time-of-day from the SSE state events.

Suno API contract:
  POST /api/v1/generate                          -> submit, returns taskId
  GET  /api/v1/generate/record-info?taskId=<id>  -> poll status
"""

import asyncio
import logging
import os
from pathlib import Path

import httpx

from src.config import load_settings
from src.models import get_session
from src.models.world_bible import WorldBible

logger = logging.getLogger(__name__)

# mood -> style descriptor appended to the world's own style
MOODS: dict[str, str] = {
    "explore": "adventurous main theme, wandering, wonder",
    "tension": "uneasy, suspenseful, something is wrong, restrained",
    "danger": "combat, urgent, driving percussion, high stakes",
    "somber": "grief, loss, mourning, sparse and quiet",
    "night": "nocturnal, mysterious, calm ambient, moonlit",
    "triumph": "victorious, hopeful, soaring resolution",
}

SUNO_BASE_URL = "https://api.sunoapi.org"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 300.0
SUCCESS_STATUSES = {"SUCCESS", "FIRST_SUCCESS"}
FAILURE_STATUSES = {
    "CREATE_TASK_FAILED",
    "GENERATE_AUDIO_FAILED",
    "CALLBACK_EXCEPTION",
    "SENSITIVE_WORD_ERROR",
}


class MusicGenerator:
    """Generates and caches the mood palette for one world."""

    def __init__(self, world_name: str):
        self.world_name = world_name
        self.music_dir = Path("data/assets") / world_name / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        settings = load_settings()
        self.model = settings.audio.suno_model
        self.api_key = os.environ.get(settings.audio.suno_key_env, "")

    # ------------------------------------------------------------------
    # Palette state
    # ------------------------------------------------------------------

    def track_path(self, mood: str) -> Path | None:
        """Existing track file for a mood, or None."""
        for ext in (".mp3", ".wav"):
            p = self.music_dir / f"{mood}{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def missing_moods(self) -> list[str]:
        return [m for m in MOODS if self.track_path(m) is None]

    # ------------------------------------------------------------------
    # Style prompt
    # ------------------------------------------------------------------

    def _build_style(self, mood: str) -> tuple[str, str]:
        """Build (title, style) for a mood from the WorldBible."""
        genre, sub_genres, tone, world_title = "fantasy", [], "", self.world_name
        with get_session() as db:
            bible = db.query(WorldBible).first()
            if bible:
                genre = bible.genre or genre
                sub_genres = list(bible.sub_genres or [])
                tone = bible.tone or ""
                world_title = bible.name or world_title

        parts = [genre, *sub_genres[:2], MOODS[mood],
                 "instrumental cinematic game soundtrack", "seamless loop"]
        if tone:
            parts.insert(1, tone)
        style = ", ".join(p for p in parts if p)
        # Suno rejects overlong style strings — trim on a comma boundary
        if len(style) > 190:
            style = style[:190].rsplit(",", 1)[0]
        return f"{world_title} — {mood}", style

    # ------------------------------------------------------------------
    # Suno API
    # ------------------------------------------------------------------

    async def _submit(self, client: httpx.AsyncClient, title: str, style: str) -> str | None:
        payload = {
            "customMode": True,
            "instrumental": True,
            "model": self.model,
            "title": title,
            "style": style,
            "callBackUrl": "https://localhost/callback",
        }
        try:
            resp = await client.post(f"{SUNO_BASE_URL}/api/v1/generate", json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            code = result.get("code")
            if code and code != 200:
                logger.warning(f"Suno error (code={code}): {result.get('msg', '')}")
                return None
            task_id = (result.get("data") or {}).get("taskId")
            if not task_id:
                logger.warning(f"Suno: no taskId in response: {result}")
            return task_id
        except Exception as e:
            logger.warning(f"Suno submit failed: {e}")
            return None

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> dict | None:
        elapsed = 0.0
        while elapsed < POLL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            try:
                resp = await client.get(
                    f"{SUNO_BASE_URL}/api/v1/generate/record-info",
                    params={"taskId": task_id},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                status = data.get("status", "PENDING")
                if status in SUCCESS_STATUSES:
                    return data
                if status in FAILURE_STATUSES:
                    logger.warning(f"Suno job failed: status={status}")
                    return None
            except Exception as e:
                logger.debug(f"Suno poll error (retrying): {e}")
        logger.warning(f"Suno: timed out after {POLL_TIMEOUT:.0f}s")
        return None

    @staticmethod
    def _extract_audio_url(data: dict) -> str | None:
        response = data.get("response", {})
        audio_list = response if isinstance(response, list) else (
            response.get("sunoData") or response.get("data") or []
        )
        for keys in (("audioUrl", "audio_url"), ("streamAudioUrl", "stream_audio_url")):
            for candidate in audio_list:
                for k in keys:
                    if candidate.get(k):
                        return candidate[k]
        return None

    async def _generate_track(self, client: httpx.AsyncClient, mood: str) -> bool:
        title, style = self._build_style(mood)
        logger.info(f"Music [{self.world_name}/{mood}]: submitting (style='{style}')")
        task_id = await self._submit(client, title, style)
        if not task_id:
            return False
        data = await self._poll(client, task_id)
        if not data:
            return False
        audio_url = self._extract_audio_url(data)
        if not audio_url:
            logger.warning(f"Suno: no audio URL for {mood}: {data}")
            return False
        try:
            resp = await client.get(audio_url, timeout=120)
            resp.raise_for_status()
            ext = ".wav" if ("wav" in resp.headers.get("content-type", "") or audio_url.endswith(".wav")) else ".mp3"
            dest = self.music_dir / f"{mood}{ext}"
            dest.write_bytes(resp.content)
            logger.info(f"Music [{self.world_name}/{mood}]: saved {len(resp.content) // 1024}KB -> {dest.name}")
            return True
        except Exception as e:
            logger.warning(f"Suno download failed for {mood}: {e}")
            return False

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def generate_palette(self) -> dict[str, bool]:
        """Generate every missing mood track. Returns {mood: success}."""
        missing = self.missing_moods()
        if not missing:
            return {}
        if not self.api_key:
            logger.info("No Suno API key set — skipping music palette generation")
            return {m: False for m in missing}

        results: dict[str, bool] = {}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(headers=headers) as client:
            # Stagger submits slightly, then let the polls run concurrently
            async def _job(i: int, mood: str) -> None:
                await asyncio.sleep(i * 1.0)
                results[mood] = await self._generate_track(client, mood)

            await asyncio.gather(*(_job(i, m) for i, m in enumerate(missing)))

        if results and not any(results.values()):
            # Surface total failure so the caller's failure-cooldown kicks in
            # (otherwise every manifest poll would re-hit the Suno API)
            raise RuntimeError(f"Music palette generation failed for all moods: {list(results)}")
        return results
