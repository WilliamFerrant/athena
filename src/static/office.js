// office.js — Pixel Office Scene
// A lightweight canvas scene with a tilemap office and animated agent sprites.
// Pure canvas 2D. No external libs. Loaded before the inline script.
//
// Agent state → animation:
//   idle      → slow walk (patrol between desks)
//   thinking  → stand + thought-bubble dots
//   working   → sit at terminal desk (typing frames)
//   attention → standing with flashing outline
//   error     → shake + red flash outline
//
// Color palette (matches CSS vars):
//   Floor    #1e1e2e  background
//   Wall     #2a2a3e  surface2
//   Desk     #3a3a52  border
//   Accent   #7ca6c4
//   Green    #50b583  (frontend)
//   Cyan     #5eb8c4  (backend)
//   Purple   #8b7ec6  (tester)
//   Yellow   #eab308  (bubble dots)

'use strict';

// ── Tile constants ────────────────────────────────────────────────────────────
const TILE = 16;           // px per tile
const COLS = 20;
const ROWS = 14;

// Tile IDs
const T_FLOOR = 0;
const T_WALL  = 1;
const T_DESK  = 2;
const T_TERM  = 3;  // terminal / monitor on desk
const T_PLANT = 4;

// ── Office layout (20 × 14 tiles) ─────────────────────────────────────────────
// W=wall, F=floor, D=desk, T=terminal, P=plant
const MAP_SRC = [
  'WWWWWWWWWWWWWWWWWWWW',
  'WFFFFFFFFFFFFFFFFFFFFW', // intentionally 22 — clipped, keep 20
  'WFDDTFFFFFFFDDTFFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFW',
  'WFFFDDTFFFFFFFDDTFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFW',
  'WFFFDDTFFFFFFFDDTFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFW',
  'WFFFFFFFFFFFFFFFFFFFFW',
  'WPFFFFFFFFFFFFFFFFFFPw',
  'WWWWWWWWWWWWWWWWWWWW',
].map(row => {
  // Trim/pad to exactly COLS chars
  const r = row.padEnd(COLS, 'F').substring(0, COLS);
  const tiles = [];
  for (let i = 0; i < COLS; i++) {
    const c = r[i];
    if (c === 'W' || c === 'w') tiles.push(T_WALL);
    else if (c === 'D') tiles.push(T_DESK);
    else if (c === 'T') tiles.push(T_TERM);
    else if (c === 'P') tiles.push(T_PLANT);
    else tiles.push(T_FLOOR);
  }
  return tiles;
});

// Desk positions (tile coords where agents sit when working)
const DESKS = [
  { x: 2,  y: 2  },   // manager's desk
  { x: 12, y: 2  },   // frontend's desk
  { x: 2,  y: 5  },   // backend's desk
  { x: 12, y: 5  },   // tester's desk
];

// ── Agent sprite data ──────────────────────────────────────────────────────────
const AGENT_DEFS = [
  { id: 'manager',  color: '#7ca6c4', deskIdx: 0, idleX: 5,  idleY: 7 },
  { id: 'frontend', color: '#50b583', deskIdx: 1, idleX: 14, idleY: 7 },
  { id: 'backend',  color: '#5eb8c4', deskIdx: 2, idleX: 5,  idleY: 10 },
  { id: 'tester',   color: '#8b7ec6', deskIdx: 3, idleX: 14, idleY: 10 },
];

// ── Walking directions ─────────────────────────────────────────────────────────
const DIR_NONE  = 0;
const DIR_LEFT  = 1;
const DIR_RIGHT = 2;
const DIR_UP    = 3;
const DIR_DOWN  = 4;

class OfficeAgent {
  constructor(def) {
    this.id     = def.id;
    this.color  = def.color;
    this.desk   = DESKS[def.deskIdx];
    this.idleX  = def.idleX;
    this.idleY  = def.idleY;

    // Pixel position (sub-tile interpolation)
    this.px = def.idleX * TILE;
    this.py = def.idleY * TILE;

    // State machine
    this.state  = 'idle';      // idle | thinking | working | attention | error
    this.frame  = 0;           // walk frame (0-3)
    this.dir    = DIR_RIGHT;
    this.walkPhase = Math.random() * Math.PI * 2; // offset idle oscillation

    // Walk target
    this.targetPx = this.px;
    this.targetPy = this.py;
    this.speed    = 0.6;       // px per frame (idle stroll)
    this.fastSpeed= 1.5;       // px per frame (heading to desk)

    // Bubble animation (thinking state)
    this.bubbleT  = 0;

    // Flash animation (attention/error state)
    this.flashT   = 0;

    // Walk frame ticker
    this._walkTick = 0;
  }

