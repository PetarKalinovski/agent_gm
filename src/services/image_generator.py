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
            return await self._call_gemini(prompt, reference_image, aspect_ratio)
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
                    "aspectRatio": aspect_ratio
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

        # Remove background for transparency
        image_data = self._remove_background(image_data)

        path = self._save_image(image_data, f"sprites/{character.id}_{direction}.png")
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

        image_data = self._remove_background(image_data)
        path = self._save_image(image_data, f"sprites/{character.id}_{direction}.png")
        return path

    async def generate_walk_frame(
        self,
        character: "NPC | Player",
        world_bible: "WorldBible",
        direction: str,
        frame: int,
        reference_image: bytes
    ) -> str:
        """Generate a single walk animation frame.

        Args:
            character: The character
            world_bible: World configuration
            direction: Facing direction
            frame: Frame number (1 = left foot forward, 2 = right foot forward)
            reference_image: The idle sprite as reference

        Returns:
            Path to saved frame
        """
        visual_style = world_bible.visual_style if world_bible else "fantasy RPG game art"
        name = character.name

        direction_desc = {
            "front": "facing the viewer",
            "back": "facing away from viewer",
            "left": "facing left (profile)",
            "right": "facing right (profile)"
        }

        foot_desc = "left foot forward" if frame == 1 else "right foot forward"

        prompt = f"""The attached image shows a character standing idle. Draw this SAME character in a walking pose with {foot_desc}, mid-stride.

Character: {name}
Direction: {direction_desc.get(direction, direction)}

CRITICAL: Output ONE character only. NOT a sprite sheet, NOT multiple frames side by side. Just ONE figure, centered, in a walking pose.

- Same character, same art style, clothing, colors, proportions as the reference
- {foot_desc} — show the character mid-step
- {direction_desc.get(direction, direction)}
- Full body visible from head to feet, large and centered in the image
- Solid bright green background (#00FF00) for chroma key removal
- No other objects, no shadows, no text, no duplicates
- Style: {visual_style}"""

        logger.info(f"Generating walk frame {frame} ({direction}) for {name}")

        image_data = await self._call_api(
            prompt,
            aspect_ratio="1:1",
            image_size="1K",
            reference_image=reference_image
        )

        image_data = self._remove_background(image_data)
        path = self._save_image(image_data, f"sprites/{character.id}_{direction}_walk{frame}.png")
        return path

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
        #    (each direction only depends on the front sprite)
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
            *(_gen_direction(d) for d in ["back", "left", "right"])
        )
        for direction, path in direction_results:
            paths[direction] = path

        # 3. Generate walk animation frames in parallel — each frame only
        #    depends on its direction's idle sprite, which now exists
        if include_walk_frames:
            async def _gen_walk(direction: str, frame: int, idle_image: bytes) -> tuple[str, str]:
                key = f"{direction}_walk{frame}"
                walk_path = sprites_dir / f"{character.id}_{key}.png"
                if walk_path.exists():
                    logger.info(f"Using existing {key} sprite for {character.name}")
                    return key, str(walk_path)
                logger.info(f"Generating {key} sprite for {character.name}")
                path = await self.generate_walk_frame(
                    character, world_bible, direction, frame, idle_image
                )
                return key, path

            walk_jobs = []
            for direction in ["front", "back", "left", "right"]:
                idle_image = Path(paths[direction]).read_bytes()
                for frame in [1, 2]:
                    walk_jobs.append(_gen_walk(direction, frame, idle_image))

            walk_results = await asyncio.gather(*walk_jobs)
            for key, path in walk_results:
                paths[key] = path

        logger.info(f"Prepared {len(paths)} sprites for {character.name}")
        return paths
