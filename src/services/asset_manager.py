"""Asset management service with caching for generated game assets."""

import logging
import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from src.config import load_settings
from src.models import get_session
from src.models.location import Location
from src.models.npc import NPC
from src.models.player import Player
from src.models.world_bible import WorldBible
from src.services import appearance
from src.services.image_generator import ImageGenerator
from src.services.reference_search import ReferenceImageSearch
from src.services.scene_objects import public_objects
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
        # Completed sprite sets: (axis, character_id, signature). Completeness
        # is monotonic — frames only ever appear — so a True is never
        # re-checked; only LRU eviction drops an entry.
        self._complete: set[tuple[str, str, str]] = set()
        cfg = load_settings().appearance
        # One variant BODY set at a time by default: seven image calls fired
        # five times over because someone is playing with the character panel
        # is exactly the bill nobody wants.
        self._body_gate = asyncio.Semaphore(max(1, cfg.body_concurrency))
        self._action_gate = asyncio.Semaphore(max(1, cfg.action_concurrency))

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

        # Detect collision obstacles in the fresh background (best-effort)
        obstacles = None
        try:
            obstacles = await self.image_gen.detect_obstacles(Path(path).read_bytes())
        except Exception as e:
            logger.warning(f"Obstacle detection failed for {location_id}: {e}")

        # Cache path in database (fresh, short-lived session)
        with get_session() as db:
            fresh = db.get(Location, location_id)
            if fresh:
                fresh.background_image_path = path
                if obstacles is not None and fresh.obstacles is None:
                    fresh.obstacles = obstacles
                db.commit()

        return {
            "background_path": path,
            "walkable_bounds": walkable_bounds
        }

    # ------------------------------------------------------------------
    # Appearance: signature-scoped sprite sets (visible equipment)
    # ------------------------------------------------------------------

    DIRECTIONS = ("front", "back", "left", "right")

    def sprite_dir(self, sig: str) -> Path:
        """Directory holding one signature's frames.

        'base' and 'base-unarmed' resolve to the FLAT legacy sprites dir — that
        is the entire migration story (appearance ruling A4).

        Raises:
            ValueError: for a signature that is not a safe directory name.
                `?a=` is client-supplied; callers turn this into a 404.
        """
        if sig in (appearance.BASE_BODY_SIG, appearance.BASE_ACTION_SIG):
            return self.assets_dir / "sprites"
        if not appearance.is_valid_signature(sig):
            raise ValueError(f"Invalid appearance signature {sig!r}")
        return self.assets_dir / "sprites" / "variants" / sig

    @staticmethod
    def _frame_ready(path: Path) -> bool:
        """A frame counts only when it exists AND has bytes (a truncated write
        from a killed process must read as missing, not as done)."""
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    def body_frame_names(self, character_id: str) -> list[str]:
        """The 58 filenames one BODY signature holds: 4 idles, 4x12 walk
        frames, 6 dodge frames (a roll shows the outfit — ruling A3)."""
        names: list[str] = []
        for direction in self.DIRECTIONS:
            names.append(f"{character_id}_{direction}.png")
            names.extend(
                f"{character_id}_{direction}_walk{frame}.png"
                for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1)
            )
        names.extend(
            f"{character_id}_dodge{frame}.png"
            for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1)
        )
        return names

    def action_frame_names(self, character_id: str) -> list[str]:
        """The 6 filenames one ACTION signature holds."""
        return [
            f"{character_id}_attack{frame}.png"
            for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1)
        ]

    def _set_complete(self, axis: str, character_id: str, sig: str, names: list[str]) -> bool:
        key = (axis, character_id, sig)
        if key in self._complete:
            return True
        if not appearance.is_valid_signature(sig):
            return False
        directory = self.sprite_dir(sig)
        if not all(self._frame_ready(directory / name) for name in names):
            return False
        self._complete.add(key)
        return True

    def body_set_complete(self, character_id: str, sig: str) -> bool:
        """True when every one of the 58 body frames for `sig` is on disk."""
        return self._set_complete("body", character_id, sig, self.body_frame_names(character_id))

    def action_set_complete(self, character_id: str, sig: str) -> bool:
        """True when all 6 attack frames for `sig` are on disk."""
        return self._set_complete("action", character_id, sig, self.action_frame_names(character_id))

    def body_set_paths(self, character_id: str, sig: str) -> dict[str, str]:
        """{"front": path, "front_walk1": path, "dodge1": path, ...}."""
        directory = self.sprite_dir(sig)
        paths: dict[str, str] = {}
        for direction in self.DIRECTIONS:
            paths[direction] = str(directory / f"{character_id}_{direction}.png")
            for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
                paths[f"{direction}_walk{frame}"] = str(
                    directory / f"{character_id}_{direction}_walk{frame}.png"
                )
        for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1):
            paths[f"dodge{frame}"] = str(directory / f"{character_id}_dodge{frame}.png")
        return paths

    def action_set_paths(self, character_id: str, sig: str) -> dict[str, str]:
        """{"attack1": path, ... "attack6": path}."""
        directory = self.sprite_dir(sig)
        return {
            f"attack{frame}": str(directory / f"{character_id}_attack{frame}.png")
            for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1)
        }

    def serving_body_sig(self, character_id: str, wanted: str) -> str:
        """`wanted` if its 58 files are all there, else 'base'.

        'base' is returned WITHOUT a completeness check: the flat path is the
        legacy self-healing one — an individual missing file 404s and spawns
        exactly as it does today.
        """
        if not wanted or wanted == appearance.BASE_BODY_SIG:
            return appearance.BASE_BODY_SIG
        return wanted if self.body_set_complete(character_id, wanted) else appearance.BASE_BODY_SIG

    def serving_action_sig(self, character_id: str, wanted: str) -> str:
        """`wanted` if its 6 attack frames are there, else 'base-unarmed'."""
        if not wanted or wanted == appearance.BASE_ACTION_SIG:
            return appearance.BASE_ACTION_SIG
        return (
            wanted if self.action_set_complete(character_id, wanted)
            else appearance.BASE_ACTION_SIG
        )

    @staticmethod
    def body_key(character_id: str, sig: str) -> str:
        """`spawn_generation` dedup key — one in-flight task per (char, sig)."""
        return f"body:{character_id}:{sig}"

    @staticmethod
    def action_key(character_id: str, sig: str) -> str:
        return f"action:{character_id}:{sig}"

    def wanted_appearance(self, player_id: str) -> dict[str, Any]:
        """`appearance_payload` for a player id — ONE indexed read.

        Only the LENIENT sprite path needs this (a request with no `?a=`),
        which the client uses during the initial load of a scene. Steady-state
        rendering passes `?a=` and never touches the database.
        """
        with get_session() as db:
            p = db.query(Player).filter(Player.id == player_id).first()
            return appearance.appearance_payload(p) if p else appearance.base_payload()

    def wanted_body_sig_from_db(self, player_id: str) -> str:
        return self.wanted_appearance(player_id)["body_sig"]

    def wanted_action_sig_from_db(self, player_id: str) -> str:
        return self.wanted_appearance(player_id)["action_sig"]

    def _spawn(self, key: str, coro_factory) -> bool:
        """`spawn_generation` that tolerates having no event loop.

        Every appearance entry point is fire-and-forget; a synchronous caller
        outside the server (a CLI, a test) must not blow up because there is
        nothing to attach a task to.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(f"No running event loop — skipped background task {key}")
            return False
        return self.spawn_generation(key, coro_factory)

    def spawn_missing_appearance(self, player_id: str, body_sig: str, action_sig: str) -> bool:
        """Start generation for whatever the player's look is still missing.

        Idempotent by construction: `spawn_generation` deduplicates on the key,
        which now carries the signature, so a reconnecting client restarts a
        task a server restart dropped without ever doubling one up.
        """
        started = False
        if body_sig != appearance.BASE_BODY_SIG and not self.body_set_complete(player_id, body_sig):
            started |= self._spawn(
                self.body_key(player_id, body_sig),
                lambda: self.ensure_body_set(player_id, body_sig),
            )
        if action_sig != appearance.BASE_ACTION_SIG and not self.action_set_complete(player_id, action_sig):
            started |= self._spawn(
                self.action_key(player_id, action_sig),
                lambda: self.ensure_action_set(player_id, action_sig),
            )
        return started

    def _appearance_block(
        self, player_id: str, payload: dict[str, Any], spawn: bool = True
    ) -> dict[str, Any]:
        """The readiness payload — the ONLY place readiness touches disk."""
        body = payload.get("body_sig") or appearance.BASE_BODY_SIG
        action = payload.get("action_sig") or appearance.BASE_ACTION_SIG
        if spawn:
            self.spawn_missing_appearance(player_id, body, action)
        body_key = self.body_key(player_id, body)
        action_key = self.action_key(player_id, action)
        return {
            "player_id": player_id,
            "body_sig": body,
            "weapon_sig": payload.get("weapon_sig") or appearance.BASE_WEAPON_SIG,
            "action_sig": action,
            "version": payload.get("version", appearance.APPEARANCE_VERSION),
            "body_ready": (
                body == appearance.BASE_BODY_SIG or self.body_set_complete(player_id, body)
            ),
            "action_ready": (
                action == appearance.BASE_ACTION_SIG or self.action_set_complete(player_id, action)
            ),
            "serving_body": self.serving_body_sig(player_id, body),
            "serving_action": self.serving_action_sig(player_id, action),
            "generating": self.is_generating(body_key) or self.is_generating(action_key),
            "failed": self._recently_failed(body_key) or self._recently_failed(action_key),
        }

    def appearance_status(self, player_id: str, spawn: bool = True) -> dict[str, Any]:
        """Readiness for both axes in one answer (`GET /api/assets/appearance`).

        Also spawns anything missing, so a client that reconnects mid-generation
        restarts a task the server restart dropped.
        """
        return self._appearance_block(player_id, self.wanted_appearance(player_id), spawn=spawn)

    def _drop_complete_memo(self, character_id: str, sig: str) -> None:
        for axis in ("body", "action"):
            self._complete.discard((axis, character_id, sig))

    def _evict_variant_dir(self, directory: Path, character_id: str) -> None:
        """Remove one character's frames from a variant directory.

        Directories are signature-keyed, not player-keyed (two players in the
        same coat share one), so eviction removes this character's files and
        only drops the directory once nothing is left in it.
        """
        for frame in directory.glob(f"{character_id}_*.png"):
            try:
                frame.unlink()
            except OSError as e:  # pragma: no cover - best effort
                logger.warning(f"Could not evict {frame}: {e}")
        try:
            if not any(directory.iterdir()):
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:  # pragma: no cover - directory already gone
            pass
        self._drop_complete_memo(character_id, directory.name)
        logger.info(f"Evicted appearance variant {directory.name} for {character_id}")

    def _evict_lru_variants(self, player_id: str, keep: set[str]) -> None:
        """Delete this player's least-recently-used variant directories down to
        the cap. Never evicts `base` (it is not under variants/ at all), never
        evicts anything in `keep` (the currently-wanted and currently-serving
        signatures), and reserves a slot for a `keep` signature not yet built.
        Drops the matching completeness memo entries."""
        cap = max(1, int(load_settings().appearance.max_variants_per_player))
        root = self.assets_dir / "sprites" / "variants"
        if not root.is_dir():
            return
        keep = {sig for sig in (keep or set()) if sig and sig != appearance.BASE_BODY_SIG}

        owned: list[tuple[float, Path]] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            frames = list(entry.glob(f"{player_id}_*.png"))
            if not frames:
                continue
            owned.append((max(f.stat().st_mtime for f in frames), entry))
        owned.sort(key=lambda row: row[0])  # least recently used first

        present = {entry.name for _mtime, entry in owned}
        reserved = sum(1 for sig in keep if sig not in present)
        allowed = max(1, cap - reserved)

        remaining = len(owned)
        for _mtime, entry in owned:
            if remaining <= allowed:
                break
            if entry.name in keep:
                continue
            self._evict_variant_dir(entry, player_id)
            remaining -= 1

    def _check_all_sprites_exist(self, character_id: str, include_walk: bool = True) -> bool:
        """Check if all sprites exist for a character."""
        directions = ["front", "back", "left", "right"]
        for direction in directions:
            if not (self.assets_dir / "sprites" / f"{character_id}_{direction}.png").exists():
                return False
            if include_walk:
                for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
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
                    for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1):
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

    def _load_character(self, character_id: str, character_type: str):
        """(character, world_bible), both detached — never hold a session
        across a generation call."""
        with get_session() as db:
            if character_type == "player":
                character = db.query(Player).filter(Player.id == character_id).first()
            else:
                character = db.query(NPC).filter(NPC.id == character_id).first()
            if not character:
                raise ValueError(f"{character_type} {character_id} not found")
            world_bible = self._get_world_bible(db)
            db.expunge(character)
            if world_bible is not None:
                db.expunge(world_bible)
        return character, world_bible

    def _load_appearance_context(self, player_id: str) -> dict[str, Any]:
        """Everything a variant generation needs, in one short session:
        the detached player row, the world bible, the WANTED signatures and
        the two gear phrases that go into prompts."""
        with get_session() as db:
            p = db.query(Player).filter(Player.id == player_id).first()
            if not p:
                raise ValueError(f"player {player_id} not found")
            world_bible = self._get_world_bible(db)
            context = {
                "character": p,
                "world_bible": world_bible,
                "appearance": appearance.appearance_payload(p),
                "outfit_desc": appearance.outfit_phrase(p),
                "weapon_desc": appearance.weapon_phrase(p),
            }
            db.expunge(p)
            if world_bible is not None:
                db.expunge(world_bible)
        return context

    def _action_reference(self, character_id: str, body_sig: str) -> bytes:
        """Reference image for an action strip.

        Side-view actions match best against the right-facing idle of the SAME
        body signature (so the armour is right); the ladder falls back to that
        body's front idle, then the base right, then the base front — NPCs only
        ever have a front.
        """
        candidates: list[Path] = []
        if body_sig and body_sig != appearance.BASE_BODY_SIG:
            variant = self.sprite_dir(body_sig)
            candidates += [variant / f"{character_id}_right.png",
                           variant / f"{character_id}_front.png"]
        flat = self.sprite_dir(appearance.BASE_BODY_SIG)
        candidates += [flat / f"{character_id}_right.png",
                       flat / f"{character_id}_front.png"]
        for path in candidates:
            if self._frame_ready(path):
                return path.read_bytes()
        raise ValueError(f"No idle sprite yet for {character_id} — actions need a reference")

    async def ensure_attack_sprites(
        self,
        character_id: str,
        character_type: str = "npc",
        sig: str = appearance.BASE_ACTION_SIG,
    ) -> dict[str, str]:
        """Ensure the 6 attack frames for one action signature exist.

        Players AND NPCs — NPCs are always `base-unarmed` in v1, so `?a=` is
        ignored for them. A player on a non-base signature routes through
        `ensure_action_set`, which knows which weapon is in the hand.
        """
        if character_type == "player" and sig != appearance.BASE_ACTION_SIG:
            return await self.ensure_action_set(character_id, sig)
        if self.action_set_complete(character_id, sig):
            return self.action_set_paths(character_id, sig)

        character, world_bible = self._load_character(character_id, character_type)
        reference = self._action_reference(character_id, appearance.split_action_sig(sig)[0])
        # Base is the legacy signature: no weapon_desc, so the prompt is
        # byte-identical to the pre-appearance one (ruling A4).
        return await self.image_gen.generate_action_cycle(
            character, world_bible, "attack", reference, sig=sig
        )

    async def _ensure_dodge_strip(
        self,
        character_id: str,
        sig: str,
        character=None,
        world_bible=None,
        reference: bytes | None = None,
    ) -> dict[str, str]:
        """The 6 dodge frames for one BODY signature (ruling A3: a roll shows
        the outfit, never the weapon — `weapon_desc` is never passed)."""
        directory = self.sprite_dir(sig)
        existing = {
            f"dodge{frame}": str(directory / f"{character_id}_dodge{frame}.png")
            for frame in range(1, ImageGenerator.ACTION_KEYFRAME_COUNT + 1)
        }
        if all(self._frame_ready(Path(p)) for p in existing.values()):
            return existing

        if character is None:
            character, world_bible = self._load_character(character_id, "player")
        if reference is None:
            reference = self._action_reference(character_id, sig)
        return await self.image_gen.generate_action_cycle(
            character, world_bible, "dodge", reference, sig=sig
        )

    async def ensure_action_sprites(
        self, character_id: str, character_type: str = "npc"
    ) -> dict[str, str]:
        """Combat action frames at the BASE signature (attack for everyone,
        dodge for players).

        DEPRECATED compatibility shim: `src/web/server.py` still calls this for
        an `attack*`/`dodge*` 404. The appearance router replaces it with
        `ensure_attack_sprites` (action axis) and `ensure_body_set` (body axis,
        which owns dodge). Behaviour here is exactly what it was before the
        appearance pipeline landed.
        """
        paths = await self.ensure_attack_sprites(character_id, character_type)
        if character_type == "player":
            paths.update(await self._ensure_dodge_strip(character_id, appearance.BASE_BODY_SIG))
        return paths

    async def ensure_action_set(self, player_id: str, sig: str) -> dict[str, str]:
        """Generate the 6-frame attack strip for one action signature. 1 call.

        Reference = the RIGHT idle of the pair's body signature (so the armour
        is right); the weapon phrase comes from the equipped weapon's
        `visual_desc` (so the sword is right). Behind `_action_gate`.
        """
        if not appearance.is_valid_signature(sig):
            raise ValueError(f"Invalid appearance signature {sig!r}")
        if self.action_set_complete(player_id, sig):
            return self.action_set_paths(player_id, sig)

        context = self._load_appearance_context(player_id)
        wanted = context["appearance"]["action_sig"]
        if wanted != sig:
            # The gear moved on while this was queued. Painting today's weapon
            # into yesterday's directory would poison that cache forever.
            raise ValueError(f"Stale action signature {sig!r} for {player_id} (now {wanted!r})")

        body_sig, _weapon_sig = appearance.split_action_sig(sig)
        reference = self._action_reference(player_id, body_sig)
        # "" means the pipeline KNOWS they are empty-handed; base keeps the
        # legacy "whatever the reference shows" sentence (A4).
        weapon_desc = context["weapon_desc"] or ""

        async with self._action_gate:
            if self.action_set_complete(player_id, sig):
                return self.action_set_paths(player_id, sig)
            paths = await self.image_gen.generate_action_cycle(
                context["character"], context["world_bible"], "attack", reference,
                weapon_desc=weapon_desc, sig=sig,
            )
        self.action_set_complete(player_id, sig)  # warm the memo
        return paths

    async def ensure_body_set(self, player_id: str, sig: str) -> dict[str, str]:
        """Generate the 58-file body set for one signature. 7 API calls.

        `sig == 'base'` delegates to today's `ensure_all_sprites_generated`
        plus the base dodge strip — one code path, no special-casing
        downstream. Variants are disk-only: `Player.sprite_base_path` keeps
        pointing at the BASE front idle, which is the identity anchor every
        variant is generated from (ruling A9).
        """
        if not appearance.is_valid_signature(sig):
            raise ValueError(f"Invalid appearance signature {sig!r}")

        # 0. A variant has nothing to be consistent WITH until the base exists.
        #    The base set is NOT gated — a first-run player never queues behind
        #    somebody's cosmetic variant.
        paths = await self.ensure_all_sprites_generated(player_id, "player", include_walk=True)
        if sig == appearance.BASE_BODY_SIG:
            paths.update(await self._ensure_dodge_strip(player_id, sig))
            return paths

        async with self._body_gate:
            return await self._generate_variant_body(player_id, sig)

    async def _generate_variant_body(self, player_id: str, sig: str) -> dict[str, str]:
        """Steps 1-7 of `ensure_body_set`, inside the concurrency gate.

        Every step reuses a frame that is already on disk, so a half-written
        directory from a killed process is re-derived, not re-billed.
        """
        from PIL import Image, ImageOps

        if self.body_set_complete(player_id, sig):
            return self.body_set_paths(player_id, sig)

        context = self._load_appearance_context(player_id)
        wanted = context["appearance"]["body_sig"]
        if wanted != sig:
            raise ValueError(f"Stale body signature {sig!r} for {player_id} (now {wanted!r})")
        outfit_desc = context["outfit_desc"]
        if not outfit_desc:
            raise ValueError(f"No outfit to draw for {player_id} — {sig!r} is not a variant")

        character = context["character"]
        world_bible = context["world_bible"]
        directory = self.sprite_dir(sig)

        # 1. Make room (never evicts base, the wanted sig, or what we serve now)
        self._evict_lru_variants(player_id, keep={sig, self.serving_body_sig(player_id, sig)})

        async def _idle(direction: str, reference: bytes) -> str:
            dest = directory / f"{player_id}_{direction}.png"
            if self._frame_ready(dest):
                return str(dest)
            return await self.image_gen.generate_outfit_variant_idle(
                character, world_bible, direction, reference, outfit_desc, sig
            )

        # 2. Variant front idle <- BASE front idle (the identity anchor)
        base_front = self.sprite_dir(appearance.BASE_BODY_SIG) / f"{player_id}_front.png"
        if not self._frame_ready(base_front):
            raise ValueError(f"No base front idle for {player_id} — a variant needs an anchor")
        paths: dict[str, str] = {"front": await _idle("front", base_front.read_bytes())}

        # 3. Back and right <- the VARIANT front idle. Chaining through the
        #    variant is the only way the garment propagates around the
        #    character; the base front would hand back the old outfit.
        variant_front = Path(paths["front"]).read_bytes()
        back, right = await asyncio.gather(
            _idle("back", variant_front), _idle("right", variant_front)
        )
        paths["back"], paths["right"] = back, right

        # 4. Left idle = mirror of right (matches the mirrored left walk)
        left = directory / f"{player_id}_left.png"
        if not self._frame_ready(left):
            ImageOps.mirror(Image.open(paths["right"]).convert("RGBA")).save(left)
        paths["left"] = str(left)

        # 5. Walk strips front/back/right <- the matching VARIANT idles
        def _cycle_complete(direction: str) -> bool:
            return all(
                self._frame_ready(directory / f"{player_id}_{direction}_walk{frame}.png")
                for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1)
            )

        async def _cycle(direction: str) -> dict[str, str]:
            if _cycle_complete(direction):
                return {
                    f"{direction}_walk{frame}": str(
                        directory / f"{player_id}_{direction}_walk{frame}.png"
                    )
                    for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1)
                }
            return await self.image_gen.generate_walk_cycle(
                character, world_bible, direction,
                Path(paths[direction]).read_bytes(), sig=sig,
            )

        for cycle_paths in await asyncio.gather(*(_cycle(d) for d in ("front", "back", "right"))):
            paths.update(cycle_paths)

        # 6. Left walk = mirror of right walk
        if _cycle_complete("left"):
            paths.update({
                f"left_walk{frame}": str(directory / f"{player_id}_left_walk{frame}.png")
                for frame in range(1, ImageGenerator.WALK_FRAME_COUNT + 1)
            })
        else:
            paths.update(self.image_gen.mirror_walk_cycle(player_id, "right", "left", sig=sig))

        # 7. Dodge strip <- the variant right idle (body axis, ruling A3)
        paths.update(await self._ensure_dodge_strip(
            player_id, sig, character, world_bible, Path(paths["right"]).read_bytes()
        ))

        self.body_set_complete(player_id, sig)  # warm the memo
        logger.info(f"Appearance variant {sig} complete for {player_id}")
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
                "obstacles": location.obstacles or [],
                "objects": public_objects(location.objects),
                # Which sprite set their gear entitles them to (pure dict math
                # over the row we already loaded — no extra query)
                "appearance": appearance.appearance_payload(player),
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

        # Player sprite: signature-aware. The scene is served the SERVING set
        # (guaranteed complete on disk), while the WANTED one generates in the
        # background — `pending` already drives the client's scene re-poll, so
        # a player who reloads mid-generation gets the variant when it lands.
        wanted = scene["appearance"]
        body_sig = wanted["body_sig"]
        serving_body = self.serving_body_sig(player_id, body_sig)
        direction = scene["player"]["direction"]
        player_sprite = self.sprite_dir(serving_body) / f"{player_id}_{direction}.png"
        if player_sprite.exists():
            scene["player"]["sprite_path"] = str(player_sprite)
        if body_sig == appearance.BASE_BODY_SIG:
            # Base stays on the legacy check: a world with idles and walks but
            # no dodge strip must not start paying for one on every scene load
            body_incomplete = not self._check_all_sprites_exist(player_id, include_walk=True)
        else:
            body_incomplete = not self.body_set_complete(player_id, body_sig)
        if body_incomplete and self.spawn_generation(
            self.body_key(player_id, body_sig),
            lambda: self.ensure_body_set(player_id, body_sig),
        ):
            pending = True

        # NPC front sprites
        npcs_ready = 0
        for npc in scene["npcs"]:
            npc_sprite = self.assets_dir / "sprites" / f"{npc['id']}_front.png"
            if npc_sprite.exists():
                npc["sprite_path"] = str(npc_sprite)
                npcs_ready += 1
            else:
                npc_id = npc["id"]
                if self.spawn_generation(
                    f"sprites:{npc_id}",
                    lambda npc_id=npc_id: self.ensure_all_sprites_generated(npc_id, "npc"),
                ):
                    pending = True

        # Scene readiness: the client holds its buffering overlay until the
        # world can actually be PLAYED, not merely glimpsed — background on
        # screen, the player's full serving set (idles + walks) on disk, and
        # a body for every character present. NPC walk frames and portraits
        # stream in after the reveal; a missing outfit VARIANT never blocks
        # (the serving set is the complete one by contract, so an equip swap
        # keeps the scene playable in the old look while the new one paints).
        if serving_body == appearance.BASE_BODY_SIG:
            player_ready = self._check_all_sprites_exist(player_id, include_walk=True)
        else:
            player_ready = True
        background_ready = background_path is not None
        scene_ready = background_ready and player_ready and npcs_ready == len(scene["npcs"])

        return {
            "location_id": location_id,
            "location_name": scene["location_name"],
            "background_path": background_path,
            "walkable_bounds": scene["walkable_bounds"],
            "obstacles": scene["obstacles"],
            "objects": scene["objects"],
            "player": scene["player"],
            "npcs": scene["npcs"],
            # What to load right now (`serving_*`, guaranteed on disk) and what
            # to aim for (`body_sig`/`action_sig`) — saves a round-trip on
            # scene load. Same shape as GET /api/assets/appearance/{player_id}.
            "appearance": self._appearance_block(player_id, wanted, spawn=False),
            "pending": pending,
            "scene_ready": scene_ready,
            "readiness": {
                "background": background_ready,
                "player": player_ready,
                "npcs_ready": npcs_ready,
                "npcs_total": len(scene["npcs"]),
            },
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
        # Completeness is memoized on the assumption that frames only ever
        # appear — wiping the tree is the one moment that stops being true.
        self._complete.clear()

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