  setState(s) {
    // Accept both AthenaScene and OfficeScene state vocabulary
    if (s === 'executing') s = 'working';
    if (s === 'reviewing') s = 'thinking';
    if (this.state === s) return;
    this.state = s;
    this.flashT = 0;
    this.bubbleT = 0;

    if (s === 'working') {
      // Head to desk
      this.targetPx = this.desk.x * TILE;
      this.targetPy = this.desk.y * TILE;
      this.speed = this.fastSpeed;
    } else if (s === 'idle') {
      // Return to idle patrol area
      this.targetPx = this.idleX * TILE;
      this.targetPy = this.idleY * TILE;
      this.speed = 0.6;
    } else if (s === 'thinking') {
      // Stop somewhere near idle zone
      this.targetPx = this.px;
      this.targetPy = this.py;
    } else if (s === 'attention' || s === 'error') {
      // Stand in place
      this.targetPx = this.px;
      this.targetPy = this.py;
    }
  }

  update(t) {
    this._walkTick++;

    switch (this.state) {
      case 'idle':
        this._doIdleWalk(t);
        break;
      case 'working':
        this._moveToTarget(this.fastSpeed);
        break;
      case 'thinking':
      case 'attention':
      case 'error':
        // Twitch toward target if there's drift
        this._moveToTarget(0.5);
        break;
    }

    this.bubbleT += 0.04;
    this.flashT  += 0.12;

    // Advance walk frame every 12 game ticks when moving
    if (this._walkTick % 12 === 0) {
      const moving = Math.abs(this.targetPx - this.px) > 1 || Math.abs(this.targetPy - this.py) > 1;
      if (moving) this.frame = (this.frame + 1) % 4;
    }
  }

