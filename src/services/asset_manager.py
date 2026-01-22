"""Asset management service with caching for generated game assets."""

import logging
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from src.models import get_session
from src.models.location import Location
from src.models.npc import NPC
from src.models.player import Player
from src.models.world_bible import WorldBible
from src.services.image_generator import ImageGenerator
from loguru import logger

if TYPE_CHECKING:
    pass



class AssetManager:
    """Manage generated game assets with caching.

    This service provides a caching layer on top of ImageGenerator.
    Assets are generated on-demand and cached to the database.
    """

    def __init__(self):
        self.image_gen = ImageGenerator()
        self.assets_dir = Path("data/assets")

    def _get_world_bible(self, db_session) -> WorldBible | None:
        """Get the world bible for style consistency."""
        return db_session.query(WorldBible).first()

    async def get_location_background(self, location_id: str) -> dict:
        """Get or generate location background.

        Returns dict with:
            - background_path: Path to background image
            - walkable_bounds: Collision bounds
        """
        with get_session() as db:
            location = db.query(Location).filter(Location.id == location_id).first()
            if not location:
                raise ValueError(f"Location {location_id} not found")

            # Check if already generated
            if location.background_image_path and Path(location.background_image_path).exists():
                logger.info(f"Using cached background for location: {location.name}")
                return {
                    "background_path": location.background_image_path,
                    "walkable_bounds": location.walkable_bounds
                }

            # Generate new background
            world_bible = self._get_world_bible(db)
            path = await self.image_gen.generate_location_background(location, world_bible)

            # Cache path in database
            location.background_image_path = path
            db.commit()

            return {
                "background_path": path,
                "walkable_bounds": location.walkable_bounds
            }

    def _check_all_sprites_exist(self, character_id: str, include_walk: bool = True) -> bool:
        """Check if all sprites exist for a character."""
        directions = ["front", "back", "left", "right"]
        for direction in directions:
            if not (self.assets_dir / "sprites" / f"{character_id}_{direction}.png").exists():
                return False
            if include_walk:
                for frame in [1, 2]:
                    if not (self.assets_dir / "sprites" / f"{character_id}_{direction}_walk{frame}.png").exists():
                        return False
        return True

    async def ensure_all_sprites_generated(
        self,
        character_id: str,
        character_type: str = "npc",
        include_walk: bool = True
    ) -> dict[str, str]:
        """Ensure all sprites exist for a character, generating them if needed.

        Args:
            character_id: The character's ID
            character_type: 'npc' or 'player'
            include_walk: Whether to include walk animation frames

        Returns:
            Dict mapping sprite key to file path
        """

        if character_type == "npc":
            include_walk = False  # Never walk for NPCs
            # Only check if front exists
            front_path = self.assets_dir / "sprites" / f"{character_id}_front.png"
            if front_path.exists() and front_path.stat().st_size > 0:
                return {"front": str(front_path)}

            # If not, generate ONLY front
            with get_session() as db:
                character = db.query(NPC).filter(NPC.id == character_id).first()
                world_bible = self._get_world_bible(db)
                # Call generator with specific front-only flags
                paths = await self.image_gen.generate_all_sprites_for_character(
                    character, world_bible, include_walk_frames=False, only_front=True
                )
                character.sprite_path = paths.get("front")
                db.commit()
                return paths

        # Check if all sprites already exist
        if self._check_all_sprites_exist(character_id, include_walk):
            logger.info(f"All sprites exist for {character_id}")
            paths = {}
            for direction in ["front", "back", "left", "right"]:
                paths[direction] = str(self.assets_dir / "sprites" / f"{character_id}_{direction}.png")
                if include_walk:
                    for frame in [1, 2]:
                        paths[f"{direction}_walk{frame}"] = str(
                            self.assets_dir / "sprites" / f"{character_id}_{direction}_walk{frame}.png"
                        )
            return paths

        # Need to generate - get character from DB
        with get_session() as db:
            if character_type == "player":
                character = db.query(Player).filter(Player.id == character_id).first()
            else:
                character = db.query(NPC).filter(NPC.id == character_id).first()

            if not character:
                raise ValueError(f"{character_type} {character_id} not found")

            world_bible = self._get_world_bible(db)

            # Generate ALL sprites at once (with style consistency)
            logger.info(f"Generating all sprites for {character.name}...")
            if character_type == "player":
                paths = await self.image_gen.generate_all_sprites_for_character(
                    character, world_bible, include_walk_frames=include_walk
                )
            else:
                path = await self.image_gen.generate_character_sprite(character, world_bible, "front")
                paths = { "front": path }


            # Update database with base sprite path
            if character_type == "player":
                character.sprite_base_path = paths.get("front")
            else:
                character.sprite_path = paths.get("front")
            db.commit()

            return paths

    async def get_npc_sprite(self, npc_id: str, direction: str = "front") -> str:
        """Get or generate NPC sprite for given direction.

        If any sprites are missing, generates ALL directions and walk frames
        for style consistency.

        Returns path to sprite image.
        """
        # Check for cached sprite
        sprite_path = self.assets_dir / "sprites" / f"{npc_id}_{direction}.png"
        if sprite_path.exists():
            logger.info(f"Using cached sprite for NPC {npc_id} ({direction})")
            return str(sprite_path)

        # Generate all sprites for this NPC
        paths = await self.ensure_all_sprites_generated(npc_id, "npc", include_walk=True)
        return paths.get(direction, paths.get("front"))

    async def get_player_sprite(self, player_id: str, direction: str = "front") -> str:
        """Get or generate player sprite for given direction.

        If any sprites are missing, generates ALL directions and walk frames
        for style consistency.

        Returns path to sprite image.
        """
        # Check for cached sprite
        sprite_path = self.assets_dir / "sprites" / f"{player_id}_{direction}.png"
        if sprite_path.exists():
            logger.info(f"Using cached sprite for player {player_id} ({direction})")
            return str(sprite_path)

        # Generate all sprites for this player
        paths = await self.ensure_all_sprites_generated(player_id, "player", include_walk=True)
        return paths.get(direction, paths.get("front"))

    async def get_walk_frame(self, character_id: str, direction: str, frame: int, character_type: str = "npc") -> str:
        """Get walk animation frame for a character.

        Args:
            character_id: Character's ID
            direction: Facing direction
            frame: Frame number (1 or 2)
            character_type: 'npc' or 'player'

        Returns:
            Path to walk frame image
        """
        frame_path = self.assets_dir / "sprites" / f"{character_id}_{direction}_walk{frame}.png"
        if frame_path.exists():
            return str(frame_path)

        # Generate all sprites if walk frame doesn't exist
        paths = await self.ensure_all_sprites_generated(character_id, character_type, include_walk=True)
        return paths.get(f"{direction}_walk{frame}", paths.get(direction))

    async def get_npc_portrait(self, npc_id: str) -> str:
        """Get or generate NPC portrait for dialogue.

        Returns path to portrait image.
        """
        with get_session() as db:
            npc = db.query(NPC).filter(NPC.id == npc_id).first()
            if not npc:
                raise ValueError(f"NPC {npc_id} not found")

            # Check for cached portrait
            if npc.portrait_path and Path(npc.portrait_path).exists():
                logger.info(f"Using cached portrait for NPC: {npc.name}")
                return npc.portrait_path

            # Generate new portrait
            world_bible = self._get_world_bible(db)
            path = await self.image_gen.generate_portrait(npc, world_bible)

            # Cache path in database
            npc.portrait_path = path
            db.commit()

            return path

    async def get_location_assets(self, location_id: str, player_id: str) -> dict:
        """Get all assets needed to render a location.

        Returns dict with:
            - background_url: URL to background image
            - walkable_bounds: Collision bounds
            - player: Player info with sprite URL
            - npcs: List of NPCs with sprite URLs and positions
        """

        logger.info(f"--- ASSET MANAGER: Requesting assets for {location_id} ---")

        with get_session() as db:
            location = db.query(Location).filter(Location.id == location_id).first()
            if not location:
                raise ValueError(f"Location {location_id} not found")

            player = db.query(Player).filter(Player.id == player_id).first()
            if not player:
                raise ValueError(f"Player {player_id} not found")

            # Get background
            bg_data = self.get_location_background(location_id)

            # Get player sprite
            player_sprite_path =self.get_player_sprite(player_id, player.facing_direction)

            # Get NPCs at this location
            npcs = db.query(NPC).filter(NPC.current_location_id == location_id).all()

            npc_tasks = [self.get_npc_sprite(npc.id, "front") for npc in npcs]

            results = await asyncio.gather(
                bg_data,
                player_sprite_path,
                *npc_tasks
            )

            bg_data = results[0]
            player_sprite_path = results[1]
            npc_paths = results[2:]

            npc_data = []
            for i, npc in enumerate(npcs):
                npc_scale = getattr(npc, 'scale', 1.0) or 1.0
                logger.info(f"Loading NPC {npc.name}: scale={npc_scale} (raw={npc.scale})")
                npc_data.append({
                    "id": npc.id,
                    "name": npc.name,
                    "x": npc.position_x,
                    "y": npc.position_y,
                    "scale": npc_scale,
                    "status": npc.status,
                    "sprite_path": npc_paths[i],
                    "tier": npc.tier.value if hasattr(npc.tier, 'value') else str(npc.tier)
                })

            return {
                "location_id": location_id,
                "location_name": location.name,
                "background_path": bg_data["background_path"],
                "walkable_bounds": bg_data["walkable_bounds"],
                "player": {
                    "id": player_id,
                    "name": player.name,
                    "x": player.position_x,
                    "y": player.position_y,
                    "scale": getattr(player, 'scale', 1.0) or 1.0,
                    "status": player.health_status,
                    "direction": player.facing_direction,
                    "sprite_path": player_sprite_path
                },
                "npcs": npc_data
            }

    async def pregenerate_location_assets(self, location_id: str) -> None:
        """Pre-generate all assets for a location (background + all NPC sprites/portraits)."""
        with get_session() as db:
            location = db.query(Location).filter(Location.id == location_id).first()
            if not location:
                raise ValueError(f"Location {location_id} not found")

            # Generate background
            await self.get_location_background(location_id)

            # Generate NPC assets
            npcs = db.query(NPC).filter(NPC.current_location_id == location_id).all()
            for npc in npcs:
                # Generate all directional sprites
                for direction in ["front", "back", "left", "right"]:
                    await self.get_npc_sprite(npc.id, direction)
                # Generate portrait
                await self.get_npc_portrait(npc.id)

            logger.info(f"Pre-generated all assets for location: {location.name}")

    def get_asset_url(self, path: str) -> str:
        """Convert asset path to URL for frontend."""
        # Convert absolute path to relative URL
        if path.startswith(str(self.assets_dir)):
            relative = path[len(str(self.assets_dir)):]
            return f"/assets{relative.replace(chr(92), '/')}"  # Handle Windows paths
        return f"/assets/{path}"

    def clear_cache(self, asset_type: str = None) -> None:
        """Clear cached assets.

        Args:
            asset_type: Optional. One of 'locations', 'sprites', 'portraits', or None for all.
        """
        import shutil

        if asset_type:
            target_dir = self.assets_dir / asset_type
            if target_dir.exists():
                shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True)
                logger.info(f"Cleared {asset_type} cache")
        else:
            for subdir in ["locations", "sprites", "portraits"]:
                target_dir = self.assets_dir / subdir
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                    target_dir.mkdir(parents=True)
            logger.info("Cleared all asset caches")
