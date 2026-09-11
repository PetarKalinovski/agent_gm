"""Image generation service using Gemini image generation API."""

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
import httpx
from PIL import Image

from src.config import load_settings
from src.services import appearance

if TYPE_CHECKING:
    from src.models.location import Location
    from src.models.npc import NPC
    from src.models.player import Player
    from src.models.world_bible import WorldBible

logger = logging.getLogger(__name__)


# Footprint = bottom slice of a detected box that actually blocks walking
FOOTPRINT_TOP_FRACTION = 0.75   # skip the upper 75% of each box (playtest
                                # feedback: 0.55 blocked too much walk-behind)
MIN_OBSTACLE_AREA = 0.0008      # ignore boxes under 0.08% of the scene
MAX_OBSTACLES = 24              # playtest: 12 left too many props walk-through


def _parse_boxes_json(text: str) -> list | None:
    """Parse a JSON array of boxes, salvaging complete entries if the model's
    output was truncated mid-array (still happens on rare long responses).

    Returns None on unrecoverable parse failure — callers use that to retry,
    since a failed parse is not the same as a scene with no obstacles."""
    import json

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # Truncated: cut back to the last complete object and close the array
    end = text.rfind("}")
    if end != -1:
        try:
            parsed = json.loads(text[: end + 1] + "]")
            if isinstance(parsed, list):
                logger.info(f"Obstacle JSON was truncated; salvaged {len(parsed)} boxes")
                return parsed
        except json.JSONDecodeError:
            pass
    logger.warning("Obstacle JSON unsalvageable")
    return None


def boxes_to_footprint_polygons(boxes: list) -> list[list[list[float]]]:
    """Convert Gemini box_2d detections ([ymin, xmin, ymax, xmax], 0-1000)
    into footprint rectangles as polygons in normalized 0-100 coordinates."""
    polygons: list[list[list[float]]] = []
    for box in boxes if isinstance(boxes, list) else []:
        coords = box.get("box_2d") if isinstance(box, dict) else None
        if not (isinstance(coords, list) and len(coords) == 4):
            continue
        try:
            ymin, xmin, ymax, xmax = (float(c) / 10.0 for c in coords)  # -> 0-100
        except (TypeError, ValueError):
            continue
        if xmax <= xmin or ymax <= ymin:
            continue
        if ((xmax - xmin) / 100.0) * ((ymax - ymin) / 100.0) < MIN_OBSTACLE_AREA:
            continue
        foot_top = ymin + (ymax - ymin) * FOOTPRINT_TOP_FRACTION
        clamp = lambda v: max(0.0, min(100.0, round(v, 2)))
        polygons.append([
            [clamp(xmin), clamp(foot_top)],
            [clamp(xmax), clamp(foot_top)],
            [clamp(xmax), clamp(ymax)],
            [clamp(xmin), clamp(ymax)],
        ])
        if len(polygons) >= MAX_OBSTACLES:
            break
    return polygons


class ImageGenerator:
    """Generate game assets via Gemini image generation API (OpenRouter or direct)."""

    def __init__(self, world_name: str):
        settings = load_settings()
        img_config = settings.image_generation

        self.provider = img_config.provider
        if self.provider == "gemini":
            self.api_key = os.getenv("GEMINI_API_KEY")
            self.model = img_config.gemini_model
            self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        else:
            self.api_key = os.getenv("OPENROUTER_API_KEY")
            self.model = img_config.openrouter_model
            self.api_url = "https://openrouter.ai/api/v1/chat/completions"

        self.assets_dir = Path("data/assets") / world_name
        self._ensure_directories()

    def _ensure_directories(self):
        """Ensure asset directories exist."""
        (self.assets_dir / "locations").mkdir(parents=True, exist_ok=True)
        (self.assets_dir / "sprites").mkdir(parents=True, exist_ok=True)
        (self.assets_dir / "portraits").mkdir(parents=True, exist_ok=True)

    async def _call_api(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        image_size: str = "2K",
        reference_image: bytes | None = None
    ) -> bytes:
        """Call image generation API and return raw image bytes.

        Routes to the appropriate provider based on self.provider.
        """
        if self.provider == "gemini":
            return await self._call_gemini(prompt, reference_image, aspect_ratio, image_size)
        return await self._call_openrouter(prompt, aspect_ratio, image_size, reference_image)

    async def _call_openrouter(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        image_size: str = "2K",
        reference_image: bytes | None = None
    ) -> bytes:
        """Call OpenRouter API and return raw image bytes."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Build message content - can be multimodal with reference image
        if reference_image:
            ref_b64 = base64.b64encode(reference_image).decode('utf-8')
            content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{ref_b64}"
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        else:
            content = prompt

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "modalities": ["image", "text"],
            "image_config": {
                "aspect_ratio": aspect_ratio,
                "image_size": image_size
            }
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(self.api_url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()

        # Extract image from response
        if result.get("choices"):
            message = result["choices"][0]["message"]
            if message.get("images"):
                image_url = message["images"][0]["image_url"]["url"]
                if image_url.startswith("data:image"):
                    _, encoded = image_url.split(",", 1)
                    return base64.b64decode(encoded)

        raise ValueError("No image returned from OpenRouter API")

    async def _call_gemini(
        self,
        prompt: str,
        reference_image: bytes | None = None,
        aspect_ratio: str = "1:1",
        image_size: str = "1K",
    ) -> bytes:
        """Call Gemini direct API and return raw image bytes."""
        parts = []

        if reference_image:
            ref_b64 = base64.b64encode(reference_image).decode('utf-8')
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": ref_b64
                }
            })

        parts.append({"text": prompt})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": image_size
                }
            }
        }

        url = f"{self.api_url}?key={self.api_key}"

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload
            )
            response.raise_for_status()
            result = response.json()

        # Extract image from response
        candidates = result.get("candidates", [])
        if candidates:
            content_parts = candidates[0].get("content", {}).get("parts", [])
            for part in content_parts:
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])

        raise ValueError("No image returned from Gemini API")

    async def detect_obstacles(self, background_image: bytes) -> list[list[list[float]]]:
        """Detect solid obstacles in a scene background via Gemini vision.

        Returns a list of polygons in normalized 0-100 scene coordinates.
        Each detected bounding box is reduced to its FOOTPRINT (the bottom
        slice) — in the 3/4 view, characters walk behind the upper part of
        an object, so only its base should block movement.
        """
        settings = load_settings()
        model = settings.image_generation.vision_model
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.info("No GEMINI_API_KEY — skipping obstacle detection")
            return []

        prompt = """This is a scene background from a 2D RPG, viewed from a three-quarter top-down angle. Characters walk on the floor/ground.

Identify EVERY solid obstacle a walking character could not pass through. Be exhaustive — walk the scene systematically (left to right, near to far) and box each object SEPARATELY, even when similar objects sit side by side:
- furniture: tables, benches, chairs, counters, beds, shelves
- market fixtures: each stall, each cart, each awning post cluster
- containers: each crate stack, each barrel or barrel cluster, sacks, chests
- structures: wells, fountains, statues, pillars, fences, low walls inside the scene
- nature: trees, stumps, boulders, bushes too dense to step through
- hazards: water pools, fire pits, braziers

Do NOT include: the open floor or ground itself, paths, rugs, doorways, shadows, wall surfaces at the outer edges of the scene, or anything a person could simply step over (a rope, a small basket, scattered straw).

