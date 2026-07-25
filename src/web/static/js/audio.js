/**
 * ForgeAudio — adaptive music + NPC voice playback.
 *
 * Music: a per-world mood palette (generated server-side via Suno) is
 * fetched from /api/assets/music/manifest. Mood is derived from the SSE
 * `state` events (tension, time of day, death) and tracks crossfade.
 *
 * Voice: `speech` SSE events carry an audio_id; clips are generated in
 * the background server-side, so we poll /api/assets/voice/{id} briefly
 * and play lines sequentially, ducking the music while a voice plays.
 *
 * Cinematics: `cinematic` SSE events (title cards, boss intros, visions,
 * weather) get their own music duck — `cineDuck` multiplies with the voice
 * duck rather than fighting it — plus an optional voice-queue hold so a
 * title card is not talked over. game.js owns the picture; this file owns
 * the sound of it and never assumes game.js ran.
 *
 * Crossfade/ducking approach referenced from the telltale project's
 * player AudioManager, reimplemented for Forge's streaming model.
 */

class ForgeAudio {
    constructor() {
        this.unlocked = false;          // browsers block audio until a user gesture
        this.manifest = null;
        this.manifestTimer = null;

        // Two music elements for crossfading
        this.musicA = new Audio();
        this.musicB = new Audio();
        this.musicA.loop = this.musicB.loop = true;
        this.active = this.musicA;      // element currently audible
        this.currentMood = null;
        this.pendingMood = null;        // mood requested before unlock/manifest
        this.fadeTimer = null;
        this.lastStateMood = null;      // last mood derived from a state event
        this.stingerTimer = null;       // active one-shot stinger (e.g. triumph)

        this.voiceEl = new Audio();
        this.voiceQueue = [];
        this.voicePlaying = false;
        this.voiceHold = false;         // cinematics pause the queue without dropping it
        this._voiceHoldTimer = null;

        // Cinematic ducking — a second music multiplier, independent of the
        // voice duck (they multiply in _effectiveMusicVolume)
        this._cineDuckLevel = 1;
        this._cineDuckTimer = null;
        this._cineTimers = [];          // per-cinematic scheduled beats

        // Looping weather ambience (silent until the bed files exist)
        this.ambName = null;
        this.ambSource = null;
        this.ambGain = null;
        this.ambBuffers = {};           // name -> AudioBuffer | null (null = known missing)

        this.musicVolume = parseFloat(localStorage.getItem('forge-music-vol') ?? '0.5');
        this.voiceVolume = parseFloat(localStorage.getItem('forge-voice-vol') ?? '0.9');
        this.sfxVolume = parseFloat(localStorage.getItem('forge-sfx-vol') ?? '0.8');
        this.muted = localStorage.getItem('forge-audio-muted') === '1';

        // Combat one-shots (WebAudio: overlapping plays + pitch variation)
        this.sfxCtx = null;
        this.sfxBuffers = {};
        this.sfxLastAt = {};

        this.DUCK = 0.25;               // music multiplier while a voice line plays

        document.addEventListener('DOMContentLoaded', () => this._initUI());
        for (const evt of ['pointerdown', 'keydown']) {
            document.addEventListener(evt, () => this._unlock(), { once: true, capture: true });
        }
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    _unlock() {
        this.unlocked = true;
        this._initSfx();
        if (this.ambName) this._restartAmbience();   // weather set before the gesture
        if (this.pendingMood) {
            const mood = this.pendingMood;
            this.pendingMood = null;
            this.setMood(mood);
        }
    }

    async loadManifest() {
        try {
            const res = await fetch('/api/assets/music/manifest');
            if (!res.ok) return;
            this.manifest = await res.json();
        } catch (e) {
            return;
        }
        if (this.manifestTimer) { clearTimeout(this.manifestTimer); this.manifestTimer = null; }
        if (this.manifest && this.manifest.generating) {
            // Palette still generating server-side — check back for new tracks
            this.manifestTimer = setTimeout(() => this.loadManifest(), 20000);
        }
        // A mood may have been requested before its track existed
        if (this.pendingMood && this.unlocked) {
            const mood = this.pendingMood;
            this.pendingMood = null;
            this.setMood(mood);
        }
    }

    /** Single entry point wired into the /api/play SSE handler. */
    handlePlayEvent(payload) {
        try {
            if (payload.type === 'state' && payload.state) {
                const mood = this.moodFromState(payload.state);
                if (mood) {
                    this.lastStateMood = mood;
                    if (this.stingerTimer && mood !== 'danger') {
                        // A stinger is holding the stage — it will revert
                        // to this mood when it finishes
                        return;
                    }
                    if (this.stingerTimer) this._clearStinger();
                    this.setMood(mood);
                }
            } else if (payload.type === 'speech' && payload.audio_id) {
                this.enqueueVoice(payload.audio_id, payload.npc_id);
            } else if (payload.type === 'scene' && payload.time_of_day === 'night') {
                // Night falls mid-turn: soften to the night theme unless
                // something more urgent is already playing
                if (this.currentMood === 'explore' || !this.currentMood) this.setMood('night');
            } else if (payload.type === 'quest_completed') {
                this.playStinger('triumph');
            } else if (payload.type === 'level_up') {
                // A level is a beat, not a quest — same theme, shorter hold.
                // SSE order is quest_completed -> xp_gain -> level_up, so a
                // triumph may already be on stage: let its longer hold stand
                // rather than cutting the quest fanfare short.
                if (!(this.stingerTimer && this.currentMood === 'triumph')) {
                    this.playStinger('triumph', 8000);
                }
            } else if (payload.type === 'cinematic') {
                this._handleCinematic(payload);
            } else if (payload.type === 'combat_start') {
                if (this.stingerTimer) this._clearStinger();
                this.lastStateMood = 'danger';
                this.setMood('danger');
            }
        } catch (e) {
            console.warn('ForgeAudio event error:', e);
        }
    }

    /** One-shot mood (quest turn-in, big beat): crossfade in, hold, revert
     *  to whatever the world state says. Danger always interrupts it. */
    playStinger(mood, holdMs = 14000) {
        if (!this.unlocked || !this.manifest) return;
        if (!this._trackUrl(mood)) return;   // track not generated — skip silently
        this._clearStinger();
        this.setMood(mood);
        this.stingerTimer = setTimeout(() => {
            this.stingerTimer = null;
            const back = this.lastStateMood || 'explore';
            if (this.currentMood === mood) this.setMood(back);
        }, holdMs);
    }

    _clearStinger() {
        if (this.stingerTimer) {
            clearTimeout(this.stingerTimer);
            this.stingerTimer = null;
        }
    }

    // ------------------------------------------------------------------
    // Cinematics
    // ------------------------------------------------------------------

    /** Durations used only when the server payload omits duration_ms. */
    static CINE_MS = { title_card: 4200, boss_intro: 3600, kill_cam: 1500, vision: 45000 };

    /** Music moods that actually exist in the palette. A cinematic `mood` is
     *  a weather label for kind=weather, so it is whitelisted before use. */
    static MUSIC_MOODS = ['explore', 'tension', 'danger', 'somber', 'night', 'triumph', 'epic'];

    /**
     * `cinematic` SSE -> sound. The picture is game.js's job; every branch
     * here is safe on its own if game.js skipped or deferred the visual.
     */
    _handleCinematic(p) {
        const kind = p && p.kind;
        if (!kind) return;
        this._clearCineTimers();
        const dur = Number(p.duration_ms) > 0
            ? Number(p.duration_ms)
            : (ForgeAudio.CINE_MS[kind] || 0);

        if (kind === 'title_card') {
            // Hush: the card is a silence, not a fanfare. Duck hard for the
            // length of the card, then let the world's mood swell back.
            this.cineDuck(0.15, dur + 400);
            this.holdVoiceQueue(dur + 300);
            this.playSfx('windup', { volume: 0.3 });   // bar-in / reveal
            const mood = this._musicMood(p.mood);
            if (mood) {
                // A card that names its own mood re-scores the scene after it
                // clears (state events keep the last word from then on).
                this._cineLater(() => { this.lastStateMood = mood; this.setMood(mood); }, dur);
            }
        } else if (kind === 'boss_intro') {
            // The one moment that gets its own track. `epic` falls back to
            // `danger` while the palette is still generating; combat_start
            // lands right after and interrupts the stinger by the usual rule.
            this.cineDuck(0.30, dur + 300);
            this.holdVoiceQueue(dur + 300);
            this.playStinger('epic', 10000);
            // Land the hit on the name splash, not on event arrival. game.js
            // may fire its own impact there — playSfx throttles by resolved
            // sample, so the pair collapses into one strike.
            this._cineLater(() => this.playSfx('clash', { volume: 0.9 }), 900);
        } else if (kind === 'vision') {
            // Visions have voices — hold only across the wash-in, then let
            // the queue run. game.endVision() releases the duck via duckFor(1);
            // the timer is the failsafe if it never does.
            this.cineDuck(0.18, dur || 45000);
            this.holdVoiceQueue(1200);
            this.playSfx('windup', { volume: 0.25 });
        } else if (kind === 'kill_cam') {
            // Client-driven today (game.js calls duckFor/playSfx directly);
            // handled here too so a server-sent kill_cam is never silent.
            this.cineDuck(0.15, dur || 1500);
            this.playSfx('clash', { volume: 1.0 });
        } else if (kind === 'weather') {
            // No music change — weather is a bed, not a mood.
            const label = (p.mood || 'clear');
            this.setAmbience(
                label === 'rain' ? 'amb_rain'
                : (label === 'snow' || label === 'fog') ? 'amb_wind'
                : null
            );
        }
    }

    /** Cinematic music duck. Multiplies with the voice duck; `ms > 0` auto-restores. */
    cineDuck(level, ms = 0) {
        this._cineDuckLevel = Math.max(0, Math.min(1, Number(level)));
        if (this._cineDuckTimer) { clearTimeout(this._cineDuckTimer); this._cineDuckTimer = null; }
        this._applyVolumes();
        if (ms > 0 && this._cineDuckLevel < 1) {
            this._cineDuckTimer = setTimeout(() => {
                this._cineDuckTimer = null;
                this._cineDuckLevel = 1;
                this._applyVolumes();
            }, ms);
        }
    }

    /** Alias — game.js calls this name from its cinematic hooks. */
    duckFor(level, ms = 0) {
        this.cineDuck(level, ms);
    }

    /** Pause the voice queue without dropping it. Accepts a boolean (hold /
     *  release) or a duration in ms (hold, then resume automatically). */
    holdVoiceQueue(on) {
        if (this._voiceHoldTimer) { clearTimeout(this._voiceHoldTimer); this._voiceHoldTimer = null; }
        const ms = typeof on === 'number' ? on : 0;
        this.voiceHold = ms > 0 ? true : !!on;
        if (this.voiceHold) {
            // A hold that is never released would swallow the turn's dialogue,
            // so every hold has an expiry — an explicit ms, or a failsafe.
            this._voiceHoldTimer = setTimeout(() => {
                this._voiceHoldTimer = null;
                this.voiceHold = false;
                this._processVoiceQueue();
            }, ms > 0 ? ms : 15000);
        } else {
            this._processVoiceQueue();
        }
    }

    _musicMood(mood) {
        const m = (mood || '').toString().toLowerCase();
        return ForgeAudio.MUSIC_MOODS.includes(m) ? m : null;
    }

    _cineLater(fn, ms) {
        const id = setTimeout(() => {
            this._cineTimers = this._cineTimers.filter(t => t !== id);
            try { fn(); } catch (e) { /* a cue is never worth an exception */ }
        }, ms);
        this._cineTimers.push(id);
    }

    _clearCineTimers() {
        for (const id of this._cineTimers) clearTimeout(id);
        this._cineTimers = [];
    }

    // ------------------------------------------------------------------
    // Music
    // ------------------------------------------------------------------

    moodFromState(state) {
        if (!state) return null;
        if (state.health === 'dead') return 'somber';
        const t = state.tension || 'low';
        if (t === 'high' || t === 'climax') return 'danger';
        if (t === 'rising') return 'tension';
        if (state.time_of_day === 'night') return 'night';
        return 'explore';
    }

    _trackUrl(mood) {
        if (!this.manifest || !this.manifest.moods) return null;
        // Fall back along "intensity neighbors" while tracks are still generating
        const fallbacks = {
            // `epic` is the newest track in the palette — existing worlds have
            // not generated it yet, so a boss entrance borrows `danger`.
            epic: ['epic', 'danger', 'tension', 'explore'],
            danger: ['danger', 'tension', 'explore'],
            tension: ['tension', 'explore'],
            somber: ['somber', 'night', 'explore'],
            night: ['night', 'explore'],
            triumph: ['triumph', 'explore'],
            explore: ['explore'],
        };
        for (const m of (fallbacks[mood] || [mood])) {
            const entry = this.manifest.moods[m];
            if (entry && entry.status === 'ready' && entry.url) return entry.url;
        }
        return null;
    }

    setMood(mood) {
        if (!this.unlocked || !this.manifest) {
            this.pendingMood = mood;
            return;
        }
        if (mood === this.currentMood) return;
        const url = this._trackUrl(mood);
        if (!url) {
            this.pendingMood = mood; // retry after the next manifest refresh
            return;
        }
        this.currentMood = mood;
        const from = this.active;
        const to = from === this.musicA ? this.musicB : this.musicA;
        if (to.src !== new URL(url, location.origin).href) {
            to.src = url;
        }
        to.volume = 0;
        to.play().catch(() => {});
        this.active = to;
        this._crossfade(from, to, 2000);
    }

    _crossfade(from, to, ms) {
        if (this.fadeTimer) clearInterval(this.fadeTimer);
        const steps = Math.max(1, Math.floor(ms / 50));
        let step = 0;
        this.fadeTimer = setInterval(() => {
            step++;
            // Re-read every tick: a voice line or a cinematic can duck
            // mid-crossfade, and the fade must land on the ducked target.
            const target = this._effectiveMusicVolume();
            const k = step / steps;
            to.volume = Math.min(target, target * k);
            from.volume = Math.max(0, (from === this.active ? target : target) * (1 - k));
            if (step >= steps) {
                clearInterval(this.fadeTimer);
                this.fadeTimer = null;
                from.pause();
                to.volume = target;
            }
        }, 50);
    }

    _effectiveMusicVolume() {
        const duck = this.voicePlaying ? this.DUCK : 1;
        const cine = this._cineDuckLevel ?? 1;
        return this.muted ? 0 : this.musicVolume * duck * cine;
    }

    _applyVolumes() {
        const v = this._effectiveMusicVolume();
        if (!this.fadeTimer) this.active.volume = v;
        this.voiceEl.volume = this.muted ? 0 : this.voiceVolume;
    }

    // ------------------------------------------------------------------
    // Voice
    // ------------------------------------------------------------------

    enqueueVoice(audioId, npcId = null) {
        this.voiceQueue.push({ audioId, npcId });
        this._processVoiceQueue();
    }

    async _processVoiceQueue() {
        if (this.voicePlaying || this.voiceQueue.length === 0) return;
        if (this.voiceHold) return;   // a cinematic owns the stage; lines wait
        this.voicePlaying = true;
        this._applyVolumes(); // duck music
        const { audioId, npcId } = this.voiceQueue.shift();
        try {
            const blobUrl = await this._fetchVoice(audioId);
            if (blobUrl && this.unlocked && !this.muted) {
                // Speech bubbles sync to these (game.js listens)
                window.dispatchEvent(new CustomEvent('forge-voice-start', {
                    detail: { audioId, npcId }
                }));
                await this._playVoice(blobUrl);
                window.dispatchEvent(new CustomEvent('forge-voice-end', {
                    detail: { audioId, npcId }
                }));
                URL.revokeObjectURL(blobUrl);
            }
        } catch (e) {
            console.warn('Voice playback failed:', e);
        }
        this.voicePlaying = false;
        this._applyVolumes(); // restore music
        this._processVoiceQueue();
    }

    /** Poll the voice endpoint until the clip is generated (max ~30s). */
    async _fetchVoice(audioId) {
        for (let attempt = 0; attempt < 15; attempt++) {
            try {
                const res = await fetch(`/api/assets/voice/${audioId}`, { cache: 'no-store' });
                if (res.ok) {
                    const blob = await res.blob();
                    return URL.createObjectURL(blob);
                }
                if (res.status === 400) return null;
            } catch (e) { /* network hiccup — retry */ }
            await new Promise(r => setTimeout(r, 2000));
        }
        return null;
    }

    _playVoice(src) {
        return new Promise((resolve) => {
            this.voiceEl.src = src;
            this.voiceEl.volume = this.muted ? 0 : this.voiceVolume;
            this.voiceEl.onended = resolve;
            this.voiceEl.onerror = resolve;
            this.voiceEl.play().catch(resolve);
        });
    }

    // ------------------------------------------------------------------
    // Combat SFX (static one-shots under /static/sfx/)
    // ------------------------------------------------------------------

    static SFX_NAMES = ['swing', 'hit', 'hurt', 'roll', 'death', 'windup', 'clash', 'boom', 'whoosh'];

    /** Cinematic cue names map onto the shipped one-shots until (if ever)
     *  dedicated samples exist. A real `boom.mp3`/`whoosh.mp3` added to
     *  SFX_NAMES silently wins — the alias is only consulted on a miss. */
    static SFX_ALIASES = { boom: 'clash', whoosh: 'windup', slam: 'hit', impact: 'clash' };

    _initSfx() {
        if (this.sfxCtx) return;
        try {
            this.sfxCtx = new (window.AudioContext || window.webkitAudioContext)();
        } catch (e) { return; }
        for (const name of ForgeAudio.SFX_NAMES) {
            fetch(`/static/sfx/${name}.mp3`)
                .then(r => (r.ok ? r.arrayBuffer() : null))
                .then(buf => (buf ? this.sfxCtx.decodeAudioData(buf) : null))
                .then(decoded => { if (decoded) this.sfxBuffers[name] = decoded; })
                .catch(() => {});
        }
    }

    /** Fire-and-forget one-shot. Slight pitch jitter keeps rapid repeats
     *  from machine-gunning; a per-name throttle stops multi-enemy frames
     *  from stacking the same sound. */
    playSfx(name, { volume = 1, throttleMs = 60 } = {}) {
        if (this.muted || !this.sfxCtx) return;
        // Resolve aliases BEFORE throttling so a cinematic cue and the sample
        // it borrows cannot double-strike on the same frame.
        let key = name;
        if (!this.sfxBuffers[key]) key = ForgeAudio.SFX_ALIASES[name] || key;
        if (!this.sfxBuffers[key]) return;
        const now = performance.now();
        if (this.sfxLastAt[key] && now - this.sfxLastAt[key] < throttleMs) return;
        this.sfxLastAt[key] = now;
        if (this.sfxCtx.state === 'suspended') this.sfxCtx.resume().catch(() => {});
        const src = this.sfxCtx.createBufferSource();
        src.buffer = this.sfxBuffers[key];
        src.playbackRate.value = 0.94 + Math.random() * 0.12;
        const gain = this.sfxCtx.createGain();
        gain.gain.value = this.sfxVolume * volume;
        src.connect(gain);
        gain.connect(this.sfxCtx.destination);
        src.start();
    }

    setSfxVolume(v) {
        this.sfxVolume = v;
        localStorage.setItem('forge-sfx-vol', String(v));
        if (this.ambGain) this.ambGain.gain.value = this.sfxVolume * ForgeAudio.AMB_GAIN;
    }

    // ------------------------------------------------------------------
    // Weather ambience (looping bed; silent while the files are absent)
    // ------------------------------------------------------------------

    static AMB_GAIN = 0.35;

    /** Start/stop a looping ambience bed. Idempotent; a missing file is a
     *  silent no-op, so shipping without beds costs nothing. */
    setAmbience(name) {
        const next = name || null;
        if (next === this.ambName) return;
        this.ambName = next;
        this._stopAmbience();
        if (!next || !this.sfxCtx || this.muted) return;
        this._loadAmbience(next).then((buf) => {
            // The weather may have turned again while the file was loading
            if (!buf || this.ambName !== next || !this.sfxCtx || this.muted) return;
            try {
                if (this.sfxCtx.state === 'suspended') this.sfxCtx.resume().catch(() => {});
                const src = this.sfxCtx.createBufferSource();
                src.buffer = buf;
                src.loop = true;
                const gain = this.sfxCtx.createGain();
                gain.gain.value = this.sfxVolume * ForgeAudio.AMB_GAIN;
                src.connect(gain);
                gain.connect(this.sfxCtx.destination);
                src.start();
                this.ambSource = src;
                this.ambGain = gain;
            } catch (e) { /* ambience is decoration — never throw */ }
        });
    }

    _loadAmbience(name) {
        if (Object.prototype.hasOwnProperty.call(this.ambBuffers, name)) {
            return Promise.resolve(this.ambBuffers[name]);
        }
        return fetch(`/static/sfx/${name}.mp3`)
            .then(r => (r.ok ? r.arrayBuffer() : null))
            .then(buf => (buf ? this.sfxCtx.decodeAudioData(buf) : null))
            .then((decoded) => { this.ambBuffers[name] = decoded || null; return this.ambBuffers[name]; })
            .catch(() => { this.ambBuffers[name] = null; return null; });
    }

    _stopAmbience() {
        if (this.ambSource) {
            try { this.ambSource.stop(); } catch (e) { /* already stopped */ }
            try { this.ambSource.disconnect(); } catch (e) { /* detached */ }
        }
        if (this.ambGain) {
            try { this.ambGain.disconnect(); } catch (e) { /* detached */ }
        }
        this.ambSource = null;
        this.ambGain = null;
    }

    _restartAmbience() {
        const name = this.ambName;
        this.ambName = null;
        this.setAmbience(name);
    }

    // ------------------------------------------------------------------
    // Controls UI (chip lives in the game HUD; panel injected here)
    // ------------------------------------------------------------------

    setMusicVolume(v) {
        this.musicVolume = v;
        localStorage.setItem('forge-music-vol', String(v));
        this._applyVolumes();
    }

    setVoiceVolume(v) {
        this.voiceVolume = v;
        localStorage.setItem('forge-voice-vol', String(v));
        this._applyVolumes();
    }

    toggleMute() {
        this.muted = !this.muted;
        localStorage.setItem('forge-audio-muted', this.muted ? '1' : '0');
        this._applyVolumes();
        this._updateChip();
        if (this.muted) {
            this.musicA.pause();
            this.musicB.pause();
            this._stopAmbience();
        } else {
            if (this.currentMood) this.active.play().catch(() => {});
            if (this.ambName) this._restartAmbience();
        }
    }

    _updateChip() {
        const chip = document.getElementById('hud-audio-icon');
        if (chip) chip.innerHTML = this.muted ? '&#128263;' : '&#128266;';
    }

    _initUI() {
        const chip = document.getElementById('hud-audio-chip');
        if (!chip) return;

        const panel = document.createElement('div');
        panel.id = 'audio-panel';
        panel.className = 'hud-panel hidden';
        panel.innerHTML = `
            <h4>Audio</h4>
            <div class="hud-item"><span>Music</span>
                <input id="audio-music-vol" type="range" min="0" max="1" step="0.05" value="${this.musicVolume}"></div>
            <div class="hud-item"><span>Voice</span>
                <input id="audio-voice-vol" type="range" min="0" max="1" step="0.05" value="${this.voiceVolume}"></div>
            <div class="hud-item"><span>Effects</span>
                <input id="audio-sfx-vol" type="range" min="0" max="1" step="0.05" value="${this.sfxVolume}"></div>
            <div class="hud-item"><span>Mute</span>
                <button id="audio-mute-btn" style="cursor:pointer;">toggle</button></div>
        `;
        chip.parentElement.appendChild(panel);

        chip.addEventListener('click', () => panel.classList.toggle('hidden'));
        panel.querySelector('#audio-music-vol').addEventListener('input',
            (e) => this.setMusicVolume(parseFloat(e.target.value)));
        panel.querySelector('#audio-voice-vol').addEventListener('input',
            (e) => this.setVoiceVolume(parseFloat(e.target.value)));
        panel.querySelector('#audio-sfx-vol').addEventListener('input',
            (e) => this.setSfxVolume(parseFloat(e.target.value)));
        panel.querySelector('#audio-mute-btn').addEventListener('click', () => this.toggleMute());
        this._updateChip();
    }
}

window.forgeAudio = new ForgeAudio();
