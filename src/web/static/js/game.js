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
        this.walkFrameCount = 6;   // full generated cycle; legacy sets have 2
        this.animationFrame = 0;   // free-running counter, wrapped per-cycle at lookup
        this.animationTimer = 0;
        this.isMoving = false;

        // Game-feel (juice) state
        this.particles = [];               // live dust puffs
        this.walkPhase = 0;                // drives bob/lean oscillation
        this.bobOffset = 0;                // current vertical bob (px, <= 0)
        this.leanAngle = 0;                // current sprite lean (radians)
        this.lookAhead = { x: 0, y: 0 };   // camera leads the movement direction

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
            // Only fire if clicking directly on stage (not on an NPC)
            if (e.target === this.app.stage) {
                window.dispatchEvent(new CustomEvent('npc-deselect'));
            }
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
                window.dispatchEvent(new CustomEvent('editor-mode-changed', {
                    detail: { enabled: this.editorMode }
                }));
                e.preventDefault();
                return;
            }

            if (['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(key)) {
                this.keys[key] = true;
                e.preventDefault();
            }
        };

        this._onKeyUp = (e) => {
            const key = e.key.toLowerCase();
            this.keys[key] = false;
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
        this.backgroundLayer.removeChildren();
        this.npcLayer.removeChildren();
        this.shadowLayer.removeChildren();
        this.particleLayer.removeChildren();
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

            // Store walkable bounds and available exits
            this.walkableBounds = data.walkable_bounds || { x_min: 5, x_max: 95, y_min: 5, y_max: 95 };
            this.exits = data.exits || [];

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

        // Skip movement if player is dead
        if (this.playerDead) {
            this.updateCamera();
            return;
        }

        const deltaTime = this.app.ticker.deltaMS;
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

                // Advance the walk cycle (length varies: 6 generated frames,
                // or the legacy idle-interleaved 4-step shuffle)
                const anim = this._getWalkCycle();
                this.animationTimer += deltaTime;
                if (this.animationTimer >= anim.speed) {
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
                if (this.edgePushTimer >= this.edgePushThreshold && this.exitPromptCooldown <= 0) {
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
            // Full generated cycle: contact frames sit at 0 and mid-cycle
            return { cycle: frames, speed: 95, stepEvery: Math.round(frames.length / 2) };
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
        if (!this.walkableBounds) return true;

        return x >= this.walkableBounds.x_min &&
               x <= this.walkableBounds.x_max &&
               y >= this.walkableBounds.y_min &&
               y <= this.walkableBounds.y_max;
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
        }

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
