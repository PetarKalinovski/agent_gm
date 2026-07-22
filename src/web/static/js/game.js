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

        // Combat gray-box prototype (Ctrl+K spawns a test enemy)
        this.combat = null;

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

        // Collision-editor overlay renders above everything in the world
        this.collisionOverlay = new PIXI.Container();
        this.worldContainer.addChild(this.collisionOverlay);

        // UI layer (doesn't move with camera)
        this.uiLayer = new PIXI.Container();
        this.app.stage.addChild(this.uiLayer);

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
            if (document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA') {
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

            // Combat verbs: space = dodge, j = attack
            if (e.key === ' ' || key === 'j') {
                this.keys[e.key === ' ' ? 'space' : 'j'] = true;
                if (this.combat) e.preventDefault();
            }
        };

        this._onKeyUp = (e) => {
            const key = e.key.toLowerCase();
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
        this.backgroundLayer.removeChildren();
        this.npcLayer.removeChildren();
        this.shadowLayer.removeChildren();
        this.particleLayer.removeChildren();
        this.collisionOverlay.removeChildren();
        this.particles = [];
        // Keep the player, but maybe hide them or move them to center
        if (this.player) {
            this.player.normalizedX = 50;
            this.player.normalizedY = 50;
            this.updatePlayerPosition();
        }

        this.showLoading(true);

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

            // Load player sprite
            await this.loadPlayer(data.player);

            // Load NPC sprites
            await this.loadNPCs(data.npcs);

            // Center camera on player initially
            this.centerCameraOnPlayer();

            console.log(`Loaded location: ${data.location_name}`);

            // Assets still generating server-side? Poll until the scene is
            // complete (placeholders show meanwhile, the game stays playable)
            if (data.pending && (this.pendingRetries || 0) < 20) {
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
            }

        } catch (error) {
            console.error('Failed to load location:', error);
            this.showError(error.message);
        } finally {
            this.isLoading = false;
            this.showLoading(false);
        }
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

    async loadPlayer(playerData) {
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
        await this.preloadAllSprites();

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

        this._updateSceneTint(this.app.ticker.deltaMS);

        // Skip movement if player is dead
        if (this.playerDead) {
            this.updateCamera();
            return;
        }

        const deltaTime = this.app.ticker.deltaMS;

        // Hitstop: freeze the whole scene for a few frames on impact
        if (this.combat && this.combat.hitstop > 0) {
            this.combat.hitstop -= deltaTime;
            return;
        }
        const step = this.moveSpeed * (deltaTime / 1000); // frame-rate independent

        if (this.exitPromptCooldown > 0) this.exitPromptCooldown -= deltaTime;

        let dx = 0;
        let dy = 0;
        let newDir = this.player.direction; // Default to current

        // Handle WASD
        if (this.keys['w'] || this.keys['arrowup']) { dy = -step; newDir = 'back'; }
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
                // Exits are locked while combat is active
                if (!this.combat && this.edgePushTimer >= this.edgePushThreshold && this.exitPromptCooldown <= 0) {
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
        if (this.combat) this._updateCombat(deltaTime);
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

        // Combat screenshake (decays in _updateCombat)
        if (this.combat && this.combat.shake > 0) {
            this.worldContainer.x += (Math.random() - 0.5) * this.combat.shake * 2;
            this.worldContainer.y += (Math.random() - 0.5) * this.combat.shake * 2;
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
            const url = `/api/assets/sprite/player/${this.playerId}/${direction}`;
            const texture = await PIXI.Assets.load(url);
            this.player.sprites[direction] = texture;
            this.player.sprite.texture = texture;
        } catch (error) {
            console.error(`Failed to load player sprite for direction ${direction}:`, error);
        }
    }

    async preloadWalkSprites(direction) {
        // Preload idle sprite for direction if not cached
        if (!this.player.sprites[direction]) {
            try {
                const idleUrl = `/api/assets/sprite/player/${this.playerId}/${direction}`;
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
                    const url = `/api/assets/sprite/player/${this.playerId}/${direction}_walk${frame}`;
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

    async preloadAllSprites() {
        const directions = ['front', 'back', 'left', 'right'];
        const frames = Array.from({ length: this.walkFrameCount }, (_, i) => i + 1);
        // Add .png to the base if you like, or just in the loop
        const playerUrlBase = `/api/assets/sprite/player/${this.playerId}`;

        const loadJobs = [];

        directions.forEach(dir => {
            // LOAD IDLE (Append .png) — sprite may still be generating (404):
            // fall back to placeholder, the pending-poll reload picks it up later
            loadJobs.push(
                PIXI.Assets.load(`${playerUrlBase}/${dir}.png`).then(tex => {
                    this.player.sprites[dir] = tex;
                }).catch(() => {
                    this.player.sprites[dir] = null;
                })
            );

            // LOAD WALK (Append .png)
            this.player.walkSprites[dir] = {};
            frames.forEach(f => {
                loadJobs.push(
                    PIXI.Assets.load(`${playerUrlBase}/${dir}_walk${f}.png`).then(tex => {
                        this.player.walkSprites[dir][f] = tex;
                    }).catch(e => {
                        this.player.walkSprites[dir][f] = this.player.sprites[dir];
                    })
                );
            });
        });

        await Promise.all(loadJobs);
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

        // Pull texture from our assigned dictionary
        if (!this.isMoving) {
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

    showLoading(show) {
        const existing = this.uiLayer.getChildByName('loading');
        if (existing) this.uiLayer.removeChild(existing);

        if (show) {
            const loading = new PIXI.Text({
                text: 'Generating scene...',
                style: {
                    fontFamily: 'Arial',
                    fontSize: 20,
                    fill: 0x6366f1,
                }
            });
            loading.anchor.set(0.5);
            loading.x = this.app.screen.width / 2;
            loading.y = this.app.screen.height / 2;
            loading.name = 'loading';
            this.uiLayer.addChild(loading);
        }
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
        this.worldContainer.tint =
            (Math.round(c.r * 255) << 16) | (Math.round(c.g * 255) << 8) | Math.round(c.b * 255);
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

    // ================= Combat gray-box prototype =================
    // Ctrl+K spawns a gray rectangle enemy in the room. Space = dodge-roll
    // (i-frames), J = melee attack. Pure client-side: this is the feel
    // prototype for in-world combat — no art, no DM integration yet.

    _spawnTestEnemy() {
        if (!this.player || this.playerDead || this.isLoading) return;
        if (!this.combat) {
            this.combat = {
                enemies: [], playerHp: 5, playerMaxHp: 5,
                iframes: 0, dodge: 0, dodgeVec: [0, 1], dodgeCd: 0,
                attackCd: 0, hitstop: 0, shake: 0,
            };
            this._drawHearts();
        }

        // Spawn on walkable ground away from the player
        let ex = Math.min(95, this.player.normalizedX + 18);
        let ey = this.player.normalizedY;
        for (let t = 0; t < 12; t++) {
            const a = Math.random() * Math.PI * 2;
            const cx = this.player.normalizedX + Math.cos(a) * 18;
            const cy = this.player.normalizedY + Math.sin(a) * 12;
            if (cx >= 3 && cx <= 97 && cy >= 3 && cy <= 97 && this.isWalkable(cx, cy)) {
                ex = cx; ey = cy;
                break;
            }
        }

        const h = this.worldHeight * 0.11;
        const g = new PIXI.Graphics();
        g.rect(-h * 0.28, -h, h * 0.56, h);
        g.fill({ color: 0x8a8f9c });
        g.rect(-h * 0.28, -h, h * 0.56, h);
        g.stroke({ color: 0x2c313a, width: 2 });
        const hpBar = new PIXI.Graphics();
        g.addChild(hpBar);
        this.entityLayer.addChild(g);

        const shadow = this._makeShadow();
        this.shadowLayer.addChild(shadow);
        const telegraph = new PIXI.Graphics();
        this.particleLayer.addChild(telegraph);

        const enemy = {
            g, hpBar, shadow, telegraph, h,
            nx: ex, ny: ey, hp: 5, maxHp: 5,
            state: 'chase', t: 0, aim: [0, 0], lungeFrom: null,
            dead: false, deathT: 0, flash: 0,
        };
        this._drawEnemyHp(enemy);
        this.combat.enemies.push(enemy);
        console.log('Gray-box enemy spawned (Space = dodge, J = attack)');
    }

    _drawEnemyHp(enemy) {
        const bar = enemy.hpBar;
        bar.clear();
        const w = enemy.h * 0.7;
        bar.rect(-w / 2, -enemy.h - 12, w, 5);
        bar.fill({ color: 0x1f232b });
        const frac = Math.max(0, enemy.hp / enemy.maxHp);
        if (frac > 0) {
            bar.rect(-w / 2, -enemy.h - 12, w * frac, 5);
            bar.fill({ color: 0xef4444 });
        }
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
        for (const e of this.combat.enemies) {
            this.entityLayer.removeChild(e.g);
            this.shadowLayer.removeChild(e.shadow);
            this.particleLayer.removeChild(e.telegraph);
            e.g.destroy({ children: true });
            e.shadow.destroy();
            e.telegraph.destroy();
        }
        const hearts = this.uiLayer.getChildByName('combat-hearts');
        if (hearts) this.uiLayer.removeChild(hearts);
        this.combat = null;
        if (!silent) console.log('Combat over');
    }

    _facingVector() {
        switch (this.player.direction) {
            case 'left': return [-1, 0];
            case 'right': return [1, 0];
            case 'back': return [0, -1];
            default: return [0, 1];
        }
    }

    _updateCombat(dt) {
        const c = this.combat;
        c.iframes = Math.max(0, c.iframes - dt);
        c.dodgeCd = Math.max(0, c.dodgeCd - dt);
        c.attackCd = Math.max(0, c.attackCd - dt);
        c.shake = Math.max(0, c.shake - dt * 0.015);

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
        if (this.keys['space'] && c.dodgeCd <= 0 && c.dodge <= 0) {
            let dx = 0, dy = 0;
            if (this.keys['w'] || this.keys['arrowup']) dy -= 1;
            if (this.keys['s'] || this.keys['arrowdown']) dy += 1;
            if (this.keys['a'] || this.keys['arrowleft']) dx -= 1;
            if (this.keys['d'] || this.keys['arrowright']) dx += 1;
            if (dx === 0 && dy === 0) [dx, dy] = this._facingVector();
            const len = Math.hypot(dx, dy);
            c.dodgeVec = [dx / len, dy / len];
            c.dodge = 230;
            c.iframes = 350;
            c.dodgeCd = 750;
            this._spawnDust();
        }

        // --- Melee attack: arc in facing direction ---
        if (this.keys['j'] && c.attackCd <= 0) {
            c.attackCd = 380;
            const [fx, fy] = this._facingVector();
            // Visual: white arc wedge that fades
            const arc = new PIXI.Graphics();
            const r = (9 / 100) * this.worldWidth * 0.9;
            const ang = Math.atan2(fy, fx);
            arc.moveTo(0, 0);
            arc.arc(0, 0, r, ang - 0.9, ang + 0.9);
            arc.closePath();
            arc.fill({ color: 0xffffff, alpha: 0.35 });
            arc.x = p.sprite.x;
            arc.y = p.sprite.y - p.sprite.height * 0.35;
            this.particleLayer.addChild(arc);
            this.particles.push({ g: arc, vx: 0, vy: 0, life: 0, maxLife: 130, startAlpha: 0.35 });

            // Hit test every live enemy
            for (const e of c.enemies) {
                if (e.dead) continue;
                const dx = e.nx - p.normalizedX, dy = e.ny - p.normalizedY;
                const dist = Math.hypot(dx, dy);
                if (dist > 10) continue;
                const dot = (dx * fx + dy * fy) / (dist || 1);
                if (dot < 0.25) continue; // outside the frontal arc
                e.hp -= 1;
                e.flash = 90;
                c.hitstop = 70;
                c.shake = Math.max(c.shake, 5);
                // Knockback away from the player
                const kn = 6;
                const kx = e.nx + (dx / (dist || 1)) * kn;
                const ky = e.ny + (dy / (dist || 1)) * kn;
                if (this.isWalkable(kx, ky)) { e.nx = kx; e.ny = ky; }
                this._drawEnemyHp(e);
                if (e.hp <= 0) { e.dead = true; e.deathT = 0; e.telegraph.clear(); }
            }
        }

        // --- Enemy AI ---
        let allDead = true;
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

            e.flash = Math.max(0, e.flash - dt);
            e.g.tint = e.flash > 0 ? 0xffb0b0 : 0xffffff;

            const dx = p.normalizedX - e.nx, dy = p.normalizedY - e.ny;
            const dist = Math.hypot(dx, dy);

            if (e.state === 'chase') {
                if (dist > 7) {
                    const step = 6.5 * dt / 1000;
                    const nx2 = e.nx + (dx / dist) * step;
                    const ny2 = e.ny + (dy / dist) * step;
                    if (this.isWalkable(nx2, e.ny)) e.nx = nx2;
                    if (this.isWalkable(e.nx, ny2)) e.ny = ny2;
                } else {
                    e.state = 'windup';
                    e.t = 480;
                    e.aim = [dx / (dist || 1), dy / (dist || 1)];
                }
            } else if (e.state === 'windup') {
                e.t -= dt;
                // Telegraph: red wedge showing the incoming lunge
                const tg = e.telegraph;
                tg.clear();
                const ang = Math.atan2(e.aim[1], e.aim[0]);
                const r = (11 / 100) * this.worldWidth * 0.9;
                tg.moveTo(0, 0);
                tg.arc(0, 0, r, ang - 0.45, ang + 0.45);
                tg.closePath();
                tg.fill({ color: 0xef4444, alpha: 0.18 + 0.14 * Math.sin(e.t * 0.02) });
                tg.x = e.g.x;
                tg.y = e.g.y;
                if (e.t <= 0) {
                    e.state = 'strike';
                    e.t = 170;
                    e.lungeFrom = [e.nx, e.ny];
                    e.hitDone = false;
                    tg.clear();
                }
            } else if (e.state === 'strike') {
                e.t -= dt;
                const prog = 1 - Math.max(0, e.t) / 170;
                const lunge = 9;
                const lx = e.lungeFrom[0] + e.aim[0] * lunge * prog;
                const ly = e.lungeFrom[1] + e.aim[1] * lunge * prog;
                e.nx = Math.max(0, Math.min(100, lx));
                e.ny = Math.max(0, Math.min(100, ly));
                // One hit chance per lunge
                if (!e.hitDone) {
                    const pd = Math.hypot(p.normalizedX - e.nx, p.normalizedY - e.ny);
                    if (pd < 4.5 && c.iframes <= 0 && c.dodge <= 0) {
                        e.hitDone = true;
                        c.playerHp -= 1;
                        c.iframes = 900;
                        c.hitstop = 60;
                        c.shake = Math.max(c.shake, 8);
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
                            // Prototype: reset instead of killing the character
                            console.log('Gray-box: you died — resetting prototype HP');
                            c.playerHp = c.playerMaxHp;
                            this._drawHearts();
                        }
                    }
                }
                if (e.t <= 0) { e.state = 'recover'; e.t = 550; }
            } else if (e.state === 'recover') {
                e.t -= dt;
                if (e.t <= 0) e.state = 'chase';
            }

            // Apply enemy position
            e.g.x = this.normalizedToWorldX(e.nx);
            e.g.y = this.normalizedToWorldY(e.ny);
            e.g.zIndex = e.g.y;
            this._fitShadow(e.shadow, e.g, e.g.y);
        }

        // Player flicker during i-frames (readable invulnerability)
        if (c.iframes > 0 && c.dodge <= 0 && !this.playerDead) {
            p.sprite.alpha = (Math.floor(c.iframes / 80) % 2 === 0) ? 0.5 : 1;
        }

        // Victory: every enemy dead and finished falling
        if (allDead && c.enemies.length > 0 && c.enemies.every(e => e.deathT > 700)) {
            this._endCombat(false);
        }
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
