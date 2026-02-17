"""Web image search for reference-based asset generation.

When the game world is based on a known IP (Game of Thrones, Star Wars, etc.),
this service searches DuckDuckGo Images for reference images of characters and
locations, then passes them to the image generator for more accurate results.
"""

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx
from ddgs import DDGS

logger = logging.getLogger(__name__)

# Total timeout for search + download
_TIMEOUT_SECONDS = 10


class ReferenceImageSearch:
    """Search the web for reference images to feed into asset generation."""

    def __init__(self):
        self.cache_dir = Path("data/assets/references")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def find_character_reference(
        self, name: str, world_name: str
    ) -> bytes | None:
        """Search for a reference image of a character.

        Args:
            name: Character name (e.g. "Tyrion Lannister")
            world_name: World/IP name (e.g. "Game of Thrones")

        Returns:
            Image bytes if found, None otherwise.
        """
        query = f"{name} {world_name}"
        return await self._search_and_download(query)

    async def find_location_reference(
        self, name: str, loc_type: str, world_name: str
    ) -> bytes | None:
        """Search for a reference image of a location.

        Args:
            name: Location name (e.g. "King's Landing")
            loc_type: Location type (e.g. "city")
            world_name: World/IP name (e.g. "Game of Thrones")

        Returns:
            Image bytes if found, None otherwise.
        """
        query = f"{name} {world_name}"
        return await self._search_and_download(query)

    def _get_cache_path(self, query: str) -> Path:
        """Return cache file path based on MD5 hash of query."""
        query_hash = hashlib.md5(query.lower().encode()).hexdigest()
        return self.cache_dir / f"{query_hash}.png"

    async def _search_and_download(self, query: str) -> bytes | None:
        """Search DDG images, download first result, return bytes.

        Returns None on any failure so the caller falls back to text-only
        generation (current behavior).
        """
        # Check cache first
        cache_path = self._get_cache_path(query)
        if cache_path.exists() and cache_path.stat().st_size > 0:
            logger.info(f"Reference cache hit for: {query}")
            return cache_path.read_bytes()

        # Also cache misses (empty file) so we don't re-search
        miss_marker = cache_path.with_suffix(".miss")
        if miss_marker.exists():
            logger.debug(f"Reference cache miss marker for: {query}")
            return None

        try:
            logger.info(f"Searching web for reference image: {query}")
            image_url = await asyncio.wait_for(
                self._do_search(query), timeout=_TIMEOUT_SECONDS
            )
            if not image_url:
                logger.info(f"No image results for: {query}")
                miss_marker.touch()
                return None

            logger.info(f"Downloading reference image from: {image_url}")
            image_bytes = await asyncio.wait_for(
                self._do_download(image_url), timeout=_TIMEOUT_SECONDS
            )
            if not image_bytes:
                logger.warning(f"Failed to download reference for: {query}")
                miss_marker.touch()
                return None

            # Cache the result
            cache_path.write_bytes(image_bytes)
            logger.info(f"Cached reference image for: {query} ({len(image_bytes)} bytes)")
            return image_bytes

        except asyncio.TimeoutError:
            logger.warning(f"Reference image search timed out for: {query}")
            miss_marker.touch()
            return None
        except Exception:
            logger.exception(f"Reference image search failed for: {query}")
            miss_marker.touch()
            return None

    async def _do_search(self, query: str) -> str | None:
        """Run DDG image search in a thread (the library is synchronous)."""
        def _search():
            ddgs = DDGS()
            results = ddgs.images(query, max_results=3)
            if results:
                return results[0]["image"]
            return None

        return await asyncio.to_thread(_search)

    async def _do_download(self, url: str) -> bytes | None:
        """Download an image from a URL."""
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type and len(resp.content) < 1000:
                    logger.warning(f"Response doesn't look like an image: {content_type}")
                    return None
                return resp.content
        except Exception:
            logger.exception(f"Failed to download image from {url}")
            return None