  _moveToTarget(speed) {
    const dx = this.targetPx - this.px;
    const dy = this.targetPy - this.py;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < speed) {
      this.px = this.targetPx;
      this.py = this.targetPy;
    } else {
      this.px += (dx / dist) * speed;
      this.py += (dy / dist) * speed;
      this.dir = Math.abs(dx) > Math.abs(dy)
        ? (dx > 0 ? DIR_RIGHT : DIR_LEFT)
        : (dy > 0 ? DIR_DOWN  : DIR_UP);
    }
  }

  _doIdleWalk(t) {
    // Wander within a 3×3 tile radius of idle position
    const arrived = Math.abs(this.targetPx - this.px) < 1 && Math.abs(this.targetPy - this.py) < 1;
    if (arrived) {
      // Pick a new random destination nearby every ~3 seconds (180 ticks at 60fps)
      if (Math.random() < 0.007) {
        const rx = (Math.random() - 0.5) * 4 * TILE;
        const ry = (Math.random() - 0.5) * 3 * TILE;
        this.targetPx = Math.max(TILE, Math.min((COLS - 2) * TILE, this.idleX * TILE + rx));
        this.targetPy = Math.max(TILE, Math.min((ROWS - 2) * TILE, this.idleY * TILE + ry));
      }
    } else {
      this._moveToTarget(0.6);
    }
  }

  draw(ctx, ghostAlpha) {
    const a  = ghostAlpha ?? 1;
    const cx = Math.round(this.px);
    const cy = Math.round(this.py);
    const sz = 10;   // sprite "body" size in pixels

    ctx.save();
    ctx.globalAlpha = a;

    // Flash effect for attention/error
    if (this.state === 'attention' || this.state === 'error') {
      const flashOn = Math.sin(this.flashT * 5) > 0;
      if (flashOn) {
        ctx.strokeStyle = this.state === 'error' ? '#ef4444' : '#eab308';
        ctx.lineWidth   = 2;
        ctx.strokeRect(cx - sz / 2 - 2, cy - sz - 2, sz + 4, sz + 4);
      }
    }

    // Body shadow
    ctx.fillStyle = 'rgba(0,0,0,0.3)';
    ctx.beginPath();
    ctx.ellipse(cx, cy + 1, sz / 2, sz / 5, 0, 0, Math.PI * 2);
    ctx.fill();

    // Body (rounded rect approximation via arc)
    ctx.fillStyle = this.color;
    ctx.beginPath();
    ctx.roundRect(cx - sz / 2, cy - sz, sz, sz, 3);
    ctx.fill();

    // Head
    ctx.fillStyle = this.color;
    ctx.beginPath();
    ctx.arc(cx, cy - sz - 4, 4, 0, Math.PI * 2);
    ctx.fill();

    // Eyes (direction-aware)
    const eyeOff = this.dir === DIR_LEFT ? -2 : this.dir === DIR_RIGHT ? 2 : 0;
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.arc(cx + eyeOff, cy - sz - 4, 1.5, 0, Math.PI * 2);
    ctx.fill();

    // Walk legs animation
    if (this.state === 'working' || this.state === 'idle') {
      const legSwing = Math.sin(this.frame * Math.PI / 2) * 3;
      ctx.strokeStyle = this.color;
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      // Left leg
      ctx.beginPath();
      ctx.moveTo(cx - 2, cy);
      ctx.lineTo(cx - 2 + legSwing, cy + 5);
      ctx.stroke();
      // Right leg
      ctx.beginPath();
      ctx.moveTo(cx + 2, cy);
      ctx.lineTo(cx + 2 - legSwing, cy + 5);
      ctx.stroke();
    }

    // Typing animation when working at desk (reached desk)
    if (this.state === 'working') {
      const atDesk = Math.abs(this.px - this.desk.x * TILE) < 3
                  && Math.abs(this.py - this.desk.y * TILE) < 3;
      if (atDesk) {
        // Arms down typing
        ctx.strokeStyle = this.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(cx - 4, cy - sz + 2);
        ctx.lineTo(cx - 6, cy - 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(cx + 4, cy - sz + 2);
        ctx.lineTo(cx + 6, cy - 2);
        ctx.stroke();
        // Keyboard blink
        if (Math.floor(this.bubbleT * 4) % 3 !== 0) {
          ctx.fillStyle = '#fff3';
          ctx.fillRect(cx - 5, cy - 1, 10, 2);
        }
      }
    }

    // Thought bubble for thinking state
    if (this.state === 'thinking') {
      this._drawThoughtBubble(ctx, cx, cy - sz - 10);
    }

    ctx.restore();
  }

  _drawThoughtBubble(ctx, x, y) {
    // Small dots above head
    for (let i = 0; i < 3; i++) {
      const phase = this.bubbleT - i * 0.5;
      const alpha = 0.4 + 0.6 * Math.max(0, Math.sin(phase * 2));
      const yOff  = -i * 5 + Math.sin(phase * 3) * 1;
      ctx.globalAlpha = alpha;
      ctx.fillStyle   = '#eab308';
      ctx.beginPath();
      ctx.arc(x, y + yOff, 2 - i * 0.3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
}

// ── Main scene class ───────────────────────────────────────────────────────────
class OfficeScene {
  constructor(canvas) {
    this.canvas     = canvas;
    this.ctx        = canvas.getContext('2d');
    this.running    = false;
    this.raf        = null;
    this.t          = 0;
    this.ghostMode  = false;
    this.statusText = 'STANDBY';
    this._dpr       = 1;
    this._resizeBound = () => this._resize();

    // Instantiate agents
    this.agents = AGENT_DEFS.map(def => new OfficeAgent(def));
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

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

  // ── State control (called from index.html SSE hooks) ─────────────────────

  setGhostMode(on) { this.ghostMode = !!on; }

  setStatus(text)  { this.statusText = String(text || ''); }

  setAgentState(name, state) {
    const ag = this._agent(name);
    if (ag) ag.setState(state);
  }

  resetAllAgents(state = 'idle') {
    this.agents.forEach(ag => ag.setState(state));
  }

  // setAgentWarning is a no-op compatibility shim (attention state covers it)
  setAgentWarning(name, warning) {
    const ag = this._agent(name);
    if (!ag) return;
    if (warning === 'down')     ag.setState('attention');
    else if (warning === null)  ag.setState('idle');
    // degraded → no change; just leave current state
  }

  // Not used by office but kept for API parity with AthenaScene
  setUsageCrit(on) { this._usageCrit = on; }

  // ── Private ───────────────────────────────────────────────────────────────

  _agent(name) { return this.agents.find(a => a.id === name) || null; }

  _resize() {
    const dpr  = window.devicePixelRatio || 1;
    this._dpr  = dpr;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    this.canvas.width        = rect.width  * dpr;
    this.canvas.height       = rect.height * dpr;
    this.canvas.style.width  = rect.width  + 'px';
    this.canvas.style.height = rect.height + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _loop() {
    if (!this.running) return;
    this._update();
    this._draw();
    this.raf = requestAnimationFrame(() => this._loop());
  }

  _update() {
    const t = ++this.t;
    for (const ag of this.agents) ag.update(t);
  }

  _draw() {
    const ctx   = this.ctx;
    const dpr   = this._dpr;
    const W     = this.canvas.width  / dpr;
    const H     = this.canvas.height / dpr;
    const ghost = this.ghostMode ? 0.18 : 1.0;

    // Scale the 20×14 tile map to fit the canvas, preserving aspect ratio
    const mapW  = COLS * TILE;
    const mapH  = ROWS * TILE;
    const scale = Math.min(W / mapW, H / mapH);
    const offX  = Math.floor((W - mapW * scale) / 2);
    const offY  = Math.floor((H - mapH * scale) / 2);

    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.globalAlpha = ghost;
    ctx.translate(offX, offY);
    ctx.scale(scale, scale);

    // Draw tilemap
    this._drawTilemap(ctx);

    // Draw agents (sorted by Y for depth)
    const sorted = [...this.agents].sort((a, b) => a.py - b.py);
    for (const ag of sorted) ag.draw(ctx, 1.0);

    // Status label
    if (!this.ghostMode) {
      ctx.fillStyle    = 'rgba(124,166,196,0.45)';
      ctx.font         = `${Math.round(8 / scale)}px monospace`;
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(this.statusText, mapW / 2, 2);
      ctx.textBaseline = 'alphabetic';
    }

    ctx.restore();
  }

  _drawTilemap(ctx) {
    for (let row = 0; row < ROWS; row++) {
      for (let col = 0; col < COLS; col++) {
        const tile = MAP_SRC[row]?.[col] ?? T_FLOOR;
        const x = col * TILE;
        const y = row * TILE;
        this._drawTile(ctx, tile, x, y);
      }
    }
  }

  _drawTile(ctx, tile, x, y) {
    switch (tile) {
      case T_WALL:
        ctx.fillStyle = '#2a2a3e';
        ctx.fillRect(x, y, TILE, TILE);
        // Top highlight
        ctx.fillStyle = '#3a3a52';
        ctx.fillRect(x, y, TILE, 2);
        break;

      case T_FLOOR:
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(x, y, TILE, TILE);
        // Subtle grid lines
        ctx.strokeStyle = '#1e1e36';
        ctx.lineWidth = 0.5;
        ctx.strokeRect(x, y, TILE, TILE);
        break;

      case T_DESK:
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(x, y, TILE, TILE);
        // Desk surface
        ctx.fillStyle = '#3d3d5c';
        ctx.fillRect(x + 1, y + 2, TILE - 2, TILE - 6);
        // Desk legs
        ctx.fillStyle = '#2a2a40';
        ctx.fillRect(x + 2, y + TILE - 5, 3, 5);
        ctx.fillRect(x + TILE - 5, y + TILE - 5, 3, 5);
        break;

      case T_TERM:
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(x, y, TILE, TILE);
        // Monitor
        ctx.fillStyle = '#1a1a30';
        ctx.fillRect(x + 2, y + 2, TILE - 4, TILE - 7);
        // Screen glow
        ctx.fillStyle = `rgba(124,166,196,${0.15 + 0.1 * Math.sin(this.t * 0.04)})`;
        ctx.fillRect(x + 3, y + 3, TILE - 6, TILE - 9);
        // Stand
        ctx.fillStyle = '#2a2a40';
        ctx.fillRect(x + TILE / 2 - 1, y + TILE - 5, 2, 5);
        break;

      case T_PLANT:
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(x, y, TILE, TILE);
        // Pot
        ctx.fillStyle = '#5a3a2a';
        ctx.fillRect(x + 4, y + TILE - 6, 8, 6);
        // Leaves
        ctx.fillStyle = '#2d6a4f';
        ctx.beginPath();
        ctx.arc(x + TILE / 2, y + TILE - 8, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#40916c';
        ctx.beginPath();
        ctx.arc(x + TILE / 2 - 2, y + TILE - 11, 4, 0, Math.PI * 2);
        ctx.fill();
        break;
    }
  }
}
