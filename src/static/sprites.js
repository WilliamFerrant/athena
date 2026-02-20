// sprites.js — AgentSprite
// Pure canvas 2D. No external dependencies.
// Loaded by index.html before scene.js and the inline script.

class AgentSprite {
  // name:  'manager' | 'frontend' | 'backend' | 'tester'
  // x, y:  position relative to the canvas centre (passed by AthenaScene)
  // color: RGB string like '124,166,196' — matches CSS var values
  constructor(name, x, y, color) {
    this.name  = name;
    this.x     = x;
    this.y     = y;
    this.color = color;
    this.state   = 'idle'; // idle | executing | reviewing | error
    this.warning = null;   // null | 'degraded' | 'down'
  }

  setState(s)  { this.state   = s; }
  setWarning(w){ this.warning = w; }

  // draw() is called every frame by AthenaScene.
  // ctx is already translated to the canvas centre.
  // t is the global frame counter.
  // ghostAlpha: 1.0 = normal, 0.15 = workshop background ghost.
  draw(ctx, t, ghostAlpha) {
    const a  = ghostAlpha ?? 1;
    let dx = 0, dy = 0;

    // ── Animation offsets by state ──────────────────────────────────────────
    switch (this.state) {
      case 'idle':
        // Gentle breathing: each sprite has its own phase offset
        dy = Math.sin(t * 0.02 + this._phase()) * 2;
        break;
      case 'executing':
        // Rapid horizontal jitter (typing)
        dx = Math.sin(t * 0.4) * 3;
        break;
      case 'reviewing':
        // Slow vertical nod
        dy = Math.sin(t * 0.06) * 4;
        break;
      case 'error':
        // Fast shake
        dx = Math.sin(t * 0.7) * 5;
        break;
    }

    ctx.save();
    ctx.translate(this.x + dx, this.y + dy);

    // ── Body circle ──────────────────────────────────────────────────────────
    ctx.beginPath();
    ctx.arc(0, 0, 14, 0, Math.PI * 2);
    ctx.fillStyle   = `rgba(${this.color},${0.15 * a})`;
    ctx.fill();
    ctx.strokeStyle = `rgba(${this.color},${0.7 * a})`;
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([]);
    ctx.stroke();

    // ── Active state ring pulse ──────────────────────────────────────────────
    if (this.state === 'executing') {
      const pulseR = 14 + 6 * Math.abs(Math.sin(t * 0.15));
      ctx.beginPath();
      ctx.arc(0, 0, pulseR, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(${this.color},${0.25 * a})`;
      ctx.lineWidth   = 1;
      ctx.stroke();
    }

    // ── Glyph (agent identity mark) ──────────────────────────────────────────
    this._drawGlyph(ctx, a);

    // ── Warning dot (health indicator) ───────────────────────────────────────
    if (this.warning) {
      const wc = this.warning === 'down' ? '239,68,68' : '234,179,8';
      ctx.beginPath();
      ctx.arc(11, -11, 4, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${wc},${0.9 * a})`;
      ctx.fill();
    }

    // ── Label ────────────────────────────────────────────────────────────────
    ctx.font        = '8px monospace';
    ctx.fillStyle   = `rgba(${this.color},${0.55 * a})`;
    ctx.textAlign   = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(this.name.toUpperCase(), 0, 18);
    ctx.textBaseline = 'alphabetic';

    ctx.restore();
  }

  // Stable per-sprite phase offset so idle breathing looks independent
  _phase() {
    return { manager: 0, frontend: 1.5, backend: 3.0, tester: 4.7 }[this.name] || 0;
  }

  // Distinct geometric glyph per agent role
  _drawGlyph(ctx, a) {
    ctx.strokeStyle = `rgba(${this.color},${0.85 * a})`;
    ctx.lineWidth   = 1.5;
    ctx.lineCap     = 'round';
    ctx.setLineDash([]);

    if (this.name === 'manager') {
      // Crosshair — commander symbol
      ctx.beginPath(); ctx.moveTo(-6, 0); ctx.lineTo(6, 0); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -6); ctx.lineTo(0, 6); ctx.stroke();
      // Small centre dot
      ctx.beginPath(); ctx.arc(0, 0, 2, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${this.color},${0.6 * a})`;
      ctx.fill();

    } else if (this.name === 'frontend') {
      // Angle brackets < > — HTML/JSX shorthand
      ctx.beginPath();
      ctx.moveTo(-4, -5); ctx.lineTo(-8, 0); ctx.lineTo(-4, 5);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(4, -5); ctx.lineTo(8, 0); ctx.lineTo(4, 5);
      ctx.stroke();

    } else if (this.name === 'backend') {
      // Three horizontal stack lines — database / server layers
      for (let i = -1; i <= 1; i++) {
        ctx.beginPath();
        ctx.moveTo(-6, i * 3.5); ctx.lineTo(6, i * 3.5);
        ctx.stroke();
      }

    } else if (this.name === 'tester') {
      // Checkmark — test pass symbol
      ctx.beginPath();
      ctx.moveTo(-6, 0); ctx.lineTo(-2, 5); ctx.lineTo(7, -5);
      ctx.stroke();
    }
  }
}
