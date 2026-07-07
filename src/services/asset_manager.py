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
from src.services.reference_search import ReferenceImageSearch
from loguru import logger

if TYPE_CHECKING:
    pass



class AssetManager:
    """Manage generated game assets with caching.

    This service provides a caching layer on top of ImageGenerator.
    Assets are generated on-demand and cached to the database.
    """

    # Don't retry a failed generation for this many seconds (avoid re-billing
    # a broken prompt/model on every page load)
    FAILURE_COOLDOWN_SECONDS = 300

    def __init__(self, world_name: str):
        self.image_gen = ImageGenerator(world_name)
        self.ref_search = ReferenceImageSearch(world_name)
        self.assets_dir = Path("data/assets") / world_name
        # In-flight background generation tasks, keyed by asset key
        self._pending_tasks: dict[str, "asyncio.Task"] = {}
        # Recent generation failures: asset key -> monotonic timestamp
        self._failures: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Background generation plumbing
    # ------------------------------------------------------------------

    def _recently_failed(self, key: str) -> bool:
        import time
        ts = self._failures.get(key)
        return ts is not None and (time.monotonic() - ts) < self.FAILURE_COOLDOWN_SECONDS

    def is_generating(self, key: str) -> bool:
        task = self._pending_tasks.get(key)
        return task is not None and not task.done()

    def spawn_generation(self, key: str, coro_factory) -> bool:
        """Start a deduplicated background generation task.

        Args:
            key: Unique key for this asset (e.g. "bg:<location_id>").
            coro_factory: Zero-arg callable returning the coroutine to run.

        Returns:
            True if a task was started (or already running), False if skipped
            due to a recent failure.
        """
        if self.is_generating(key):
            return True
        if self._recently_failed(key):
            return False

        async def _run():
            import time
            try:
                await coro_factory()
                self._failures.pop(key, None)
            except Exception as e:
                logger.error(f"Background generation failed for {key}: {e}")
                self._failures[key] = time.monotonic()

        self._pending_tasks[key] = asyncio.create_task(_run())
        return True

    def _get_world_bible(self, db_session) -> WorldBible | None:
        """Get the world bible for style consistency."""
        return db_session.query(WorldBible).first()

    async def get_location_background(self, location_id: str) -> dict:
        """Get or generate location background.

        Returns dict with:
            - background_path: Path to background image
            - walkable_bounds: Collision bounds
        """
        # Fetch what we need, then RELEASE the session before the slow
        # network calls (image generation can take minutes — never hold a
        # DB connection across it)
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

            world_bible = self._get_world_bible(db)
            ref_query = location.reference_search_query
            walkable_bounds = location.walkable_bounds
            # Detach with attributes loaded so they stay readable
            db.expunge(location)
            if world_bible is not None:
                db.expunge(world_bible)

        # Generate new background (no session held)
        ref = await self.ref_search.find_reference(location_id, ref_query)
        path = await self.image_gen.generate_location_background(location, world_bible, reference_image=ref)

        # Cache path in database (fresh, short-lived session)
        with get_session() as db:
            fresh = db.get(Location, location_id)
            if fresh:
                fresh.background_image_path = path
                db.commit()

        return {
            "background_path": path,
            "walkable_bounds": walkable_bounds
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

            # If not, generate ONLY front (release the session before generating)
            with get_session() as db:
                character = db.query(NPC).filter(NPC.id == character_id).first()
                if not character:
                    raise ValueError(f"npc {character_id} not found")
                world_bible = self._get_world_bible(db)
                ref_query = character.reference_search_query
                db.expunge(character)
                if world_bible is not None:
                    db.expunge(world_bible)

            ref = await self.ref_search.find_reference(character_id, ref_query)
            path = await self.image_gen.generate_character_sprite(
                character, world_bible, "front", reference_image=ref
            )
            paths = {"front": path}

            with get_session() as db:
                fresh = db.get(NPC, character_id)
                if fresh:
                    fresh.sprite_path = path
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

        # Need to generate — fetch character, then release the session before
        # the (potentially minutes-long) generation calls
        with get_session() as db:
            if character_type == "player":
                character = db.query(Player).filter(Player.id == character_id).first()
            else:
                character = db.query(NPC).filter(NPC.id == character_id).first()

            if not character:
                raise ValueError(f"{character_type} {character_id} not found")

            world_bible = self._get_world_bible(db)
            ref_query = getattr(character, 'reference_search_query', None)
            db.expunge(character)
            if world_bible is not None:
                db.expunge(world_bible)

        # Search for a web reference image before generating
        ref = await self.ref_search.find_reference(character_id, ref_query)

        # Generate ALL sprites at once (with style consistency)
        logger.info(f"Generating all sprites for {character.name}...")
        if character_type == "player":
            # For players, pass reference to the front sprite generation
            # The front sprite then becomes the reference for other directions
            if ref:
                # Generate front with web reference, then use front for rest
                front_path = await self.image_gen.generate_character_sprite(
                    character, world_bible, "front", reference_image=ref
                )
            paths = await self.image_gen.generate_all_sprites_for_character(
                character, world_bible, include_walk_frames=include_walk
            )
        else:
            path = await self.image_gen.generate_character_sprite(
                character, world_bible, "front", reference_image=ref
            )
            paths = { "front": path }

        # Update database with base sprite path (fresh, short-lived session)
        with get_session() as db:
            if character_type == "player":
                fresh = db.get(Player, character_id)
                if fresh:
                    fresh.sprite_base_path = paths.get("front")
            else:
                fresh = db.get(NPC, character_id)
                if fresh:
                    fresh.sprite_path = paths.get("front")
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

            world_bible = self._get_world_bible(db)
            ref_query = npc.reference_search_query
            db.expunge(npc)
            if world_bible is not None:
                db.expunge(world_bible)

        # Generate new portrait (no session held)
        ref = await self.ref_search.find_reference(npc_id, ref_query)
        path = await self.image_gen.generate_portrait(npc, world_bible, reference_image=ref)

        # Cache path in database
        with get_session() as db:
            fresh = db.get(NPC, npc_id)
            if fresh:
                fresh.portrait_path = path
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

        scene = self._load_scene_data(location_id, player_id)

        # Generate/fetch everything concurrently — no DB session held here
        results = await asyncio.gather(
            self.get_location_background(location_id),
            self.get_player_sprite(player_id, scene["player"]["direction"]),
            *(self.get_npc_sprite(npc["id"], "front") for npc in scene["npcs"]),
        )

        bg_data = results[0]
        scene["player"]["sprite_path"] = results[1]
        for i, npc in enumerate(scene["npcs"]):
            npc["sprite_path"] = results[2 + i]

        return {
            "location_id": location_id,
            "location_name": scene["location_name"],
            "background_path": bg_data["background_path"],
            "walkable_bounds": bg_data["walkable_bounds"],
            "player": scene["player"],
            "npcs": scene["npcs"],
        }

    def _load_scene_data(self, location_id: str, player_id: str) -> dict:
        """Load location/player/NPC data as plain dicts (short-lived session)."""
        with get_session() as db:
            location = db.query(Location).filter(Location.id == location_id).first()
            if not location:
                raise ValueError(f"Location {location_id} not found")

            player = db.query(Player).filter(Player.id == player_id).first()
            if not player:
                raise ValueError(f"Player {player_id} not found")

            npcs = db.query(NPC).filter(NPC.current_location_id == location_id).all()

            return {
                "location_name": location.name,
                "background_path_cached": location.background_image_path,
                "walkable_bounds": location.walkable_bounds,
                "player": {
                    "id": player_id,
                    "name": player.name,
                    "x": player.position_x,
                    "y": player.position_y,
                    "scale": getattr(player, 'scale', 1.0) or 1.0,
                    "status": player.health_status,
                    "direction": player.facing_direction,
                    "sprite_path": None,
                },
                "npcs": [
                    {
                        "id": npc.id,
                        "name": npc.name,
                        "x": npc.position_x,
                        "y": npc.position_y,
                        "scale": getattr(npc, 'scale', 1.0) or 1.0,
                        "status": npc.status,
                        "sprite_path": None,
                        "tier": npc.tier.value if hasattr(npc.tier, 'value') else str(npc.tier),
                    }
                    for npc in npcs
                ],
            }

    async def get_location_assets_fast(self, location_id: str, player_id: str) -> dict:
        """Like get_location_assets, but NEVER blocks on generation.

        Returns whatever is already cached immediately; anything missing is
        generated in deduplicated background tasks and reported via the
        "pending" flag so the client can poll until the scene is complete.
        """
        scene = self._load_scene_data(location_id, player_id)
        pending = False

        # Background
        bg_path = scene["background_path_cached"]
        if bg_path and Path(bg_path).exists():
            background_path = bg_path
        else:
            background_path = None
            if self.spawn_generation(f"bg:{location_id}", lambda: self.get_location_background(location_id)):
                pending = True

        # Player sprite (full set generated in background if anything missing)
        direction = scene["player"]["direction"]
        player_sprite = self.assets_dir / "sprites" / f"{player_id}_{direction}.png"
        if player_sprite.exists():
            scene["player"]["sprite_path"] = str(player_sprite)
        if not self._check_all_sprites_exist(player_id, include_walk=True):
            if self.spawn_generation(
                f"sprites:{player_id}",
                lambda: self.ensure_all_sprites_generated(player_id, "player", include_walk=True),
            ):
                pending = True

        # NPC front sprites
        for npc in scene["npcs"]:
            npc_sprite = self.assets_dir / "sprites" / f"{npc['id']}_front.png"
            if npc_sprite.exists():
                npc["sprite_path"] = str(npc_sprite)
            else:
                npc_id = npc["id"]
                if self.spawn_generation(
                    f"sprites:{npc_id}",
                    lambda npc_id=npc_id: self.ensure_all_sprites_generated(npc_id, "npc"),
                ):
                    pending = True

        return {
            "location_id": location_id,
            "location_name": scene["location_name"],
            "background_path": background_path,
            "walkable_bounds": scene["walkable_bounds"],
            "player": scene["player"],
            "npcs": scene["npcs"],
            "pending": pending,
        }

    def prewarm_connected_locations(self, location_id: str, limit: int = 3) -> None:
        """Kick off background-image generation for connected locations.

        Only backgrounds (not sprites) to keep generation costs bounded —
        by the time the player travels, the scene image is usually ready.
        """
        try:
            from src.models.location import Connection

            with get_session() as db:
                conns = db.query(Connection).filter(
                    ((Connection.from_location_id == location_id) |
                     ((Connection.to_location_id == location_id) & (Connection.bidirectional == True)))  # noqa: E712
                ).limit(10).all()

                neighbor_ids = []
                for c in conns:
                    other = c.to_location_id if c.from_location_id == location_id else c.from_location_id
                    if other and other not in neighbor_ids:
                        neighbor_ids.append(other)

                # Only pre-warm locations without a cached background
                to_warm = []
                for loc_id in neighbor_ids:
                    loc = db.get(Location, loc_id)
                    if loc and not (loc.background_image_path and Path(loc.background_image_path).exists()):
                        to_warm.append(loc_id)
                    if len(to_warm) >= limit:
                        break

            for loc_id in to_warm:
                self.spawn_generation(f"bg:{loc_id}", lambda loc_id=loc_id: self.get_location_background(loc_id))
                logger.info(f"Pre-warming background for connected location {loc_id}")
        except Exception as e:
            logger.warning(f"Pre-warm of connected locations failed: {e}")

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
        # Static mount serves data/assets/ as /assets/
        assets_root = str(Path("data/assets"))
        if path.startswith(assets_root):
            relative = path[len(assets_root):]
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