Return a JSON array (no other text): [{"label": "<short name>", "box_2d": [ymin, xmin, ymax, xmax]}] with coordinates in the 0-1000 range. Return at most 24 boxes, largest first. Missing a real obstacle is worse than a slightly loose box. Return [] if there are none."""

        payload = {
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(background_image).decode()}},
                {"text": prompt},
            ]}],
            # 24 boxes of JSON overflow the default output budget — a
            # truncated array used to lose the whole detection
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8192},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        # A parse failure is not "no obstacles" — retry once before giving up
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers={"Content-Type": "application/json"}, json=payload)
                    response.raise_for_status()
                    result = response.json()
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                boxes = _parse_boxes_json(text)
            except Exception as e:
                logger.warning(f"Obstacle detection failed (attempt {attempt}): {e}")
                boxes = None
            if boxes is not None:
                return boxes_to_footprint_polygons(boxes)
        return []

    def sprite_rel(self, sig: str, filename: str) -> str:
        """Relative path for `_save_image`: 'sprites/x.png' or
        'sprites/variants/<sig>/x.png'.

        `base` and `base-unarmed` resolve to the FLAT legacy sprites dir —
        that is the entire migration story (appearance ruling A4).
        """
        if sig in (appearance.BASE_BODY_SIG, appearance.BASE_ACTION_SIG):
            return f"sprites/{filename}"
        if not appearance.is_valid_signature(sig):
            raise ValueError(f"Invalid appearance signature {sig!r}")
        return f"sprites/variants/{sig}/{filename}"

    def sprite_path(self, sig: str, filename: str) -> Path:
        """Absolute path of one frame in one signature's directory."""
        return self.assets_dir / self.sprite_rel(sig, filename)

    def _save_debug_strip(self, kind: str, character_id: str, strip_data: bytes) -> None:
        """Keep the raw filmstrip beside the sliced frames. A bad cycle can
        only be diagnosed from what the model actually drew — the frames
        alone hide whether the slicer, the cutout, or the drawing failed."""
        try:
            debug_dir = self.assets_dir / "debug" / "strips"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (debug_dir / f"{character_id}_{kind}_{stamp}.png").write_bytes(strip_data)
        except OSError as e:
            logger.debug(f"Could not save debug strip: {e}")

    def _save_image(self, image_data: bytes, relative_path: str) -> str:
        """Save image to assets directory. Returns absolute path."""
        full_path = self.assets_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(image_data)
        logger.info(f"Saved image to {full_path}")
        return str(full_path)

    def _build_location_prompt(
        self,
        location: "Location",
        world_bible: "WorldBible"
    ) -> str:
        """Build prompt for location background generation."""
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        color_palette = ", ".join(world_bible.color_palette) if world_bible and world_bible.color_palette else "varied"
        atmosphere = ", ".join(location.atmosphere_tags) if location.atmosphere_tags else "neutral"

        return f"""Create a 2D top-down game scene background.
Style: {visual_style}
Color palette: {color_palette}

It should be a 2d sprite detailed background suitable for use in a top-down RPG game.

Location: {location.name}
Type: {location.type.value if hasattr(location.type, 'value') else location.type}
Description: {location.description}
Atmosphere: {atmosphere}

Avoid creating small indoor rooms. Beause they are zoomed in too much and the player and npc sprites will not fit well. They will look awkwardly small compared to the environment.
But if you must, and you can't make it an open area, create that small room but make it zoomed out enough, and everything not in the room just make it black. (like a large walkable area but everything outside the room is just black).

Requirements:
- Isometric top-down perspective
- Wide view, suitable for use as a background
- No characters, people, or creatures in the scene
- Clear walkable floor area in the center
- Detailed environment matching the description
- Game-ready art style consistent with: visual_style
- High quality, detailed, suitable for a 2D RPG game"""

    def _build_sprite_prompt(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str = "front"
    ) -> str:
        """Build prompt for character sprite generation."""
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        color_palette = ", ".join(world_bible.color_palette) if world_bible and world_bible.color_palette else "varied"

        # Get character details
        name = character.name
        physical_desc = getattr(character, "description_physical", "") or getattr(character, "description", "")
        profession = getattr(character, "profession", "adventurer")

        # Map direction to view description
        direction_map = {
            "front": "front-facing view, looking at viewer",
            "back": "back view, facing away from viewer",
            "left": "left side profile view",
            "right": "right side profile view"
        }
        view_desc = direction_map.get(direction, direction_map["front"])

        return f"""Draw ONE character standing in the center of the image. NOT a sprite sheet. NOT multiple views. Just ONE character, ONE pose.

Style: {visual_style}, 2D game character illustration
Color palette: {color_palette}

Character: {name}
Appearance: {physical_desc}
Role/Profession: {profession}

Pose: Standing idle, {view_desc}
- Full body visible from head to feet
- Character should be large and centered, filling most of the image height
- Solid bright green background (#00FF00) for easy chroma key removal
- ONLY ONE CHARACTER IN THE IMAGE. No duplicates, no alternate views, no turnaround sheet.
- No other objects, no shadows on the ground, no text, no labels
- Clean edges suitable for cutting out"""

    def _build_portrait_prompt(
        self,
        npc: "NPC",
        world_bible: "WorldBible"
    ) -> str:
        """Build prompt for NPC portrait generation."""
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"

        return f"""Create a 2D hand-painted character portrait for an RPG dialogue box.

Style: {visual_style}, 2D illustration, digital painting with visible brushwork
NOT photorealistic, NOT 3D rendered, NOT AI-generated look

Character: {npc.name}
Appearance: {npc.description_physical}
Personality: {npc.description_personality}
Current mood: {npc.current_mood}

Requirements:
- Head and shoulders portrait, close-up framing
- 3/4 view angle, slightly facing viewer
- Expressive face showing mood: {npc.current_mood}
- Hand-painted illustration style with painterly texture
- Bold linework and defined features
- Rich colors, stylized shading (NOT realistic lighting)
- Simple gradient or solid color background
- Style inspired by: Baldur's Gate portraits, Pillars of Eternity, classic CRPG art
- Square format, focus entirely on the character's face and expression"""

    def _remove_background(self, image_data: bytes) -> bytes:
        """Remove background from sprite image using rembg or color key."""
        try:
            # Try rembg first (better quality) - optional dependency
            from rembg import remove
            input_image = Image.open(io.BytesIO(image_data))
            output_image = remove(input_image)
            output_bytes = io.BytesIO()
            output_image.save(output_bytes, format="PNG")
            return output_bytes.getvalue()
        except ImportError:
            logger.info("Using color key background removal (rembg not installed)")
            return self._remove_colored_background(image_data)

    @staticmethod
    def _cutout_failed(image_data: bytes) -> bool:
        """True when background removal left an opaque card instead of a cutout.

        A proper sprite cutout has transparent borders; when the model paints
        a full scene behind the character, rembg can fail to segment it and
        return the image nearly untouched (observed in playtesting: a player
        idle shipped with its full painted background).
        """
        import numpy as np

        img = Image.open(io.BytesIO(image_data)).convert("RGBA")
        alpha = np.asarray(img.getchannel("A")) > 128
        border = np.concatenate([alpha[0, :], alpha[-1, :], alpha[:, 0], alpha[:, -1]])
        return border.mean() > 0.35 or alpha.mean() > 0.90

    def _remove_colored_background(self, image_data: bytes) -> bytes:
        """Remove solid colored background using color key with tolerance.

        Works best with bright green (#00FF00), but also handles other
        solid backgrounds by detecting the most common edge color.
        """
        img = Image.open(io.BytesIO(image_data)).convert("RGBA")
        width, height = img.size
        pixels = img.load()

        # Sample edge pixels to detect background color
        edge_colors = []
        for x in range(width):
            edge_colors.append(pixels[x, 0][:3])  # Top edge
            edge_colors.append(pixels[x, height - 1][:3])  # Bottom edge
        for y in range(height):
            edge_colors.append(pixels[0, y][:3])  # Left edge
            edge_colors.append(pixels[width - 1, y][:3])  # Right edge

        # Find most common edge color (likely background)
        from collections import Counter
        color_counts = Counter(edge_colors)
        bg_color = color_counts.most_common(1)[0][0]
        logger.debug(f"Detected background color: RGB{bg_color}")

        # Remove pixels matching background color (with tolerance)
        tolerance = 40
        new_data = []
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                # Check if pixel is close to background color
                if (abs(r - bg_color[0]) < tolerance and
                    abs(g - bg_color[1]) < tolerance and
                    abs(b - bg_color[2]) < tolerance):
                    new_data.append((0, 0, 0, 0))  # Transparent
                else:
                    new_data.append((r, g, b, a))

        img.putdata(new_data)

        # Optional: Clean up edges with slight alpha feathering
        output_bytes = io.BytesIO()
        img.save(output_bytes, format="PNG")
        return output_bytes.getvalue()

    async def generate_location_background(
        self,
        location: "Location",
        world_bible: "WorldBible",
        reference_image: bytes | None = None
    ) -> str:
        """Generate top-down location background.

        Args:
            location: The location to generate background for
            world_bible: World configuration for style consistency
            reference_image: Optional web reference image for known IPs

        Returns:
            Path to saved image file
        """
        if reference_image:
            visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
            prompt = f"""The attached reference image shows what "{location.name}" looks like. This is your PRIMARY visual reference — your output MUST capture the appearance, architecture, colors, and atmosphere of this image.

Reinterpret this reference as a 2D top-down game scene background in this style: {visual_style}

Requirements:
- Isometric top-down perspective
- Wide view, suitable for use as a background
- No characters, people, or creatures in the scene
- Clear walkable floor area in the center
- Game-ready art style
- High quality, detailed, suitable for a 2D RPG game
- Avoid creating small indoor rooms. If you must, zoom out enough and make everything outside the room black.

The reference image is the ground truth for how this place looks. Match it."""
        else:
            prompt = self._build_location_prompt(location, world_bible)
        logger.info(f"Generating location background for: {location.name}")

        image_data = await self._call_api(prompt, aspect_ratio="16:9", reference_image=reference_image)
        path = self._save_image(image_data, f"locations/{location.id}.png")
        return path

    async def generate_character_sprite(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str = "front",
        reference_image: bytes | None = None
    ) -> str:
        """Generate isometric character sprite.

        Args:
            character: The NPC or Player to generate sprite for
            world_bible: World configuration for style consistency
            direction: Facing direction (front, back, left, right)
            reference_image: Optional web reference image for known IPs

        Returns:
            Path to saved image file (transparent background)
        """
        if reference_image:
            visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
            direction_map = {
                "front": "front-facing view, looking at viewer",
                "back": "back view, facing away from viewer",
                "left": "left side profile view",
                "right": "right side profile view"
            }
            view_desc = direction_map.get(direction, direction_map["front"])
            prompt = f"""The attached reference image shows the character "{character.name}". Draw this SAME character but seen {view_desc}.

Style: {visual_style}, 2D game character illustration

CRITICAL: Output must contain ONLY ONE character, ONE pose. NOT a sprite sheet, NOT multiple views side by side.

- Match the character's face, body, clothing, colors, and proportions from the reference
- {view_desc}, standing idle pose
- Full body visible from head to feet, large and centered in the image
- Solid bright green background (#00FF00) for chroma key removal
- No other objects, no shadows, no text, no duplicates"""
        else:
            prompt = self._build_sprite_prompt(character, world_bible, direction)
        logger.info(f"Generating sprite for {character.name} ({direction})")

        image_data = await self._call_api(prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image)

        # Remove background for transparency; if the model painted a full
        # scene rembg couldn't segment, one fresh generation usually fixes it
        cutout = self._remove_background(image_data)
        if self._cutout_failed(cutout):
            logger.warning(f"Cutout failed for {character.name} ({direction}); regenerating once")
            image_data = await self._call_api(prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image)
            retry = self._remove_background(image_data)
            if not self._cutout_failed(retry):
                cutout = retry

        path = self._save_image(cutout, f"sprites/{character.id}_{direction}.png")
        return path

    async def generate_portrait(
        self,
        npc: "NPC",
        world_bible: "WorldBible",
        reference_image: bytes | None = None
    ) -> str:
        """Generate NPC portrait for dialogue.

        Args:
            npc: The NPC to generate portrait for
            world_bible: World configuration for style consistency
            reference_image: Optional web reference image for known IPs

        Returns:
            Path to saved image file
        """
        if reference_image:
            visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
            prompt = f"""The attached reference image shows the character "{npc.name}". This is your PRIMARY visual reference — the portrait MUST look like this person. Match their face, features, hair, skin tone, and overall appearance from the reference image.

Reinterpret this character as a 2D hand-painted portrait for an RPG dialogue box.
Style: {visual_style}, 2D illustration, digital painting with visible brushwork
Current mood: {npc.current_mood}

Requirements:
- Head and shoulders portrait, close-up framing
- 3/4 view angle, slightly facing viewer
- Expressive face showing mood: {npc.current_mood}
- Hand-painted illustration style with painterly texture
- Bold linework and defined features
- Rich colors, stylized shading (NOT realistic lighting)
- Simple gradient or solid color background
- Style inspired by: Baldur's Gate portraits, Pillars of Eternity, classic CRPG art
- Square format, focus entirely on the character's face and expression

DO NOT invent a new face or appearance. The reference image IS what this character looks like."""
        else:
            prompt = self._build_portrait_prompt(npc, world_bible)
        logger.info(f"Generating portrait for: {npc.name}")

        image_data = await self._call_api(prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image)
        path = self._save_image(image_data, f"portraits/{npc.id}.png")
        return path

    async def generate_character_sprite_with_reference(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        reference_image: bytes
    ) -> str:
        """Generate sprite using a reference image for style consistency.

        Args:
            character: The character to generate sprite for
            world_bible: World configuration
            direction: Target direction (back, left, right)
            reference_image: The front-facing sprite as reference

        Returns:
            Path to saved sprite
        """
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        name = character.name
        physical_desc = getattr(character, "description_physical", "") or getattr(character, "description", "")

        direction_map = {
            "back": "from behind (back view), facing away",
            "left": "from the left side (profile view)",
            "right": "from the right side (profile view)"
        }
        view_desc = direction_map.get(direction, "front-facing")

        prompt = f"""The attached image shows a character. Draw this SAME character but seen {view_desc}.

Character: {name}

CRITICAL: Output ONE character only. NOT a sprite sheet, NOT multiple views side by side. Just ONE figure, centered.

- Same art style, clothing, colors, proportions, and details as the reference
- {view_desc}, standing idle pose
- Full body visible from head to feet, large and centered in the image
- Solid bright green background (#00FF00) for chroma key removal
- No other objects, no shadows, no text, no duplicates
- Style: {visual_style}"""

        logger.info(f"Generating {direction} sprite for {name} (with reference)")

        image_data = await self._call_api(
            prompt,
            aspect_ratio="1:1",
            image_size="1K",
            reference_image=reference_image
        )

        cutout = self._remove_background(image_data)
        if self._cutout_failed(cutout):
            logger.warning(f"Cutout failed for {name} ({direction}); regenerating once")
            image_data = await self._call_api(
                prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image
            )
            retry = self._remove_background(image_data)
            if not self._cutout_failed(retry):
                cutout = retry
        path = self._save_image(cutout, f"sprites/{character.id}_{direction}.png")
        return path

    # Same wording as `_build_sprite_prompt`'s direction_map — a variant idle
    # must be framed exactly like the base idle it is generated from.
    _IDLE_DIRECTION_MAP = {
        "front": "front-facing view, looking at viewer",
        "back": "back view, facing away from viewer",
        "left": "left side profile view",
        "right": "right side profile view",
    }

    def _build_outfit_idle_prompt(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        outfit_desc: str,
    ) -> str:
        """Prompt for one outfit-variant idle (the only genuinely new prompt).

        The garment rides in as TEXT here and as a REFERENCE IMAGE everywhere
        downstream: walk/dodge/attack strips are generated from the variant
        idle this produces, so their existing "matching the reference image
        EXACTLY" clause is already the whole contract (ruling A8).
        """
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        view_desc = self._IDLE_DIRECTION_MAP.get(direction, self._IDLE_DIRECTION_MAP["front"])

        return f"""The attached image shows the character "{character.name}" from a 2D RPG. That is exactly
who this is: same face, same hair, same skin tone, same build, same age, same
art style.

Draw the SAME character, now WEARING: {outfit_desc}

The clothing is the ONLY thing that changes. Ignore any clothing described
elsewhere — the garment above replaces what they are wearing in the reference.
Do not redesign the face. Do not change their proportions or their height. Do
not add a weapon, a prop, a backdrop, or a companion.

Pose: standing idle, {view_desc}
- Full body visible from head to feet
- SAME scale and framing as the reference image: the character fills the same
  share of the canvas, feet at the same height
- Solid bright green background (#00FF00) for chroma key removal
- ONLY ONE CHARACTER, ONE POSE. No sprite sheet, no alternate views, no
  turnaround, no duplicates
- No shadows on the ground, no text, no labels, clean edges for cutting out
- Style: {visual_style}"""

    async def generate_outfit_variant_idle(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        reference_image: bytes,
        outfit_desc: str,
        sig: str,
    ) -> str:
        """One variant idle, saved to sprites/variants/{sig}/{id}_{dir}.png.

        Same cutout + `_cutout_failed` + one-retry logic as
        `generate_character_sprite_with_reference` — a variant that ships with
        its painted background is exactly as broken as a base one.
        """
        prompt = self._build_outfit_idle_prompt(character, world_bible, direction, outfit_desc)
        logger.info(f"Generating {direction} outfit idle for {character.name} ({sig})")

        image_data = await self._call_api(
            prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image
        )
        cutout = self._remove_background(image_data)
        if self._cutout_failed(cutout):
            logger.warning(
                f"Cutout failed for {character.name} ({direction}, {sig}); regenerating once"
            )
            image_data = await self._call_api(
                prompt, aspect_ratio="1:1", image_size="1K", reference_image=reference_image
            )
            retry = self._remove_background(image_data)
            if not self._cutout_failed(retry):
                cutout = retry

        return self._save_image(cutout, self.sprite_rel(sig, f"{character.id}_{direction}.png"))

    # One Gemini call per direction produces the KEY poses as a filmstrip.
    # Frames generated together stay consistent (style, scale, proportions);
    # frames generated one-by-one visibly morph between poses. RIFE then
    # in-betweens the keys locally (frame_interpolator), doubling the count —
    # WALK_FRAME_COUNT is the on-disk contract the frontend and asset checks
    # rely on; it must always be fully present.
    WALK_KEYFRAME_COUNT = 6
    WALK_FRAME_COUNT = 12

    # Side view is generated RIGHT-facing: image models have a strong
    # left-to-right walking bias and flip a requested left-facing strip
    # anyway (observed on gemini-3.1-flash-image). Left is mirrored.
    #
    # Every frame is spelled out with the acting leg NAMED. Never describe a
    # frame as "mirror of frame N" — models skim that and draw the same leg
    # in both half-cycles, which animates as hopping on one foot.
    _WALK_CHOREOGRAPHY = {
        "right": """A walk is TWO steps: frames 1-3 land on the RIGHT foot, frames 4-6 land on the
LEFT foot. The legs MUST alternate — frames 1 and 4 are opposite-leg poses, and
if they look alike the animation is wrong.
Frame 1 — RIGHT-FOOT CONTACT: legs wide apart in full stride; RIGHT leg extended
  forward, heel striking the ground; LEFT leg stretched behind, toe down, heel
  lifted. Left arm swung forward, right arm swung back.
Frame 2 — DOWN: full weight settles onto the front RIGHT leg, knees bent, body at
  its lowest; the LEFT foot peels off the ground behind.
Frame 3 — PASSING: the LEFT leg swings forward past the planted RIGHT leg, left
  knee raised, body at its tallest; arms passing by the sides.
Frame 4 — LEFT-FOOT CONTACT: legs wide apart again but SWAPPED; LEFT leg extended
  forward, heel striking; RIGHT leg stretched behind, heel lifted. Right arm
  swung forward, left arm swung back.
Frame 5 — DOWN: full weight settles onto the front LEFT leg, body at its lowest;
  the RIGHT foot peels off the ground behind.
Frame 6 — PASSING: the RIGHT leg swings forward past the planted LEFT leg, right
  knee raised, body at its tallest.
Every frame is a strict FULL SIDE PROFILE view facing right, walking toward the
right edge of the image (never three-quarter view).""",
        "front": """A walk is TWO steps: frames 1-3 step onto the LEFT foot, frames 4-6 step onto
the RIGHT foot. The legs MUST alternate — frames 1 and 4 show OPPOSITE legs
forward, and if they look alike the animation is wrong.
Frame 1 — LEFT-FOOT CONTACT: LEFT leg stepping toward the viewer, left knee bent,
  left foot forward and slightly larger (closer to camera); RIGHT leg straight
  behind. Right arm swings forward, left arm back; hips tilt slightly left.
Frame 2 — DOWN: weight drops onto the LEFT leg, body slightly lower, right foot
  starting to lift behind.
Frame 3 — PASSING: RIGHT knee lifts and crosses in front, feet close together,
  body at its tallest.
Frame 4 — RIGHT-FOOT CONTACT: RIGHT leg stepping toward the viewer, right knee
  bent, right foot forward and slightly larger; LEFT leg straight behind. Left
  arm swings forward, right arm back; hips tilt slightly right.
Frame 5 — DOWN: weight drops onto the RIGHT leg, body slightly lower, left foot
  starting to lift behind.
Frame 6 — PASSING: LEFT knee lifts and crosses in front, feet close together,
  body at its tallest.
Every frame the character faces the viewer directly.""",
        "back": """A walk is TWO steps: frames 1-3 step onto the LEFT foot, frames 4-6 step onto
the RIGHT foot. The legs MUST alternate — frames 1 and 4 show OPPOSITE legs
forward, and if they look alike the animation is wrong.
Frame 1 — LEFT-FOOT CONTACT: LEFT leg stepping away from the viewer, left foot
  planted ahead (higher in the image); RIGHT leg trailing, right heel lifted
  toward the camera. Right arm swings forward (away), left arm back.
Frame 2 — DOWN: weight onto the LEFT leg, body slightly lower, right sole visible
  as it lifts.
Frame 3 — PASSING: RIGHT leg swings past the planted LEFT leg, feet close
  together, body at its tallest.
Frame 4 — RIGHT-FOOT CONTACT: RIGHT leg stepping away, right foot planted ahead;
  LEFT leg trailing, left heel lifted toward the camera. Left arm swings
  forward (away), right arm back.
Frame 5 — DOWN: weight onto the RIGHT leg, body slightly lower, left sole visible
  as it lifts.
Frame 6 — PASSING: LEFT leg swings past the planted RIGHT leg, feet close
  together, body at its tallest.
Every frame shows the character from directly behind, walking away from the viewer.""",
    }

    # Combat actions: 6 keys, side view RIGHT-facing (same bias rule as
    # walks; the frontend mirrors for left by flipping scale.x). NO RIFE
    # in-betweening — attacks read through SNAP: few frames, strong
    # anticipation holds, a single explosive strike frame.
    ACTION_KEYFRAME_COUNT = 6
    _ACTION_CHOREOGRAPHY = {
        "attack": """A melee attack in 6 frames — anticipation, strike, recovery. The strike must
be EXPLOSIVE: frames 1-2 coil, frame 3 releases, frames 4-6 settle.
Frame 1 — READY: fighting stance, side profile facing right; knees bent, weight
  on the back LEFT leg, weapon arm (right) drawn back at hip height, off arm
  raised forward as a guard.
Frame 2 — COIL: deeper crouch, torso twisted away from the target, weapon arm
  cocked fully behind the body, shoulder loaded; the pose visibly stores energy.
Frame 3 — STRIKE: explosive lunge toward the right edge; RIGHT leg thrusts
  forward into a deep lunge, weapon arm fully extended in a horizontal slash,
  torso driving forward; hair/clothing whip backward from the speed.
Frame 4 — FOLLOW-THROUGH: the swing carries across the body to the far side,
  weight fully on the front RIGHT leg, torso rotated toward the target, weapon
  arm across the chest.
Frame 5 — REBALANCE: pushing back off the front leg, weapon arm retracting,
  torso straightening.
Frame 6 — GUARD: back to the ready stance of frame 1, arms in guard.
Every frame is a strict FULL SIDE PROFILE facing right, attacking toward the
right edge of the image.""",
        "dodge": """An evasive dodge-roll in 6 frames — drop, roll, recover, moving toward the
right edge of the image.
Frame 1 — DROP: side profile facing right; sudden crouch, knees deeply bent,
  torso leaning forward-right, arms swinging back for the push-off.
Frame 2 — PUSH-OFF: body launching low and forward to the right, back leg
  extended from the push, torso diving.
Frame 3 — TUCK: body curled into a tight ball mid-air, knees to chest, head
  tucked, rotated partway through the roll.
Frame 4 — GROUND ROLL: the curled body contacts the ground on the shoulder,
  mid-roll, legs coming over.
Frame 5 — RISE: uncurling out of the roll into a low crouch, one knee down,
  one hand on the ground, head coming up.
Frame 6 — RECOVERED: low ready stance facing right, knees bent, arms in guard.
Every frame is a strict FULL SIDE PROFILE facing right.""",
    }

    # Animals get their own choreography. The humanoid one ("weapon arm
    # drawn back at hip height", "guard up") turned a wolf into a bipedal
    # boxer on its hind legs — six clean frames of the wrong creature.
    _BEAST_CHOREOGRAPHY = {
        "attack": """A predator's lunge in 6 frames — crouch, spring, bite, land. The animal stays
ON ALL FOURS in every frame; it never stands upright, never uses its forelegs
like arms, never adopts a human fighting stance.
Frame 1 — STALK: side profile facing right; low crouch, head lowered below the
  shoulders, ears flat, weight gathered over the hindquarters, tail low.
Frame 2 — COIL: hindquarters compressed, forelegs braced, lips pulled back from
  the teeth, every muscle loaded.
Frame 3 — SPRING: fully airborne, body stretched horizontal toward the right
  edge, forelegs reaching, jaws wide open.
Frame 4 — BITE: forelegs planting, jaws snapping shut at the target's height,
  head driven forward, hindquarters still in the air.
Frame 5 — LAND: all four paws on the ground, body compressed from the landing,
  head still low and forward.
Frame 6 — SNARL: back in the low stalking crouch of frame 1, teeth bared.
Every frame is a strict FULL SIDE PROFILE facing right, attacking toward the
right edge of the image, on four legs.""",
        "dodge": """A predator's evasive leap in 6 frames — sidestep, bound, land. The animal stays
ON ALL FOURS in every frame; it never stands upright.
Frame 1 — FLINCH: side profile facing right; body crouched, head jerking away.
Frame 2 — PUSH-OFF: hindquarters driving, forelegs lifting, body launching
  forward-right.
Frame 3 — BOUND: fully airborne, body arched, legs tucked.
Frame 4 — DESCENT: forelegs reaching down toward the ground, body extending.
Frame 5 — LAND: forepaws down, hindquarters following, body compressed.
Frame 6 — READY: low crouch facing right, weight on all four paws.
Every frame is a strict FULL SIDE PROFILE facing right, on four legs.""",
    }
    _BEAST_WEAPON_LINE = (
        "The animal fights with teeth and claws only. No weapon, no tool, nothing held —\n"
        "it is an animal in every frame."
    )
    _HUMANOID_SPECIES = {
        "human", "humans", "humanoid", "elf", "elves", "dwarf", "dwarves", "halfling",
        "orc", "orcs", "goblin", "gnome", "half-elf", "half-orc", "tiefling", "android",
        "robot", "cyborg", "vampire", "ghoul", "zombie", "skeleton", "undead", "demon",
        "angel", "giant", "ogre", "troll", "lizardfolk", "kobold", "dragonborn",
    }
    _BEAST_WORDS = {
        "wolf", "wolves", "dog", "hound", "cur", "bear", "boar", "cat", "lion", "tiger",
        "panther", "leopard", "jaguar", "lynx", "fox", "rat", "horse", "deer", "stag",
        "elk", "bull", "ox", "goat", "ram", "snake", "serpent", "viper", "spider",
        "beast", "creature", "drake", "wyrm", "raptor", "lizard", "crocodile",
        "hyena", "jackal", "coyote", "badger", "wolverine", "mule", "donkey", "pig",
        "sow", "hog", "wyvern", "griffin", "basilisk", "manticore",
    }

    @classmethod
    def _is_beast(cls, character: "NPC | Player") -> bool:
        """True when the character is an animal (draws on four legs).

        The `species` field decides when it is set; otherwise the name and
        the physical description are searched for animal words. Humanoid
        species are never beasts even if their description mentions one
        ("wolf-tooth trophies").
        """
        import re as _re

        def tokens(value) -> set[str]:
            return set(_re.split(r"[^a-z]+", str(value or "").lower())) - {""}

        # The name is the strongest signal: "Hollow Wolf" is a wolf even when
        # the species column still holds its "human" default
        if tokens(getattr(character, "name", "")) & cls._BEAST_WORDS:
            return True
        species = tokens(getattr(character, "species", ""))
        if species & cls._BEAST_WORDS:
            return True
        if species & cls._HUMANOID_SPECIES:
            return False
        physical = str(getattr(character, "description_physical", "") or "").lower()
        return bool(tokens(" ".join(physical.split()[:12])) & cls._BEAST_WORDS)

    # What the strip says about the weapon. Three states, not two:
    #   None  -> the LEGACY sentence (byte-identical default; the reference
    #            image decides, which is what every existing character got)
    #   ""    -> the caller KNOWS the character is empty-handed
    #   text  -> the caller knows exactly which weapon is in the hand
    _LEGACY_WEAPON_LINE = (
        "If the character carries a weapon in the reference, they use that weapon;\n"
        "otherwise they fight bare-handed."
    )
    _UNARMED_WEAPON_LINE = (
        "The character fights BARE-HANDED. No weapon in any frame — fists, forearms and\n"
        "elbows only."
    )

    @classmethod
    def _weapon_line(cls, weapon_desc: str | None) -> str:
        """The weapon clause of the action-cycle prompt.

        A weapon that flickers between frames is the failure mode this line
        exists to kill, so an armed strip names the weapon in every beat of
        the arc rather than trusting the reference image.
        """
        if weapon_desc is None:
            return cls._LEGACY_WEAPON_LINE
        weapon_desc = " ".join(str(weapon_desc).split())
        if not weapon_desc:
            return cls._UNARMED_WEAPON_LINE
        return (
            f"The character is striking with {weapon_desc}. The weapon is DRAWN AND IN HAND in\n"
            "every single frame — cocked back at the hip in frames 1-2, sweeping through the\n"
            "arc in frames 3-4, settling back to guard in frames 5-6. Draw the same weapon at\n"
            "the same size, shape and colour in every frame; it never disappears, never\n"
            "changes into a different weapon, and is never sheathed."
        )

    def _build_action_cycle_prompt(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        action: str,
        weapon_desc: str | None = None,
    ) -> str:
        """Prompt for one 6-frame action filmstrip.

        With `weapon_desc=None` this is byte-identical to the pre-appearance
        prompt — the default path must not move a single character.
        """
        if action not in self._ACTION_CHOREOGRAPHY:
            raise ValueError(f"Unknown action {action!r}")
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        n = self.ACTION_KEYFRAME_COUNT
        beast = self._is_beast(character)
        choreography = (self._BEAST_CHOREOGRAPHY if beast else self._ACTION_CHOREOGRAPHY)[action]
        weapon_line = self._BEAST_WEAPON_LINE if beast else self._weapon_line(weapon_desc)

        return f"""The attached image shows the character "{character.name}" standing idle, from a 2D RPG.
Draw a sprite sheet: this EXACT character performing a {n}-frame {action} animation.

Format: a single horizontal filmstrip of {n} frames. Divide the image into {n} equal
vertical columns; place exactly one full-body pose centered in each column, with
clear empty space between poses (poses never touch the image edges).

The motion must be dynamic and clearly readable, like classic action-game animation:
{choreography}

Every frame: the SAME character design — clothing, colors, face and proportions
matching the reference image EXACTLY (do not redesign or simplify the outfit),
identical art style ({visual_style}), identical scale, one shared ground line.
{weapon_line}
But every frame is a DIFFERENT pose from the choreography above — no two frames in
the strip may repeat the same pose.
The ENTIRE canvas must be one continuous solid white background from edge to edge —
no black areas, no cards, no panels, no grid lines, no frame borders, no numbers,
no text, no shadows."""

    async def generate_action_cycle(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        action: str,
        reference_image: bytes,
        *,
        weapon_desc: str | None = None,
        sig: str = appearance.BASE_ACTION_SIG,
    ) -> dict[str, str]:
        """Generate a 6-frame combat action (attack/dodge) filmstrip.

        Same machinery as walks (one strip call, artifact-immune slicing,
        validation, feet-centroid normalization) but NO interpolation and
        neighbor-repair instead of phase-partner repair (an action is an arc,
        not a loop — frame i+3 is a different pose, not a leg-swap).

        Args:
            weapon_desc: None keeps the legacy "whatever the reference shows"
                sentence; "" states the character is empty-handed; a phrase
                names the weapon in every frame. Dodge always passes None
                (a roll shows the outfit, never the weapon — ruling A3).
            sig: appearance signature picking the output directory.

        Returns:
            Dict mapping "{action}{n}" to saved file path.
        """
        n = self.ACTION_KEYFRAME_COUNT
        prompt = self._build_action_cycle_prompt(character, world_bible, action, weapon_desc)

        logger.info(f"Generating {n}-frame {action} cycle for {character.name}")
        strip_data = await self._call_api(
            prompt, aspect_ratio="21:9", image_size="2K", reference_image=reference_image
        )
        self._save_debug_strip(action, character.id, strip_data)
        cells = self._slice_walk_strip(strip_data, n)
        try:
            frames = self._normalize_walk_frames(cells, reference_image, partner_repair=False)
        except ValueError as e:
            logger.warning(f"{action} strip unusable ({e}); regenerating once")
            strip_data = await self._call_api(
                prompt, aspect_ratio="21:9", image_size="2K", reference_image=reference_image
            )
            self._save_debug_strip(action, character.id, strip_data)
            cells = self._slice_walk_strip(strip_data, n)
            frames = self._normalize_walk_frames(cells, reference_image, partner_repair=False)

        paths: dict[str, str] = {}
        for i, frame_img in enumerate(frames, start=1):
            buf = io.BytesIO()
            frame_img.save(buf, format="PNG")
            key = f"{action}{i}"
            paths[key] = self._save_image(
                buf.getvalue(), self.sprite_rel(sig, f"{character.id}_{key}.png")
            )
        return paths

    async def generate_walk_cycle(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        reference_image: bytes,
        sig: str = appearance.BASE_BODY_SIG,
    ) -> dict[str, str]:
        """Generate a full walk cycle for one direction from a single API call.

        The model draws a 6-frame filmstrip which is sliced, background-removed,
        and normalized so every frame renders at the same on-screen size and
        baseline as the idle sprite.

        Returns:
            Dict mapping "{direction}_walk{n}" to saved file path.
        """
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        n = self.WALK_KEYFRAME_COUNT
        choreography = self._WALK_CHOREOGRAPHY.get(direction, self._WALK_CHOREOGRAPHY["right"])

        prompt = f"""The attached image shows the character "{character.name}" standing idle, from a 2D RPG.
Draw a sprite sheet: this EXACT character performing a {n}-frame walking animation.

Format: a single horizontal filmstrip of {n} frames. Divide the image into {n} equal
vertical columns; place exactly one full-body pose centered in each column, with
clear empty space between poses (poses never touch the image edges).

The walk must look energetic and clearly readable, like classic game animation:
{choreography}
The cycle loops seamlessly back to frame 1.

Every frame: the SAME character design — clothing, colors, face and proportions
matching the reference image EXACTLY (do not redesign or simplify the outfit),
identical art style ({visual_style}), identical scale, feet on one shared ground
line, pronounced opposite arm swing with relaxed open hands (never clenched fists).
But every frame is a DIFFERENT pose from the choreography above — no two frames in
the strip may repeat the same pose.
The ENTIRE canvas must be one continuous solid white background from edge to edge —
no black areas, no cards, no panels, no grid lines, no frame borders, no numbers,
no text, no shadows."""

        logger.info(f"Generating {n}-frame walk cycle ({direction}) for {character.name}")

        # 2K: more pixels per frame directly reduces partial/cropped characters
        strip_data = await self._call_api(
            prompt, aspect_ratio="21:9", image_size="2K", reference_image=reference_image
        )
        self._save_debug_strip(f"walk_{direction}", character.id, strip_data)
        cells = self._slice_walk_strip(strip_data, n)
        try:
            frames = self._normalize_walk_frames(cells, reference_image)
        except ValueError as e:
            # Too many broken cells to repair — one fresh strip is cheaper
            # than shipping a glitchy cycle. Second failure propagates.
            logger.warning(f"Walk strip unusable ({e}); regenerating once")
            strip_data = await self._call_api(
                prompt, aspect_ratio="21:9", image_size="2K", reference_image=reference_image
            )
            self._save_debug_strip(f"walk_{direction}", character.id, strip_data)
            cells = self._slice_walk_strip(strip_data, n)
            frames = self._normalize_walk_frames(cells, reference_image)
        logger.info(
            f"Walk cycle ({direction}) for {character.name}: motion "
            f"{self._motion_score(frames):.2f} (0 = every key identical)"
        )

        # In-between the keys locally: n keys -> 2n frames (free, seconds)
        from src.services.frame_interpolator import interpolate_cycle
        frames = interpolate_cycle(frames)

        paths: dict[str, str] = {}
        for i, frame_img in enumerate(frames, start=1):
            buf = io.BytesIO()
            frame_img.save(buf, format="PNG")
            key = f"{direction}_walk{i}"
            paths[key] = self._save_image(
                buf.getvalue(), self.sprite_rel(sig, f"{character.id}_{key}.png")
            )
        return paths

    def upgrade_walk_cycle(
        self, character_id: str, direction: str, sig: str = appearance.BASE_BODY_SIG
    ) -> dict[str, str] | None:
        """Upgrade a legacy key-only cycle (6 frames) to the full count by
        interpolation — no API call. Returns paths, or None if the legacy
        set isn't complete."""
        sprites_dir = self.sprite_path(sig, "")
        key_paths = [
            sprites_dir / f"{character_id}_{direction}_walk{i}.png"
            for i in range(1, self.WALK_KEYFRAME_COUNT + 1)
        ]
        if not all(p.exists() for p in key_paths):
            return None

        from src.services.frame_interpolator import interpolate_cycle
        logger.info(f"Upgrading legacy {direction} walk cycle for {character_id} by interpolation")
        frames = interpolate_cycle([Image.open(p).convert("RGBA") for p in key_paths])

        paths: dict[str, str] = {}
        for i, frame_img in enumerate(frames, start=1):
            key = f"{direction}_walk{i}"
            dest = sprites_dir / f"{character_id}_{key}.png"
            frame_img.save(dest)
            paths[key] = str(dest)
        return paths

    # Poses land roughly — not exactly — on the n-column grid, and wide
    # lunges run straight into their neighbours (a trailing boot under the
    # next pose's coat). A fixed slice cuts such poses in half (seen live: an
    # attack strip shipped frame 4 as half a character), and there is often
    # no white gap to find. So poses are segmented as connected blobs of
    # ink: touching poses are split at the thinnest column between them,
    # detached weapon tips are attached to the nearest pose, and every
    # foreign pixel inside a pose's crop is painted out. The grid is only
    # the last resort.
    _SLICE_LINE_WIDTH = 0.015       # a vertical rule is thinner than this × width
    _SLICE_LINE_FULL = 0.9          # a row/column this occupied is decoration
    _SLICE_LINE_EDGE = 0.5          # ...if occupancy just outside it drops below this
    _SLICE_LINE_FRINGE = 0.6        # ...and so are its anti-aliased neighbours
    _SLICE_LINE_GROW = 0.012        # ...but a fringe is never wider than this × size
    _SLICE_BIG_BLOB = 0.20          # a blob this × the largest is a pose, not debris
    _SLICE_ATTACH = 0.6             # debris within this × cell width joins a pose
    _SLICE_CUT_THROUGH = 0.15       # a split through ink this × the peak is a cut body

    @classmethod
    def _grow_bands(cls, seeds, occupancy, limit: int):
        """Extend every seeded run of rows/columns outward by at most
        `limit` while the occupancy stays above the fringe floor — the soft
        edge of a drawn line is lighter than its core but still darker than
        paper."""
        out = seeds.copy()
        n = len(out)
        i = 0
        while i < n:
            if not seeds[i]:
                i += 1
                continue
            j = i
            while j < n and seeds[j]:
                j += 1
            lo, hi = i, j
            while lo > 0 and i - lo < limit and occupancy[lo - 1] > cls._SLICE_LINE_FRINGE:
                lo -= 1
            while hi < n and hi - j < limit and occupancy[hi] > cls._SLICE_LINE_FRINGE:
                hi += 1
            out[lo:hi] = True
            i = j
        return out

    @classmethod
    def _band_runs(cls, flags) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        start = None
        for i, on in enumerate(flags):
            if on and start is None:
                start = i
            elif not on and start is not None:
                runs.append((start, i))
                start = None
        if start is not None:
            runs.append((start, len(flags)))
        return runs

    def _erase_lines(self, rgb, mask) -> None:
        """Paint drawn lines and cards out of the strip, in place.

        A full-width row band is a ground line (thin) or a letterbox card
        (thick); a thin full-height column band is a grid rule. A card is
        erased whole. A line is erased only where the paper on both sides
        of it is empty — where it crosses a pose it is left alone rather
        than carving a white hairline through the character.
        """
        import numpy as np

        h, w = mask.shape
        row_limit = max(3, int(h * self._SLICE_LINE_GROW))
        col_limit = 3  # a rule's anti-aliasing is a pixel or two wide

        row_occ = mask.mean(axis=1)
        row_seed = row_occ > self._SLICE_LINE_FULL
        for y0, y1 in self._band_runs(row_seed):
            if y1 - y0 > row_limit * 2:            # a card: no fringe, erase whole
                rgb[y0:y1, :, :] = 255
                mask[y0:y1, :] = False
                row_seed[y0:y1] = False
        for y0, y1 in self._band_runs(self._grow_bands(row_seed, row_occ, row_limit)):
            # Six wide poses can fill most of a row too — but a line has a
            # sharp edge: the rows just outside its fringe are nearly empty.
            outside = max(
                row_occ[y0 - 1] if y0 > 0 else 0.0,
                row_occ[y1] if y1 < h else 0.0,
            )
            if outside > self._SLICE_LINE_EDGE:
                continue
            above = mask[y0 - 1] if y0 > 0 else np.zeros(w, dtype=bool)
            below = mask[y1] if y1 < h else np.zeros(w, dtype=bool)
            clear = ~above & ~below
            rgb[y0:y1, clear, :] = 255
            mask[y0:y1, clear] = False

        col_occ = mask.mean(axis=0)
        col_seed = col_occ > self._SLICE_LINE_FULL
        max_line = max(1, int(w * self._SLICE_LINE_WIDTH))
        for x0, x1 in self._band_runs(col_seed):
            if x1 - x0 > max_line:                 # a side card: erase whole
                rgb[:, x0:x1, :] = 255
                mask[:, x0:x1] = False
                col_seed[x0:x1] = False
        for x0, x1 in self._band_runs(self._grow_bands(col_seed, col_occ, col_limit)):
            left = mask[:, x0 - 1] if x0 > 0 else np.zeros(h, dtype=bool)
            right = mask[:, x1] if x1 < w else np.zeros(h, dtype=bool)
            clear = ~left & ~right
            rgb[clear, x0:x1, :] = 255
            mask[clear, x0:x1] = False

    def _slice_walk_strip(self, strip_data: bytes, n: int) -> list["Image.Image"]:
        """Slice a filmstrip into n cells, immune to Gemini's decorations.

        Mask = not-near-white. Drawn lines and cards are erased first
        (`_erase_lines`), then the poses are found as blobs of ink
        (`_cells_from_blobs`); only a strip that does not read as n poses
        falls back to the fixed grid (`_cells_from_grid`), which flags any
        cell whose pose touches a wall as `clipped` so validation drops it.
        """
        import numpy as np

        img = Image.open(io.BytesIO(strip_data)).convert("RGB")
        rgb = np.array(img)
        mask = np.asarray(img.convert("L")) <= 240
        self._erase_lines(rgb, mask)
        img = Image.fromarray(rgb, "RGB")

        cells = self._cells_from_blobs(img, mask, n)
        if cells is not None:
            return cells
        return self._cells_from_grid(img, mask, n)

    def _cells_from_blobs(
        self, img: "Image.Image", mask, n: int
    ) -> list["Image.Image"] | None:
        """Segment n poses as connected blobs of ink.

        Big blobs are poses (or several poses touching); small blobs are
        debris or detached parts. Touching poses are split at the thinnest
        column between them, over-segmented strips are merged at the
        narrowest gap, and each pose's crop has every pixel that is not its
        own painted white. Returns None when the strip does not resolve to
        exactly n poses.
        """
        import numpy as np
        from scipy import ndimage

        w, h = img.size
        labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
        if count == 0:
            return None
        sizes = ndimage.sum(mask, labels, range(1, count + 1))
        slices = ndimage.find_objects(labels)
        big_thr = float(sizes.max()) * self._SLICE_BIG_BLOB
        big, small = [], []
        for lab, (sl, size) in enumerate(zip(slices, sizes), start=1):
            entry = (lab, sl[1].start, sl[1].stop, int(size))
            (big if size >= big_thr else small).append(entry)
        if not big:
            return None

        # A group is a set of labels restricted to a column range
        groups = [
            {"labels": {lab}, "x0": x0, "x1": x1, "cut": False}
            for lab, x0, x1, _ in sorted(big, key=lambda b: b[1])
        ]
        profile = mask.sum(axis=0)

        def group_mask(g):
            m = np.isin(labels, list(g["labels"]))
            m[:, :g["x0"]] = False
            m[:, g["x1"]:] = False
            return m

        # Too few blobs: poses touch. Split the widest at its valley.
        guard = 0
        while len(groups) < n and guard < n:
            guard += 1
            g = max(groups, key=lambda g: g["x1"] - g["x0"])
            gm = group_mask(g)
            prof = gm.sum(axis=0)
            x0, x1 = g["x0"], g["x1"]
            lo = x0 + (x1 - x0) // 4
            hi = x1 - (x1 - x0) // 4
            if hi - lo < 4:
                return None
            cut = lo + int(np.argmin(prof[lo:hi]))
            through = prof[cut] > prof[x0:x1].max() * self._SLICE_CUT_THROUGH
            left = {"labels": set(g["labels"]), "x0": x0, "x1": cut, "cut": through}
            right = {"labels": set(g["labels"]), "x0": cut, "x1": x1, "cut": through}
            groups[groups.index(g)] = left
            groups.insert(groups.index(left) + 1, right)

        # Too many blobs: a partial seventh pose at the edge is dropped
        # when it is light; a pose that broke in two (a white stripe, a
        # stray erase) is rejoined with its closest neighbour.
        while len(groups) > n:
            masses = [int(group_mask(g).sum()) for g in groups]
            median = float(np.median(masses))
            lightest = min(range(len(groups)), key=lambda i: masses[i])
            if masses[lightest] < median * 0.5:
                del groups[lightest]
                continue
            gaps = [
                (groups[i + 1]["x0"] - groups[i]["x1"], i)
                for i in range(len(groups) - 1)
            ]
            _, i = min(gaps)
            a, b = groups[i], groups[i + 1]
            groups[i:i + 2] = [{
                "labels": a["labels"] | b["labels"],
                "x0": a["x0"], "x1": b["x1"], "cut": a["cut"] or b["cut"],
            }]
        if len(groups) != n:
            return None

        # Debris near a pose is a detached part of it (a weapon tip, a
        # flicked cloak edge); debris far from every pose is a mark.
        reach = (w / n) * self._SLICE_ATTACH
        centers = [(g["x0"] + g["x1"]) / 2 for g in groups]
        for lab, x0, x1, _ in small:
            cx = (x0 + x1) / 2
            i = min(range(n), key=lambda i: abs(centers[i] - cx))
            if abs(centers[i] - cx) <= reach:
                groups[i]["labels"].add(lab)
                groups[i]["x0"] = min(groups[i]["x0"], x0)
                groups[i]["x1"] = max(groups[i]["x1"], x1)

        pad = 6
        rgb = np.array(img)
        cells: list[Image.Image] = []
        for g in groups:
            gm = group_mask(g)
            ys, xs = np.nonzero(gm)
            if len(xs) == 0:
                return None
            box = (
                max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
                min(w, int(xs.max()) + pad + 1), min(h, int(ys.max()) + pad + 1),
            )
            keep = ndimage.binary_dilation(
                gm[box[1]:box[3], box[0]:box[2]], iterations=2
            )
            patch = rgb[box[1]:box[3], box[0]:box[2]].copy()
            patch[~keep] = 255
            cell = Image.fromarray(patch, "RGB")
            cell.info["clipped"] = bool(g["cut"])
            cells.append(cell)
        return cells

    def _cells_from_grid(self, img: "Image.Image", mask, n: int) -> list["Image.Image"]:
        """The fixed n-column grid (legacy slicing). Cells are inset 2% so
        neighboring-cell slivers don't leak; a pose touching a cell wall is
        flagged `clipped` — half of it lives next door."""
        import numpy as np

        w, h = img.size
        cell_w = w // n
        inset = max(2, cell_w // 50)
        min_px = max(4, int(h * 0.01))
        cells: list[Image.Image] = []
        for i in range(n):
            x0, x1 = i * cell_w + inset, (i + 1) * cell_w - inset
            cell = img.crop((x0, 0, x1, h))
            cmask = mask[:, x0:x1].copy()
            cmask[cmask.mean(axis=1) > 0.8, :] = False
            cmask[:, cmask.mean(axis=0) > 0.8] = False
            ys, xs = np.nonzero(cmask)
            if len(xs) == 0:
                raise ValueError(f"Walk strip cell {i + 1} came back empty")
            pad = 6
            box = (
                max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
                min(cell.width, int(xs.max()) + pad), min(cell.height, int(ys.max()) + pad),
            )
            edge = max(2, cell.width // 100)
            out = cell.crop(box)
            out.info["clipped"] = bool(
                (cmask[:, :edge].sum(axis=0) >= min_px).any()
                or (cmask[:, -edge:].sum(axis=0) >= min_px).any()
            )
            cells.append(out)
        return cells

    # A frame is broken when its solid-pixel mass or dimensions collapse
    # relative to the strip median: rembg ate the character (ghost), the
    # slice caught a boundary (sliver), or the model merged two poses.
    _FRAME_MIN_HEIGHT = 0.75   # × median char height
    _FRAME_MIN_WIDTH = 0.45    # × median char width
    _FRAME_MAX_WIDTH = 2.2     # × median char width
    _FRAME_MIN_MASS = 0.55     # × median opaque-pixel count (a half
                               # character measured 0.45 and slipped through)
    _COMPONENT_KEEP = 0.025    # alpha islands lighter than this × the main
                               # body are debris: grid-line stubs, ground-line
                               # fragments, rembg crumbs — never the character

    @classmethod
    def _keep_main_components(cls, cut: "Image.Image") -> "Image.Image":
        """Drop small disconnected alpha islands from a background-removed
        frame. Seen live: white stubs of a ground line drifting beside the
        feet, a floating sliver of the neighbouring pose. The anti-aliased
        rim around kept islands is preserved by a small dilation."""
        import numpy as np
        from scipy import ndimage

        rgba = np.array(cut.convert("RGBA"))
        alpha = rgba[..., 3]
        solid = alpha > 128
        labels, count = ndimage.label(solid)
        if count <= 1:
            return cut
        sizes = ndimage.sum(solid, labels, range(1, count + 1))
        keep = np.zeros(count + 1, dtype=bool)
        keep[1:] = sizes >= float(sizes.max()) * cls._COMPONENT_KEEP
        keep_mask = ndimage.binary_dilation(keep[labels], iterations=3)
        rgba[..., 3] = np.where(keep_mask, alpha, 0)
        return Image.fromarray(rgba, "RGBA")

    @staticmethod
    def _frame_stats(frame: "Image.Image") -> tuple[int, int, int, float]:
        """(char_w, char_h, opaque_px, feet_center_x) of a cropped RGBA frame.

        feet_center_x is the alpha centroid of the bottom 15% band — the point
        the eye tracks as "where the character stands"; centering on it kills
        the side-to-side jitter that bbox-centering causes when a stride pose
        extends one leg.
        """
        import numpy as np

        alpha = np.asarray(frame.getchannel("A"))
        solid = alpha > 128
        opaque = int(solid.sum())
        if not opaque:
            return frame.width, frame.height, 0, frame.width / 2
        band_top = max(0, int(frame.height * 0.85))
        band = solid[band_top:, :]
        xs = np.nonzero(band)[1]
        feet_cx = float(xs.mean()) if len(xs) else frame.width / 2
        return frame.width, frame.height, opaque, feet_cx

    def _normalize_walk_frames(
        self, cells: list["Image.Image"], idle_sprite: bytes, partner_repair: bool = True
    ) -> list["Image.Image"]:
        """Background-remove, validate, repair, and align a strip's frames.

        Broken frames (ghosts, slivers, merges) are replaced by their phase
        partner — frame i and i + n/2 are the same pose with legs swapped, so
        the substitution preserves the cycle's rhythm. For non-looping ACTION
        strips pass partner_repair=False: the donor is the nearest intact
        neighbor instead (an attack's frame i+3 is a different pose entirely).
        More than half broken raises ValueError so the caller regenerates.

        Good frames get one uniform scale per strip (preserves the natural
        bob) on a shared canvas matching the idle sprite's character-height
        ratio and baseline fraction, and are anchored horizontally by feet
        centroid so the character doesn't shuffle sideways between frames.

        Raises:
            ValueError: if the strip has more broken frames than repair
                can plausibly hide.
        """
        import statistics

        idle = Image.open(io.BytesIO(idle_sprite)).convert("RGBA")
        idle_bbox = idle.getchannel("A").getbbox() or (0, 0, idle.width, idle.height)
        idle_ratio = (idle_bbox[3] - idle_bbox[1]) / idle.height
        idle_baseline_frac = idle_bbox[3] / idle.height

        transparent: list[Image.Image] = []
        clipped: list[bool] = []
        for cell in cells:
            clipped.append(bool(cell.info.get("clipped", False)))
            buf = io.BytesIO()
            cell.save(buf, format="PNG")
            cut = Image.open(io.BytesIO(self._remove_background(buf.getvalue()))).convert("RGBA")
            cut = self._keep_main_components(cut)
            bbox = cut.getchannel("A").getbbox()
            transparent.append(cut.crop(bbox) if bbox else cut)

        stats = [self._frame_stats(f) for f in transparent]
        med_w = statistics.median(s[0] for s in stats)
        med_h = statistics.median(s[1] for s in stats)
        med_mass = statistics.median(s[2] for s in stats)

        def is_broken(s: tuple) -> bool:
            w, h, mass, _ = s
            return (
                h < med_h * self._FRAME_MIN_HEIGHT
                or w < med_w * self._FRAME_MIN_WIDTH
                or w > med_w * self._FRAME_MAX_WIDTH
                or mass < med_mass * self._FRAME_MIN_MASS
            )

        broken = [i for i, s in enumerate(stats) if is_broken(s) or clipped[i]]
        if len(broken) > len(transparent) // 2:
            raise ValueError(
                f"{len(broken)}/{len(transparent)} frames broken (ghost/sliver/merge)"
            )
        n = len(transparent)
        for i in broken:
            if partner_repair:
                partner = (i + n // 2) % n
                donor = partner if partner not in broken else next(
                    j for j in range(n) if j not in broken
                )
            else:
                donor = min(
                    (j for j in range(n) if j not in broken),
                    key=lambda j: abs(j - i),
                )
            logger.info(f"Frame {i + 1} broken; substituting frame {donor + 1}")
            transparent[i] = transparent[donor].copy()
            stats[i] = stats[donor]

        max_char_h = max(f.height for f in transparent)
        canvas_h = max(1, round(max_char_h / idle_ratio))
        canvas_w = canvas_h  # square, like idle sprites
        baseline_y = round(canvas_h * idle_baseline_frac)

        frames: list[Image.Image] = []
        for f, s in zip(transparent, stats):
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            # Anchor the feet centroid to the canvas center, clamped so the
            # frame never clips out of the canvas.
            x = round(canvas_w / 2 - s[3])
            x = max(0, min(canvas_w - f.width, x))
            canvas.paste(f, (x, baseline_y - f.height), f)
            frames.append(canvas)
        return frames

    @staticmethod
    def _motion_score(frames: list["Image.Image"]) -> float:
        """1 - mean silhouette overlap between opposite-phase keys. A walk
        that actually walks scores ~0.3+; a strip of six identical standing
        poses scores ~0. Logged so weak strips can be found in the logs."""
        import numpy as np

        n = len(frames)
        if n < 2:
            return 0.0
        masks = [np.asarray(f.getchannel("A")) > 128 for f in frames]
        scores = []
        for i in range(n // 2):
            a, b = masks[i], masks[(i + n // 2) % n]
            if a.shape != b.shape:
                continue
            union = (a | b).sum()
            scores.append(1.0 - ((a & b).sum() / union if union else 1.0))
        return float(sum(scores) / len(scores)) if scores else 0.0

    def mirror_walk_cycle(
        self,
        character_id: str,
        source_direction: str,
        target_direction: str,
        sig: str = appearance.BASE_BODY_SIG,
    ) -> dict[str, str]:
        """Create walk frames for target_direction by mirroring source frames.

        Left/right cycles are mirror images — one API call covers both, and
        the mirrored pair is guaranteed to animate symmetrically.
        """
        from PIL import ImageOps

        paths: dict[str, str] = {}
        sprites_dir = self.sprite_path(sig, "")
        sprites_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, self.WALK_FRAME_COUNT + 1):
            src = sprites_dir / f"{character_id}_{source_direction}_walk{i}.png"
            if not src.exists():
                continue
            mirrored = ImageOps.mirror(Image.open(src).convert("RGBA"))
            key = f"{target_direction}_walk{i}"
            dest = sprites_dir / f"{character_id}_{key}.png"
            mirrored.save(dest)
            paths[key] = str(dest)
        return paths

    async def generate_all_sprites_for_character(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        include_walk_frames: bool = True,
        only_front: bool = False
    ) -> dict[str, str]:
        """Generate all directional sprites (and optionally walk animations) for a character.

        Only generates missing sprites. Uses front sprite as reference for other directions.

        Args:
            character: The character to generate sprites for
            world_bible: World configuration
            include_walk_frames: Whether to generate walk animation frames

        Returns:
            Dict mapping sprite key to file path:
            - "front", "back", "left", "right" for idle poses
            - "front_walk1", "front_walk2", etc. for walk frames
        """
        paths = {}
        sprites_dir = self.assets_dir / "sprites"

        # 1. Get or generate front sprite first (this sets the style)
        front_path = sprites_dir / f"{character.id}_front.png"
        if front_path.exists():
            logger.info(f"Using existing front sprite for {character.name}")
            paths["front"] = str(front_path)
        else:
            logger.info(f"Generating base front sprite for {character.name}")
            paths["front"] = await self.generate_character_sprite(character, world_bible, "front")

        # Read front sprite as reference for consistency
        front_image = Path(paths["front"]).read_bytes()

        if only_front:
            return paths

        # 2. Generate other directions using front as reference — in parallel
        #    (each direction only depends on the front sprite). Only back and
        #    right are generated; left is a mirror of right, which keeps it
        #    consistent with the mirrored left walk cycle (same side details)
        #    and saves a call.
        import asyncio

        async def _gen_direction(direction: str) -> tuple[str, str]:
            direction_path = sprites_dir / f"{character.id}_{direction}.png"
            if direction_path.exists():
                logger.info(f"Using existing {direction} sprite for {character.name}")
                return direction, str(direction_path)
            logger.info(f"Generating {direction} sprite for {character.name}")
            path = await self.generate_character_sprite_with_reference(
                character, world_bible, direction, front_image
            )
            return direction, path

        direction_results = await asyncio.gather(
            *(_gen_direction(d) for d in ["back", "right"])
        )
        for direction, path in direction_results:
            paths[direction] = path

        # Left is ALWAYS re-derived from right. A left idle generated by an
        # older pipeline is a separate drawing — playtesting found a player
        # whose left-facing idle was a different person (hat, coat, lantern)
        # from the other three views. Re-mirroring is free and idempotent.
        left_path = sprites_dir / f"{character.id}_left.png"
        from PIL import ImageOps
        ImageOps.mirror(Image.open(paths["right"]).convert("RGBA")).save(left_path)
        paths["left"] = str(left_path)

        # 3. Generate walk cycles — one filmstrip call per direction (frames
        #    drawn together stay consistent; right is mirrored from left)
        if include_walk_frames:
            def _cycle_complete(direction: str) -> bool:
                return all(
                    (sprites_dir / f"{character.id}_{direction}_walk{f}.png").exists()
                    for f in range(1, self.WALK_FRAME_COUNT + 1)
                )

            async def _gen_cycle(direction: str, idle_image: bytes) -> dict[str, str]:
                if _cycle_complete(direction):
                    logger.info(f"Using existing {direction} walk cycle for {character.name}")
                    return {
                        f"{direction}_walk{f}": str(sprites_dir / f"{character.id}_{direction}_walk{f}.png")
                        for f in range(1, self.WALK_FRAME_COUNT + 1)
                    }
                # Legacy 6-key set: interpolate up to the full count for free
                upgraded = self.upgrade_walk_cycle(character.id, direction)
                if upgraded:
                    return upgraded
                return await self.generate_walk_cycle(character, world_bible, direction, idle_image)

            walk_jobs = []
            for direction in ["front", "back", "right"]:
                idle_image = Path(paths[direction]).read_bytes()
                walk_jobs.append(_gen_cycle(direction, idle_image))

            cycle_results = await asyncio.gather(*walk_jobs)
            for cycle_paths in cycle_results:
                paths.update(cycle_paths)

            # Left cycle: always the mirror of right — free, guaranteed
            # symmetric, and matches the (re-mirrored) left idle above
            paths.update(self.mirror_walk_cycle(character.id, "right", "left"))

        logger.info(f"Prepared {len(paths)} sprites for {character.name}")
        return paths
