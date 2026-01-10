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
        this.moveSpeed = 0.5; // Normalized units per frame (slower for better control)
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
        this.playerLayer.removeChildren();

        try {
            const texture = await PIXI.Assets.load(playerData.sprite_url);
            const sprite = new PIXI.Sprite(texture);

            // Scale sprite relative to world size
            const targetHeight = this.worldHeight * 0.12;
            const scale = targetHeight / sprite.height;
            sprite.scale.set(scale);

            // Center anchor at bottom center (feet)
            sprite.anchor.set(0.5, 1);

            // Position in world coordinates
            sprite.x = this.normalizedToWorldX(playerData.x);
            sprite.y = this.normalizedToWorldY(playerData.y);

            this.playerLayer.addChild(sprite);

            this.player = {
                sprite: sprite,
                id: playerData.id,
                name: playerData.name,
                direction: playerData.direction,
                normalizedX: playerData.x,
                normalizedY: playerData.y,
                sprites: {},      // Idle sprites by direction
                walkSprites: {}   // Walk sprites: walkSprites[direction][frame]
            };

            this.player.sprites[playerData.direction] = texture;

            console.log('Player loaded, direction:', playerData.direction, 'playerId:', this.playerId);
            console.log('Player sprites cache:', Object.keys(this.player.sprites));

            // Pre-load ALL direction sprites and walk animations
            this.preloadAllSprites().catch(err => console.error('preloadAllSprites error:', err));

        } catch (error) {
            console.error('Failed to load player sprite:', error);
            // Create placeholder circle
            const placeholder = new PIXI.Graphics();
            placeholder.circle(0, 0, 20);
            placeholder.fill(0x6366f1);
            placeholder.x = this.normalizedToWorldX(playerData.x);
            placeholder.y = this.normalizedToWorldY(playerData.y);
            this.playerLayer.addChild(placeholder);

            this.player = {
                sprite: placeholder,
                id: playerData.id,
                name: playerData.name,
                direction: 'front',
                normalizedX: playerData.x,
                normalizedY: playerData.y,
                sprites: {},
                walkSprites: {}
            };
        }
    }

    async loadNPCs(npcsData) {
        this.npcLayer.removeChildren();
        this.npcs = {};

        for (const npcData of npcsData) {
            try {
                const texture = await PIXI.Assets.load(npcData.sprite_url);
                const sprite = new PIXI.Sprite(texture);

                // Scale sprite
                const targetHeight = this.worldHeight * 0.10;
                const scale = targetHeight / sprite.height;
                sprite.scale.set(scale);

                sprite.anchor.set(0.5, 1);

                // Position in world coordinates
                sprite.x = this.normalizedToWorldX(npcData.x);
                sprite.y = this.normalizedToWorldY(npcData.y);

                // Make interactive
                sprite.eventMode = 'static';
                sprite.cursor = 'pointer';

                sprite.on('pointerdown', () => this.onNpcClick(npcData));
                sprite.on('pointerover', () => {
                    sprite.tint = 0xaaaaff;
                    this.showNpcTooltip(npcData.name, sprite);
                });
                sprite.on('pointerout', () => {
                    sprite.tint = 0xffffff;
                    this.hideNpcTooltip();
                });

                this.npcLayer.addChild(sprite);
                this.npcs[npcData.id] = { sprite: sprite, data: npcData };

            } catch (error) {
                console.error(`Failed to load NPC sprite for ${npcData.name}:`, error);
                // Placeholder
                const placeholder = new PIXI.Graphics();
                placeholder.circle(0, 0, 15);
                placeholder.fill(0x22c55e);
                placeholder.x = this.normalizedToWorldX(npcData.x);
                placeholder.y = this.normalizedToWorldY(npcData.y);
                placeholder.eventMode = 'static';
                placeholder.cursor = 'pointer';
                placeholder.on('pointerdown', () => this.onNpcClick(npcData));
                this.npcLayer.addChild(placeholder);
                this.npcs[npcData.id] = { sprite: placeholder, data: npcData };
            }
        }
    }

    update() {
        if (!this.player || this.isLoading) return;

        const deltaTime = this.app.ticker.deltaMS;

        // Handle movement
        let dx = 0;
        let dy = 0;
        let newDirection = this.player.direction;

        if (this.keys['w'] || this.keys['arrowup']) {
            dy = -this.moveSpeed;
            newDirection = 'back';
        }
        if (this.keys['s'] || this.keys['arrowdown']) {
            dy = this.moveSpeed;
            newDirection = 'front';
        }
        if (this.keys['a'] || this.keys['arrowleft']) {
            dx = -this.moveSpeed;
            newDirection = 'left';
        }
        if (this.keys['d'] || this.keys['arrowright']) {
            dx = this.moveSpeed;
            newDirection = 'right';
        }

        const wasMoving = this.isMoving;
        this.isMoving = (dx !== 0 || dy !== 0);

        if (this.isMoving) {
            const newX = Math.max(0, Math.min(100, this.player.normalizedX + dx));
            const newY = Math.max(0, Math.min(100, this.player.normalizedY + dy));

            if (this.isWalkable(newX, newY)) {
                this.player.normalizedX = newX;
                this.player.normalizedY = newY;
                this.player.sprite.x = this.normalizedToWorldX(newX);
                this.player.sprite.y = this.normalizedToWorldY(newY);
                this.positionDirty = true;

                if (newDirection !== this.player.direction) {
                    this.player.direction = newDirection;
                    // Load idle sprite for new direction immediately
                    this.updatePlayerSprite(newDirection);
                    // Preload walk sprites for new direction in background
                    this.preloadWalkSprites(newDirection);
                    // Reset animation to start fresh with new direction
                    this.animationFrame = 0;
                    this.animationTimer = 0;
                }
            }

            this.syncPlayerPosition();

            // Update walk animation
            this.animationTimer += deltaTime;
            if (this.animationTimer >= this.animationSpeed) {
                this.animationTimer = 0;
                // Cycle through frames: 0 (idle) -> 1 (walk1) -> 2 (idle) -> 3 (walk2)
                this.animationFrame = (this.animationFrame + 1) % 4;
            }

            // Apply animation frame
            this.applyAnimationFrame();
        } else if (wasMoving) {
            // Just stopped moving - return to idle
            this.animationFrame = 0;
            this.animationTimer = 0;
            this.applyAnimationFrame();
        }

        // Update camera to follow player
        this.updateCamera();

        // Sort sprites by Y for depth
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
        // Preload all 4 directions and their walk animations
        const directions = ['front', 'back', 'left', 'right'];

        console.log('preloadAllSprites called, playerId:', this.playerId);
        console.log('Current sprites cache:', Object.keys(this.player.sprites));

        for (const direction of directions) {
            // Load idle sprite for this direction
            const alreadyCached = !!this.player.sprites[direction];
            console.log(`Checking direction ${direction}, already cached: ${alreadyCached}`);

            if (!this.player.sprites[direction]) {
                try {
                    const url = `/api/assets/sprite/player/${this.playerId}/${direction}`;
                    console.log(`Fetching: ${url}`);
                    const texture = await PIXI.Assets.load(url);
                    this.player.sprites[direction] = texture;
                    console.log(`Loaded idle sprite: ${direction}`);
                } catch (error) {
                    console.error(`Failed to load idle sprite for ${direction}:`, error);
                }
            }

            // Load walk frames for this direction
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
                        console.error(`Failed to load walk sprite ${direction}_walk${frame}:`, error);
                        // Fall back to idle sprite for this direction
                        this.player.walkSprites[direction][frame] = this.player.sprites[direction] || null;
                    }
                }
            }
        }

        console.log('All player sprites preloaded!');
    }

    applyAnimationFrame() {
        if (!this.player || !this.player.sprite) return;

        const direction = this.player.direction;

        // Animation cycle: 0=idle, 1=walk1, 2=idle, 3=walk2
        let texture = null;
        let textureSource = 'none';

        if (this.animationFrame === 0 || this.animationFrame === 2) {
            // Idle frames
            texture = this.player.sprites[direction];
            textureSource = `idle-${direction}`;
        } else if (this.animationFrame === 1) {
            // Walk frame 1
            texture = this.player.walkSprites[direction]?.[1];
            textureSource = `walk1-${direction}`;
        } else if (this.animationFrame === 3) {
            // Walk frame 2
            texture = this.player.walkSprites[direction]?.[2];
            textureSource = `walk2-${direction}`;
        }

        // Debug log (throttled)
        if (!this._lastAnimLog || Date.now() - this._lastAnimLog > 500) {
            console.log(`Animation: dir=${direction}, frame=${this.animationFrame}, source=${textureSource}, hasTexture=${!!texture}`);
            console.log('Available idle sprites:', Object.keys(this.player.sprites));
            console.log('Available walk sprites:', Object.keys(this.player.walkSprites),
                        this.player.walkSprites[direction] ? Object.keys(this.player.walkSprites[direction]) : 'none');
            this._lastAnimLog = Date.now();
        }

        // Apply texture if available, otherwise fall back to idle
        if (texture) {
            this.player.sprite.texture = texture;
        } else if (this.player.sprites[direction]) {
            this.player.sprite.texture = this.player.sprites[direction];
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
                dropShadowColor: 0x000000,
                dropShadowDistance: 2,
            }
        });

        tooltip.anchor.set(0.5, 1);
        // Position in screen space (relative to sprite's screen position)
        tooltip.x = sprite.x - this.camera.x;
        tooltip.y = sprite.y - sprite.height - this.camera.y - 10;
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
