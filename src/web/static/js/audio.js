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

        this.voiceEl = new Audio();
        this.voiceQueue = [];
        this.voicePlaying = false;

        this.musicVolume = parseFloat(localStorage.getItem('forge-music-vol') ?? '0.5');
        this.voiceVolume = parseFloat(localStorage.getItem('forge-voice-vol') ?? '0.9');
        this.muted = localStorage.getItem('forge-audio-muted') === '1';

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
                if (mood) this.setMood(mood);
            } else if (payload.type === 'speech' && payload.audio_id) {
                this.enqueueVoice(payload.audio_id);
            } else if (payload.type === 'scene' && payload.time_of_day === 'night') {
                // Night falls mid-turn: soften to the night theme unless
                // something more urgent is already playing
                if (this.currentMood === 'explore' || !this.currentMood) this.setMood('night');
            }
        } catch (e) {
            console.warn('ForgeAudio event error:', e);
        }
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
        const target = this._effectiveMusicVolume();
        const steps = Math.max(1, Math.floor(ms / 50));
        let step = 0;
        this.fadeTimer = setInterval(() => {
            step++;
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
        return this.muted ? 0 : this.musicVolume * duck;
    }

    _applyVolumes() {
        const v = this._effectiveMusicVolume();
        if (!this.fadeTimer) this.active.volume = v;
        this.voiceEl.volume = this.muted ? 0 : this.voiceVolume;
    }

    // ------------------------------------------------------------------
    // Voice
    // ------------------------------------------------------------------

    enqueueVoice(audioId) {
        this.voiceQueue.push(audioId);
        this._processVoiceQueue();
    }

    async _processVoiceQueue() {
        if (this.voicePlaying || this.voiceQueue.length === 0) return;
        this.voicePlaying = true;
        this._applyVolumes(); // duck music
        const audioId = this.voiceQueue.shift();
        try {
            const blobUrl = await this._fetchVoice(audioId);
            if (blobUrl && this.unlocked && !this.muted) {
                await this._playVoice(blobUrl);
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
        } else if (this.currentMood) {
            this.active.play().catch(() => {});
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
            <div class="hud-item"><span>Mute</span>
                <button id="audio-mute-btn" style="cursor:pointer;">toggle</button></div>
        `;
        chip.parentElement.appendChild(panel);

        chip.addEventListener('click', () => panel.classList.toggle('hidden'));
        panel.querySelector('#audio-music-vol').addEventListener('input',
            (e) => this.setMusicVolume(parseFloat(e.target.value)));
        panel.querySelector('#audio-voice-vol').addEventListener('input',
            (e) => this.setVoiceVolume(parseFloat(e.target.value)));
        panel.querySelector('#audio-mute-btn').addEventListener('click', () => this.toggleMute());
        this._updateChip();
    }
}

window.forgeAudio = new ForgeAudio();
