"""Image generation service using Gemini image generation API."""

import base64
import io
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
import httpx
from PIL import Image

from src.config import load_settings

if TYPE_CHECKING:
    from src.models.location import Location
    from src.models.npc import NPC
    from src.models.player import Player
    from src.models.world_bible import WorldBible

logger = logging.getLogger(__name__)


# Footprint = bottom slice of a detected box that actually blocks walking
FOOTPRINT_TOP_FRACTION = 0.75   # skip the upper 75% of each box (playtest
                                # feedback: 0.55 blocked too much walk-behind)
MIN_OBSTACLE_AREA = 0.0015      # ignore boxes under 0.15% of the scene
MAX_OBSTACLES = 12


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

Identify SOLID OBSTACLES a walking character could not pass through: furniture, tables, counters, market stalls, wells, fountains, statues, trees, boulders, crates, carts, water pools, fire pits.

Do NOT include: the open floor or ground itself, paths, rugs, doorways, shadows, wall surfaces at the edges of the scene, or anything a person could simply step over.

Return a JSON array (no other text): [{"label": "<short name>", "box_2d": [ymin, xmin, ymax, xmax]}] with coordinates in the 0-1000 range. Return at most 12 boxes, largest and most important obstacles first. Return [] if there are none."""

        payload = {
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(background_image).decode()}},
                {"text": prompt},
            ]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers={"Content-Type": "application/json"}, json=payload)
                response.raise_for_status()
                result = response.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            import json as _json
            boxes = _json.loads(text)
        except Exception as e:
            logger.warning(f"Obstacle detection failed: {e}")
            return []

        return boxes_to_footprint_polygons(boxes)

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

    # One Gemini call per direction produces the whole cycle as a filmstrip.
    # Frames generated together stay consistent (style, scale, proportions);
    # frames generated one-by-one visibly morph between poses.
    WALK_FRAME_COUNT = 6

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

    async def generate_walk_cycle(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        reference_image: bytes
    ) -> dict[str, str]:
        """Generate a full walk cycle for one direction from a single API call.

        The model draws a 6-frame filmstrip which is sliced, background-removed,
        and normalized so every frame renders at the same on-screen size and
        baseline as the idle sprite.

        Returns:
            Dict mapping "{direction}_walk{n}" to saved file path.
        """
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        n = self.WALK_FRAME_COUNT
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
            cells = self._slice_walk_strip(strip_data, n)
            frames = self._normalize_walk_frames(cells, reference_image)

        paths: dict[str, str] = {}
        for i, frame_img in enumerate(frames, start=1):
            buf = io.BytesIO()
            frame_img.save(buf, format="PNG")
            key = f"{direction}_walk{i}"
            paths[key] = self._save_image(buf.getvalue(), f"sprites/{character.id}_{key}.png")
        return paths

    def _slice_walk_strip(self, strip_data: bytes, n: int) -> list["Image.Image"]:
        """Slice a filmstrip into n cells, immune to Gemini's decorations.

        The model spaces poses evenly but unpredictably adds grid lines,
        ground lines, or letterbox cards. Mask = not-near-white, then clear
        any row/column masked across (almost) its full span — that kills
        lines and borders while keeping the character, which never spans a
        full cell. Cells are inset 2% so neighboring-cell slivers don't leak.
        """
        import numpy as np

        img = Image.open(io.BytesIO(strip_data)).convert("RGB")
        w, h = img.size
        cell_w = w // n
        inset = max(2, cell_w // 50)
        cells: list[Image.Image] = []
        for i in range(n):
            cell = img.crop((i * cell_w + inset, 0, (i + 1) * cell_w - inset, h))
            arr = np.asarray(cell.convert("L"))
            mask = arr <= 240
            mask[mask.mean(axis=1) > 0.8, :] = False
            mask[:, mask.mean(axis=0) > 0.8] = False
            ys, xs = np.nonzero(mask)
            if len(xs) == 0:
                raise ValueError(f"Walk strip cell {i + 1} came back empty")
            pad = 6
            box = (
                max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
                min(cell.width, int(xs.max()) + pad), min(cell.height, int(ys.max()) + pad),
            )
            cells.append(cell.crop(box))
        return cells

    # A frame is broken when its solid-pixel mass or dimensions collapse
    # relative to the strip median: rembg ate the character (ghost), the
    # slice caught a boundary (sliver), or the model merged two poses.
    _FRAME_MIN_HEIGHT = 0.75   # × median char height
    _FRAME_MIN_WIDTH = 0.45    # × median char width
    _FRAME_MAX_WIDTH = 2.2     # × median char width
    _FRAME_MIN_MASS = 0.40     # × median opaque-pixel count

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
        self, cells: list["Image.Image"], idle_sprite: bytes
    ) -> list["Image.Image"]:
        """Background-remove, validate, repair, and align a strip's frames.

        Broken frames (ghosts, slivers, merges) are replaced by their phase
        partner — frame i and i + n/2 are the same pose with legs swapped, so
        the substitution preserves the cycle's rhythm. More than half broken
        raises ValueError so the caller regenerates the strip.

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
        for cell in cells:
            buf = io.BytesIO()
            cell.save(buf, format="PNG")
            cut = Image.open(io.BytesIO(self._remove_background(buf.getvalue()))).convert("RGBA")
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

        broken = [i for i, s in enumerate(stats) if is_broken(s)]
        if len(broken) > len(transparent) // 2:
            raise ValueError(
                f"{len(broken)}/{len(transparent)} frames broken (ghost/sliver/merge)"
            )
        n = len(transparent)
        for i in broken:
            partner = (i + n // 2) % n
            donor = partner if partner not in broken else next(
                j for j in range(n) if j not in broken
            )
            logger.info(f"Walk frame {i + 1} broken; substituting frame {donor + 1}")
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

    def mirror_walk_cycle(self, character_id: str, source_direction: str, target_direction: str) -> dict[str, str]:
        """Create walk frames for target_direction by mirroring source frames.

        Left/right cycles are mirror images — one API call covers both, and
        the mirrored pair is guaranteed to animate symmetrically.
        """
        from PIL import ImageOps

        paths: dict[str, str] = {}
        sprites_dir = self.assets_dir / "sprites"
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

        left_path = sprites_dir / f"{character.id}_left.png"
        if not left_path.exists():
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
                return await self.generate_walk_cycle(character, world_bible, direction, idle_image)

            walk_jobs = []
            for direction in ["front", "back", "right"]:
                idle_image = Path(paths[direction]).read_bytes()
                walk_jobs.append(_gen_cycle(direction, idle_image))

            cycle_results = await asyncio.gather(*walk_jobs)
            for cycle_paths in cycle_results:
                paths.update(cycle_paths)

            # Left cycle: mirror of right — free, guaranteed symmetric, and
            # matches the mirrored left idle
            if _cycle_complete("left"):
                paths.update({
                    f"left_walk{f}": str(sprites_dir / f"{character.id}_left_walk{f}.png")
                    for f in range(1, self.WALK_FRAME_COUNT + 1)
                })
            else:
                paths.update(self.mirror_walk_cycle(character.id, "right", "left"))

        logger.info(f"Prepared {len(paths)} sprites for {character.name}")
        return paths
