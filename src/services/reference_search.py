"""Web image search for reference-based asset generation.

When the game world is based on a known IP (Game of Thrones, Star Wars, etc.),
this service searches Bing Images for reference images of characters and
locations, then passes them to the image generator for more accurate results.

Uses curl for all HTTP requests to bypass TLS fingerprint blocking by
Cloudflare and similar CDNs that reject Python HTTP libraries.
"""

import asyncio
import logging
import re
import subprocess
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

# Total timeout for search + download
_TIMEOUT_SECONDS = 15

# Regex to extract full-size image URLs from Bing image search HTML
# Bing encodes them as murl&quot;:&quot;URL&quot; in the page source
_BING_IMAGE_RE = re.compile(r'murl&quot;:&quot;(https?://[^&]+?)&quot;')


class ReferenceImageSearch:
    """Search the web for reference images to feed into asset generation."""

    def __init__(self, world_name: str):
        self.cache_dir = Path("data/assets") / world_name / "references"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_reference_path(self, entity_id: str) -> Path:
        """Return reference file path based on entity ID."""
        return self.cache_dir / f"{entity_id}.png"

    async def find_reference(
        self, entity_id: str, search_query: str | None = None
    ) -> bytes | None:
        """Find or search for a reference image for an entity.

        Args:
            entity_id: The entity's unique ID (NPC, location, player).
            search_query: Optional web search query. If None, no web search
                is performed (but a manually placed file is still returned).

        Returns:
            Image bytes if found, None otherwise.
        """
        # 1. Check if entity reference file already exists
        ref_path = self._get_reference_path(entity_id)
        if ref_path.exists() and ref_path.stat().st_size > 0:
            logger.info(f"Reference cache hit for entity: {entity_id}")
            return ref_path.read_bytes()

        # 2. No search query → no web search
        if not search_query:
            return None

        # 3. Check miss marker (avoid re-searching known failures)
        miss_marker = ref_path.with_suffix(".miss")
        if miss_marker.exists():
            logger.debug(f"Reference cache miss marker for entity: {entity_id}")
            return None

        # 4. Search web and download
        return await self._search_and_download(entity_id, search_query)

    async def _search_and_download(self, entity_id: str, query: str) -> bytes | None:
        """Search Bing images, download first result, return bytes.

        Returns None on any failure so the caller falls back to text-only
        generation (current behavior).
        """
        ref_path = self._get_reference_path(entity_id)
        miss_marker = ref_path.with_suffix(".miss")

        try:
            logger.info(f"Searching web for reference image: {query}")
            image_urls = await asyncio.wait_for(
                self._do_search(query), timeout=_TIMEOUT_SECONDS
            )
            if not image_urls:
                logger.info(f"No image results for: {query}")
                miss_marker.touch()
                return None

            # Try each URL until one succeeds
            for image_url in image_urls:
                logger.info(f"Downloading reference image from: {image_url}")
                image_bytes = await asyncio.wait_for(
                    self._do_download(image_url), timeout=_TIMEOUT_SECONDS
                )
                if image_bytes:
                    ref_path.write_bytes(image_bytes)
                    logger.info(f"Cached reference image for entity {entity_id}: {query} ({len(image_bytes)} bytes)")
                    return image_bytes

            logger.warning(f"All download attempts failed for: {query}")
            miss_marker.touch()
            return None

        except asyncio.TimeoutError:
            logger.warning(f"Reference image search timed out for: {query}")
            miss_marker.touch()
            return None
        except Exception:
            logger.exception(f"Reference image search failed for: {query}")
            miss_marker.touch()
            return None

    async def _do_search(self, query: str) -> list[str]:
        """Search Bing Images via curl and extract image URLs from HTML."""
        def _search():
            encoded_query = urllib.parse.quote_plus(query)
            url = f"https://www.bing.com/images/search?q={encoded_query}&first=1"
            result = subprocess.run(
                ["curl", "-sL", "--max-time", "10",
                 "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                 "-H", "Accept: text/html",
                 "-H", "Accept-Language: en-US,en;q=0.9",
                 url],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                return []
            html = result.stdout.decode("utf-8", errors="replace")
            urls = _BING_IMAGE_RE.findall(html)
            # Deduplicate while preserving order, return top 3
            seen = set()
            unique = []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    unique.append(u)
                    if len(unique) >= 3:
                        break
            return unique

        return await asyncio.to_thread(_search)

    async def _do_download(self, url: str) -> bytes | None:
        """Download an image using curl (bypasses TLS fingerprint blocking)."""
        def _curl_download():
            result = subprocess.run(
                ["curl", "-sL", "--max-time", "10",
                 "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                 "-H", "Accept: image/*,*/*",
                 "-H", "Accept-Language: en-US,en;q=0.9",
                 url],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                return None
            content = result.stdout
            # Basic sanity check: images should be at least 1KB
            if len(content) < 1000:
                return None
            return content

        try:
            return await asyncio.to_thread(_curl_download)
        except Exception:
            logger.exception(f"Failed to download image from {url}")
            return None
