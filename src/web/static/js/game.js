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
        this.moveSpeed = 0.3; // Normalized units per frame (slower for better control)
        this.syncInterval = 200; // ms between server syncs
        this.lastSync = 0;
        this.positionDirty = false;

        // Animation settings
        this.animationFrame = 0; // Current frame: 0=idle, 1=walk1, 2=idle, 3=walk2
        this.animationTimer = 0;
        this.animationSpeed = 150; // ms per frame
        this.isMoving = false;

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


        // Create world container (this moves with camera)
        this.worldContainer = new PIXI.Container();
        this.app.stage.addChild(this.worldContainer);

        // Create render layers inside world container
        this.backgroundLayer = new PIXI.Container();
        this.npcLayer = new PIXI.Container();
        this.playerLayer = new PIXI.Container();

        this.worldContainer.addChild(this.backgroundLayer);
        this.worldContainer.addChild(this.npcLayer);
        this.worldContainer.addChild(this.playerLayer);

        // UI layer (doesn't move with camera)
        this.uiLayer = new PIXI.Container();
        this.app.stage.addChild(this.uiLayer);

        // Setup input handlers
        this.setupInput();

        // Start game loop
        this.app.ticker.add(() => this.update());

        // Handle resize
        window.addEventListener('resize', () => this.onResize());

        console.log('GameRenderer initialized with camera system');
    }

    setupInput() {
        window.addEventListener('keydown', (e) => {
            if (document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA') {
                return;
            }

            const key = e.key.toLowerCase();
            if (['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(key)) {
                this.keys[key] = true;
                e.preventDefault();
            }
        });

        window.addEventListener('keyup', (e) => {
            const key = e.key.toLowerCase();
            this.keys[key] = false;
        });
    }

    async loadLocation(locationId, playerId) {
        if (this.isLoading) return;
        this.isLoading = true;

        this.playerId = playerId;
        this.locationId = locationId;

        // 1. CLEAR PREVIOUS SCENE IMMEDIATELY
        this.backgroundLayer.removeChildren();
        this.npcLayer.removeChildren();
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

            // Store walkable bounds
            this.walkableBounds = data.walkable_bounds || { x_min: 5, x_max: 95, y_min: 5, y_max: 95 };

            // Load player sprite
            await this.loadPlayer(data.player);

            // Load NPC sprites
            await this.loadNPCs(data.npcs);

            // Center camera on player initially
            this.centerCameraOnPlayer();

            console.log(`Loaded location: ${data.location_name}`);

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

    async loadPlayer(playerData) {
        if (!this.playerLayer) return;
        this.playerLayer.removeChildren();

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
        this.npcLayer.removeChildren();
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
                sprite.scale.set(baseScale * npcScale);
                sprite.anchor.set(0.5, 1);
                sprite.x = this.normalizedToWorldX(npcData.x);
                sprite.y = this.normalizedToWorldY(npcData.y);

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
                        // Prevent bubbling and select for move
                        e.stopPropagation();
                        this.startTransform(sprite, npcData);
                    });

                    sprite.on('pointerover', () => { if(!this.editingNpc) sprite.tint = 0xaaaaff; });
                    sprite.on('pointerout', () => { if(!this.editingNpc) sprite.tint = 0xffffff; });
                }

                this.npcLayer.addChild(sprite);
                this.npcs[npcData.id] = { sprite: sprite, data: npcData, status: npcStatus, baseScale: baseScale };

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

            // Fade to 50% alpha
            sprite.alpha = 1 - (0.5 * progress);

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

        let dx = 0;
        let dy = 0;
        let newDir = this.player.direction; // Default to current

        // Handle WASD
        if (this.keys['w'] || this.keys['arrowup']) { dy = -this.moveSpeed; newDir = 'back'; }
        else if (this.keys['s'] || this.keys['arrowdown']) { dy = this.moveSpeed; newDir = 'front'; }
        else if (this.keys['a'] || this.keys['arrowleft']) { dx = -this.moveSpeed; newDir = 'left'; }
        else if (this.keys['d'] || this.keys['arrowright']) { dx = this.moveSpeed; newDir = 'right'; }

        this.isMoving = (dx !== 0 || dy !== 0);

        if (this.isMoving) {
            // Apply Movement
            this.player.normalizedX = Math.max(0, Math.min(100, this.player.normalizedX + dx));
            this.player.normalizedY = Math.max(0, Math.min(100, this.player.normalizedY + dy));

            // CRITICAL: Update the direction in the player object
            this.player.direction = newDir;

            this.updatePlayerPosition();
            this.positionDirty = true;

            // Handle Animation Frames
            this.animationTimer += deltaTime;
            if (this.animationTimer >= this.animationSpeed) {
                this.animationTimer = 0;
                this.animationFrame = (this.animationFrame + 1) % 4;
            }
            this.syncPlayerPosition();
        } else {
            this.animationFrame = 0; // Return to idle frame
        }

        // Apply visual updates
        this.applyAnimationFrame();
        this.updateCamera();
        this.sortByDepth();
    }

    updateCamera() {
        if (!this.player) return;

        // Target camera position (center player on screen)
        const targetX = this.player.sprite.x - this.app.screen.width / 2;
        const targetY = this.player.sprite.y - this.app.screen.height / 2;

        // Clamp camera to world bounds
        const maxX = this.worldWidth - this.app.screen.width;
        const maxY = this.worldHeight - this.app.screen.height;

        const clampedX = Math.max(0, Math.min(maxX, targetX));
        const clampedY = Math.max(0, Math.min(maxY, targetY));

        // Smooth camera follow
        this.camera.x += (clampedX - this.camera.x) * this.cameraSmoothing;
        this.camera.y += (clampedY - this.camera.y) * this.cameraSmoothing;

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

        for (const frame of [1, 2]) {
            if (!this.player.walkSprites[direction][frame]) {
                try {
                    const url = `/api/assets/sprite/player/${this.playerId}/${direction}_walk${frame}`;
                    const texture = await PIXI.Assets.load(url);
                    this.player.walkSprites[direction][frame] = texture;
                    console.log(`Loaded walk sprite: ${direction}_walk${frame}`);
                } catch (error) {
                    console.error(`Failed to preload walk sprite ${direction}_walk${frame}:`, error);
                    // Fall back to idle sprite
                    this.player.walkSprites[direction][frame] = this.player.sprites[direction] || null;
                }
            }
        }
    }

    async preloadAllSprites() {
        const directions = ['front', 'back', 'left', 'right'];
        const frames = [1, 2];
        // Add .png to the base if you like, or just in the loop
        const playerUrlBase = `/api/assets/sprite/player/${this.playerId}`;

        const loadJobs = [];

        directions.forEach(dir => {
            // LOAD IDLE (Append .png)
            loadJobs.push(
                PIXI.Assets.load(`${playerUrlBase}/${dir}.png`).then(tex => {
                    this.player.sprites[dir] = tex;
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
            const walk = this.player.walkSprites[dir] || {};
            const cycle = [
                this.player.sprites[dir],
                walk[1] || this.player.sprites[dir],
                this.player.sprites[dir],
                walk[2] || this.player.sprites[dir]
            ];
            tex = cycle[this.animationFrame];
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

    sortByDepth() {
        // Sort NPC layer children by Y position
        this.npcLayer.children.sort((a, b) => a.y - b.y);

        // Insert player in correct position among NPCs for proper depth
        // (Player is in separate layer, but we could merge them for better sorting)
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

        // Convert 0-100 coordinates to actual world pixels
        this.player.sprite.x = this.normalizedToWorldX(this.player.normalizedX);
        this.player.sprite.y = this.normalizedToWorldY(this.player.normalizedY);

        // Debugging to ensure coordinates are valid
        // console.log(`Player Pos: ${this.player.sprite.x}, ${this.player.sprite.y}`);
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
