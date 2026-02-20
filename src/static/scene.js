// scene.js — AthenaScene
// Pure canvas 2D operations room scene.
// Depends on AgentSprite defined in sprites.js (loaded first).
// Loaded by index.html before the inline script block.

class AthenaScene {
  constructor(canvas) {
    this.canvas     = canvas;
    this.ctx        = canvas.getContext('2d');
    this.running    = false;
    this.raf        = null;
    this.t          = 0;
    this.ghostMode  = false;   // true = Workshop background (15% alpha)
    this.usageCrit  = false;   // true = blink red clock
    this.statusText = 'STANDBY';
    this.clockBlink = 0;
    this._dpr       = 1;
    this._resizeBound = () => this._resize();

    // ── Agent sprites at compass positions ──────────────────────────────────
    // Orbit radius 130px; sprites at N / E / S / W
    const r = 130;
    this.sprites = [
      new AgentSprite('manager',   0,  -r, '124,166,196'), // top    — accent
      new AgentSprite('frontend',  r,   0, '80,181,131'),  // right  — green
      new AgentSprite('backend',   0,   r, '94,184,196'),  // bottom — cyan
      new AgentSprite('tester',   -r,   0, '139,126,198'), // left   — purple
    ];

    // ── Background orbital rings (echoes the Jarvis aesthetic) ──────────────
    this.rings = [
      { r: 60,  speed:  0.0004, dash: [2, 6],  opacity: 0.10 },
      { r: 95,  speed: -0.0006, dash: [4, 8],  opacity: 0.13 },
      { r: 170, speed: -0.0005, dash: [3, 10], opacity: 0.13 },
    ];
  }

  // ── Lifecycle ────────────────────────────────────────────────────────────

  start() {
    if (this.running) return;
    this.running = true;
    this._resize();
    window.addEventListener('resize', this._resizeBound);
    this._loop();
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
    window.removeEventListener('resize', this._resizeBound);
  }

  // ── State setters (called from index.html SSE hooks) ─────────────────────

  setGhostMode(on) { this.ghostMode = !!on; }

  setStatus(text)  { this.statusText = String(text || ''); }

  setUsageCrit(on) { this.usageCrit = !!on; }

  setAgentState(agentName, state) {
    const sp = this._sprite(agentName);
    if (sp) sp.setState(state);
  }

  resetAllAgents(state = 'idle') {
    this.sprites.forEach(sp => sp.setState(state));
  }

  setAgentWarning(agentName, warning) {
    const sp = this._sprite(agentName);
    if (sp) sp.setWarning(warning || null);
  }

  // ── Private helpers ───────────────────────────────────────────────────────

  _sprite(name) {
    return this.sprites.find(s => s.name === name) || null;
  }

  _resize() {
    const dpr  = window.devicePixelRatio || 1;
    this._dpr  = dpr;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    this.canvas.width        = rect.width  * dpr;
    this.canvas.height       = rect.height * dpr;
    this.canvas.style.width  = rect.width  + 'px';
    this.canvas.style.height = rect.height + 'px';
    // Reset transform before re-scaling (avoids accumulating scale on resize)
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _loop() {
    if (!this.running) return;
    this._draw();
    this.raf = requestAnimationFrame(() => this._loop());
  }

  // ── Main draw ────────────────────────────────────────────────────────────

  _draw() {
    const ctx    = this.ctx;
    const dpr    = this._dpr;
    const w      = this.canvas.width  / dpr;
    const h      = this.canvas.height / dpr;
    const cx     = w / 2;
    const cy     = h / 2;
    const t      = ++this.t;
    const ghost  = this.ghostMode ? 0.15 : 1.0;
    const accent = '124,166,196';

    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(cx, cy);

    // ── Background orbital rings ───────────────────────────────────────────
    for (const ring of this.rings) {
      ctx.save();
      ctx.rotate(t * ring.speed);
      ctx.beginPath();
      ctx.arc(0, 0, ring.r, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(${accent},${ring.opacity * ghost})`;
      ctx.lineWidth   = 1;
      ctx.setLineDash(ring.dash);
      ctx.stroke();
      ctx.restore();
    }
    ctx.setLineDash([]);

    // ── Sprite orbit guide ring ────────────────────────────────────────────
    ctx.beginPath();
    ctx.arc(0, 0, 130, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${accent},${0.05 * ghost})`;
    ctx.lineWidth   = 1;
    ctx.stroke();

    // ── Connector lines: centre → each sprite ─────────────────────────────
    ctx.setLineDash([4, 8]);
    for (const sp of this.sprites) {
      const active = sp.state === 'executing';
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(sp.x, sp.y);
      ctx.strokeStyle = `rgba(${accent},${(active ? 0.30 : 0.07) * ghost})`;
      ctx.lineWidth   = active ? 1.5 : 1;
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // ── Centre glow ───────────────────────────────────────────────────────
    const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, 28);
    grad.addColorStop(0, `rgba(${accent},${0.25 * ghost})`);
    grad.addColorStop(1, `rgba(${accent},0)`);
    ctx.beginPath();
    ctx.arc(0, 0, 28, 0, Math.PI * 2);
    ctx.fillStyle = grad;
    ctx.fill();

    // Centre dot
    ctx.beginPath();
    ctx.arc(0, 0, 3, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${accent},${0.75 * ghost})`;
    ctx.fill();

    // ── Agent sprites ─────────────────────────────────────────────────────
    for (const sp of this.sprites) {
      sp.draw(ctx, t, ghost);
    }

    // ── Status text (centre) ──────────────────────────────────────────────
    if (!this.ghostMode) {
      ctx.font         = '9px monospace';
      ctx.fillStyle    = `rgba(${accent},0.4)`;
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(this.statusText, 0, 0);
      ctx.textBaseline = 'alphabetic';
    }

    // ── Critical usage clock (blinking red) ───────────────────────────────
    if (this.usageCrit && !this.ghostMode) {
      this.clockBlink++;
      // Blink at ~1 Hz (30 frames on, 30 frames off at 60fps)
      if (Math.floor(this.clockBlink / 30) % 2 === 0) {
        this._drawClock(ctx, 24, -24, 8);
      }
    }

    ctx.restore();
  }

  // Draw a minimalist clock icon at (ox, oy) with radius r
  _drawClock(ctx, ox, oy, r) {
    ctx.strokeStyle = 'rgba(239,68,68,0.9)';
    ctx.lineWidth   = 1.5;
    ctx.lineCap     = 'round';
    ctx.setLineDash([]);

    // Clock face
    ctx.beginPath();
    ctx.arc(ox, oy, r, 0, Math.PI * 2);
    ctx.stroke();

    // Hour hand (pointing ~10 o'clock)
    ctx.beginPath();
    ctx.moveTo(ox, oy);
    ctx.lineTo(ox - r * 0.5, oy - r * 0.6);
    ctx.stroke();

    // Minute hand (pointing ~12 o'clock)
    ctx.beginPath();
    ctx.moveTo(ox, oy);
    ctx.lineTo(ox, oy - r * 0.8);
    ctx.stroke();
  }
}
