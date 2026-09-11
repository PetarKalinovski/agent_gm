/**
 * Game Renderer - PixiJS-based isometric RPG renderer with camera system
 *
 * Features:
 * - Camera follows player (scene scrolls)
 * - Background zoom/crop (no stretching)
 * - WASD movement with collision detection
 * - NPC sprites with click interactions
 */

class GameRenderer {
    constructor(containerId) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);

        // Create PixiJS application
        this.app = new PIXI.Application();

        // State
        this.playerId = null;
        this.locationId = null;
        this.player = null;
        this.npcs = {};
        this.walkableBounds = null;
        this.isLoading = false;
        this.sceneReady = true;     // false = buffering overlay holds a half-built scene
        this._loadingSince = 0;     // when the current buffering wait began
        this._lastReadiness = null; // server's last readiness report (overlay progress)

        // Camera/World settings
        this.worldWidth = 1600;  // Virtual world size (larger than screen)
        this.worldHeight = 900;
        this.camera = { x: 0, y: 0 };
        this.cameraSmoothing = 0.1; // Smooth camera follow (0-1, lower = smoother)

        // Input state
        this.keys = {};

        // Movement settings
        this.moveSpeed = 18; // Normalized units per second (frame-rate independent)
        this.syncInterval = 200; // ms between server syncs
        this.lastSync = 0;
        this.positionDirty = false;

        // World-editor mode (gates NPC drag/scale editing; toggle with Ctrl+E)
        this.editorMode = false;

        // Exit/transition detection
        this.exits = [];               // Available destinations for current location
        this.edgePushTimer = 0;        // ms spent pushing against a walkable bound
        this.edgePushThreshold = 350;  // push this long against an edge to trigger exit prompt
        this.exitPromptCooldown = 0;   // don't re-prompt immediately after dismissal

        // Animation settings
        this.walkFrameCount = 12;  // 6 generated keys + 6 RIFE in-betweens; legacy sets have 6 or 2
        this.animationFrame = 0;   // free-running counter, wrapped per-cycle at lookup
        this.animationTimer = 0;
        this.isMoving = false;

        // Game-feel (juice) state
        this.particles = [];               // live dust puffs
        this.walkPhase = 0;                // drives bob/lean oscillation
        this.bobOffset = 0;                // current vertical bob (px, <= 0)
        this.leanAngle = 0;                // current sprite lean (radians)
        this.lookAhead = { x: 0, y: 0 };   // camera leads the movement direction

        // Collision obstacles (polygons in normalized 0-100 coords)
        this.obstacles = [];
        this.collisionMode = false;        // collision editor (press C inside Ctrl+E)
        this.drawingPolygon = null;        // in-progress vertex list

        // Day/night scene tint
        this.sceneTint = { r: 1, g: 1, b: 1 };
        this.targetTint = { r: 1, g: 1, b: 1 };

        // Canvas feedback: floating text (damage/gold/items) + NPC speech bubbles
        this.floaters = [];
        this.bubbles = {};   // npcId -> {c, text, until, audioId}

        // Combat gray-box prototype (Ctrl+K spawns a test enemy)
        this.combat = null;
        this._jEdge = false;           // J keydown edge (light attack + charge start)

        // Canvas-first exploration: doors, scene objects, ambient barks
        this.doors = [];               // [{exitId,name,locked,x,y,slot,g,label}]
        this.doorDwell = 0;            // ms spent walking into an unlocked door zone
        this.doorArmed = null;         // exitId currently being dwelled on
        this.doorCooldown = 0;         // ms before another transition may fire
        this.objects = [];             // [{id,kind,label,x,y,consumed,locked,g,phase}]
        this.nearObject = null;        // the object E would act on
        this.nearDoor = null;          // the locked door E would try
        this.barks = [];               // [{npc_id,text}]
        this.barkTimer = 0;
        this.barkRecent = [];          // last 4 texts, to avoid repeats

        // Cinematics (see docs/design/cinematics.md)
        this.cine = null;                        // active moment: {kind, t, dur, …}
        this.cineTint = { r: 1, g: 1, b: 1 };    // multiplies the day/night tint
        this.cineTintTarget = { r: 1, g: 1, b: 1 };
        this.cineShake = 0;
        this.cineZoom = 1;
        this.cineFocus = null;                   // {x, y} in world pixels
        this.timeScale = 1;                      // kill_cam slow-mo
        this._pendingCombat = null;              // roster queued behind a boss_intro
        this._queuedForReady = null;             // events that arrived mid-load
        this._queuedFor = null;                  // ...and which scene they belong to
        this._visionEndTimer = null;

        // Weather: screen-space particle field + a tint that MULTIPLIES the
        // day/night tint (never replaces it)
        this.weather = 'clear';
        this._weatherParts = [];
        this.weatherTint = { r: 1, g: 1, b: 1 };
        this.weatherTintTarget = { r: 1, g: 1, b: 1 };
        this._weatherFadeTarget = 0;

        // Visible equipment: the engine names the sprite set the player's
        // gear entitles them to (state.appearance signatures); the renderer
        // swaps to it only when its whole set is on disk — never a naked
        // frame, never a half-dressed one. Signatures ride every player
        // sprite URL as `?a=` (strict server-side, a real cache-buster).
        this.appearance = {
            wantedBody: 'base', wantedAction: 'base-unarmed',
            loadedBody: null, loadedAction: null,
            poll: null, pollTries: 0,
        };
        this._pendingSwap = null;   // swap deferred while an action animates

        // Initialize
        this.init();
    }

    async init() {
        // Initialize PixiJS
        await this.app.init({
            resizeTo: this.container,
            backgroundColor: 0x0a0a0f,
            antialias: true,
        });

        this.container.appendChild(this.app.canvas);

        this.app.canvas.addEventListener('contextmenu', (e) => e.preventDefault());
        this.app.canvas.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });

        // Track the currently selected NPC for editing
        this.editingNpc = null;

        // Add a global move listener for the "follow mouse" behavior
        this.app.stage.eventMode = 'static';
        this.app.stage.on('pointermove', (e) => this.onGlobalMouseMove(e));

        // Clear NPC selection when clicking on empty space
        this.app.stage.on('pointerdown', (e) => {
            // Clicking the scene releases the chat input so WASD and E work
            if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
            // Collision editor: left-click adds a polygon vertex
            if (this.collisionMode && e.button === 0) {
                this._addPolygonVertex(e);
                return;
            }
            // Only fire if clicking directly on stage (not on an NPC)
            if (e.target === this.app.stage) {
                window.dispatchEvent(new CustomEvent('npc-deselect'));
            }
        });

        // Collision editor: right-click deletes the obstacle under the cursor
        this.app.stage.on('rightclick', (e) => {
            if (this.collisionMode) this._deleteObstacleAt(e);
        });

        // Create world container (this moves with camera)
        this.worldContainer = new PIXI.Container();
        this.app.stage.addChild(this.worldContainer);

        // Create render layers inside world container.
        // Player and NPCs share one z-sorted layer so depth (y position)
        // decides who renders in front.
        this.backgroundLayer = new PIXI.Container();
        // Shadows and dust render above the background but below every entity
        this.shadowLayer = new PIXI.Container();
        this.particleLayer = new PIXI.Container();
        this.entityLayer = new PIXI.Container();
        this.entityLayer.sortableChildren = true;
        this.npcLayer = this.entityLayer;
        this.playerLayer = this.entityLayer;

        this.worldContainer.addChild(this.backgroundLayer);
        this.worldContainer.addChild(this.shadowLayer);
        this.worldContainer.addChild(this.particleLayer);
        this.worldContainer.addChild(this.entityLayer);

        // Doors are scenery: they render just above the background but
        // behind every shadow and entity
        this.doorLayer = new PIXI.Container();
        this.worldContainer.addChildAt(this.doorLayer, 1);

        // Speech bubbles float above all entities but below editor overlays
        this.bubbleLayer = new PIXI.Container();
        this.worldContainer.addChild(this.bubbleLayer);

        // Collision-editor overlay renders above everything in the world
        this.collisionOverlay = new PIXI.Container();
        this.worldContainer.addChild(this.collisionOverlay);

        // Speech bubbles hold while their voice line is playing
        this._onVoiceStart = (e) => {
            const b = this._bubbleByAudio(e.detail?.audioId);
            if (b) b.until = Infinity;
        };
        this._onVoiceEnd = (e) => {
            const b = this._bubbleByAudio(e.detail?.audioId);
            if (b) b.until = performance.now() + 500;
        };
        window.addEventListener('forge-voice-start', this._onVoiceStart);
        window.addEventListener('forge-voice-end', this._onVoiceEnd);

        // Weather is screen-space: it must not scroll with the camera
        this.weatherLayer = new PIXI.Container();
        this.weatherLayer.eventMode = 'none';
        this.app.stage.addChild(this.weatherLayer);
        // A state event may have set weather before init finished
        if (this.weather && this.weather !== 'clear') this._buildWeather(this.weather);

        // UI layer (doesn't move with camera)
        this.uiLayer = new PIXI.Container();
        this.app.stage.addChild(this.uiLayer);

        // Cinematic overlay DOM (bars/scrim/text are DOM for crisp type;
        // zoom, tint, shake and particles are PixiJS)
        this.cineEl = document.getElementById('cine-overlay');
        this.cineTitleEl = document.getElementById('cine-title');
        this.cineSubEl = document.getElementById('cine-sub');
        if (this.cineEl) {
            this._onCineClick = () => {
                if (this.cine && this.cine.skippable && performance.now() >= this.cine.skipAt) {
                    this._endCinematic(false);
                }
            };
            this.cineEl.addEventListener('pointerdown', this._onCineClick);
        }

        // Setup input handlers
        this.setupInput();

        // Start game loop
        this.app.ticker.add(() => this.update());

        // Handle resize
        this._onResize = () => this.onResize();
        window.addEventListener('resize', this._onResize);

        console.log('GameRenderer initialized with camera system');
    }

    setupInput() {
        this._onKeyDown = (e) => {
            // Esc skips a skippable cinematic even while the chat box has focus
            if (e.key === 'Escape' && this.cine && this.cine.skippable &&
                performance.now() >= this.cine.skipAt) {
                this._endCinematic(false);
                e.preventDefault();
                return;
            }

            if (document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA') {
                return;
            }

            // Any key skips a skippable cinematic (when not typing)
            if (this.cine && this.cine.skippable && performance.now() >= this.cine.skipAt) {
                this._endCinematic(false);
                e.preventDefault();
                return;
            }

            const key = e.key.toLowerCase();

            // Ctrl+E toggles world-editor mode (NPC drag/scale)
            if (e.ctrlKey && key === 'e') {
                this.editorMode = !this.editorMode;
                if (!this.editorMode && this.editingNpc) this.saveAndExitTransform();
                if (!this.editorMode && this.collisionMode) this._toggleCollisionMode();
                window.dispatchEvent(new CustomEvent('editor-mode-changed', {
                    detail: { enabled: this.editorMode }
                }));
                e.preventDefault();
                return;
            }

            // Ctrl+K: spawn a gray-box test enemy (combat prototype)
            if (e.ctrlKey && key === 'k') {
                this._spawnTestEnemy();
                e.preventDefault();
                return;
            }

            // E: interact with the nearest object (or try a locked door)
            if (!e.ctrlKey && !this.editorMode && key === 'e') {
                this._tryInteract();
                e.preventDefault();
                return;
            }

            // Collision editor controls (inside editor mode)
            if (this.editorMode && !e.ctrlKey) {
                if (key === 'c') { this._toggleCollisionMode(); e.preventDefault(); return; }
                if (this.collisionMode) {
                    if (key === 'enter') { this._closePolygon(); e.preventDefault(); return; }
                    if (key === 'escape') { this._cancelPolygon(); e.preventDefault(); return; }
                    if (key === 'g') { this._autoDetectObstacles(); e.preventDefault(); return; }
                }
            }

            if (['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(key)) {
                this.keys[key] = true;
                e.preventDefault();
            }

            // Combat verbs: space = dodge, j = attack (tap light / hold heavy),
            // k = parry, q = quick-use consumable
            if (e.key === ' ' || key === 'j') {
                const kk = e.key === ' ' ? 'space' : 'j';
                if (kk === 'j' && !e.repeat && !this.keys['j']) this._jEdge = true;
                this.keys[kk] = true;
                if (this.combat) e.preventDefault();
            }
            if (key === 'k' && !e.ctrlKey && !e.repeat) {
                this.keys['k'] = true;
                this._tryParry();
                if (this.combat) e.preventDefault();
            }
            if (key === 'q' && !e.ctrlKey && !e.repeat) {
                this._useQuickSlot();
                if (this.combat) e.preventDefault();
            }
        };

        this._onKeyUp = (e) => {
            const key = e.key.toLowerCase();
            // Releasing J fires (or aborts) the charged heavy
            if (key === 'j' && this.keys['j'] && this.combat) this._releaseCharge();
            this.keys[e.key === ' ' ? 'space' : key] = false;
        };

        window.addEventListener('keydown', this._onKeyDown);
        window.addEventListener('keyup', this._onKeyUp);
    }

    destroy() {
        // Full teardown so a new GameRenderer can be created (world switch)
        // without leaking listeners, tickers, or a second canvas
        window.removeEventListener('keydown', this._onKeyDown);
        window.removeEventListener('keyup', this._onKeyUp);
        window.removeEventListener('resize', this._onResize);
        window.removeEventListener('forge-voice-start', this._onVoiceStart);
        window.removeEventListener('forge-voice-end', this._onVoiceEnd);
        this._stopAppearancePoll();
        this._pendingSwap = null;
        this._pendingCombat = null;
        if (this._visionEndTimer) { clearTimeout(this._visionEndTimer); this._visionEndTimer = null; }
        if (this.cine) this._endCinematic(true);
        if (this.cineEl && this._onCineClick) {
            this.cineEl.removeEventListener('pointerdown', this._onCineClick);
        }
        this._weatherParts = [];
        try {
            this.app.destroy(true, { children: true, texture: false });
        } catch (e) {
            console.warn('GameRenderer destroy:', e);
        }
        this.npcs = {};
        this.player = null;
    }

    async loadLocation(locationId, playerId) {
        if (this.isLoading) return;
        this.isLoading = true;

        this.playerId = playerId;
        this.locationId = locationId;

        // 1. CLEAR PREVIOUS SCENE IMMEDIATELY
        if (this.combat) this._endCombat(true);
        this._pendingCombat = null;      // never spawn a stale roster in a new scene
        // Events that arrive WHILE this scene loads (a boss entrance, the
        // combat roster the DM sent right after moving the player) wait in
        // _queuedForReady and fire when the scene is playable. Seen live:
        // start_combat landed during the walk into Wolf's Hollow and the
        // fight was silently dropped. A different destination discards it.
        if (this._queuedFor && this._queuedFor !== locationId) this._queuedForReady = null;
        this._queuedFor = locationId;
        if (this.cine) this._endCinematic(true);
        this.backgroundLayer.removeChildren();
        this.npcLayer.removeChildren();
        this.shadowLayer.removeChildren();
        this.particleLayer.removeChildren();
        this.collisionOverlay.removeChildren();
        this.bubbleLayer.removeChildren();
        this.doorLayer.removeChildren();
        this.particles = [];
        this.floaters = [];
        this.bubbles = {};
        // Canvas-play state (weather is world state — it survives the scene change)
        for (const o of this.objects) {
            if (o.g && !o.g.destroyed) o.g.destroy({ children: true });
        }
        this.doors = [];
        this.objects = [];
        this.barks = [];
        this.nearObject = null;
        this.nearDoor = null;
        this.doorArmed = null;
        this.doorDwell = 0;
        this.doorCooldown = GameRenderer.DOOR_COOLDOWN_MS;  // no instant bounce-back
        this._stopAppearancePoll();     // the scene fetch restates the wanted look
        this._pendingSwap = null;
        // Keep the player, but maybe hide them or move them to center
        if (this.player) {
            this.player.normalizedX = 50;
            this.player.normalizedY = 50;
            this.updatePlayerPosition();
        }

        this.showLoading(true, this._lastReadiness);

        try {
            const response = await fetch(`/api/assets/location/${locationId}?player_id=${playerId}`);
            const data = await response.json();

            if (data.error) {
                console.error('Error loading location:', data.error);
                this.showError(data.error);
                return;
            }

            // Load background with zoom/crop
            await this.loadBackground(data.background_url);

            // Store walkable bounds, obstacle polygons, and available exits
            this.walkableBounds = data.walkable_bounds || { x_min: 5, x_max: 95, y_min: 5, y_max: 95 };
            this.obstacles = data.obstacles || [];
            this.exits = data.exits || [];
            this._redrawCollisionOverlay();

            // Canvas-first play: door markers, scene objects, ambient barks
            this._buildDoors(this.exits);
            this._buildObjects(data.objects || []);
            this._loadBarks(locationId);   // fire and forget

            // Visible equipment: what to aim for (wanted signatures) and
            // what is guaranteed on disk RIGHT NOW (serving signatures)
            const ap = data.appearance || {};
            this.appearance.wantedBody = ap.body_sig || 'base';
            this.appearance.wantedAction = ap.action_sig || 'base-unarmed';
            const servingBody = ap.serving_body || 'base';
            const servingAction = ap.serving_action || 'base-unarmed';

            // Load player sprite (the serving set — never a placeholder the
            // initial load could have avoided)
            await this.loadPlayer(data.player, servingBody, servingAction);

            // Load NPC sprites
            await this.loadNPCs(data.npcs);

            // Center camera on player initially
            this.centerCameraOnPlayer();

            // Mid-generation reload: watch for the wanted set to finish
            if (this.appearance.loadedBody !== this.appearance.wantedBody ||
                this.appearance.loadedAction !== this.appearance.wantedAction) {
                this.appearance.pollTries = 0;
                this._startAppearancePoll();
            }

            console.log(`Loaded location: ${data.location_name}`);

            // Scene readiness: the world is revealed only when it can be
            // PLAYED, not merely glimpsed — background painted, the player's
            // full sprite set on disk, a body for every character present.
            // Until then the buffering overlay holds and input stays gated.
            this.sceneReady = data.scene_ready !== false;
            this._lastReadiness = data.readiness || null;
            if (this.sceneReady) {
                this._loadingSince = 0;
            } else if (!this._loadingSince) {
                this._loadingSince = performance.now();
            }

            // Assets still generating server-side? Poll until the scene is
            // complete. A not-ready scene gets a longer leash — a brand-new
            // character paints idles plus full walk cycles before the reveal.
            const cap = this.sceneReady ? 20 : 50;
            if (data.pending && (this.pendingRetries || 0) < cap) {
                this.pendingRetries = (this.pendingRetries || 0) + 1;
                console.log(`Assets pending, re-checking in 6s (attempt ${this.pendingRetries})`);
                setTimeout(() => {
                    // Only reload if the player is still in this location
                    if (this.locationId === locationId && !this.isLoading) {
                        this.loadLocation(locationId, playerId);
                    }
                }, 6000);
            } else if (!data.pending) {
                this.pendingRetries = 0;
            } else if (!this.sceneReady) {
                // Generation stalled outright — never trap the player behind
                // the overlay; reveal with placeholders like the old behavior
                console.warn('Scene never finished generating — revealing anyway');
                this.sceneReady = true;
                this._loadingSince = 0;
            }

        } catch (error) {
            console.error('Failed to load location:', error);
            this.sceneReady = true;   // an error screen beats a stuck overlay
            this._loadingSince = 0;
            this.showError(error.message);
        } finally {
            this.isLoading = false;
            // The overlay outlives the fetch while the scene is unplayable —
            // that persistence IS the buffering the reveal waits behind
            this.showLoading(!this.sceneReady, this._lastReadiness);
            this._flushSceneQueue();
        }
    }

    /** Play whatever arrived while the scene was loading, now that it can
     *  be played: the cinematic first (a boss entrance parks the roster
     *  behind itself), then the fight. */
    _flushSceneQueue() {
        if (this.isLoading || !this.sceneReady || !this.player) return;
        const queued = this._queuedForReady;
        if (!queued) return;
        this._queuedForReady = null;
        if (queued.cinematic) this.playCinematic(queued.cinematic);
        if (queued.combat) this.startCombat(queued.combat.enemies, queued.combat.boss);
    }

    _queueForReady(key, value) {
        this._queuedForReady = this._queuedForReady || {};
        this._queuedForReady[key] = value;
        this._queuedFor = this.locationId;
        console.log(`Scene still loading — ${key} queued until it is playable`);
    }

    async loadBackground(url) {
        this.backgroundLayer.removeChildren();

        if (!url) {
            // Background still generating — dark placeholder, poll will replace it
            const placeholder = new PIXI.Graphics();
            placeholder.rect(0, 0, this.worldWidth, this.worldHeight);
            placeholder.fill(0x1a1a25);
            this.backgroundLayer.addChild(placeholder);
            return;
        }

        try {
            const texture = await PIXI.Assets.load(url);
            const bg = new PIXI.Sprite(texture);

            // Calculate zoom to cover screen while maintaining aspect ratio
            const textureAspect = texture.width / texture.height;
            const screenAspect = this.app.screen.width / this.app.screen.height;

            // Make background larger than screen for scrolling
            const scaleFactor = 1.5; // World is 1.5x screen size for scrolling room

            if (textureAspect > screenAspect) {
                // Texture is wider - fit height, crop width
                this.worldHeight = this.app.screen.height * scaleFactor;
                this.worldWidth = this.worldHeight * textureAspect;
            } else {
                // Texture is taller - fit width, crop height
                this.worldWidth = this.app.screen.width * scaleFactor;
                this.worldHeight = this.worldWidth / textureAspect;
            }

            bg.width = this.worldWidth;
            bg.height = this.worldHeight;

            this.backgroundLayer.addChild(bg);
            this.background = bg;

            console.log(`Background loaded: ${this.worldWidth}x${this.worldHeight}, screen: ${this.app.screen.width}x${this.app.screen.height}`);

        } catch (error) {
            console.error('Failed to load background:', error);
            // Create placeholder
            const placeholder = new PIXI.Graphics();
            placeholder.rect(0, 0, this.worldWidth, this.worldHeight);
            placeholder.fill(0x1a1a25);
            this.backgroundLayer.addChild(placeholder);
        }
    }

    /** Soft elliptical ground shadow — grounds sprites in the painted scene. */
    _makeShadow() {
        const g = new PIXI.Graphics();
        g.ellipse(0, 0, 30, 9);
        g.fill({ color: 0x000000, alpha: 0.28 });
        return g;
    }

    /** Size and place a shadow under a sprite (call after scale/texture changes). */
    _fitShadow(shadow, sprite, groundY = null) {
        if (!shadow || !sprite) return;
        const w = sprite.width;
        if (w > 2) {
            shadow.width = w * 0.58;
            shadow.height = w * 0.17;
        }
        shadow.x = sprite.x;
        shadow.y = (groundY !== null ? groundY : sprite.y) + 2;
    }

    async loadPlayer(playerData, bodySig = 'base', actionSig = 'base-unarmed') {
        if (!this.playerLayer) return;
        if (this.player && this.player.sprite) {
            this.entityLayer.removeChild(this.player.sprite);
        }
        if (this.player && this.player.shadow) {
            this.shadowLayer.removeChild(this.player.shadow);
        }

        console.log("Setting up player sprite structure...");
        this.player = {
            sprite: new PIXI.Sprite(PIXI.Texture.WHITE),
            id: playerData.id,
            name: playerData.name,
            direction: playerData.direction || 'front',
            normalizedX: playerData.x,
            normalizedY: playerData.y,
            scale: playerData.scale || 1.0,
            status: playerData.status || 'healthy',
            sprites: {},
            walkSprites: {}
        };

        // Check if player is dead
        this.playerDead = (this.player.status === 'dead');

        const s = this.player.sprite;
        s.anchor.set(0.5, 1);
        s.tint = 0x6366f1; // Purple (loading indicator)
        this.playerLayer.addChild(s);

        this.player.shadow = this._makeShadow();
        this.shadowLayer.addChild(this.player.shadow);

        // Initial positioning
        this.updatePlayerPosition();

        // Load textures one by one (safer than bundle mapping)
        await this.preloadAllSprites(bodySig, actionSig);

        // Remove tint and force initial frame
        s.tint = 0xffffff;
        this.applyAnimationFrame();

        // Apply dead visual state if player is dead
        if (this.playerDead) {
            s.rotation = Math.PI / 2;
            s.alpha = 0.5;
            s.tint = 0x808080;
        }

        console.log("Player initialization complete. Status:", this.player.status);
    }


     async loadNPCs(npcsData) {
        console.log(`loadNPCs called with ${npcsData?.length || 0} NPCs`, npcsData);
        // Remove only NPC sprites (the player shares this layer)
        for (const id in this.npcs) {
            this.entityLayer.removeChild(this.npcs[id].sprite);
        }
        this.npcs = {};

        for (const npcData of npcsData) {
            try {
                const apiUrl = `/api/assets/sprite/npc/${npcData.id}/front.png`;
                const texture = await PIXI.Assets.load(apiUrl);
                const sprite = new PIXI.Sprite(texture);

                // Setup Initial Scale & Pos
                const targetHeight = this.worldHeight * 0.12;
                const baseScale = targetHeight / texture.height;
                const npcScale = npcData.scale || 1.0;
                console.log(`Loading NPC ${npcData.name}: received scale=${npcData.scale}, using npcScale=${npcScale}, baseScale=${baseScale}, final=${baseScale * npcScale}`);
                sprite.scale.set(baseScale * npcScale);
                sprite.anchor.set(0.5, 1);
                sprite.x = this.normalizedToWorldX(npcData.x);
                sprite.y = this.normalizedToWorldY(npcData.y);
                sprite.zIndex = sprite.y;

                // Store status with NPC data
                const npcStatus = npcData.status || 'alive';

                // Apply dead NPC visual state
                if (npcStatus === 'dead') {
                    sprite.rotation = Math.PI / 2;  // 90 degrees sideways
                    sprite.alpha = 0.5;
                    sprite.tint = 0x808080;  // Gray tint
                    sprite.eventMode = 'none';  // Not interactive
                    sprite.cursor = 'default';
                } else {
                    sprite.eventMode = 'static';
                    sprite.cursor = 'pointer';

                    // --- INTERACTION LOGIC (only for alive NPCs) ---
                    sprite.on('pointerdown', (e) => {
                        if (this.collisionMode) return; // collision editing owns clicks
                        // Left Click (0): Talk
                        if (e.button === 0) {
                            if (this.editingNpc) {
                                this.saveAndExitTransform();
                            } else {
                                this.onNpcClick(npcData);
                            }
                        }
                    });

                    sprite.on('rightclick', (e) => {
                        // NPC drag/scale editing is a world-builder tool —
                        // only active in editor mode (Ctrl+E)
                        if (!this.editorMode) return;
                        e.stopPropagation();
                        this.startTransform(sprite, npcData);
                    });

                    sprite.on('pointerover', () => { if(!this.editingNpc) sprite.tint = 0xaaaaff; });
                    sprite.on('pointerout', () => { if(!this.editingNpc) sprite.tint = 0xffffff; });
                }

                this.npcLayer.addChild(sprite);

                const shadow = this._makeShadow();
                if (npcStatus === 'dead') shadow.alpha = 0.3;
                this.shadowLayer.addChild(shadow);
                this._fitShadow(shadow, sprite);

                this.npcs[npcData.id] = { sprite: sprite, data: npcData, status: npcStatus, baseScale: baseScale, shadow: shadow };

            } catch (error) { console.error("NPC Load Error", error); }
        }
    }

    /**
     * Play Minecraft-style death animation for an NPC.
     * Sprite rotates 90°, falls, fades to gray.
     */
    async playDeathAnimation(npcId) {
        const npc = this.npcs[npcId];
        if (!npc) return;

        const sprite = npc.sprite;
        const duration = 1000; // 1 second
        const startTime = Date.now();
        const originalY = sprite.y;

        // Disable interaction immediately
        sprite.eventMode = 'none';

        const animate = () => {
            const elapsed = Date.now() - startTime;
            const progress = Math.min(elapsed / duration, 1);

            // Rotate from 0 to 90 degrees
            sprite.rotation = (Math.PI / 2) * progress;

            // Fall down slightly
            const fallDistance = sprite.height * 0.3;
            sprite.y = originalY + (fallDistance * progress);

            // Fade to 50% alpha; shadow fades with the body
            sprite.alpha = 1 - (0.5 * progress);
            if (npc.shadow) npc.shadow.alpha = 1 - (0.7 * progress);

            if (progress < 1) {
                requestAnimationFrame(animate);
            } else {
                // Animation complete - apply final dead state
                sprite.tint = 0x808080;
                npc.status = 'dead';
                console.log(`Death animation complete for NPC ${npcId}`);
            }
        };

        animate();
    }

    /**
     * Update NPC status and trigger death animation if needed.
     */
    updateNPCStatus(npcId, newStatus) {
        const npc = this.npcs[npcId];
        if (!npc) return;

        const oldStatus = npc.status;
        if (oldStatus === 'alive' && newStatus === 'dead') {
            this.playDeathAnimation(npcId);
        }
        npc.status = newStatus;
    }

    update() {
        if (!this.player || this.isLoading) return;
        // Buffering: the world is not playable until the scene is ready —
        // no movement, no doors, no wandering under the overlay
        if (!this.sceneReady) return;

        const realDt = this.app.ticker.deltaMS;
        this._updateCinematic(realDt);      // always real time — the show must finish
        this._updateWeather(realDt);
        const deltaTime = realDt * this.timeScale;   // kill_cam slows the world

        this._updateSceneTint(realDt);
        this._updateFloaters(realDt);
        this._updateBubbles();

        // A world-freezing cinematic (boss_intro) suspends simulation and input
        if (this.cine && this.cine.freeze === 'world') {
            this.updateCamera();
            return;
        }

        // Skip movement if player is dead
        if (this.playerDead) {
            this.updateCamera();
            return;
        }

        // Hitstop: freeze the whole scene for a few frames on impact
        if (this.combat && this.combat.hitstop > 0) {
            this.combat.hitstop -= realDt;
            return;
        }
        // Charging a heavy roots the player to the spot they committed from
        const chargeSlow = (this.combat && this.combat.charging) ? 0.45 : 1;
        const step = this.moveSpeed * chargeSlow * (deltaTime / 1000); // frame-rate independent

        if (this.exitPromptCooldown > 0) this.exitPromptCooldown -= realDt;

        const inputLocked = !!(this.cine && this.cine.freeze);
        let dx = 0;
        let dy = 0;
        let newDir = this.player.direction; // Default to current

        // Handle WASD (frozen while an input-locking cinematic plays)
        if (inputLocked) { /* world keeps animating, the player holds still */ }
        else if (this.keys['w'] || this.keys['arrowup']) { dy = -step; newDir = 'back'; }
        else if (this.keys['s'] || this.keys['arrowdown']) { dy = step; newDir = 'front'; }
        else if (this.keys['a'] || this.keys['arrowleft']) { dx = -step; newDir = 'left'; }
        else if (this.keys['d'] || this.keys['arrowright']) { dx = step; newDir = 'right'; }

        const wantsToMove = (dx !== 0 || dy !== 0);
        this.isMoving = false;

        if (wantsToMove) {
            this.player.direction = newDir;

            // Collision against walkable bounds, with axis slide
            const targetX = Math.max(0, Math.min(100, this.player.normalizedX + dx));
            const targetY = Math.max(0, Math.min(100, this.player.normalizedY + dy));

            let moved = false;
            if (this.isWalkable(targetX, targetY)) {
                this.player.normalizedX = targetX;
                this.player.normalizedY = targetY;
                moved = true;
            } else if (dx !== 0 && this.isWalkable(targetX, this.player.normalizedY)) {
                this.player.normalizedX = targetX;
                moved = true;
            } else if (dy !== 0 && this.isWalkable(this.player.normalizedX, targetY)) {
                this.player.normalizedY = targetY;
                moved = true;
            }

            if (moved) {
                this.isMoving = true;
                this.edgePushTimer = 0;
                this.updatePlayerPosition();
                this.positionDirty = true;

                // Advance the walk cycle (length varies: 12 in-betweened
                // frames, 6 legacy keys, or the idle-interleaved 4-step
                // shuffle). Per-frame durations carry the animation spacing.
                const anim = this._getWalkCycle();
                const frameDur = anim.durations
                    ? anim.durations[this.animationFrame % anim.cycle.length]
                    : anim.speed;
                this.animationTimer += deltaTime;
                if (this.animationTimer >= frameDur) {
                    this.animationTimer = 0;
                    this.animationFrame++;
                    // Footstep on contact frames: dust + shadow pulse
                    if (anim.stepEvery > 0 && this.animationFrame % anim.stepEvery === 0) {
                        this._spawnDust();
                    }
                }
                this.syncPlayerPosition();
            } else {
                // Blocked by a walkable bound: pushing against it long enough
                // means the player wants to leave the scene
                this.animationFrame = 0;
                this.edgePushTimer += deltaTime;
                // Exits are locked while combat is active. A door within
                // reach handles this edge — don't also pop the chooser.
                const doorNear = this.doors.some(d =>
                    Math.hypot(d.x - this.player.normalizedX, d.y - this.player.normalizedY) < 12);
                if (!this.combat && !this.cine && !doorNear &&
                    this.edgePushTimer >= this.edgePushThreshold && this.exitPromptCooldown <= 0) {
                    this.edgePushTimer = 0;
                    this.exitPromptCooldown = 2000;
                    window.dispatchEvent(new CustomEvent('location-exit', {
                        detail: { direction: newDir, exits: this.exits }
                    }));
                }
            }
        } else {
            this.animationFrame = 0; // Return to idle frame
            this.animationTimer = 0;
            this.edgePushTimer = 0;
        }

        // Apply visual updates
        this._updateNpcWander(deltaTime);
        this._updateDoors(deltaTime);
        this._updateObjects();
        this._updateBarks(deltaTime);
        if (this.combat) this._updateCombat(deltaTime);
        // A texture swap deferred past an attack/dodge lands the frame after
        if (this._pendingSwap && !this.playerAction) this._applyPendingSwap();
        this._updateJuice(deltaTime);
        this.applyAnimationFrame();
        this.updateCamera();
    }

    /** Ordered walk textures for the current direction, with timing metadata. */
    _getWalkCycle() {
        const dir = this.player.direction;
        const walk = this.player.walkSprites[dir] || {};
        const idle = this.player.sprites[dir];
        const frames = [];
        for (let f = 1; f <= this.walkFrameCount; f++) {
            // 404 fallbacks alias to the idle texture — exclude them so a
            // legacy 2-frame set doesn't masquerade as a 6-frame cycle
            if (walk[f] && walk[f] !== idle) frames.push(walk[f]);
        }
        if (frames.length >= 4) {
            // Full generated cycle: contact frames sit at 0 and mid-cycle.
            // Cycle duration stays ~constant regardless of frame count, so
            // 6-frame legacy sets and 12-frame sets walk at the same cadence.
            // Hand-animation spacing: contacts hold longest, the frames
            // leading into a contact snap through fastest.
            const half = Math.round(frames.length / 2);
            const base = 560 / frames.length;
            const durations = frames.map((_, i) => {
                const phase = i % half;
                if (phase === 0) return Math.round(base * 1.35);          // contact: hold
                if (phase === half - 1) return Math.round(base * 0.8);    // into contact: snap
                return Math.round(base * 0.95);
            });
            return { cycle: frames, durations, speed: Math.round(base), stepEvery: half };
        }
        return { cycle: [idle, walk[1] || idle, idle, walk[2] || idle], speed: 150, stepEvery: 2 };
    }

    /** Per-tick game-feel: walk bob/lean easing, shadow fit, dust particles. */
    _updateJuice(deltaTime) {
        const s = this.player.sprite;

        // Bob + lean while moving; ease both back to rest when idle.
        // The generated frames carry most of the bob — this layer adds a
        // subtle continuous motion that hides frame quantization.
        if (this.isMoving && !this.playerDead) {
            this.walkPhase += deltaTime * 0.014;
            const amp = s.height * 0.012;
            this.bobOffset = -Math.abs(Math.sin(this.walkPhase)) * amp;
            const leanTarget =
                this.player.direction === 'left' ? -0.035 :
                this.player.direction === 'right' ? 0.035 :
                Math.sin(this.walkPhase) * 0.02; // front/back: gentle sway
            this.leanAngle += (leanTarget - this.leanAngle) * Math.min(1, deltaTime / 120);
        } else {
            this.bobOffset += (0 - this.bobOffset) * Math.min(1, deltaTime / 90);
            this.leanAngle += (0 - this.leanAngle) * Math.min(1, deltaTime / 90);
        }
        if (!this.playerDead) {
            s.rotation = this.leanAngle;
            // Bob is visual-only: it rides on top of the ground position
            if (this._playerGroundY !== undefined) s.y = this._playerGroundY + this.bobOffset;
        }

        // Keep the player's shadow glued to the ground line (bob excluded)
        this._fitShadow(this.player.shadow, s, this._playerGroundY ?? s.y);

        // Simulate dust puffs
        for (let i = this.particles.length - 1; i >= 0; i--) {
            const p = this.particles[i];
            p.life += deltaTime;
            const t = p.life / p.maxLife;
            if (t >= 1) {
                this.particleLayer.removeChild(p.g);
                p.g.destroy();
                this.particles.splice(i, 1);
                continue;
            }
            p.g.x += p.vx * deltaTime;
            p.g.y += p.vy * deltaTime;
            p.g.alpha = p.startAlpha * (1 - t);
            const sc = 1 + t * 1.1;
            p.g.scale.set(sc);
        }
    }

    /** Kick up a few dust puffs at the player's feet (footstep feedback). */
    _spawnDust() {
        if (!this.player || !this.player.sprite || this.playerDead) return;
        const s = this.player.sprite;
        const groundY = this._playerGroundY ?? s.y;
        const count = 2 + (Math.random() < 0.5 ? 1 : 0);
        for (let i = 0; i < count; i++) {
            const g = new PIXI.Graphics();
            const r = 2 + Math.random() * 2.5;
            g.circle(0, 0, r);
            g.fill({ color: 0xcfc4b0, alpha: 1 });
            g.x = s.x + (Math.random() - 0.5) * s.width * 0.35;
            g.y = groundY - Math.random() * 3;
            const startAlpha = 0.28 + Math.random() * 0.12;
            g.alpha = startAlpha;
            this.particleLayer.addChild(g);
            this.particles.push({
                g,
                vx: (Math.random() - 0.5) * 0.02 - (this.player.direction === 'left' ? -0.01 : this.player.direction === 'right' ? 0.01 : 0),
                vy: -0.008 - Math.random() * 0.01,
                life: 0,
                maxLife: 350 + Math.random() * 200,
                startAlpha,
            });
        }
    }

    updateCamera() {
        if (!this.player) return;

        const dt = this.app.ticker.deltaMS;

        // Look-ahead: the camera leads the movement direction so the player
        // sees more of where they're going (eases in and out)
        const leadDist = Math.min(this.app.screen.width, this.app.screen.height) * 0.07;
        let leadX = 0, leadY = 0;
        if (this.isMoving && !this.playerDead) {
            if (this.player.direction === 'left') leadX = -leadDist;
            else if (this.player.direction === 'right') leadX = leadDist;
            else if (this.player.direction === 'back') leadY = -leadDist;
            else if (this.player.direction === 'front') leadY = leadDist;
        }
        const leadEase = 1 - Math.pow(0.9975, dt);
        this.lookAhead.x += (leadX - this.lookAhead.x) * leadEase;
        this.lookAhead.y += (leadY - this.lookAhead.y) * leadEase;

        // Target camera position (player centered, plus look-ahead)
        const targetX = this.player.sprite.x + this.lookAhead.x - this.app.screen.width / 2;
        const targetY = this.player.sprite.y + this.lookAhead.y - this.app.screen.height / 2;

        // Clamp camera to world bounds
        const maxX = this.worldWidth - this.app.screen.width;
        const maxY = this.worldHeight - this.app.screen.height;

        const clampedX = Math.max(0, Math.min(maxX, targetX));
        const clampedY = Math.max(0, Math.min(maxY, targetY));

        // Frame-rate-independent smoothing (equivalent feel at any refresh rate)
        const t = 1 - Math.pow(1 - this.cameraSmoothing, dt / 16.67);
        this.camera.x += (clampedX - this.camera.x) * t;
        this.camera.y += (clampedY - this.camera.y) * t;

        // Apply camera offset to world container
        this.worldContainer.x = -this.camera.x;
        this.worldContainer.y = -this.camera.y;

        // Cinematic zoom: keep the focus point pinned while the world scales
        const z = this.cineZoom || 1;
        if (z !== 1) {
            const f = this.cineFocus || { x: this.player.sprite.x, y: this.player.sprite.y };
            this.worldContainer.scale.set(z);
            this.worldContainer.x = this.app.screen.width * 0.5 - f.x * z;
            this.worldContainer.y = this.app.screen.height * 0.55 - f.y * z;
        } else if (this.worldContainer.scale.x !== 1) {
            this.worldContainer.scale.set(1);
        }

        // Screenshake: cinematic and combat shakes compose (max wins)
        const shake = Math.max(this.cineShake || 0, (this.combat && this.combat.shake) || 0);
        if (shake > 0) {
            this.worldContainer.x += (Math.random() - 0.5) * shake * 2;
            this.worldContainer.y += (Math.random() - 0.5) * shake * 2;
        }
    }

    centerCameraOnPlayer() {
        if (!this.player) return;

        // Instantly center camera on player
        const targetX = this.player.sprite.x - this.app.screen.width / 2;
        const targetY = this.player.sprite.y - this.app.screen.height / 2;

        const maxX = this.worldWidth - this.app.screen.width;
        const maxY = this.worldHeight - this.app.screen.height;

        this.camera.x = Math.max(0, Math.min(maxX, targetX));
        this.camera.y = Math.max(0, Math.min(maxY, targetY));

        this.worldContainer.x = -this.camera.x;
        this.worldContainer.y = -this.camera.y;
    }

    isWalkable(x, y) {
        if (this.walkableBounds) {
            if (x < this.walkableBounds.x_min || x > this.walkableBounds.x_max ||
                y < this.walkableBounds.y_min || y > this.walkableBounds.y_max) {
                return false;
            }
        }
        for (const poly of (this.obstacles || [])) {
            if (this._pointInPolygon(x, y, poly)) return false;
        }
        return true;
    }

    /** Ray-casting point-in-polygon test (normalized 0-100 coords). */
    _pointInPolygon(x, y, poly) {
        if (!poly || poly.length < 3) return false;
        let inside = false;
        for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
            const xi = poly[i][0], yi = poly[i][1];
            const xj = poly[j][0], yj = poly[j][1];
            if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
                inside = !inside;
            }
        }
        return inside;
    }

    // ================= Collision editor (C inside Ctrl+E) =================
    // Click = add vertex · Enter = close polygon & save · Escape = cancel
    // Right-click a polygon = delete · G = auto-detect from background

    _toggleCollisionMode() {
        this.collisionMode = !this.collisionMode;
        this.drawingPolygon = null;
        this._redrawCollisionOverlay();
        window.dispatchEvent(new CustomEvent('collision-mode-changed', {
            detail: { enabled: this.collisionMode }
        }));
    }

    _eventToNormalized(e) {
        const pos = e.getLocalPosition(this.worldContainer);
        return [this.worldToNormalizedX(pos.x), this.worldToNormalizedY(pos.y)];
    }

    _addPolygonVertex(e) {
        const [nx, ny] = this._eventToNormalized(e);
        if (!this.drawingPolygon) this.drawingPolygon = [];
        this.drawingPolygon.push([Math.round(nx * 100) / 100, Math.round(ny * 100) / 100]);
        this._redrawCollisionOverlay();
    }

    _closePolygon() {
        if (this.drawingPolygon && this.drawingPolygon.length >= 3) {
            this.obstacles.push(this.drawingPolygon);
            this._saveObstacles();
        }
        this.drawingPolygon = null;
        this._redrawCollisionOverlay();
    }

    _cancelPolygon() {
        this.drawingPolygon = null;
        this._redrawCollisionOverlay();
    }

    _deleteObstacleAt(e) {
        const [nx, ny] = this._eventToNormalized(e);
        for (let i = this.obstacles.length - 1; i >= 0; i--) {
            if (this._pointInPolygon(nx, ny, this.obstacles[i])) {
                this.obstacles.splice(i, 1);
                this._saveObstacles();
                this._redrawCollisionOverlay();
                return;
            }
        }
    }

    async _saveObstacles() {
        if (!this.locationId) return;
        try {
            await fetch(`/api/world/locations/${this.locationId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ obstacles: this.obstacles }),
            });
            console.log(`Saved ${this.obstacles.length} obstacle polygons`);
        } catch (err) {
            console.error('Failed to save obstacles:', err);
        }
    }

    async _autoDetectObstacles() {
        if (!this.locationId) return;
        console.log('Auto-detecting obstacles...');
        try {
            const res = await fetch(`/api/world/locations/${this.locationId}/detect-obstacles`, { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                this.obstacles = data.obstacles || [];
                this._redrawCollisionOverlay();
                console.log(`Auto-detected ${this.obstacles.length} obstacles`);
            } else {
                console.error('Obstacle detection failed:', data.error);
            }
        } catch (err) {
            console.error('Obstacle detection failed:', err);
        }
    }

    _redrawCollisionOverlay() {
        if (!this.collisionOverlay) return;
        this.collisionOverlay.removeChildren();
        if (!this.collisionMode) return;

        const g = new PIXI.Graphics();
        for (const poly of this.obstacles) {
            if (!poly || poly.length < 3) continue;
            g.moveTo(this.normalizedToWorldX(poly[0][0]), this.normalizedToWorldY(poly[0][1]));
            for (let i = 1; i < poly.length; i++) {
                g.lineTo(this.normalizedToWorldX(poly[i][0]), this.normalizedToWorldY(poly[i][1]));
            }
            g.closePath();
            g.fill({ color: 0xef4444, alpha: 0.28 });
            g.stroke({ color: 0xef4444, width: 2, alpha: 0.8 });
        }
        // In-progress polygon: yellow vertices + polyline
        if (this.drawingPolygon && this.drawingPolygon.length > 0) {
            const pts = this.drawingPolygon;
            g.moveTo(this.normalizedToWorldX(pts[0][0]), this.normalizedToWorldY(pts[0][1]));
            for (let i = 1; i < pts.length; i++) {
                g.lineTo(this.normalizedToWorldX(pts[i][0]), this.normalizedToWorldY(pts[i][1]));
            }
            g.stroke({ color: 0xfbbf24, width: 2, alpha: 0.9 });
            for (const [px, py] of pts) {
                g.circle(this.normalizedToWorldX(px), this.normalizedToWorldY(py), 4);
                g.fill({ color: 0xfbbf24 });
            }
        }
        this.collisionOverlay.addChild(g);
    }

    async updatePlayerSprite(direction) {
        if (this.player.sprites[direction]) {
            this.player.sprite.texture = this.player.sprites[direction];
            return;
        }

        try {
            // ?a= pins the frame to the set on screen — a lazy single-
            // direction load can never pull from a different outfit
            const b = `?a=${encodeURIComponent(this.appearance.loadedBody || 'base')}`;
            const url = `/api/assets/sprite/player/${this.playerId}/${direction}${b}`;
            const texture = await PIXI.Assets.load(url);
            this.player.sprites[direction] = texture;
            this.player.sprite.texture = texture;
        } catch (error) {
            console.error(`Failed to load player sprite for direction ${direction}:`, error);
        }
    }

    async preloadWalkSprites(direction) {
        const b = `?a=${encodeURIComponent(this.appearance.loadedBody || 'base')}`;
        // Preload idle sprite for direction if not cached
        if (!this.player.sprites[direction]) {
            try {
                const idleUrl = `/api/assets/sprite/player/${this.playerId}/${direction}${b}`;
                const idleTexture = await PIXI.Assets.load(idleUrl);
                this.player.sprites[direction] = idleTexture;
            } catch (error) {
                console.error(`Failed to preload idle sprite for ${direction}:`, error);
            }
        }

        // Preload walk frames for this direction
        if (!this.player.walkSprites[direction]) {
            this.player.walkSprites[direction] = {};
        }

        for (let frame = 1; frame <= this.walkFrameCount; frame++) {
            if (!this.player.walkSprites[direction][frame]) {
                try {
                    const url = `/api/assets/sprite/player/${this.playerId}/${direction}_walk${frame}${b}`;
                    const texture = await PIXI.Assets.load(url);
                    this.player.walkSprites[direction][frame] = texture;
                    console.log(`Loaded walk sprite: ${direction}_walk${frame}`);
                } catch (error) {
                    // Frame not generated (legacy 2-frame set) — alias to idle,
                    // _getWalkCycle filters these out
                    this.player.walkSprites[direction][frame] = this.player.sprites[direction] || null;
                }
            }
        }
    }

    async preloadAllSprites(bodySig = 'base', actionSig = 'base-unarmed') {
        const directions = ['front', 'back', 'left', 'right'];
        const frames = Array.from({ length: this.walkFrameCount }, (_, i) => i + 1);
        // Add .png to the base if you like, or just in the loop
        const playerUrlBase = `/api/assets/sprite/player/${this.playerId}`;
        // Signature per axis: dodge is BODY (a roll shows the outfit),
        // attack is the ACTION pair (armor + weapon)
        const b = `?a=${encodeURIComponent(bodySig)}`;
        const a = `?a=${encodeURIComponent(actionSig)}`;

        const loadJobs = [];

        // Combat action frames (attack/dodge, right-facing; left mirrors via
        // scale flip). A 404 also TRIGGERS their lazy generation server-side,
        // so characters made before this feature upgrade themselves.
        this.player.actionSprites = { attack: {}, dodge: {} };
        for (const action of ['attack', 'dodge']) {
            for (let f = 1; f <= 6; f++) {
                loadJobs.push(
                    PIXI.Assets.load(`${playerUrlBase}/${action}${f}.png${action === 'attack' ? a : b}`).then(tex => {
                        this.player.actionSprites[action][f] = tex;
                    }).catch(() => {
                        this.player.actionSprites[action][f] = null;
                    })
                );
            }
        }

        directions.forEach(dir => {
            // LOAD IDLE (Append .png) — sprite may still be generating (404):
            // fall back to placeholder, the pending-poll reload picks it up later
            loadJobs.push(
                PIXI.Assets.load(`${playerUrlBase}/${dir}.png${b}`).then(tex => {
                    this.player.sprites[dir] = tex;
                }).catch(() => {
                    this.player.sprites[dir] = null;
                })
            );

            // LOAD WALK (Append .png)
            this.player.walkSprites[dir] = {};
            frames.forEach(f => {
                loadJobs.push(
                    PIXI.Assets.load(`${playerUrlBase}/${dir}_walk${f}.png${b}`).then(tex => {
                        this.player.walkSprites[dir][f] = tex;
                    }).catch(e => {
                        this.player.walkSprites[dir][f] = this.player.sprites[dir];
                    })
                );
            });
        });

        await Promise.all(loadJobs);
        this.appearance.loadedBody = bodySig;
        this.appearance.loadedAction = actionSig;
    }

    // ================= Visible equipment (appearance swaps) =================
    // state.appearance names the WANTED signatures; /api/assets/appearance
    // reports when their art is completely on disk; the swap is atomic per
    // axis and deferred while an attack/dodge animation is mid-flight.

    static APPEARANCE_POLL_MS = 5000;
    static APPEARANCE_POLL_MAX = 48;    // ~4 min — comfortably past a 7-call set

    /** The engine's wanted look (from every state event). Does NOT touch
     *  textures — it only decides whether to start watching for a new set. */
    setAppearance(block) {
        if (!block || !this.player) return;
        const body = block.body_sig || 'base';
        const action = block.action_sig || 'base-unarmed';
        if (body === this.appearance.wantedBody &&
            action === this.appearance.wantedAction) return;
        this.appearance.wantedBody = body;
        this.appearance.wantedAction = action;
        this.appearance.pollTries = 0;
        this._startAppearancePoll();
    }

    _startAppearancePoll() {
        this._stopAppearancePoll();
        if (!this.playerId) return;
        this.appearance.poll = setInterval(
            () => this._pollAppearance(), GameRenderer.APPEARANCE_POLL_MS);
        this._pollAppearance();   // first check now, not in 5 s
    }

    _stopAppearancePoll() {
        if (this.appearance && this.appearance.poll) {
            clearInterval(this.appearance.poll);
            this.appearance.poll = null;
        }
    }

    async _pollAppearance() {
        const ap = this.appearance;
        if (!this.playerId || !this.player) { this._stopAppearancePoll(); return; }
        if (++ap.pollTries > GameRenderer.APPEARANCE_POLL_MAX) {
            this._stopAppearancePoll();
            return;
        }
        let data;
        try {
            const res = await fetch(`/api/assets/appearance/${this.playerId}`);
            if (!res.ok) return;
            data = await res.json();
        } catch (e) { return; }   // transient — the interval retries
        if (!this.appearance.poll) return;   // stopped while the fetch flew

        // The server is the authority on what is wanted (gear may have moved
        // again since the state event that started this poll)
        ap.wantedBody = data.body_sig || 'base';
        ap.wantedAction = data.action_sig || 'base-unarmed';

        if (data.body_ready && ap.loadedBody !== ap.wantedBody) {
            await this._swapBody(ap.wantedBody);
        }
        if (data.action_ready && ap.loadedAction !== ap.wantedAction) {
            await this._swapAction(ap.wantedAction);
        }

        const done = ap.loadedBody === ap.wantedBody &&
                     ap.loadedAction === ap.wantedAction && !this._pendingSwap;
        if (done || (data.failed && !data.generating)) {
            // Done — or generation failed: keep the current look, stop asking
            this._stopAppearancePoll();
        }
    }

    /** Every URL one signature's textures were loaded under. */
    _appearanceUrls(axis, sig) {
        if (!sig || !this.playerId) return [];
        const base = `/api/assets/sprite/player/${this.playerId}`;
        const q = `?a=${encodeURIComponent(sig)}`;
        const urls = [];
        if (axis === 'body') {
            for (const dir of ['front', 'back', 'left', 'right']) {
                urls.push(`${base}/${dir}.png${q}`);
                for (let f = 1; f <= this.walkFrameCount; f++) {
                    urls.push(`${base}/${dir}_walk${f}.png${q}`);
                }
            }
            for (let f = 1; f <= 6; f++) urls.push(`${base}/dodge${f}.png${q}`);
        } else {
            for (let f = 1; f <= 6; f++) urls.push(`${base}/attack${f}.png${q}`);
        }
        return urls;
    }

    /** Unload retired texture URLs — bounded memory across many swaps. */
    _unloadUrls(urls) {
        try {
            const known = (urls || []).filter(u => PIXI.Assets.cache.has(u));
            if (known.length) {
                const out = PIXI.Assets.unload(known);
                if (out && out.catch) out.catch(() => {});
            }
        } catch (e) { /* stale cache entries are a leak, not a crash */ }
    }

    /** Atomic body swap: idles + walks + dodge all resolve before ANY texture
     *  is assigned. One failed frame discards the whole batch — the player
     *  stays dressed as they were and the poll retries. */
    async _swapBody(sig) {
        if (!this.player || !this.playerId || this.appearance.loadedBody === sig) return false;
        const base = `/api/assets/sprite/player/${this.playerId}`;
        const q = `?a=${encodeURIComponent(sig)}`;
        const idles = {}, walks = {}, dodge = {};
        const jobs = [];
        for (const dir of ['front', 'back', 'left', 'right']) {
            walks[dir] = {};
            jobs.push(PIXI.Assets.load(`${base}/${dir}.png${q}`).then(t => { idles[dir] = t; }));
            for (let f = 1; f <= this.walkFrameCount; f++) {
                jobs.push(PIXI.Assets.load(`${base}/${dir}_walk${f}.png${q}`)
                    .then(t => { walks[dir][f] = t; }));
            }
        }
        for (let f = 1; f <= 6; f++) {
            jobs.push(PIXI.Assets.load(`${base}/dodge${f}.png${q}`).then(t => { dodge[f] = t; }));
        }
        try {
            await Promise.all(jobs);
        } catch (e) {
            this._unloadUrls(this._appearanceUrls('body', sig));
            return false;
        }
        const apply = () => {
            if (!this.player) return;
            const old = this.appearance.loadedBody;
            this.player.sprites = idles;
            this.player.walkSprites = walks;
            if (!this.player.actionSprites) this.player.actionSprites = { attack: {}, dodge: {} };
            this.player.actionSprites.dodge = dodge;
            this.appearance.loadedBody = sig;
            this.applyAnimationFrame();
            if (old && old !== sig) this._unloadUrls(this._appearanceUrls('body', old));
            console.log(`Appearance: body set swapped to ${sig}`);
        };
        if (this.playerAction) {   // never mid-swing — defer one action's length
            this._pendingSwap = Object.assign(this._pendingSwap || {}, { body: apply });
            return false;
        }
        apply();
        return true;
    }

    /** Same shape for the 6 attack frames (the armor+weapon pair). */
    async _swapAction(sig) {
        if (!this.player || !this.playerId || this.appearance.loadedAction === sig) return false;
        const base = `/api/assets/sprite/player/${this.playerId}`;
        const q = `?a=${encodeURIComponent(sig)}`;
        const attack = {};
        const jobs = [];
        for (let f = 1; f <= 6; f++) {
            jobs.push(PIXI.Assets.load(`${base}/attack${f}.png${q}`).then(t => { attack[f] = t; }));
        }
        try {
            await Promise.all(jobs);
        } catch (e) {
            this._unloadUrls(this._appearanceUrls('action', sig));
            return false;
        }
        const apply = () => {
            if (!this.player) return;
            const old = this.appearance.loadedAction;
            if (!this.player.actionSprites) this.player.actionSprites = { attack: {}, dodge: {} };
            this.player.actionSprites.attack = attack;
            this.appearance.loadedAction = sig;
            if (old && old !== sig) this._unloadUrls(this._appearanceUrls('action', old));
            console.log(`Appearance: attack set swapped to ${sig}`);
        };
        if (this.playerAction) {
            this._pendingSwap = Object.assign(this._pendingSwap || {}, { action: apply });
            return false;
        }
        apply();
        return true;
    }

    /** The deferred swap lands on the first frame after the action ends. */
    _applyPendingSwap() {
        const pending = this._pendingSwap;
        this._pendingSwap = null;
        if (!pending) return;
        if (pending.body) pending.body();
        if (pending.action) pending.action();
    }

    startTransform(sprite, npcData) {
        // If we are already editing someone, save them first
        if (this.editingNpc) this.saveAndExitTransform();

        console.log("Picking up:", npcData.name);
        // Get baseScale from stored NPC data
        const storedNpc = this.npcs[npcData.id];
        const baseScale = storedNpc ? storedNpc.baseScale : 1.0;
        this.editingNpc = { sprite, data: npcData, baseScale };

        sprite.tint = 0xffaa00; // Orange
        sprite.alpha = 0.7;

        // --- ADD THIS: Global listener to "Drop" the NPC anywhere ---
        const dropHandler = (e) => {
            // Only drop on Left Click (0)
            if (e.button === 0) {
                this.saveAndExitTransform();
                // Remove this temporary global listener
                this.app.stage.off('pointerdown', dropHandler);
            }
        };

        // Use a timeout so the same right-click doesn't immediately trigger a drop
        setTimeout(() => {
            this.app.stage.on('pointerdown', dropHandler);
        }, 100);
    }

    async saveAndExitTransform() {
        if (!this.editingNpc) return;

        const { sprite, data, baseScale } = this.editingNpc;

        // Convert screen pixels back to 0-100 for the DB
        const normX = this.worldToNormalizedX(sprite.x);
        const normY = this.worldToNormalizedY(sprite.y);

        // Extract just the user's scale multiplier (divide out the baseScale)
        const userScale = baseScale > 0 ? sprite.scale.y / baseScale : 1.0;

        console.log(`Sending Save Request for ${data.name}... scale=${userScale.toFixed(2)}`);

        // Immediate visual feedback
        sprite.tint = 0xffffff;
        sprite.alpha = 1.0;

        try {
            const response = await fetch('/api/npc/transform', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    npc_id: data.id,
                    x: normX,
                    y: normY,
                    scale: userScale
                })
            });
            const result = await response.json();
            if (result.success) {
                console.log(`SAVE SUCCESS: ${data.name} is now at ${normX.toFixed(2)}%`);
            } else {
                console.error("Save failed on server:", result.error);
            }
        } catch (err) {
            console.error("Network error during save:", err);
        }

        // New home position: rebase the NPC's ambient wandering on it
        data.x = normX;
        data.y = normY;
        const stored = this.npcs[data.id];
        if (stored) stored.wander = null;

        this.editingNpc = null;
    }

    onGlobalMouseMove(event) {
        // If we are in transform mode, make the sprite follow the mouse
        if (this.editingNpc) {
            const newPos = event.getLocalPosition(this.worldContainer);
            this.editingNpc.sprite.x = newPos.x;
            this.editingNpc.sprite.y = newPos.y;
            const stored = this.npcs[this.editingNpc.data.id];
            if (stored) this._fitShadow(stored.shadow, this.editingNpc.sprite);
        }
    }

    onWheel(e) {
        if (this.editingNpc) {
            e.preventDefault();
            const sprite = this.editingNpc.sprite;
            // deltaY is usually 100 or -100
            const factor = e.deltaY > 0 ? 0.9 : 1.1;
            sprite.scale.x *= factor;
            sprite.scale.y *= factor;
        }
    }


     applyAnimationFrame() {
        if (!this.player || !this.player.sprite) return;

        // Skip animation updates if player is dead (keep dead appearance)
        if (this.playerDead) return;

        const dir = this.player.direction;
        let tex = null;
        let mirror = false;

        // Combat action animation overrides walk/idle for its duration
        const act = this.playerAction;
        if (act) {
            let t = act.t, idx = 0;
            for (let i = 0; i < act.durs.length; i++) {
                if (t < act.durs[i]) { idx = i; break; }
                t -= act.durs[i];
                idx = i;
            }
            tex = act.frames[idx];
            // Frames are drawn right-facing; mirror when facing left
            mirror = dir === 'left';
        } else if (!this.isMoving) {
            tex = this.player.sprites[dir];
        } else {
            const anim = this._getWalkCycle();
            tex = anim.cycle[this.animationFrame % anim.cycle.length];
        }

        // Final safety fallback
        if (!tex) tex = this.player.sprites['front'] || this.player.sprites['back'];

        if (tex) {
            const s = this.player.sprite;
            s.texture = tex;

            // Debugging: If texture is 1x1, it's not a real image
            if (tex.width <= 1) {
                console.warn("Applying an empty/invalid texture!");
                return;
            }

            // Apply scale with player's scale multiplier
            const targetH = this.worldHeight * 0.15; // Slightly larger 15%
            const baseScale = targetH / tex.height;
            const playerScale = this.player.scale || 1.0;
            s.scale.set(baseScale * playerScale);
            if (mirror) s.scale.x = -s.scale.x;

            // Force Alpha/Visible
            s.alpha = 1;
            s.visible = true;
        }
    }


    async syncPlayerPosition() {
        const now = Date.now();
        if (!this.positionDirty || now - this.lastSync < this.syncInterval) return;

        this.lastSync = now;
        this.positionDirty = false;

        try {
            await fetch('/api/player/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    player_id: this.playerId,
                    x: this.player.normalizedX,
                    y: this.player.normalizedY,
                    direction: this.player.direction
                })
            });
        } catch (error) {
            console.error('Failed to sync player position:', error);
        }
    }

    onNpcClick(npcData) {
        console.log('NPC clicked:', npcData.name);

        // Dispatch event - but DON'T auto-start chat, just show portrait
        window.dispatchEvent(new CustomEvent('npc-interact', {
            detail: {
                npcId: npcData.id,
                npcName: npcData.name,
                autoChat: false  // Signal to NOT auto-send message
            }
        }));
    }

    showNpcTooltip(name, sprite) {
        this.hideNpcTooltip();

        const tooltip = new PIXI.Text({
            text: name,
            style: {
                fontFamily: 'Arial',
                fontSize: 14,
                fill: 0xffffff,
                align: 'center',
                dropShadow: true,
                dropShadowDistance: 2,
            }
        });

        tooltip.anchor.set(0.5, 1);
        // Calculate based on the sprite's current scaled height
        const screenX = sprite.x - this.camera.x;
        const screenY = sprite.y - (sprite.texture.height * sprite.scale.y) - this.camera.y - 15;

        tooltip.x = screenX;
        tooltip.y = screenY;
        tooltip.name = 'npc-tooltip';

        this.uiLayer.addChild(tooltip);
    }

    hideNpcTooltip() {
        const tooltip = this.uiLayer.getChildByName('npc-tooltip');
        if (tooltip) {
            this.uiLayer.removeChild(tooltip);
        }
    }

    /** Buffering overlay. A scene under construction is HIDDEN, not shown
     *  half-built: the scrim covers placeholder sprites and the dark
     *  background stand-in, progress lines mirror the server's readiness
     *  report, and after 45s of waiting the overlay becomes a clickable
     *  escape hatch so a stalled generation can never trap the player. */
    showLoading(show, readiness = null) {
        const existing = this.uiLayer.getChildByName('loading');
        if (existing) this.uiLayer.removeChild(existing);
        if (!show) return;

        const w = this.app.screen.width, h = this.app.screen.height;
        const overlay = new PIXI.Container();
        overlay.name = 'loading';

        const scrim = new PIXI.Graphics();
        scrim.rect(0, 0, w, h);
        scrim.fill({ color: 0x0b0e16, alpha: 0.97 });
        overlay.addChild(scrim);

        const title = new PIXI.Text({
            text: 'Bringing the scene to life…',
            style: { fontFamily: 'Georgia, serif', fontSize: 26, fill: 0xe5e7eb },
        });
        title.anchor.set(0.5);
        title.x = w / 2;
        title.y = h / 2 - 74;
        overlay.addChild(title);

        const lines = [];
        if (readiness) {
            const mark = (ok) => (ok ? '✓' : '…');
            lines.push([readiness.background, `${mark(readiness.background)}  Painting the scene`]);
            lines.push([readiness.player, `${mark(readiness.player)}  Your character`]);
            if (readiness.npcs_total > 0) {
                const done = readiness.npcs_ready === readiness.npcs_total;
                lines.push([done, `${mark(done)}  The locals  ${readiness.npcs_ready}/${readiness.npcs_total}`]);
            }
        } else {
            lines.push([false, '…  Reading the scene']);
        }
        lines.forEach(([done, text], i) => {
            const line = new PIXI.Text({
                text,
                style: { fontFamily: 'Arial', fontSize: 17, fill: done ? 0x9ca3af : 0x818cf8 },
            });
            line.anchor.set(0.5);
            line.x = w / 2;
            line.y = h / 2 - 16 + i * 28;
            overlay.addChild(line);
        });

        const waited = this._loadingSince ? performance.now() - this._loadingSince : 0;
        if (waited > 45000) {
            const skip = new PIXI.Text({
                text: 'Taking a while — click to step in early',
                style: { fontFamily: 'Arial', fontSize: 14, fill: 0x9ca3af },
            });
            skip.anchor.set(0.5);
            skip.x = w / 2;
            skip.y = h / 2 + 96;
            overlay.addChild(skip);
            overlay.eventMode = 'static';
            overlay.cursor = 'pointer';
            overlay.on('pointerdown', () => {
                this.sceneReady = true;
                this._loadingSince = 0;
                this.showLoading(false);
                this._flushSceneQueue();
            });
        }

        this.uiLayer.addChild(overlay);
    }

    showError(message) {
        const error = new PIXI.Text({
            text: `Error: ${message}`,
            style: {
                fontFamily: 'Arial',
                fontSize: 14,
                fill: 0xef4444,
            }
        });
        error.anchor.set(0.5);
        error.x = this.app.screen.width / 2;
        error.y = this.app.screen.height / 2;
        error.name = 'error';
        this.uiLayer.addChild(error);
    }

    // ================= Day/night scene tint =================

    /** Called from the UI when the game clock's time of day changes. */
    setTimeOfDay(timeOfDay) {
        const tints = {
            dawn: 0xffe6d0, morning: 0xfff1dd, day: 0xffffff, noon: 0xffffff,
            afternoon: 0xfff8ec, evening: 0xffc9a1, dusk: 0xe0a898, night: 0x8b97c9,
        };
        const hex = tints[timeOfDay] !== undefined ? tints[timeOfDay] : 0xffffff;
        this.targetTint = {
            r: ((hex >> 16) & 255) / 255,
            g: ((hex >> 8) & 255) / 255,
            b: (hex & 255) / 255,
        };
    }

    _updateSceneTint(dt) {
        const k = Math.min(1, dt / 1500); // ease over ~1.5s
        const c = this.sceneTint, t = this.targetTint;
        c.r += (t.r - c.r) * k;
        c.g += (t.g - c.g) * k;
        c.b += (t.b - c.b) * k;
        // Cinematic and weather tints MULTIPLY the day/night tint (never replace)
        const k2 = Math.min(1, dt / 700);
        const ct = this.cineTint, tt = this.cineTintTarget;
        ct.r += (tt.r - ct.r) * k2; ct.g += (tt.g - ct.g) * k2; ct.b += (tt.b - ct.b) * k2;
        const wt = this.weatherTint;
        this.worldContainer.tint =
            (Math.round(c.r * ct.r * wt.r * 255) << 16) |
            (Math.round(c.g * ct.g * wt.g * 255) << 8) |
             Math.round(c.b * ct.b * wt.b * 255);
    }

    // ================= Ambient NPC wandering =================

    _updateNpcWander(dt) {
        if (this.editorMode || this.combat) return;
        for (const id in this.npcs) {
            const npc = this.npcs[id];
            if (npc.status === 'dead') continue;
            if (this.editingNpc && this.editingNpc.data.id === id) continue;

            let w = npc.wander;
            if (!w) {
                w = npc.wander = {
                    homeX: npc.data.x, homeY: npc.data.y,
                    nx: npc.data.x, ny: npc.data.y,
                    tx: 0, ty: 0, state: 'idle',
                    wait: 2000 + Math.random() * 9000,
                    phase: Math.random() * Math.PI * 2,
                };
            }

            // Stand still near the player so they're easy to click and talk to
            const pdx = this.player.normalizedX - w.nx;
            const pdy = this.player.normalizedY - w.ny;
            const nearPlayer = (pdx * pdx + pdy * pdy) < 64;

            if (w.state === 'idle') {
                w.wait -= dt;
                if (w.wait <= 0 && !nearPlayer) {
                    for (let tries = 0; tries < 6; tries++) {
                        const tx = w.homeX + (Math.random() - 0.5) * 20;
                        const ty = w.homeY + (Math.random() - 0.5) * 14;
                        if (tx >= 2 && tx <= 98 && ty >= 2 && ty <= 98 && this.isWalkable(tx, ty)) {
                            w.tx = tx; w.ty = ty; w.state = 'walk';
                            break;
                        }
                    }
                    if (w.state !== 'walk') w.wait = 5000;
                }
            } else if (w.state === 'walk') {
                if (nearPlayer) {
                    w.state = 'idle'; w.wait = 3000;
                } else {
                    const dx = w.tx - w.nx, dy = w.ty - w.ny;
                    const dist = Math.hypot(dx, dy);
                    const step = 3.2 * dt / 1000; // slow stroll
                    if (dist <= step) {
                        w.nx = w.tx; w.ny = w.ty;
                        w.state = 'idle'; w.wait = 5000 + Math.random() * 12000;
                    } else {
                        const nx2 = w.nx + (dx / dist) * step;
                        const ny2 = w.ny + (dy / dist) * step;
                        if (this.isWalkable(nx2, ny2)) { w.nx = nx2; w.ny = ny2; }
                        else { w.state = 'idle'; w.wait = 6000; }
                        // Face the walking direction (front sprites just flip)
                        if (Math.abs(dx) > 0.1) {
                            npc.sprite.scale.x = Math.abs(npc.sprite.scale.x) * (dx < 0 ? -1 : 1);
                        }
                    }
                    w.phase += dt * 0.012;
                }
            }

            const gx = this.normalizedToWorldX(w.nx);
            const gy = this.normalizedToWorldY(w.ny);
            const bob = w.state === 'walk'
                ? -Math.abs(Math.sin(w.phase)) * npc.sprite.height * 0.012 : 0;
            npc.sprite.x = gx;
            npc.sprite.y = gy + bob;
            npc.sprite.zIndex = gy;
            this._fitShadow(npc.shadow, npc.sprite, gy);
        }
    }

    // ================= Canvas feedback: floaters & speech bubbles =================

    /** Floating text over the player (damage, gold, items). */
    spawnFloater(text, color = 0xffffff) {
        if (!this.player || !this.player.sprite) return;
        const t = new PIXI.Text({
            text,
            style: {
                fontFamily: 'Georgia, serif',
                fontSize: 17,
                fontWeight: '700',
                fill: color,
                stroke: { color: 0x000000, width: 4 },
            },
        });
        t.anchor.set(0.5, 1);
        const s = this.player.sprite;
        t.x = s.x + (Math.random() - 0.5) * 24;
        t.y = s.y - s.height - 6 - this.floaters.length * 20; // stack multiples
        this.bubbleLayer.addChild(t);
        this.floaters.push({ t, life: 0, maxLife: 1500, startY: t.y });
    }

    _updateFloaters(dt) {
        for (let i = this.floaters.length - 1; i >= 0; i--) {
            const f = this.floaters[i];
            f.life += dt;
            const k = f.life / f.maxLife;
            if (k >= 1) {
                this.bubbleLayer.removeChild(f.t);
                f.t.destroy();
                this.floaters.splice(i, 1);
                continue;
            }
            f.t.y = f.startY - 44 * (1 - Math.pow(1 - k, 2)); // ease-out rise
            f.t.alpha = k < 0.55 ? 1 : 1 - (k - 0.55) / 0.45;
        }
    }

    /** Speech bubble above an NPC, synced to the voice line when one plays. */
    showSpeechBubble(npcId, text, durationMs = null, audioId = null) {
        const npc = this.npcs[npcId];
        if (!npc || !npc.sprite || !text) return;

        this.hideSpeechBubble(npcId);

        const display = text.length > 220 ? text.slice(0, 217) + '…' : text;
        const label = new PIXI.Text({
            text: display,
            style: {
                fontFamily: 'Georgia, serif',
                fontSize: 13,
                fill: 0xf4f0e6,
                wordWrap: true,
                wordWrapWidth: 230,
                lineHeight: 17,
            },
        });
        const padX = 10, padY = 7;
        const bg = new PIXI.Graphics();
        bg.roundRect(-label.width / 2 - padX, -label.height - padY * 2, label.width + padX * 2, label.height + padY * 2, 8);
        bg.fill({ color: 0x14141d, alpha: 0.88 });
        bg.roundRect(-label.width / 2 - padX, -label.height - padY * 2, label.width + padX * 2, label.height + padY * 2, 8);
        bg.stroke({ color: 0x6366f1, width: 1, alpha: 0.5 });
        // Tail
        bg.moveTo(-5, 0); bg.lineTo(5, 0); bg.lineTo(0, 7); bg.closePath();
        bg.fill({ color: 0x14141d, alpha: 0.88 });

        label.anchor.set(0.5, 1);
        label.y = -padY;

        const c = new PIXI.Container();
        c.addChild(bg);
        c.addChild(label);
        this.bubbleLayer.addChild(c);

        const hold = durationMs || Math.min(2600 + display.length * 42, 11000);
        this.bubbles[npcId] = { c, until: performance.now() + hold, audioId };
        this._positionBubble(npcId);
    }

    hideSpeechBubble(npcId) {
        const b = this.bubbles[npcId];
        if (!b) return;
        this.bubbleLayer.removeChild(b.c);
        b.c.destroy({ children: true });
        delete this.bubbles[npcId];
    }

    _bubbleByAudio(audioId) {
        if (!audioId) return null;
        for (const id in this.bubbles) {
            if (this.bubbles[id].audioId === audioId) return this.bubbles[id];
        }
        return null;
    }

    _positionBubble(npcId) {
        const b = this.bubbles[npcId];
        const npc = this.npcs[npcId];
        if (!b || !npc || !npc.sprite) return;
        b.c.x = npc.sprite.x;
        b.c.y = npc.sprite.y - npc.sprite.height - 10;
        // Keep the bubble inside the world horizontally
        const half = b.c.width / 2;
        b.c.x = Math.max(half + 4, Math.min(this.worldWidth - half - 4, b.c.x));
    }

    _updateBubbles() {
        const now = performance.now();
        for (const id in this.bubbles) {
            if (now > this.bubbles[id].until) {
                this.hideSpeechBubble(id);
            } else {
                this._positionBubble(id);
            }
        }
    }

    // ================= Canvas-first play: doors, objects, barks =================
    // Walking into an unlocked door travels instantly (REST, no LLM); E takes/
    // reads/opens/searches the nearest scene object; ambient barks bubble up
    // on timers. All of it is suppressed during combat and cinematics.

    static DOOR_RADIUS = 5.5;     // normalized units
    static DOOR_DWELL_MS = 220;   // walk into it this long -> travel
    static DOOR_COOLDOWN_MS = 2500;
    static OBJECT_RADIUS = 7.0;
    static BARK_MIN_MS = 12000;
    static BARK_MAX_MS = 26000;
    static BARK_NEAR = 34.0;      // only bark when this close to the player

    /** One arch marker + label per exit that has a derived door slot.
     *  Locked doors draw in amber with a keyhole; unlocked in soft indigo. */
    _buildDoors(exits) {
        this.doorLayer.removeChildren();
        this.doors = [];
        for (const ex of (exits || [])) {
            if (!ex || !ex.door || ex.id == null) continue;
            const locked = !!ex.locked;
            const color = locked ? 0xfbbf24 : 0x818cf8;
            const g = new PIXI.Graphics();
            // Soft ground glow
            g.ellipse(0, 4, 26, 9);
            g.fill({ color, alpha: 0.14 });
            // Arch outline
            g.moveTo(-13, 2);
            g.lineTo(-13, -20);
            g.arc(0, -20, 13, Math.PI, 0);
            g.lineTo(13, 2);
            g.stroke({ color, width: 2, alpha: 0.7 });
            if (locked) {
                // Keyhole glyph
                g.circle(0, -16, 3);
                g.fill({ color, alpha: 0.9 });
                g.rect(-1.2, -14, 2.4, 7);
                g.fill({ color, alpha: 0.9 });
            }
            g.x = this.normalizedToWorldX(ex.door.x);
            g.y = this.normalizedToWorldY(ex.door.y);
            this.doorLayer.addChild(g);

            const label = new PIXI.Text({
                text: ex.name || '',
                style: {
                    fontFamily: 'Georgia, serif', fontSize: 11,
                    fill: locked ? 0xfcd34d : 0xc7d2fe,
                    align: 'center', wordWrap: true, wordWrapWidth: 140,
                    dropShadow: true, dropShadowDistance: 1,
                },
            });
            label.anchor.set(0.5, 1);
            label.x = g.x;
            label.y = g.y - 38;
            label.alpha = 0.8;
            this.doorLayer.addChild(label);

            this.doors.push({
                exitId: ex.id, name: ex.name || 'the way out', locked,
                x: ex.door.x, y: ex.door.y, slot: ex.door.slot, g, label,
            });
        }
    }

    /** A pulsing 4-point sparkle per interactable; consumed ones grey out. */
    _buildObjects(objects) {
        for (const o of this.objects) {
            if (o.g && !o.g.destroyed) {
                this.entityLayer.removeChild(o.g);
                o.g.destroy({ children: true });
            }
        }
        this.objects = [];
        for (const ob of (objects || [])) {
            if (!ob || ob.id == null) continue;
            const g = new PIXI.Graphics();
            g.moveTo(0, -8); g.lineTo(2.2, -2.2); g.lineTo(8, 0); g.lineTo(2.2, 2.2);
            g.lineTo(0, 8); g.lineTo(-2.2, 2.2); g.lineTo(-8, 0); g.lineTo(-2.2, -2.2);
            g.closePath();
            g.fill({ color: ob.locked ? 0xfbbf24 : 0xfde68a, alpha: 0.9 });
            g.circle(0, 0, 2.4);
            g.fill({ color: 0xffffff, alpha: 0.9 });
            g.x = this.normalizedToWorldX(ob.x);
            g.y = this.normalizedToWorldY(ob.y);
            g.zIndex = g.y;
            if (ob.consumed) g.alpha = 0.22;
            this.entityLayer.addChild(g);
            this.objects.push({
                id: ob.id, kind: ob.kind, label: ob.label || 'it',
                x: ob.x, y: ob.y, consumed: !!ob.consumed, locked: !!ob.locked,
                g, phase: Math.random() * Math.PI * 2,
            });
        }
    }

    /** Dwell-to-travel on unlocked doors; locked ones arm E instead. */
    _updateDoors(dt) {
        this.doorCooldown = Math.max(0, this.doorCooldown - dt);
        this.nearDoor = null;
        if (!this.doors.length || this.combat || this.playerDead || this.editorMode || this.cine) {
            this.doorArmed = null;
            this.doorDwell = 0;
            return;
        }
        const pulse = 0.55 + 0.2 * Math.sin(performance.now() * 0.003);
        let best = null, bestDist = Infinity;
        for (const d of this.doors) {
            if (d.g && !d.g.destroyed) d.g.alpha = pulse;
            const dist = Math.hypot(d.x - this.player.normalizedX, d.y - this.player.normalizedY);
            if (dist < GameRenderer.DOOR_RADIUS && dist < bestDist) { best = d; bestDist = dist; }
        }
        if (!best) { this.doorArmed = null; this.doorDwell = 0; return; }
        if (best.locked) { this.nearDoor = best; this.doorArmed = null; this.doorDwell = 0; return; }
        if (window.forgeTurnBusy) { this.doorDwell = 0; return; }   // DM turn streaming
        if (best.exitId !== this.doorArmed) { this.doorArmed = best.exitId; this.doorDwell = 0; }
        // Travel means WALKING into it, not standing by it — pushing against
        // the scene edge behind the door counts as walking into it
        if (!this.isMoving && this.edgePushTimer <= 0) return;
        this.doorDwell += dt;
        if (this.doorDwell >= GameRenderer.DOOR_DWELL_MS && this.doorCooldown <= 0) {
            this.doorCooldown = GameRenderer.DOOR_COOLDOWN_MS;
            this.doorDwell = 0;
            window.dispatchEvent(new CustomEvent('canvas-travel', {
                detail: { exitId: best.exitId, name: best.name, locked: false },
            }));
        }
    }

    /** Sparkle pulse + nearest-object pick; drives the [E] prompt. */
    _updateObjects() {
        this.nearObject = null;
        const now = performance.now();
        let best = null, bestDist = Infinity;
        for (const o of this.objects) {
            if (!o.g || o.g.destroyed || o.consumed) continue;
            const k = 0.85 + 0.25 * Math.sin(now * 0.004 + o.phase);
            o.g.scale.set(k);
            o.g.alpha = 0.75 + 0.25 * Math.sin(now * 0.005 + o.phase);
            const dist = Math.hypot(o.x - this.player.normalizedX, o.y - this.player.normalizedY);
            if (dist < GameRenderer.OBJECT_RADIUS && dist < bestDist) { best = o; bestDist = dist; }
        }
        if (!this.combat && !this.playerDead && !this.editorMode && !this.cine) {
            this.nearObject = best;
        }
        this._updateInteractPrompt();
    }

    _updateInteractPrompt() {
        const existing = this.uiLayer.getChildByName('interact-prompt');
        let text = null;
        if (!this.combat && !this.cine && !this.editorMode && !this.playerDead) {
            if (this.nearObject) {
                const verbs = { take: 'Take', read: 'Read', open: 'Open', search: 'Search' };
                text = `[E]  ${verbs[this.nearObject.kind] || 'Use'} ${this.nearObject.label}`;
            } else if (this.nearDoor) {
                text = `[E]  Try the way to ${this.nearDoor.name}`;
            }
        }
        if (!text) {
            if (existing) { this.uiLayer.removeChild(existing); existing.destroy(); }
            return;
        }
        if (existing && existing.text === text) return;
        if (existing) { this.uiLayer.removeChild(existing); existing.destroy(); }
        const t = new PIXI.Text({
            text,
            style: {
                fontFamily: 'Georgia, serif', fontSize: 15, fill: 0xf4f0e6,
                stroke: { color: 0x000000, width: 4 },
            },
        });
        t.anchor.set(0.5, 1);
        t.x = this.app.screen.width / 2;
        t.y = this.app.screen.height - 24;
        t.name = 'interact-prompt';
        this.uiLayer.addChild(t);
    }

    /** E: hand the action to the page (index.html owns the REST calls). */
    _tryInteract() {
        if (this.combat || this.playerDead || this.editorMode || this.cine || !this.sceneReady) return;
        if (this.nearObject) {
            window.dispatchEvent(new CustomEvent('canvas-interact', {
                detail: { objectId: this.nearObject.id, label: this.nearObject.label },
            }));
            return;
        }
        if (this.nearDoor) {
            window.dispatchEvent(new CustomEvent('canvas-travel', {
                detail: { exitId: this.nearDoor.exitId, name: this.nearDoor.name, locked: true },
            }));
        }
    }

    /** Grey out a scene object after a successful interaction. */
    markObjectConsumed(objectId) {
        const o = this.objects.find(x => x.id === objectId);
        if (!o) return;
        o.consumed = true;
        if (o.g && !o.g.destroyed) {
            o.g.alpha = 0.22;
            o.g.scale.set(0.85);
        }
        if (this.nearObject === o) this.nearObject = null;
    }

    setBarks(barks) {
        this.barks = Array.isArray(barks) ? barks.filter(b => b && b.text) : [];
        this.barkTimer = 7000 + Math.random() * 9000;
    }

    async _loadBarks(locationId, attempt = 0) {
        try {
            const res = await fetch(`/api/barks/${locationId}`);
            if (!res.ok) return;
            const data = await res.json();
            if (this.locationId !== locationId) return;   // scene changed mid-flight
            this.setBarks(data.barks || []);
            // Pool still generating server-side: one retry, then quiet
            if (data.pending && attempt === 0) {
                setTimeout(() => {
                    if (this.locationId === locationId && !this.isLoading) {
                        this._loadBarks(locationId, 1);
                    }
                }, data.refresh_after_ms || 20000);
            }
        } catch (err) { /* a quiet scene beats an error */ }
    }

    /** Timed ambient chatter through the existing bubble system.
     *  No TTS, no chat line — audioId stays null on purpose. */
    _updateBarks(dt) {
        if (!this.barks.length) return;
        if (this.combat || this.editorMode || this.playerDead || this.cine) return;
        if (window.forgeTurnBusy) return;      // never talk over the DM
        this.barkTimer -= dt;
        if (this.barkTimer > 0) return;
        this.barkTimer = GameRenderer.BARK_MIN_MS +
            Math.random() * (GameRenderer.BARK_MAX_MS - GameRenderer.BARK_MIN_MS);

        const near = [];
        for (const id in this.npcs) {
            const npc = this.npcs[id];
            if (npc.status === 'dead' || this.bubbles[id]) continue;
            const nx = npc.wander ? npc.wander.nx : npc.data.x;
            const ny = npc.wander ? npc.wander.ny : npc.data.y;
            if (Math.hypot(nx - this.player.normalizedX, ny - this.player.normalizedY) < GameRenderer.BARK_NEAR) {
                near.push(id);
            }
        }
        if (!near.length) return;

        let bark = null;
        for (let tries = 0; tries < 5; tries++) {
            const candidate = this.barks[Math.floor(Math.random() * this.barks.length)];
            if (candidate && !this.barkRecent.includes(candidate.text)) { bark = candidate; break; }
        }
        if (!bark) return;
        // Named speaker if they're actually here; ambient lines get a body
        const speaker = (bark.npc_id && near.includes(bark.npc_id))
            ? bark.npc_id
            : near[Math.floor(Math.random() * near.length)];
        this.showSpeechBubble(speaker, bark.text, 3800, null);
        this.barkRecent.push(bark.text);
        if (this.barkRecent.length > 4) this.barkRecent.shift();
    }

    // ================= Real-time combat =================
    // The DM starts fights via the start_combat tool (SSE combat_start →
    // startCombat below); enemies are real NPCs fighting with their own
    // sprites and archetype-driven behavior. Ctrl+K still spawns a gray-box
    // test enemy. Space = dodge-roll (i-frames), J = melee arc. Outcomes of
    // real fights are reported through the 'combat-ended' event → server.

    static ARCHETYPES = {
        // FALLBACK ONLY: authoritative params arrive resolved server-side in
        // combat_start.enemies[].params (tier x archetype tables live in
        // src/services/combat.py). This table survives for the Ctrl+K
        // gray-box enemy and as old-server defence.
        brute:      { hp: 7, speed: 4.8, windup: 650, lungeDist: 10, lungeMs: 215, recover: 800, engage: 7,  scaleMul: 1.12 },
        skirmisher: { hp: 3, speed: 8.5, windup: 340, lungeDist: 8,  lungeMs: 150, recover: 430, engage: 6,  scaleMul: 0.95 },
        stalker:    { hp: 4, speed: 6.8, windup: 280, lungeDist: 12, lungeMs: 140, recover: 520, engage: 9,  scaleMul: 1.0, circles: true },
        test:       { hp: 5, speed: 6.5, windup: 480, lungeDist: 9,  lungeMs: 170, recover: 550, engage: 7,  scaleMul: 1.0 },
    };

    // The danger ladder, taught visually: HP bar colour + width by tier
    static TIER_INDEX = { harmless: 0, weak: 1, standard: 2, dangerous: 3, deadly: 4, boss: 5 };
    static TIER_COLORS = {
        harmless: 0x9ca3af, weak: 0x84cc16, standard: 0xef4444,
        dangerous: 0xf97316, deadly: 0xdc2626,
    };

    // Old-server defence only — a real boss always ships params.bossPattern
    static BOSS_FALLBACK = {
        phase2_at: 0.5, phase_transition_ms: 1200,
        slam_first_at: 4000, slam_every: 9000, slam_every_p2: 6500,
        slam_windup: 900, slam_radius: 18, slam_damage: 3, slam_recover: 1100,
        combo_count: 3, combo_gap: 180,
        p2_speed_mul: 1.15, p2_windup_mul: 0.78, p2_stagger: 440,
    };

    /** Player combat numbers come from the SERVER (level + gear + health)
     *  via state.combat. The fallback is exactly the pre-progression
     *  behaviour, so an old server or a state event that has not arrived
     *  yet still plays. game.js never derives stats from items. */
    _playerCombatStats() {
        const c = (window.lastPlayerState && window.lastPlayerState.combat) || null;
        if (c && typeof c.max_hearts === 'number') {
            const light = c.light_damage || 1;
            return {
                maxHearts: c.max_hearts,
                hearts: (typeof c.hearts === 'number') ? c.hearts : c.max_hearts,
                light,
                heavy: c.heavy_damage || (light * 2 + 1),
                dodgeCd: c.dodge_cooldown_ms || 750,
                iframeMs: c.iframe_ms || 350,
                quickSlot: c.quick_slot || null,
                parryUnlocked: c.parry_unlocked !== false,
                heavyUnlocked: c.heavy_unlocked !== false,
            };
        }
        const map = { healthy: 5, winded: 4, hurt: 3, badly_hurt: 2, critical: 1 };
        const h = (window.lastPlayerState && window.lastPlayerState.health) || 'healthy';
        return {
            maxHearts: 5, hearts: map[h] ?? 5, light: 1, heavy: 3,
            dodgeCd: 750, iframeMs: 350, quickSlot: null,
            parryUnlocked: true, heavyUnlocked: true,
        };
    }

    _ensureCombat() {
        if (this.combat) return;
        // Stats are snapshotted ONCE per fight — a mid-fight state event
        // must never silently re-roll the player's live HP.
        const st = this._playerCombatStats();
        this._jEdge = false;
        this.combat = {
            enemies: [], playerHp: st.hearts, playerMaxHp: st.maxHearts,
            stats: st, heartsLost: 0, heartsHealed: 0, buffDamage: 0, buffUntil: 0,
            hitsTaken: 0, reported: false,
            iframes: 0, dodge: 0, dodgeVec: [0, 1], dodgeCd: 0,
            attackCd: 0, hitstop: 0, shake: 0,
            jDownAt: 0, charging: false, chargeReady: false, chargeLock: 0,
            parry: 0, parryLag: 0, parryCd: 0,
            rings: [], boss: null, fizzleT: 0,
        };
        this._drawHearts();
        window.forgeAudio?.playSfx('clash', { volume: 0.9 });
    }

    /** DM-initiated combat: spawn each named NPC as a combatant.
     *  While a combat-blocking cinematic (boss_intro) plays, the roster is
     *  parked in _pendingCombat and spawns when the entrance ends. */
    startCombat(enemies, boss) {
        if (this.playerDead) return;
        if (!this.player || this.isLoading || !this.sceneReady) {
            this._queueForReady('combat', { enemies: enemies || [], boss: boss || null });
            return;
        }
        if (this.cine && GameRenderer.CINE[this.cine.kind]?.blocksCombat) {
            this._pendingCombat = { enemies: enemies || [], boss: boss || null };
            window.dispatchEvent(new CustomEvent('combat-active', { detail: { active: true } }));
            return;
        }
        this._ensureCombat();
        if (boss && boss.npc_id) {
            this.combat.boss = {
                npcId: boss.npc_id,
                name: boss.name || '',
                title: boss.title || '',
            };
        }
        for (const en of (enemies || [])) {
            this._spawnCombatant(
                en.npc_id || null,
                en.archetype || 'skirmisher',
                en.name,
                en.params || null,
                !!(en.boss || (en.params && en.params.boss)),
            );
        }
        if (this.combat.boss) this._drawBossBar();
        // Real fight: the chat locks — the canvas resolves this, not prose
        window.dispatchEvent(new CustomEvent('combat-active', { detail: { active: true } }));
        console.log(`Combat started: ${(enemies || []).map(e => `${e.name} (${e.tier || '?'} ${e.archetype})`).join(', ')}`);
    }

    _spawnTestEnemy() {
        if (!this.player || this.playerDead || this.isLoading || !this.sceneReady) return;
        this._ensureCombat();
        this._spawnCombatant(null, 'test', null);
        console.log('Gray-box enemy spawned (Space = dodge, J = attack)');
    }

    _spawnCombatant(npcId, archetype, name, params, isBoss) {
        // Server-resolved params are authoritative; the local table is only
        // the Ctrl+K gray-box / old-server fallback.
        params = params || GameRenderer.ARCHETYPES[archetype] || GameRenderer.ARCHETYPES.skirmisher;
        const npc = npcId ? this.npcs[npcId] : null;

        // Spawn where the NPC is standing; test enemies appear near the player
        let ex, ey;
        if (npc) {
            ex = npc.wander ? npc.wander.nx : npc.data.x;
            ey = npc.wander ? npc.wander.ny : npc.data.y;
        } else {
            ex = Math.min(95, this.player.normalizedX + 18);
            ey = this.player.normalizedY;
            for (let t = 0; t < 12; t++) {
                const a = Math.random() * Math.PI * 2;
                const cx = this.player.normalizedX + Math.cos(a) * 18;
                const cy = this.player.normalizedY + Math.sin(a) * 12;
                if (cx >= 3 && cx <= 97 && cy >= 3 && cy <= 97 && this.isWalkable(cx, cy)) {
                    ex = cx; ey = cy;
                    break;
                }
            }
        }

        let g, h, isSprite = false;
        if (npc && npc.sprite && npc.sprite.texture && npc.sprite.texture.width > 1) {
            // Fight with the NPC's real art; hide their ambient body meanwhile
            g = new PIXI.Sprite(npc.sprite.texture);
            g.anchor.set(0.5, 1);
            g.scale.set(Math.abs(npc.sprite.scale.y) * (params.scaleMul || 1));
            h = g.height;
            isSprite = true;
            npc.sprite.visible = false;
            if (npc.shadow) npc.shadow.visible = false;
            this.hideSpeechBubble(npcId);
        } else {
            h = this.worldHeight * 0.11 * (params.scaleMul || 1);
            g = new PIXI.Graphics();
            g.rect(-h * 0.28, -h, h * 0.56, h);
            g.fill({ color: 0x8a8f9c });
            g.rect(-h * 0.28, -h, h * 0.56, h);
            g.stroke({ color: 0x2c313a, width: 2 });
        }
        this.entityLayer.addChild(g);

        const hpBar = new PIXI.Graphics();
        this.particleLayer.addChild(hpBar);
        const shadow = this._makeShadow();
        this.shadowLayer.addChild(shadow);
        const telegraph = new PIXI.Graphics();
        this.particleLayer.addChild(telegraph);

        const bossEnemy = !!(isBoss || params.boss);
        const enemy = {
            g, hpBar, shadow, telegraph, h, params, isSprite,
            npcId: npcId || null, name: name || null,
            tier: params.tier || null,
            isBoss: bossEnemy,
            bossPattern: bossEnemy ? (params.bossPattern || GameRenderer.BOSS_FALLBACK) : null,
            nx: ex, ny: ey, hp: params.hp, maxHp: params.hp,
            state: 'chase', t: 0, aim: [0, 0], lungeFrom: null,
            dead: false, deathT: 0, flash: 0,
            strafeDir: Math.random() < 0.5 ? -1 : 1, circleT: 0,
            vulnMul: 1, staggerT: 0, invuln: false,
            phase: 1, comboLeft: 0, comboCount: 0,
            slamT: null, slamEvery: null,
            baseTexture: isSprite ? g.texture : null,
            attackFrames: {}, attackReady: false,
        };
        if (enemy.bossPattern) {
            enemy.slamT = enemy.bossPattern.slam_first_at || 4000;
            enemy.slamEvery = enemy.bossPattern.slam_every || 9000;
            enemy.comboCount = enemy.bossPattern.combo_count || 3;
        }
        // NPC attack animation frames load in the background (a 404 also
        // triggers their generation server-side — ready for the next fight)
        if (npcId && isSprite) {
            let loaded = 0;
            for (let f = 1; f <= 6; f++) {
                PIXI.Assets.load(`/api/assets/sprite/npc/${npcId}/attack${f}.png`).then(tex => {
                    enemy.attackFrames[f] = tex;
                    if (++loaded === 6) enemy.attackReady = true;
                }).catch(() => {});
            }
        }
        this._drawEnemyHp(enemy);
        this.combat.enemies.push(enemy);
    }

    _drawEnemyHp(enemy) {
        const bar = enemy.hpBar;
        bar.clear();
        if (enemy.isBoss) return;   // boss health lives in the named top bar
        const tierIdx = GameRenderer.TIER_INDEX[enemy.tier] ?? 2;
        const w = Math.max(30, enemy.h * 0.7) * (1 + 0.12 * tierIdx);
        bar.rect(-w / 2, 0, w, 5);
        bar.fill({ color: 0x1f232b });
        const frac = Math.max(0, enemy.hp / enemy.maxHp);
        if (frac > 0) {
            bar.rect(-w / 2, 0, w * frac, 5);
            bar.fill({ color: GameRenderer.TIER_COLORS[enemy.tier] || 0xef4444 });
        }
    }

    /** Named boss HP bar pinned to the top of the screen (uiLayer).
     *  Redrawn on damage and on phase change only. */
    _drawBossBar() {
        if (!this.uiLayer) return;
        const existing = this.uiLayer.getChildByName('boss-bar');
        if (existing) { this.uiLayer.removeChild(existing); existing.destroy({ children: true }); }
        const c = this.combat;
        if (!c || !c.boss) return;
        const e = c.enemies.find(en => en.isBoss);
        const w = Math.min(560, this.app.screen.width * 0.62);
        const cx = this.app.screen.width / 2;
        const phase2 = !!(e && e.phase >= 2);

        const container = new PIXI.Container();
        container.name = 'boss-bar';

        const nameText = new PIXI.Text({
            text: (c.boss.name || '').toUpperCase(),
            style: {
                fontFamily: 'Georgia, serif', fontSize: 14, fontWeight: '700',
                letterSpacing: 3, fill: 0xf4f0e6,
                dropShadow: true, dropShadowDistance: 2,
            },
        });
        nameText.anchor.set(0.5, 1);
        nameText.x = cx;
        nameText.y = 30;
        container.addChild(nameText);

        const g = new PIXI.Graphics();
        g.rect(cx - w / 2, 34, w, 16);
        g.fill({ color: 0x14141d, alpha: 0.85 });
        const frac = e ? Math.max(0, e.hp / e.maxHp) : 0;
        if (frac > 0) {
            g.rect(cx - w / 2 + 2, 36, (w - 4) * frac, 12);
            g.fill({ color: phase2 ? 0xf59e0b : 0xef4444 });
        }
        g.rect(cx - w / 2, 34, w, 16);
        g.stroke({ color: 0x7f1d1d, width: 2 });
        container.addChild(g);

        if (c.boss.title) {
            const t = new PIXI.Text({
                text: c.boss.title,
                style: { fontFamily: 'Georgia, serif', fontSize: 10, fontStyle: 'italic', fill: 0x9ca3af },
            });
            t.anchor.set(0.5, 0);
            t.x = cx;
            t.y = 54;
            container.addChild(t);
        }
        this.uiLayer.addChild(container);
    }

    _drawHearts() {
        if (!this.combat) return;
        let hearts = this.uiLayer.getChildByName('combat-hearts');
        if (hearts) this.uiLayer.removeChild(hearts);
        hearts = new PIXI.Container();
        hearts.name = 'combat-hearts';
        for (let i = 0; i < this.combat.playerMaxHp; i++) {
            const heart = new PIXI.Graphics();
            heart.rect(0, 0, 18, 18);
            heart.fill({ color: i < this.combat.playerHp ? 0xef4444 : 0x3a3f4a });
            heart.x = 14 + i * 24;
            heart.y = 14;
            hearts.addChild(heart);
        }
        this.uiLayer.addChild(hearts);
    }

    _endCombat(silent) {
        if (!this.combat) return;
        this._clearRings();
        const bossBar = this.uiLayer.getChildByName('boss-bar');
        if (bossBar) { this.uiLayer.removeChild(bossBar); bossBar.destroy({ children: true }); }
        if (this.player && this.player.sprite && !this.playerDead) {
            this.player.sprite.tint = 0xffffff;   // clear any charge glow
        }
        const wasReal = this.combat.enemies.some(e => e.npcId);
        for (const e of this.combat.enemies) {
            // Reconcile the ambient NPC body with how the fight ended
            const npc = e.npcId ? this.npcs[e.npcId] : null;
            if (npc && npc.sprite) {
                if (e.dead) {
                    // They fell where the fight ended — settle the ambient
                    // sprite there in its final dead pose
                    npc.status = 'dead';
                    npc.data.x = e.nx; npc.data.y = e.ny;
                    npc.wander = null;
                    npc.sprite.x = this.normalizedToWorldX(e.nx);
                    npc.sprite.y = this.normalizedToWorldY(e.ny);
                    npc.sprite.zIndex = npc.sprite.y;
                    npc.sprite.rotation = Math.PI / 2;
                    npc.sprite.alpha = 0.5;
                    npc.sprite.tint = 0x808080;
                    npc.sprite.eventMode = 'none';
                    npc.sprite.cursor = 'default';
                    if (npc.shadow) { npc.shadow.alpha = 0.3; this._fitShadow(npc.shadow, npc.sprite); }
                }
                npc.sprite.visible = true;
                if (npc.shadow) npc.shadow.visible = true;
            }
            this.entityLayer.removeChild(e.g);
            this.shadowLayer.removeChild(e.shadow);
            this.particleLayer.removeChild(e.telegraph);
            this.particleLayer.removeChild(e.hpBar);
            e.g.destroy({ children: true });
            e.shadow.destroy();
            e.telegraph.destroy();
            e.hpBar.destroy();
        }
        const hearts = this.uiLayer.getChildByName('combat-hearts');
        if (hearts) this.uiLayer.removeChild(hearts);
        this.combat = null;
        this.playerAction = null;
        if (wasReal) {
            window.dispatchEvent(new CustomEvent('combat-active', { detail: { active: false } }));
        }
        if (!silent) console.log('Combat over');
    }

    /** A real (DM-integrated) fight resolved — report the outcome. */
    _finishCombat(outcome) {
        const c = this.combat;
        if (!c || c.reported) return;
        c.reported = true;
        const detail = {
            outcome,
            hitsTaken: c.hitsTaken || 0,
            heartsLost: c.heartsLost || 0,
            heartsHealed: c.heartsHealed || 0,
            defeated: c.enemies.filter(e => e.dead && e.npcId).map(e => e.npcId),
            playerHp: Math.max(0, c.playerHp),
        };
        this._endCombat(false);
        window.dispatchEvent(new CustomEvent('combat-ended', { detail }));
    }

    _facingVector() {
        switch (this.player.direction) {
            case 'left': return [-1, 0];
            case 'right': return [1, 0];
            case 'back': return [0, -1];
            default: return [0, 1];
        }
    }

    /** Play a generated combat action animation if its frames exist.
     *  Timing: instant into the strike (the hit already landed in code —
     *  responsiveness beats anticipation), longer follow-through. */
    _startPlayerAction(kind, durs) {
        const set = this.player.actionSprites && this.player.actionSprites[kind];
        if (!set) return;
        const frames = [];
        for (let f = 1; f <= 6; f++) {
            if (!set[f]) return; // incomplete set (still generating) — skip
            frames.push(set[f]);
        }
        this.playerAction = { kind, frames, durs, t: 0, total: durs.reduce((a, b) => a + b, 0) };
    }

    // ---------------- Player verbs: light / heavy / parry / quick slot ----------------

    _lightProfile() {
        const c = this.combat;
        const buff = (c.buffUntil > performance.now()) ? (c.buffDamage || 0) : 0;
        return {
            damage: Math.max(1, (c.stats.light || 1) + buff),
            halfAngle: 0.9, dotMin: 0.25, reach: 10, knock: 6,
            hitstop: 70, shake: 5, cd: 380, staggerMs: 0, heavy: false,
        };
    }

    _heavyProfile() {
        const c = this.combat;
        const buff = (c.buffUntil > performance.now()) ? (c.buffDamage || 0) : 0;
        return {
            damage: Math.max(1, (c.stats.heavy || 3) + buff),
            halfAngle: 1.15, dotMin: 0.08, reach: 12, knock: 10,
            hitstop: 110, shake: 9, cd: 760, staggerMs: 300, heavy: true,
        };
    }

    /** One swing, shared by light and heavy: arc visual + hit test + damage. */
    _swing(profile) {
        const c = this.combat;
        const p = this.player;
        if (!c || !p) return;
        c.attackCd = profile.cd;
        c.fizzleT = 0;
        this._startPlayerAction('attack', profile.heavy ? [40, 40, 80, 90, 85, 95] : [35, 35, 70, 80, 75, 85]);
        window.forgeAudio?.playSfx('swing', { volume: profile.heavy ? 0.95 : 0.7 });
        const [fx, fy] = this._facingVector();

        // Visual: arc wedge that fades (bigger and warmer for the heavy)
        const arc = new PIXI.Graphics();
        const r = ((profile.reach - 1) / 100) * this.worldWidth * 0.9;
        const ang = Math.atan2(fy, fx);
        arc.moveTo(0, 0);
        arc.arc(0, 0, r, ang - profile.halfAngle, ang + profile.halfAngle);
        arc.closePath();
        arc.fill({ color: profile.heavy ? 0xffe9a8 : 0xffffff, alpha: 0.35 });
        arc.x = p.sprite.x;
        arc.y = p.sprite.y - p.sprite.height * 0.35;
        this.particleLayer.addChild(arc);
        this.particles.push({ g: arc, vx: 0, vy: 0, life: 0, maxLife: profile.heavy ? 170 : 130, startAlpha: 0.35 });

        // Hit test every live enemy
        for (const e of c.enemies) {
            if (e.dead) continue;
            const dx = e.nx - p.normalizedX, dy = e.ny - p.normalizedY;
            const dist = Math.hypot(dx, dy);
            if (dist > profile.reach) continue;
            const dot = (dx * fx + dy * fy) / (dist || 1);
            if (dot < profile.dotMin) continue; // outside the frontal arc
            if (e.invuln) continue;             // phase roar: untouchable
            const dmg = Math.max(1, Math.round(profile.damage * (e.vulnMul || 1)));
            e.hp -= dmg;
            e.flash = 90;
            c.hitstop = profile.hitstop;
            c.shake = Math.max(c.shake, profile.shake);
            window.forgeAudio?.playSfx('hit', { volume: 0.9 });
            // Heavies rock non-boss enemies back on their heels
            if (profile.staggerMs > 0 && !e.isBoss && e.hp > 0) {
                this._staggerEnemy(e, profile.staggerMs);
            }
            // Knockback away from the player (bosses don't move: knockMul 0)
            const kn = profile.knock * (e.params.knockMul ?? 1);
            if (kn > 0) {
                const kx = e.nx + (dx / (dist || 1)) * kn;
                const ky = e.ny + (dy / (dist || 1)) * kn;
                if (this.isWalkable(kx, ky)) { e.nx = kx; e.ny = ky; }
            }
            this._drawEnemyHp(e);
            if (e.isBoss) {
                if (e.hp > 0 && e.phase === 1 && e.bossPattern &&
                    e.hp <= e.maxHp * (e.bossPattern.phase2_at || 0.5)) {
                    this._enterBossPhase2(e);
                }
                this._drawBossBar();
            }
            if (e.hp <= 0) {
                e.dead = true; e.deathT = 0; e.staggerT = 0;
                e.telegraph.clear(); e.hpBar.clear();
                window.forgeAudio?.playSfx('death', { volume: 0.9 });
                if (e.isBoss) {
                    this._drawBossBar();
                    // The last boss standing goes down in slow motion
                    if (c.enemies.every(o => o === e || o.dead)) this._startKillCam(e);
                }
            }
        }
    }

    /** J released (or auto at 1600 ms): heavy fires if charged, else whiff. */
    _releaseCharge() {
        const c = this.combat;
        if (!c) return;
        if (c.chargeReady) {
            c.charging = false;
            c.chargeReady = false;
            if (c.parryLag <= 0 && c.dodge <= 0) this._swing(this._heavyProfile());
        } else if (c.charging) {
            c.charging = false;
            c.chargeLock = 120;   // aborted mid-charge: brief whiff lockout
        }
        c.jDownAt = 0;
    }

    /** K: a 150 ms read window bought with 260 ms of whiffed helplessness. */
    _tryParry() {
        const c = this.combat;
        if (!c || this.playerDead) return;
        if (!c.stats.parryUnlocked) return;
        if (c.parryCd > 0 || c.parryLag > 0 || c.dodge > 0) return;
        c.parry = 150;
        c.parryLag = 410;
        c.parryCd = 520;
        // A whiffed parry still rings out — audible commitment
        window.forgeAudio?.playSfx('clash', { volume: 0.5 });
        const g = new PIXI.Graphics();
        g.circle(0, 0, Math.max(14, this.player.sprite.width * 0.55));
        g.stroke({ color: 0xffffff, width: 3, alpha: 0.8 });
        g.x = this.player.sprite.x;
        g.y = this.player.sprite.y - this.player.sprite.height * 0.4;
        this.particleLayer.addChild(g);
        this.particles.push({ g, vx: 0, vy: 0, life: 0, maxLife: 90, startAlpha: 0.8 });
    }

    _staggerEnemy(e, ms) {
        if (e.invuln) return;
        e.staggerT = Math.max(e.staggerT || 0, ms || 0);
        e.vulnMul = 2;      // staggered enemies take double damage
        e.telegraph.clear();
    }

    /** Full-screen white pop for a successful parry (90 ms). */
    _parryFlash() {
        const flash = new PIXI.Graphics();
        flash.rect(0, 0, this.app.screen.width, this.app.screen.height);
        flash.fill({ color: 0xffffff, alpha: 0.25 });
        this.uiLayer.addChild(flash);
        setTimeout(() => {
            if (!flash.destroyed) {
                this.uiLayer.removeChild(flash);
                flash.destroy();
            }
        }, 90);
    }

    /** Q: drink the quick-slot consumable — optimistic, in or out of combat.
     *  The fight never waits for the network; the server state reconciles. */
    async _useQuickSlot() {
        const c = this.combat;
        const slot = (c ? c.stats.quickSlot : null) ||
                     (window.lastPlayerState?.combat?.quick_slot);
        if (!slot || !slot.item_id || !this.playerId) return;
        if (this._quickCd && performance.now() < this._quickCd) return;
        this._quickCd = performance.now() + 600;

        const healed = c ? Math.max(0, Math.min(slot.hearts || 0, c.playerMaxHp - c.playerHp)) : 0;
        if (c && healed > 0) {
            c.playerHp += healed;
            c.heartsHealed += healed;   // reported in the combat-ended detail
            this._drawHearts();
        }
        this.spawnFloater(`+ ${slot.name}`, 0x4ade80);
        window.forgeAudio?.playSfx('clash', { volume: 0.5 });

        try {
            const res = await fetch(`/api/player/${this.playerId}/use-consumable`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ item_id: slot.item_id, in_combat: !!c }),
            });
            const data = await res.json();
            if (!data.success) throw new Error(data.error || 'no');
            if (data.effect && data.effect.buff && c) {
                c.buffDamage = data.effect.bonus || 1;
                c.buffUntil = performance.now() + (data.effect.duration_ms || 20000);
            }
            // Reconcile HUD/inventory from the embedded state (index.html listens)
            if (data.state) {
                window.dispatchEvent(new CustomEvent('forge-state', { detail: data.state }));
            }
        } catch (e) {
            if (c && healed > 0) {   // roll back the optimism
                c.playerHp -= healed;
                c.heartsHealed -= healed;
                this._drawHearts();
            }
            this.spawnFloater('nothing left', 0x9ca3af);
        }
    }

    // ---------------- Ground rings (ranged volleys + boss slams) ----------------

    /** An expanding telegraphed circle that detonates after windupMs.
     *  Rings IGNORE parry — you must leave the circle. */
    _spawnGroundRing(nx, ny, radius, windupMs, damage) {
        if (!this.combat) return;
        const g = new PIXI.Graphics();
        this.particleLayer.addChild(g);
        this.combat.rings.push({
            nx, ny, r: radius, t: 0, dur: Math.max(1, windupMs || 1),
            dmg: damage ?? 1, g, done: false, doneT: 0,
        });
    }

    _clearRings() {
        const c = this.combat;
        if (!c || !c.rings) return;
        for (const ring of c.rings) {
            if (ring.g && !ring.g.destroyed) {
                this.particleLayer.removeChild(ring.g);
                ring.g.destroy();
            }
        }
        c.rings = [];
    }

    _updateRings(dt) {
        const c = this.combat;
        if (!c || !c.rings.length) return;
        const p = this.player;
        for (let i = c.rings.length - 1; i >= 0; i--) {
            const ring = c.rings[i];
            ring.t += dt;
            const g = ring.g;
            if (!g || g.destroyed) { c.rings.splice(i, 1); continue; }
            const wx = this.normalizedToWorldX(ring.nx);
            const wy = this.normalizedToWorldY(ring.ny);
            const wr = (ring.r / 100) * this.worldWidth;
            if (!ring.done) {
                const k = Math.min(1, ring.t / ring.dur);
                g.clear();
                g.circle(wx, wy, wr);
                g.stroke({ color: 0xef4444, width: 2, alpha: 0.5 });
                g.circle(wx, wy, wr * k);
                g.fill({ color: 0xef4444, alpha: 0.10 + 0.06 * Math.sin(ring.t * 0.02) });
                if (ring.t >= ring.dur) {
                    ring.done = true;
                    ring.doneT = 0;
                    g.clear();
                    g.circle(wx, wy, wr);
                    g.fill({ color: 0xffffff, alpha: 0.5 });
                    const pd = Math.hypot(p.normalizedX - ring.nx, p.normalizedY - ring.ny);
                    if (pd <= ring.r && c.iframes <= 0 && c.dodge <= 0) {
                        const dmg = ring.dmg ?? 1;
                        c.playerHp -= dmg;
                        c.heartsLost += dmg;
                        c.hitsTaken = (c.hitsTaken || 0) + 1;
                        c.iframes = 900;
                        c.hitstop = 60;
                        c.shake = Math.max(c.shake, 12);
                        window.forgeAudio?.playSfx('hurt');
                        // Knock the player away from the centre
                        const kd = pd || 1;
                        const px2 = p.normalizedX + ((p.normalizedX - ring.nx) / kd) * 7;
                        const py2 = p.normalizedY + ((p.normalizedY - ring.ny) / kd) * 7;
                        if (this.isWalkable(px2, py2)) {
                            p.normalizedX = px2;
                            p.normalizedY = py2;
                            this.updatePlayerPosition();
                            this.positionDirty = true;
                        }
                        this._drawHearts();
                        if (c.playerHp <= 0) {
                            if (c.enemies.some(en => en.npcId)) {
                                this._finishCombat('defeat');
                                return;
                            }
                            console.log('Gray-box: you died — resetting prototype HP');
                            c.playerHp = c.playerMaxHp;
                            this._drawHearts();
                        }
                    }
                }
            } else {
                ring.doneT += dt;
                if (ring.doneT > 140) {
                    this.particleLayer.removeChild(g);
                    g.destroy();
                    c.rings.splice(i, 1);
                }
            }
        }
    }

    // ---------------- Boss phases ----------------

    /** 50% HP: invulnerable roar, then faster, meaner, and chaining lunges. */
    _enterBossPhase2(e) {
        const BP = e.bossPattern || GameRenderer.BOSS_FALLBACK;
        e.phase = 2;
        e.invuln = true;
        e.state = 'roar';
        e.t = BP.phase_transition_ms || 1200;
        e.staggerT = 0;
        e.vulnMul = 1;
        e.telegraph.clear();
        this._clearRings();   // the arena resets for the new phase
        if (this.combat) this.combat.shake = Math.max(this.combat.shake, 14);
        window.forgeAudio?.playSfx('clash', { volume: 1.0 });
        window.forgeAudio?.playSfx('windup', { volume: 0.8 });
        // Fire-and-forget hook — no listener in v1
        window.dispatchEvent(new CustomEvent('boss-phase', { detail: { npcId: e.npcId, phase: 2 } }));
        this._drawBossBar();
    }

    _endRoar(e) {
        const BP = e.bossPattern || GameRenderer.BOSS_FALLBACK;
        const P = e.params;
        e.invuln = false;
        e.state = 'chase';
        e.t = 0;
        e.params = Object.assign({}, P, {
            speed: P.speed * (BP.p2_speed_mul || 1.15),
            windup: Math.max(220, Math.round(P.windup * (BP.p2_windup_mul || 0.78))),
            stagger: BP.p2_stagger || P.stagger,
        });
        e.slamEvery = BP.slam_every_p2 || BP.slam_every || 6500;
        e.slamT = e.slamEvery;
        this._drawBossBar();
    }

    _updateCombat(dt) {
        const c = this.combat;
        c.iframes = Math.max(0, c.iframes - dt);
        c.dodgeCd = Math.max(0, c.dodgeCd - dt);
        c.attackCd = Math.max(0, c.attackCd - dt);
        c.shake = Math.max(0, c.shake - dt * 0.015);
        c.parry = Math.max(0, c.parry - dt);
        c.parryLag = Math.max(0, c.parryLag - dt);
        c.parryCd = Math.max(0, c.parryCd - dt);
        c.chargeLock = Math.max(0, c.chargeLock - dt);

        // Tick the player's action animation
        if (this.playerAction) {
            this.playerAction.t += dt;
            if (this.playerAction.t >= this.playerAction.total) this.playerAction = null;
        }

        const p = this.player;

        // --- Dodge roll: burst movement + i-frames ---
        if (c.dodge > 0) {
            c.dodge -= dt;
            const step = this.moveSpeed * 3.0 * dt / 1000;
            const tx = Math.max(0, Math.min(100, p.normalizedX + c.dodgeVec[0] * step));
            const ty = Math.max(0, Math.min(100, p.normalizedY + c.dodgeVec[1] * step));
            if (this.isWalkable(tx, ty)) { p.normalizedX = tx; p.normalizedY = ty; }
            p.sprite.alpha = 0.55;
            this.updatePlayerPosition();
            this.positionDirty = true;
        } else if (p.sprite.alpha !== 1 && !this.playerDead) {
            p.sprite.alpha = 1;
        }
        if (this.keys['space'] && c.dodgeCd <= 0 && c.dodge <= 0 &&
            c.parryLag <= 0 && c.chargeLock <= 0) {
            let dx = 0, dy = 0;
            if (this.keys['w'] || this.keys['arrowup']) dy -= 1;
            if (this.keys['s'] || this.keys['arrowdown']) dy += 1;
            if (this.keys['a'] || this.keys['arrowleft']) dx -= 1;
            if (this.keys['d'] || this.keys['arrowright']) dx += 1;
            if (dx === 0 && dy === 0) [dx, dy] = this._facingVector();
            const len = Math.hypot(dx, dy);
            c.dodgeVec = [dx / len, dy / len];
            c.dodge = 230;
            c.iframes = c.stats.iframeMs;    // trinkets widen the window
            c.dodgeCd = c.stats.dodgeCd;     // levels shorten the wait
            c.charging = false; c.chargeReady = false; c.jDownAt = 0;
            this._startPlayerAction('dodge', [40, 45, 50, 55, 50, 60]);
            this._spawnDust();
            window.forgeAudio?.playSfx('roll', { volume: 0.7 });
        }

        // --- Melee: light fires on the J keydown edge; the charge grows
        // underneath its recovery (hold ≥700 ms, release = heavy) ---
        if (this._jEdge) {
            this._jEdge = false;
            c.jDownAt = performance.now();
            c.charging = false;
            c.chargeReady = false;
            if (c.attackCd <= 0 && c.parryLag <= 0 && c.chargeLock <= 0) {
                this._swing(this._lightProfile());
            }
        }
        if (this.keys['j'] && c.jDownAt && c.stats.heavyUnlocked) {
            const held = performance.now() - c.jDownAt;
            if (held > 300 && !c.charging && !c.chargeReady) {
                c.charging = true;
                window.forgeAudio?.playSfx('windup', { volume: 0.4, throttleMs: 250 });
            }
            if (held > 700 && !c.chargeReady) {
                c.chargeReady = true;
                window.forgeAudio?.playSfx('clash', { volume: 0.35 });
            }
            if (held > 1600) this._releaseCharge();  // nobody holds J forever
        }
        // Charge glow on the player sprite (cleared when idle)
        if (!this.playerDead) {
            if (c.chargeReady) p.sprite.tint = 0xffe9a8;
            else if (c.charging) p.sprite.tint = (Math.floor(performance.now() / 90) % 2 === 0) ? 0xfff4cf : 0xffffff;
            else if (p.sprite.tint !== 0xffffff) p.sprite.tint = 0xffffff;
        }

        // --- Ground rings (ranged volleys + boss slams) may down the player ---
        this._updateRings(dt);
        if (!this.combat) return;   // a detonation ended the fight

        // --- Enemy AI ---
        let allDead = true;
        let allFlee = true;
        for (const e of c.enemies) {
            if (e.dead) {
                e.deathT += dt;
                const t = Math.min(1, e.deathT / 450);
                e.g.rotation = (Math.PI / 2) * t;
                e.g.alpha = 1 - 0.8 * t;
                e.shadow.alpha = 1 - t;
                continue;
            }
            allDead = false;

            const P = e.params || GameRenderer.ARCHETYPES.test;
            if (!P.flees) allFlee = false;

            e.flash = Math.max(0, e.flash - dt);
            if (e.flash > 0) e.g.tint = 0xffb0b0;
            else if (e.staggerT > 0) e.g.tint = 0xfff0a0;
            else if (e.invuln) e.g.tint = (Math.floor(e.t / 110) % 2 === 0) ? 0xff9d9d : 0xffffff;
            else e.g.tint = 0xffffff;

            const dx = p.normalizedX - e.nx, dy = p.normalizedY - e.ny;
            const dist = Math.hypot(dx, dy);
            const BP = e.bossPattern;

            // Boss slam clock ticks whatever the state
            if (BP && e.slamT !== null) e.slamT -= dt;

            if (e.staggerT > 0) {
                // Parried (or rocked by a heavy): wide open, punish window
                e.staggerT -= dt;
                e.telegraph.clear();
                if (e.staggerT <= 0) {
                    e.staggerT = 0;
                    e.vulnMul = 1;
                    e.state = 'recover';
                    e.t = 250;
                }
            } else if (e.state === 'roar') {
                // Phase transition: invulnerable, rooted, deafening
                e.t -= dt;
                e.telegraph.clear();
                if (e.t <= 0) this._endRoar(e);
            } else if (BP && e.slamT <= 0 && (e.state === 'chase' || e.state === 'recover')) {
                // Boss slam: telegraphed ground ring, then a long punish recover
                e.state = 'slam';
                e.t = BP.slam_windup || 900;
                e.slamT = e.slamEvery || BP.slam_every || 9000;
                this._spawnGroundRing(e.nx, e.ny, BP.slam_radius || 18, BP.slam_windup || 900, BP.slam_damage || 3);
                window.forgeAudio?.playSfx('windup', { volume: 0.8 });
            } else if (e.state === 'slam') {
                e.t -= dt;
                if (e.t <= 0) {
                    e.state = 'recover';
                    e.t = BP ? (BP.slam_recover || 1100) : P.recover;  // land your heavy here
                }
            } else if (e.state === 'chase') {
                if (P.flees) {
                    // Harmless: scramble away from the player, never attack
                    if (dist < 26) {
                        const ux = dx / (dist || 1), uy = dy / (dist || 1);
                        const stepF = P.speed * dt / 1000;
                        const nx2 = e.nx - ux * stepF;
                        const ny2 = e.ny - uy * stepF;
                        if (nx2 >= 1 && nx2 <= 99 && this.isWalkable(nx2, e.ny)) e.nx = nx2;
                        if (ny2 >= 1 && ny2 <= 99 && this.isWalkable(e.nx, ny2)) e.ny = ny2;
                    }
                } else if (P.ranged) {
                    // Ranged: hold a standoff band, then plant a ground ring
                    if (dist < (P.standoff || 10)) {
                        const ux = dx / (dist || 1), uy = dy / (dist || 1);
                        const stepR = P.speed * dt / 1000;
                        const nx2 = e.nx - ux * stepR;
                        const ny2 = e.ny - uy * stepR;
                        if (nx2 >= 1 && nx2 <= 99 && this.isWalkable(nx2, e.ny)) e.nx = nx2;
                        if (ny2 >= 1 && ny2 <= 99 && this.isWalkable(e.nx, ny2)) e.ny = ny2;
                    } else if (dist > P.engage) {
                        const step = P.speed * dt / 1000;
                        const nx2 = e.nx + (dx / dist) * step;
                        const ny2 = e.ny + (dy / dist) * step;
                        if (this.isWalkable(nx2, e.ny)) e.nx = nx2;
                        if (this.isWalkable(e.nx, ny2)) e.ny = ny2;
                    } else {
                        // The growing ring IS the telegraph — no wedge
                        e.state = 'windup';
                        e.t = P.windup;
                        e.aim = [dx / (dist || 1), dy / (dist || 1)];
                        this._spawnGroundRing(p.normalizedX, p.normalizedY,
                                              P.ringRadius || 8, P.windup, P.damage ?? 1);
                        window.forgeAudio?.playSfx('windup', { volume: 0.55, throttleMs: 200 });
                    }
                } else {
                    const circling = P.circles && dist < P.engage * 1.8 && dist > P.engage * 0.9 && e.circleT < 1600;
                    if (circling) {
                        // Stalkers orbit before committing — pressure without contact
                        e.circleT += dt;
                        const ux = dx / (dist || 1), uy = dy / (dist || 1);
                        const step = P.speed * 0.85 * dt / 1000 * e.strafeDir;
                        const nx2 = e.nx + (-uy) * step;
                        const ny2 = e.ny + ux * step;
                        if (this.isWalkable(nx2, ny2)) { e.nx = nx2; e.ny = ny2; }
                        else e.strafeDir *= -1;
                    } else if (dist > P.engage) {
                        const step = P.speed * dt / 1000;
                        const nx2 = e.nx + (dx / dist) * step;
                        const ny2 = e.ny + (dy / dist) * step;
                        if (this.isWalkable(nx2, e.ny)) e.nx = nx2;
                        if (this.isWalkable(e.nx, ny2)) e.ny = ny2;
                    } else {
                        e.state = 'windup';
                        e.t = P.windup;
                        e.circleT = 0;
                        e.aim = [dx / (dist || 1), dy / (dist || 1)];
                        // Phase-2 bosses chain their basic into a 3-lunge combo
                        e.comboLeft = (BP && e.phase >= 2) ? (e.comboCount || 3) - 1 : 0;
                        // Audio telegraph alongside the red wedge
                        window.forgeAudio?.playSfx('windup', { volume: 0.55, throttleMs: 200 });
                    }
                }
            } else if (e.state === 'windup') {
                e.t -= dt;
                if (P.ranged) {
                    e.telegraph.clear();   // the ring carries the telegraph
                    if (e.t <= 0) { e.state = 'recover'; e.t = P.recover; }
                } else {
                    // Telegraph: red wedge showing the incoming lunge
                    const tg = e.telegraph;
                    tg.clear();
                    const ang = Math.atan2(e.aim[1], e.aim[0]);
                    const r = ((P.lungeDist + 2) / 100) * this.worldWidth * 0.9;
                    tg.moveTo(0, 0);
                    tg.arc(0, 0, r, ang - 0.45, ang + 0.45);
                    tg.closePath();
                    tg.fill({ color: 0xef4444, alpha: 0.18 + 0.14 * Math.sin(e.t * 0.02) });
                    tg.x = e.g.x;
                    tg.y = e.g.y;
                    if (e.t <= 0) {
                        e.state = 'strike';
                        e.t = P.lungeMs;
                        e.lungeFrom = [e.nx, e.ny];
                        e.hitDone = false;
                        tg.clear();
                    }
                }
            } else if (e.state === 'strike') {
                e.t -= dt;
                const prog = 1 - Math.max(0, e.t) / P.lungeMs;
                const lx = e.lungeFrom[0] + e.aim[0] * P.lungeDist * prog;
                const ly = e.lungeFrom[1] + e.aim[1] * P.lungeDist * prog;
                e.nx = Math.max(0, Math.min(100, lx));
                e.ny = Math.max(0, Math.min(100, ly));
                // One hit chance per lunge
                if (!e.hitDone) {
                    const pd = Math.hypot(p.normalizedX - e.nx, p.normalizedY - e.ny);
                    if (pd < 4.5 && c.iframes <= 0 && c.dodge <= 0) {
                        e.hitDone = true;
                        if (c.parry > 0) {
                            // Read rewarded: negate, stagger the attacker,
                            // and open the double-damage punish window
                            this._staggerEnemy(e, P.stagger ?? 900);
                            c.parry = 0;
                            c.iframes = Math.max(c.iframes, 120);
                            c.hitstop = 140;
                            c.shake = Math.max(c.shake, 10);
                            this._parryFlash();
                            window.forgeAudio?.playSfx('clash', { volume: 1.0 });
                        } else {
                            const dmg = P.damage ?? 1;
                            c.playerHp -= dmg;
                            c.heartsLost += dmg;
                            c.hitsTaken = (c.hitsTaken || 0) + 1;
                            c.iframes = 900;
                            c.hitstop = 60;
                            c.shake = Math.max(c.shake, 8);
                            window.forgeAudio?.playSfx('hurt');
                            // Knock the player back
                            const kb = 7;
                            const px2 = p.normalizedX + e.aim[0] * kb;
                            const py2 = p.normalizedY + e.aim[1] * kb;
                            if (this.isWalkable(px2, py2)) {
                                p.normalizedX = px2;
                                p.normalizedY = py2;
                                this.updatePlayerPosition();
                                this.positionDirty = true;
                            }
                            this._drawHearts();
                            if (c.playerHp <= 0) {
                                const isReal = c.enemies.some(en => en.npcId);
                                if (isReal) {
                                    // Downed. Code marks them critical; the DM
                                    // narrates what the victors do next.
                                    this._finishCombat('defeat');
                                    return;
                                }
                                // Gray-box prototype: reset instead of killing
                                console.log('Gray-box: you died — resetting prototype HP');
                                c.playerHp = c.playerMaxHp;
                                this._drawHearts();
                            }
                        }
                    }
                }
                if (e.t <= 0) {
                    if (e.comboLeft > 0 && BP) {
                        // Phase-2 combo: straight back into the next lunge
                        e.comboLeft--;
                        e.state = 'combo';
                        e.t = BP.combo_gap || 180;
                    } else {
                        e.state = 'recover';
                        e.t = P.recover;
                    }
                }
            } else if (e.state === 'combo') {
                e.t -= dt;
                if (e.t <= 0) {
                    const d2 = Math.hypot(p.normalizedX - e.nx, p.normalizedY - e.ny) || 1;
                    e.aim = [(p.normalizedX - e.nx) / d2, (p.normalizedY - e.ny) / d2];
                    e.state = 'strike';
                    e.t = P.lungeMs;
                    e.lungeFrom = [e.nx, e.ny];
                    e.hitDone = false;
                }
            } else if (e.state === 'recover') {
                e.t -= dt;
                if (e.t <= 0) e.state = 'chase';
            }

            // Enemy attack animation: texture follows the AI state
            if (e.isSprite && e.attackReady) {
                let frame = null;
                if (e.state === 'windup') {
                    frame = e.t > e.params.windup * 0.45 ? 1 : 2;   // coil as t runs DOWN
                } else if (e.state === 'strike') {
                    frame = e.t > e.params.lungeMs * 0.5 ? 3 : 4;
                } else if (e.state === 'recover') {
                    frame = e.t > e.params.recover * 0.5 ? 5 : 6;
                } else if (e.state === 'slam' || e.state === 'roar') {
                    frame = 1;   // coiled — the threat pose
                }
                const want = frame ? e.attackFrames[frame] : e.baseTexture;
                if (want && e.g.texture !== want) e.g.texture = want;
            }

            // Apply enemy position; sprites face the player
            e.g.x = this.normalizedToWorldX(e.nx);
            e.g.y = this.normalizedToWorldY(e.ny);
            e.g.zIndex = e.g.y;
            if (e.isSprite && Math.abs(dx) > 0.5) {
                e.g.scale.x = Math.abs(e.g.scale.x) * (dx < 0 ? -1 : 1);
            }
            e.hpBar.x = e.g.x;
            e.hpBar.y = e.g.y - e.h - 14;
            this._fitShadow(e.shadow, e.g, e.g.y);
        }

        // Fizzle guard: a "fight" where everyone flees ends on its own —
        // 4 s of not chasing beggars is mercy, not stalemate
        if (!allDead && allFlee && c.enemies.length > 0) {
            c.fizzleT = (c.fizzleT || 0) + dt;
            if (c.fizzleT > 4000) {
                if (c.enemies.some(e => e.npcId)) this._finishCombat('fled');
                else this._endCombat(false);
                return;
            }
        } else {
            c.fizzleT = 0;
        }

        // Player flicker during i-frames (readable invulnerability)
        if (c.iframes > 0 && c.dodge <= 0 && !this.playerDead) {
            p.sprite.alpha = (Math.floor(c.iframes / 80) % 2 === 0) ? 0.5 : 1;
        }

        // Victory: every enemy dead and finished falling
        if (allDead && c.enemies.length > 0 && c.enemies.every(e => e.deathT > 700)) {
            if (c.enemies.some(e => e.npcId)) {
                this._finishCombat('victory');
            } else {
                this._endCombat(false);
            }
        }
    }

    // ================= Cinematics =================
    // The server queues/emits `cinematic` SSE events; playCinematic realizes
    // them: DOM overlay (bars/scrim/text) + canvas zoom/tint/shake/particles.
    // kill_cam is client-only (the killing blow on a boss). Weather is a
    // persistent state, not a moment — it routes to setWeather.

    static CINE = {
        title_card: { dur: 4200,  skipAfter: 400,  freeze: 'input', zoom: 1.06, blocksCombat: false },
        boss_intro: { dur: 3600,  skipAfter: 1200, freeze: 'world', zoom: 1.35, blocksCombat: true  },
        kill_cam:   { dur: 1500,  skipAfter: null, freeze: null,    zoom: 1.45, blocksCombat: false },
        vision:     { dur: 45000, skipAfter: 600,  freeze: null,    zoom: 1.04, blocksCombat: false },
    };

    _cineDom(classes, on) {
        if (!this.cineEl) return;
        for (const cls of String(classes).split(/\s+/)) {
            if (cls) this.cineEl.classList.toggle(cls, !!on);
        }
    }

    /** Single entry point from the SSE `cinematic` event. */
    playCinematic(p) {
        if (!p || !p.kind) return;
        if (p.kind === 'weather') { this.setWeather(p.mood || 'clear'); return; }
        const spec = GameRenderer.CINE[p.kind];
        if (!spec) return;
        if (!this.player || this.isLoading || !this.sceneReady) {
            this._queueForReady('cinematic', p);
            return;
        }
        // Client-side safety: a fight owns the screen. boss_intro precedes
        // its spawn, so it is the only one that can queue combat behind it.
        if (this.combat && p.kind !== 'kill_cam') return;
        if (this.cine) this._endCinematic(true);   // never stack

        const cine = {
            kind: p.kind, t: 0, dur: spec.dur, freeze: spec.freeze,
            skippable: spec.skipAfter !== null && spec.skipAfter !== undefined,
            skipAt: (spec.skipAfter == null) ? Infinity : performance.now() + spec.skipAfter,
            skipShown: false, textShown: false, textOut: false, barsOut: false,
            splashDone: false, moteT: 0,
            dimmed: [], bossSprite: null, bossScaleX: 0, bossScaleY: 0,
        };
        this.cine = cine;

        if (this.cineTitleEl) this.cineTitleEl.textContent = p.title || '';
        if (this.cineSubEl) this.cineSubEl.textContent = p.subtitle || '';

        // Audio (duck/hold/stinger) rides the SSE event itself — audio.js's
        // _handleCinematic owns it. game.js only fires frame-exact impacts.
        const ps = this.player.sprite;
        if (p.kind === 'title_card') {
            this._cineDom('boss thin-bars vignette show-text skippable flash', false);
            this._cineDom('active dim letterbox', true);
            this.cineFocus = { x: ps.x, y: ps.y };
        } else if (p.kind === 'boss_intro') {
            this._cineDom('show-text skippable flash', false);
            this._cineDom('active thin-bars letterbox boss vignette dim', true);
            const npc = p.npc_id ? this.npcs[p.npc_id] : null;
            if (npc && npc.sprite && !npc.sprite.destroyed) {
                // Push in on the boss; everyone else falls into shadow
                this.cineFocus = { x: npc.sprite.x, y: npc.sprite.y - npc.sprite.height * 0.4 };
                cine.bossSprite = npc.sprite;
                cine.bossScaleX = npc.sprite.scale.x;
                cine.bossScaleY = npc.sprite.scale.y;
                npc.sprite.scale.set(npc.sprite.scale.x * 1.05, npc.sprite.scale.y * 1.05);
                for (const id in this.npcs) {
                    const other = this.npcs[id];
                    if (other !== npc && other.sprite && other.status !== 'dead') {
                        cine.dimmed.push({ sprite: other.sprite, alpha: other.sprite.alpha });
                        other.sprite.alpha = 0.35;
                    }
                }
            } else {
                this.cineFocus = { x: ps.x, y: ps.y };
            }
            this.cineTintTarget = { r: 1.0, g: 0.86, b: 0.86 };
        } else if (p.kind === 'vision') {
            this._cineDom('dim letterbox thin-bars boss show-text skippable flash', false);
            this._cineDom('active vignette', true);
            this.cineTintTarget = { r: 0.72, g: 0.76, b: 1.00 };
        }
    }

    /** Client-only: the killing blow on a boss earns 1.5 s of slow motion. */
    _startKillCam(enemy) {
        if (this.cine || !this.combat) return;
        this.combat.hitstop = 120;
        this.cineFocus = {
            x: (this.player.sprite.x + enemy.g.x) / 2,
            y: (this.player.sprite.y + enemy.g.y) / 2,
        };
        this.cine = {
            kind: 'kill_cam', t: 0, dur: 1500,
            skippable: false, skipAt: Infinity, freeze: null,
            skipShown: false, textShown: false, textOut: false, barsOut: false,
            splashDone: false, moteT: 0, dimmed: [], bossSprite: null,
        };
        this.cineShake = 14;
        this.timeScale = 0.18;
        this._cineDom('active flash', true);
        setTimeout(() => this._cineDom('flash', false), 120);
        window.forgeAudio?.playSfx?.('boom', { volume: 1.0 });
        window.forgeAudio?.duckFor?.(0.15, 1500);
    }

    /** Advance the active moment. Runs on REAL time — the show must finish. */
    _updateCinematic(dt) {
        if (this.cineShake > 0) this.cineShake = Math.max(0, this.cineShake - dt * 0.02);
        const c = this.cine;
        if (!c) return;
        c.t += dt;
        const audio = window.forgeAudio;

        if (c.skippable && !c.skipShown && performance.now() >= c.skipAt) {
            c.skipShown = true;
            this._cineDom('skippable', true);
        }

        if (c.kind === 'title_card') {
            const k = Math.min(1, c.t / c.dur);
            this.cineZoom = 1 + 0.06 * (1 - Math.pow(1 - k, 2));
            if (!c.textShown && c.t >= 520) {
                c.textShown = true;
                this._cineDom('show-text', true);
            }
            if (!c.textOut && c.t >= 3600) { c.textOut = true; this._cineDom('show-text', false); }
            if (!c.barsOut && c.t >= 4000) { c.barsOut = true; this._cineDom('letterbox dim', false); }
        } else if (c.kind === 'boss_intro') {
            if (c.t <= 1400) {
                const k = c.t / 1400;
                this.cineZoom = 1 + 0.35 * (1 - Math.pow(1 - k, 2));
            } else if (c.t >= c.dur - 500) {
                const k = Math.min(1, (c.t - (c.dur - 500)) / 500);
                this.cineZoom = 1.35 - 0.35 * k;
            } else {
                this.cineZoom = 1.35;
            }
            if (!c.splashDone && c.t >= 900) {
                c.splashDone = true;
                this._cineDom('show-text', true);
                this.cineShake = 9;
                audio?.playSfx?.('boom', { volume: 1.0 });
            }
        } else if (c.kind === 'vision') {
            this.cineZoom = 1 + 0.04 * Math.min(1, c.t / 20000);
            c.moteT -= dt;
            if (c.moteT <= 0) {
                c.moteT = 220;
                this._spawnVisionMote();
            }
        } else if (c.kind === 'kill_cam') {
            if (c.t < 900) this.timeScale = 0.18;
            else if (c.t < 1300) this.timeScale = 0.18 + 0.82 * ((c.t - 900) / 400);
            else this.timeScale = 1.0;
            if (c.t < 180) this.cineZoom = 1 + 0.45 * (c.t / 180);
            else if (c.t >= c.dur - 250) this.cineZoom = 1 + 0.45 * Math.max(0, 1 - (c.t - (c.dur - 250)) / 250);
            else this.cineZoom = 1.45;
        }

        if (c.t >= c.dur) this._endCinematic(true);
    }

    /** Drifting pale motes for the vision treatment (cap 40, pooled with dust). */
    _spawnVisionMote() {
        let count = 0;
        for (const p of this.particles) if (p.mote) count++;
        if (count >= 40) return;
        const g = new PIXI.Graphics();
        g.circle(0, 0, 1 + Math.random());
        g.fill({ color: 0xdfe6ff, alpha: 1 });
        g.x = this.camera.x + Math.random() * this.app.screen.width;
        g.y = this.camera.y + Math.random() * this.app.screen.height;
        const startAlpha = 0.22;
        g.alpha = startAlpha;
        this.particleLayer.addChild(g);
        this.particles.push({
            g, vx: (Math.random() - 0.5) * 0.004, vy: -0.006,
            life: 0, maxLife: 3200, startAlpha, mote: true,
        });
    }

    /** Tear the moment down and hand the stage back (spawns deferred combat). */
    _endCinematic(silent) {
        const c = this.cine;
        if (!c) return;
        this.cine = null;
        if (this.cineEl) {
            this._cineDom('dim letterbox thin-bars boss vignette show-text skippable flash', false);
            this.cineEl.classList.remove('active');   // 350 ms CSS fade
        }
        this.cineZoom = 1;
        this.cineFocus = null;
        this.cineTintTarget = { r: 1, g: 1, b: 1 };
        this.timeScale = 1;
        this.cineShake = 0;
        for (const d of (c.dimmed || [])) {
            if (d.sprite && !d.sprite.destroyed) d.sprite.alpha = d.alpha;
        }
        if (c.bossSprite && !c.bossSprite.destroyed) {
            c.bossSprite.scale.set(c.bossScaleX, c.bossScaleY);
        }
        const audio = window.forgeAudio;
        audio?.holdVoiceQueue?.(false);
        audio?.duckFor?.(1);
        if (c.kind === 'vision') audio?.playSfx?.('whoosh', { volume: 0.35 });
        if (this._visionEndTimer) {
            clearTimeout(this._visionEndTimer);
            this._visionEndTimer = null;
        }
        if (this._pendingCombat) {
            const pc = this._pendingCombat;
            this._pendingCombat = null;
            this.startCombat(pc.enemies, pc.boss);
        }
        if (!silent) console.log('Cinematic skipped');
    }

    /** The turn that opened a vision ended — let it linger, then fade.
     *  index.html calls this from its stream `finally`. */
    endVision(delayMs = 0) {
        if (!this.cine || this.cine.kind !== 'vision') return;
        if (!delayMs) { this._endCinematic(true); return; }
        if (this._visionEndTimer) return;   // already scheduled
        this._visionEndTimer = setTimeout(() => {
            this._visionEndTimer = null;
            if (this.cine && this.cine.kind === 'vision') this._endCinematic(true);
        }, delayMs);
    }

    // ================= Weather =================
    // Screen-space particle field + a tint MULTIPLIER (composes with the
    // day/night tint). Driven by the state event; 'clear'/null means none.

    static WEATHER = {
        rain:   { count: 220, tint: [0.82, 0.86, 0.95], ambience: 'amb_rain' },
        snow:   { count: 140, tint: [0.92, 0.95, 1.00], ambience: 'amb_wind' },
        fog:    { count: 6,   tint: [0.88, 0.90, 0.92], ambience: 'amb_wind' },
        embers: { count: 70,  tint: [1.00, 0.90, 0.82], ambience: null },
        ash:    { count: 90,  tint: [0.86, 0.85, 0.84], ambience: null },
        clear:  { count: 0,   tint: [1, 1, 1],          ambience: null },
    };

    /** Idempotent: the state event re-sends the label every turn. */
    setWeather(label) {
        const name = (label && GameRenderer.WEATHER[label]) ? label : 'clear';
        if (name === this.weather) return;
        this.weather = name;
        const W = GameRenderer.WEATHER[name];
        this.weatherTintTarget = { r: W.tint[0], g: W.tint[1], b: W.tint[2] };
        if (name === 'clear') {
            this._weatherFadeTarget = 0;   // fade out, particles culled at ~0
        } else {
            this._buildWeather(name);
        }
        window.forgeAudio?.setAmbience?.(W.ambience || null);
    }

    _buildWeather(label) {
        if (!this.weatherLayer) return;
        for (const p of this._weatherParts) {
            if (p.g && !p.g.destroyed) p.g.destroy();
        }
        this.weatherLayer.removeChildren();
        this._weatherParts = [];
        const W = GameRenderer.WEATHER[label] || GameRenderer.WEATHER.clear;
        const sw = this.app.screen.width, sh = this.app.screen.height;
        const count = Math.min(240, W.count);
        for (let i = 0; i < count; i++) {
            const g = new PIXI.Graphics();
            let vx = 0, vy = 0, drift = 0, margin = 24;
            if (label === 'rain') {
                g.rect(0, 0, 1, 14);
                g.fill({ color: 0x9fb6d4, alpha: 0.35 });
                vx = -60 * (0.9 + Math.random() * 0.2);
                vy = 900 * (0.9 + Math.random() * 0.2);
            } else if (label === 'snow') {
                g.circle(0, 0, 1.5 + Math.random() * 1.5);
                g.fill({ color: 0xffffff, alpha: 0.55 });
                vy = 70 * (0.8 + Math.random() * 0.4);
                drift = 25;
            } else if (label === 'fog') {
                g.ellipse(0, 0, sw * 0.45, sh * 0.175);
                g.fill({ color: 0xffffff, alpha: 0.06 });
                vx = (8 + Math.random() * 8) * (Math.random() < 0.5 ? -1 : 1);
                margin = sw * 0.5;
            } else if (label === 'embers') {
                g.circle(0, 0, 1 + Math.random() * 1.5);
                g.fill({ color: 0xff8844, alpha: 0.7 });
                g.blendMode = 'add';
                vx = (Math.random() - 0.5) * 40;
                vy = -60 * (0.7 + Math.random() * 0.6);
            } else if (label === 'ash') {
                g.rect(0, 0, 2, 2);
                g.fill({ color: 0xd7d3cc, alpha: 0.4 });
                vx = (Math.random() - 0.5) * 30;
                vy = 45 * (0.8 + Math.random() * 0.4);
            }
            g.x = Math.random() * sw;
            g.y = Math.random() * sh;
            this.weatherLayer.addChild(g);
            this._weatherParts.push({
                g, vx, vy, drift, margin,
                phase: Math.random() * Math.PI * 2, baseAlpha: g.alpha,
            });
        }
        this.weatherLayer.alpha = 0;   // cross-fade in
        this._weatherFadeTarget = count > 0 ? 1 : 0;
    }

    _updateWeather(dt) {
        // Tint multiplier eases whether or not particles exist
        const k = Math.min(1, dt / 1200);
        const wt = this.weatherTint, wtt = this.weatherTintTarget;
        wt.r += (wtt.r - wt.r) * k;
        wt.g += (wtt.g - wt.g) * k;
        wt.b += (wtt.b - wt.b) * k;
        if (!this.weatherLayer) return;
        const target = this._weatherFadeTarget || 0;
        this.weatherLayer.alpha += (target - this.weatherLayer.alpha) * k;
        if (target === 0 && this.weatherLayer.alpha < 0.02 && this._weatherParts.length) {
            for (const p of this._weatherParts) {
                if (p.g && !p.g.destroyed) p.g.destroy();
            }
            this.weatherLayer.removeChildren();
            this._weatherParts = [];
            return;
        }
        if (!this._weatherParts.length) return;
        const sw = this.app.screen.width, sh = this.app.screen.height;
        const s = dt / 1000;
        const embers = this.weather === 'embers';
        for (const p of this._weatherParts) {
            p.phase += s * 2;
            p.g.x += (p.vx + (p.drift ? Math.sin(p.phase) * p.drift : 0)) * s;
            p.g.y += p.vy * s;
            if (embers) p.g.alpha = p.baseAlpha * (0.6 + 0.4 * Math.sin(p.phase * 3));
            // Wrap: the field is stable and allocation-free
            const m = p.margin;
            if (p.g.x < -m) p.g.x = sw + m;
            else if (p.g.x > sw + m) p.g.x = -m;
            if (p.g.y > sh + m) p.g.y = -m;
            else if (p.g.y < -m) p.g.y = sh + m;
        }
    }

    // ================= Progression feel =================

    /** The moment a level lands: gold ring of particles + glow + floater.
     *  index.html calls this on the level_up SSE / combat REST response. */
    spawnLevelUpBurst(level) {
        this.spawnFloater(level ? `Level ${level}` : 'Level up!', 0xfbbf24);
        if (!this.player || !this.player.sprite) return;
        const cx = this.player.sprite.x;
        const cy = this.player.sprite.y - this.player.sprite.height * 0.5;
        // Brief golden glow under the ring
        const glow = new PIXI.Graphics();
        glow.circle(0, 0, Math.max(20, this.player.sprite.height * 0.45));
        glow.fill({ color: 0xfbbf24, alpha: 1 });
        glow.x = cx;
        glow.y = cy;
        glow.alpha = 0.3;
        this.particleLayer.addChild(glow);
        this.particles.push({ g: glow, vx: 0, vy: -0.004, life: 0, maxLife: 700, startAlpha: 0.3 });
        for (let i = 0; i < 14; i++) {
            const a = (i / 14) * Math.PI * 2;
            const g = new PIXI.Graphics();
            g.circle(0, 0, 3);
            g.fill({ color: 0xfbbf24, alpha: 0.9 });
            g.x = cx;
            g.y = cy;
            this.particleLayer.addChild(g);
            this.particles.push({
                g, vx: Math.cos(a) * 0.06, vy: Math.sin(a) * 0.06,
                life: 0, maxLife: 900, startAlpha: 0.9,
            });
        }
    }

    // A glyph, a word, a colour — never a number, never a DC
    static CHECK_CUE = {
        critical_success: ['⚄ Success', 0x4ade80],
        success:          ['⚄ Success', 0x4ade80],
        partial:          ['⚄ Barely',  0xfbbf24],
        failure:          ['⚄ Failed',  0xf87171],
        critical_failure: ['⚄ Failed',  0xef4444],
    };

    /** Subtle dice floater for the check_result SSE event. */
    showCheckResult(payload) {
        const outcome = typeof payload === 'string' ? payload : (payload && payload.outcome);
        const cue = GameRenderer.CHECK_CUE[outcome];
        if (cue) this.spawnFloater(cue[0], cue[1]);
    }

    updatePlayerPosition() {
        if (!this.player || !this.player.sprite) return;

        // Convert 0-100 coordinates to actual world pixels. The ground Y is
        // authoritative for depth-sorting and the shadow; the visible sprite
        // additionally rides the walk-bob offset.
        const groundY = this.normalizedToWorldY(this.player.normalizedY);
        this._playerGroundY = groundY;
        this.player.sprite.x = this.normalizedToWorldX(this.player.normalizedX);
        this.player.sprite.y = groundY + (this.bobOffset || 0);
        this.player.sprite.zIndex = groundY;
    }

    // Coordinate conversions (normalized 0-100 <-> world pixels)
    normalizedToWorldX(normalized) {
        return (normalized / 100) * this.worldWidth;
    }

    normalizedToWorldY(normalized) {
        return (normalized / 100) * this.worldHeight;
    }

    worldToNormalizedX(world) {
        return (world / this.worldWidth) * 100;
    }

    worldToNormalizedY(world) {
        return (world / this.worldHeight) * 100;
    }

    onResize() {
        // Recalculate world size based on new screen size
        if (this.background) {
            const texture = this.background.texture;
            const textureAspect = texture.width / texture.height;
            const screenAspect = this.app.screen.width / this.app.screen.height;
            const scaleFactor = 1.5;

            if (textureAspect > screenAspect) {
                this.worldHeight = this.app.screen.height * scaleFactor;
                this.worldWidth = this.worldHeight * textureAspect;
            } else {
                this.worldWidth = this.app.screen.width * scaleFactor;
                this.worldHeight = this.worldWidth / textureAspect;
            }

            this.background.width = this.worldWidth;
            this.background.height = this.worldHeight;
        }

        // Reposition all sprites
        if (this.player) {
            this.player.sprite.x = this.normalizedToWorldX(this.player.normalizedX);
            this.player.sprite.y = this.normalizedToWorldY(this.player.normalizedY);
        }

        for (const npcId in this.npcs) {
            const npc = this.npcs[npcId];
            npc.sprite.x = this.normalizedToWorldX(npc.data.x);
            npc.sprite.y = this.normalizedToWorldY(npc.data.y);
            this._fitShadow(npc.shadow, npc.sprite);
            npc.wander = null; // world size changed — rebase wander state
        }

        // Doors and object markers follow the resized world
        for (const d of this.doors) {
            if (d.g && !d.g.destroyed) {
                d.g.x = this.normalizedToWorldX(d.x);
                d.g.y = this.normalizedToWorldY(d.y);
            }
            if (d.label && !d.label.destroyed) {
                d.label.x = this.normalizedToWorldX(d.x);
                d.label.y = this.normalizedToWorldY(d.y) - 38;
            }
        }
        for (const o of this.objects) {
            if (o.g && !o.g.destroyed) {
                o.g.x = this.normalizedToWorldX(o.x);
                o.g.y = this.normalizedToWorldY(o.y);
                o.g.zIndex = o.g.y;
            }
        }

        // Weather field is sized to the screen; boss bar is centred on it
        if (this.weather !== 'clear') {
            this._buildWeather(this.weather);
            this.weatherLayer.alpha = 1;
        }
        if (this.combat && this.combat.boss) this._drawBossBar();

        this._redrawCollisionOverlay();

        // Re-center camera
        this.centerCameraOnPlayer();
    }

    async refresh() {
        if (this.locationId && this.playerId) {
            await this.loadLocation(this.locationId, this.playerId);
        }
    }

    getPlayerPosition() {
        if (!this.player) return null;
        return {
            x: this.player.normalizedX,
            y: this.player.normalizedY,
            direction: this.player.direction
        };
    }
}

window.GameRenderer = GameRenderer;
