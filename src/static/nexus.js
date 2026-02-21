// nexus.js — Athena Presence v4.1
// Athena as a singular living entity. Flowing aurora fills the entire space,
// with deep layering, organic motion, and dramatic state reactions.
// Pure Canvas 2D, no dependencies. Same public API.

'use strict';

var TAU = Math.PI * 2;

/* ── Perlin noise ── */
var _nP = (function() {
  var p = [];
  for (var i = 0; i < 256; i++) p[i] = i;
  for (var i = 255; i > 0; i--) {
    var j = (Math.random() * (i + 1)) | 0;
    var t = p[i]; p[i] = p[j]; p[j] = t;
  }
  return p.concat(p);
})();
function _fade(t) { return t * t * t * (t * (t * 6 - 15) + 10); }
function _lerp(a, b, t) { return a + t * (b - a); }
function _grad(h, x, y) {
  var g = h & 3, u = g < 2 ? x : y, v = g < 2 ? y : x;
  return ((g & 1) ? -u : u) + ((g & 2) ? -v : v);
}
function noise2(x, y) {
  var X = Math.floor(x) & 255, Y = Math.floor(y) & 255;
  x -= Math.floor(x); y -= Math.floor(y);
  var u = _fade(x), v = _fade(y);
  var A = _nP[X] + Y, B = _nP[X + 1] + Y;
  return _lerp(
    _lerp(_grad(_nP[A], x, y), _grad(_nP[B], x - 1, y), u),
    _lerp(_grad(_nP[A + 1], x, y - 1), _grad(_nP[B + 1], x - 1, y - 1), u), v
  );
}
function fbm(x, y, oct) {
  var val = 0, amp = 0.5, freq = 1;
  for (var i = 0; i < oct; i++) {
    val += amp * noise2(x * freq, y * freq);
    amp *= 0.5; freq *= 2.1;
  }
  return val;
}
function spring(cur, tgt, vel, k, d) {
  k = k || 0.04; d = d || 0.8;
  return { v: cur + ((vel + (tgt - cur) * k) * d), vel: (vel + (tgt - cur) * k) * d };
}
function rgba(r, g, b, a) { return 'rgba(' + (r|0) + ',' + (g|0) + ',' + (b|0) + ',' + a + ')'; }
function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

class NexusScene {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.running = false;
    this.raf = null;
    this.t = 0;
    this._w = 0; this._h = 0; this._dpr = 1;
    this._resizeBound = () => this._resize();
    this._moveBound = (e) => this._onMove(e);

    this.ghostMode = false;
    this.usageCrit = false;
    this.statusText = 'STANDBY';
    this._statusChars = [];
    this._statusReveal = 7;

    // Agent state (hidden — feeds Athena's mood)
    this.agents = {};
    var defs = [
      { name: 'manager',  rgb: [100,160,220] },
      { name: 'frontend', rgb: [60,200,140]  },
      { name: 'backend',  rgb: [255,190,50]  },
      { name: 'tester',   rgb: [160,130,220] },
    ];
    for (var i = 0; i < defs.length; i++) {
      var d = defs[i];
      this.agents[d.name] = {
        state: 'idle', warning: null, rgb: d.rgb,
        drives: { energy: 80, focus: 80, morale: 80, knowledge: 60 },
      };
    }

    this.tokenBudget = { used: 0, total: 200 };
    this._dispUsed = 0; this._usedVel = 0;
    this.pipelineStage = null;
    this._pipeIdx = -1;

    // Mood
    this._mood = { r: 80, g: 130, b: 200 };
    this._moodTarget = { r: 80, g: 130, b: 200 };
    this._moodVel = { r: 0, g: 0, b: 0 };
    this._intensity = 0.35;
    this._intensityTarget = 0.35;
    this._intensityVel = 0;

    // Animation phases
    this._breathPhase = 0;
    this._auroraPhase = Math.random() * 100;
    this._driftA = Math.random() * TAU;
    this._driftB = Math.random() * TAU;

    // Ripples
    this._ripples = [];

    // Particles
    this._sparks = [];
    for (var i = 0; i < 90; i++) {
      this._sparks.push(this._newSpark());
    }

    // Floating wisps (larger slow-moving blobs)
    this._wisps = [];
    for (var i = 0; i < 8; i++) {
      this._wisps.push({
        x: 0.1 + Math.random() * 0.8,
        y: 0.15 + Math.random() * 0.55,
        vx: (Math.random() - 0.5) * 0.0002,
        vy: (Math.random() - 0.5) * 0.00015,
        size: 40 + Math.random() * 80,
        phase: Math.random() * TAU,
        speed: 0.003 + Math.random() * 0.006,
      });
    }

    // Glitch + Flash
    this._glitchT = 0;
    this._glitchI = 0;
    this._flashAlpha = 0;
    this._flashRGB = [255,255,255];

    // Mouse
    this._mx = 0.5; this._my = 0.5;
    this._nameReveal = 0;
  }

  _newSpark() {
    return {
      x: Math.random(), y: 0.1 + Math.random() * 0.7,
      vx: (Math.random() - 0.5) * 0.0004,
      vy: -0.00015 - Math.random() * 0.0004,
      size: 0.4 + Math.random() * 2.5,
      maxAlpha: 0.15 + Math.random() * 0.55,
      phase: Math.random() * TAU,
      life: Math.random() * 300 | 0,
      maxLife: 150 + Math.random() * 350 | 0,
    };
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._resize();
    window.addEventListener('resize', this._resizeBound);
    this.canvas.addEventListener('mousemove', this._moveBound);
    this._loop();
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    window.removeEventListener('resize', this._resizeBound);
    this.canvas.removeEventListener('mousemove', this._moveBound);
  }

  setGhostMode(on) { this.ghostMode = !!on; }

  setStatus(text) {
    var s = String(text || 'STANDBY');
    if (s !== this.statusText) { this.statusText = s; this._statusChars = s.split(''); this._statusReveal = 0; }
  }

  setUsageCrit(on) { this.usageCrit = !!on; this._recalcMood(); }

  setAgentState(agentName, state) {
    var a = this.agents[agentName]; if (!a) return;
    var prev = a.state; a.state = state;
    if (state === 'executing' && prev !== 'executing') {
      this._ripple(a.rgb, 1.0); this._flash(a.rgb, 0.12);
      // Burst sparks
      for (var i = 0; i < 12; i++) {
        var sp = this._newSpark();
        sp.x = 0.4 + Math.random() * 0.2;
        sp.y = 0.3 + Math.random() * 0.2;
        sp.size = 1.5 + Math.random() * 3;
        sp.maxAlpha = 0.6 + Math.random() * 0.4;
        sp.vy = -0.001 - Math.random() * 0.002;
        sp.vx = (Math.random() - 0.5) * 0.002;
        this._sparks.push(sp);
      }
    }
    if (state === 'error') {
      this._glitchT = 40; this._glitchI = 1.0; this._flash([239,68,68], 0.25);
    }
    this._recalcMood();
  }

  resetAllAgents(state) {
    state = state || 'idle';
    for (var n in this.agents) this.agents[n].state = state;
    this._recalcMood();
    if (state === 'error') { this._glitchT = 50; this._glitchI = 1; }
  }

  setAgentWarning(agentName, w) { var a = this.agents[agentName]; if (a) a.warning = w || null; }
  setTokenBudget(used, total) { this.tokenBudget = { used: used || 0, total: total || 200 }; }

  setDrives(agentName, d) {
    var a = this.agents[agentName]; if (!a || !d) return;
    if (d.energy != null) a.drives.energy = d.energy;
    if (d.focus != null) a.drives.focus = d.focus;
    if (d.morale != null) a.drives.morale = d.morale;
    if (d.knowledge != null) a.drives.knowledge = d.knowledge;
    this._recalcMood();
  }

  setPipelineStage(stage) {
    var P = ['intake','planning','executing','reviewing','synthesizing'];
    this._pipeIdx = P.indexOf(stage); this.pipelineStage = stage;
    if (this._pipeIdx >= 0) this._ripple([100,160,220], 0.6);
  }

  triggerPulse(color) { this._ripple(color ? this._parseColor(color) : [100,160,220], 0.8); }

  _parseColor(hex) {
    if (typeof hex === 'string' && hex[0] === '#') {
      var v = parseInt(hex.slice(1), 16);
      return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
    }
    return [100,160,220];
  }

  _resize() {
    var dpr = window.devicePixelRatio || 1; this._dpr = dpr;
    var rect = this.canvas.parentElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    this._w = rect.width; this._h = rect.height;
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.canvas.style.width = rect.width + 'px';
    this.canvas.style.height = rect.height + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _onMove(e) {
    var r = this.canvas.getBoundingClientRect();
    this._mx = clamp((e.clientX - r.left) / r.width, 0, 1);
    this._my = clamp((e.clientY - r.top) / r.height, 0, 1);
  }

  _ripple(rgb, a) { this._ripples.push({ t: 0, alpha: a, rgb: rgb }); if (this._ripples.length > 6) this._ripples.shift(); }
  _flash(rgb, a) { this._flashRGB = rgb; this._flashAlpha = a; }

  _recalcMood() {
    var r = 0, g = 0, b = 0, w = 0, exec = 0, err = 0;
    for (var n in this.agents) {
      var a = this.agents[n];
      var wt = a.state === 'executing' ? 3 : a.state === 'reviewing' ? 1.5 : a.state === 'error' ? 2.5 : 0.3;
      r += a.rgb[0] * wt; g += a.rgb[1] * wt; b += a.rgb[2] * wt; w += wt;
      if (a.state === 'executing') exec++;
      if (a.state === 'error') err++;
    }
    if (w > 0) { this._moodTarget.r = r/w; this._moodTarget.g = g/w; this._moodTarget.b = b/w; }
    if (err > 0) { this._moodTarget.r = 220; this._moodTarget.g = 55; this._moodTarget.b = 55; }
    if (this.usageCrit) { this._moodTarget.r = Math.min(255, this._moodTarget.r + 50); this._moodTarget.g *= 0.6; this._moodTarget.b *= 0.6; }
    this._intensityTarget = clamp(0.3 + exec * 0.18 + (err > 0 ? 0.15 : 0), 0, 1);
  }

  _loop() { if (!this.running) return; this._draw(); this.raf = requestAnimationFrame(this._loop.bind(this)); }

  _draw() {
    var ctx = this.ctx, w = this._w, h = this._h;
    if (w < 1 || h < 1) return;
    var t = ++this.t;
    var ghost = this.ghostMode ? 0.12 : 1.0;
    var sc = Math.min(w, h) / 600;

    // Spring mood
    var mr = spring(this._mood.r, this._moodTarget.r, this._moodVel.r, 0.015, 0.9);
    var mg = spring(this._mood.g, this._moodTarget.g, this._moodVel.g, 0.015, 0.9);
    var mb = spring(this._mood.b, this._moodTarget.b, this._moodVel.b, 0.015, 0.9);
    this._mood.r = mr.v; this._moodVel.r = mr.vel;
    this._mood.g = mg.v; this._moodVel.g = mg.vel;
    this._mood.b = mb.v; this._moodVel.b = mb.vel;
    var si = spring(this._intensity, this._intensityTarget, this._intensityVel, 0.025, 0.87);
    this._intensity = si.v; this._intensityVel = si.vel;

    if (this._glitchT > 0) this._glitchT--;
    this._glitchI *= 0.93;
    this._flashAlpha *= 0.9;

    this._breathPhase += 0.006 + this._intensity * 0.008;
    this._auroraPhase += 0.0025 + this._intensity * 0.003;
    this._driftA += 0.003;
    this._driftB += 0.002;

    var m = this._mood;
    var inten = this._intensity;
    var breath = Math.sin(this._breathPhase);

    // ── Deep background ──
    // Gradient from very dark to slightly tinted
    var bgGrad = ctx.createRadialGradient(w*0.5, h*0.42, 0, w*0.5, h*0.42, Math.max(w, h) * 0.7);
    bgGrad.addColorStop(0, rgba(m.r * 0.06, m.g * 0.06, m.b * 0.06, 1));
    bgGrad.addColorStop(0.5, rgba(8, 10, 16, 1));
    bgGrad.addColorStop(1, rgba(5, 6, 10, 1));
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, w, h);

    // ── Wisps (large soft blobs behind aurora) ──
    this._drawWisps(ctx, w, h, t, ghost, m, inten, breath);

    // ── Main Aurora ──
    this._drawAurora(ctx, w, h, t, ghost, m, inten, breath, sc);

    // ── Sparks ──
    this._drawSparks(ctx, w, h, t, ghost, m, inten);

    // ── Ripples ──
    this._drawRipples(ctx, w, h, ghost);

    // ── Central heart glow ──
    this._drawHeart(ctx, w, h, t, ghost, m, inten, breath, sc);

    // ── Text ──
    this._drawText(ctx, w, h, t, ghost, m, inten, sc);

    // ── Budget ──
    this._drawBudget(ctx, w, h, t, ghost, m, sc);

    // Flash
    if (this._flashAlpha > 0.005) {
      ctx.fillStyle = rgba(this._flashRGB[0], this._flashRGB[1], this._flashRGB[2], this._flashAlpha * ghost);
      ctx.fillRect(0, 0, w, h);
    }

    // Glitch
    if (this._glitchI > 0.02) this._drawGlitch(ctx, w, h);
  }

  _drawWisps(ctx, w, h, t, ghost, m, inten, breath) {
    for (var i = 0; i < this._wisps.length; i++) {
      var wp = this._wisps[i];
      wp.phase += wp.speed;
      wp.x += wp.vx + Math.sin(wp.phase) * 0.00015;
      wp.y += wp.vy + Math.cos(wp.phase * 0.7) * 0.0001;

      // Soft boundary bounce
      if (wp.x < 0.05 || wp.x > 0.95) wp.vx *= -0.8;
      if (wp.y < 0.1 || wp.y > 0.75) wp.vy *= -0.8;
      wp.x = clamp(wp.x, 0.02, 0.98);
      wp.y = clamp(wp.y, 0.05, 0.8);

      var sz = wp.size * (1 + breath * 0.15 + inten * 0.3) * Math.min(w, h) / 600;
      var px = wp.x * w, py = wp.y * h;

      var pulse = 0.5 + 0.5 * Math.sin(wp.phase);
      var a = (0.015 + inten * 0.025 + pulse * 0.01) * ghost;

      var grad = ctx.createRadialGradient(px, py, 0, px, py, sz);
      grad.addColorStop(0, rgba(m.r + 30, m.g + 30, m.b + 30, a));
      grad.addColorStop(0.5, rgba(m.r, m.g, m.b, a * 0.4));
      grad.addColorStop(1, rgba(m.r, m.g, m.b, 0));
      ctx.beginPath(); ctx.arc(px, py, sz, 0, TAU);
      ctx.fillStyle = grad; ctx.fill();
    }
  }

  _drawAurora(ctx, w, h, t, ghost, m, inten, breath, sc) {
    var ph = this._auroraPhase;
    var mxOff = (this._mx - 0.5) * 0.25;
    var myOff = (this._my - 0.5) * 0.12;

    // Multiple aurora band layers, filling more of the screen
    var layers = [
      { baseY: 0.18, thick: 1.4, speed: 1.0, alphaM: 1.2, colorShift: -30 },
      { baseY: 0.28, thick: 1.0, speed: 0.8, alphaM: 1.0, colorShift: 0 },
      { baseY: 0.35, thick: 1.3, speed: 1.2, alphaM: 0.9, colorShift: 20 },
      { baseY: 0.42, thick: 0.8, speed: 0.6, alphaM: 1.4, colorShift: -15 },
      { baseY: 0.50, thick: 1.1, speed: 1.0, alphaM: 0.7, colorShift: 35 },
      { baseY: 0.58, thick: 0.9, speed: 0.9, alphaM: 0.5, colorShift: -20 },
      { baseY: 0.65, thick: 0.7, speed: 0.7, alphaM: 0.3, colorShift: 10 },
    ];

    for (var li = 0; li < layers.length; li++) {
      var L = layers[li];
      var bandAlpha = (0.025 + inten * 0.055) * L.alphaM * ghost;
      var cr = clamp(m.r + L.colorShift, 0, 255);
      var cg = clamp(m.g - L.colorShift * 0.4, 0, 255);
      var cb = clamp(m.b + L.colorShift * 0.6, 0, 255);
      var thickness = (25 + inten * 55 + breath * 8) * L.thick * sc;

      ctx.beginPath();
      var segs = 60;
      var pts = [];

      for (var s = 0; s <= segs; s++) {
        var sx = s / segs;
        var nx = sx * 3.5 + ph * L.speed + li * 5.7;
        var ny = li * 3.1 + ph * 0.6;
        var n1 = fbm(nx, ny, 4);
        var n2 = fbm(nx * 0.7 + 50, ny * 0.7 + 50, 3);
        var yOff = n1 * 0.1 + n2 * 0.05 + breath * 0.012 * (1 + li * 0.15);
        yOff += myOff * (1 - Math.abs(sx - 0.5) * 1.6);
        var xOff = mxOff * 0.06 * Math.sin(sx * Math.PI);
        var px = (sx + xOff) * w;
        var py = (L.baseY + yOff) * h;
        pts.push({ x: px, y: py });
      }

      // Draw top edge
      ctx.moveTo(pts[0].x, pts[0].y);
      for (var s = 1; s < pts.length; s++) {
        var cpx = (pts[s-1].x + pts[s].x) / 2;
        var cpy = (pts[s-1].y + pts[s].y) / 2;
        ctx.quadraticCurveTo(pts[s-1].x, pts[s-1].y, cpx, cpy);
      }

      // Bottom edge (shifted + noise offset)
      ctx.lineTo(w + 10, pts[pts.length-1].y + thickness);
      for (var s = pts.length - 1; s >= 0; s--) {
        var bNoise = fbm(pts[s].x * 0.005 + ph * L.speed + 10, li * 2 + ph * 0.4, 2);
        var bOff = thickness + bNoise * thickness * 0.3;
        ctx.lineTo(pts[s].x, pts[s].y + bOff);
      }
      ctx.closePath();

      // Gradient fill — vertical through band
      var topY = L.baseY * h - 20;
      var botY = L.baseY * h + thickness + 60;
      var grad = ctx.createLinearGradient(0, topY, 0, botY);
      grad.addColorStop(0, rgba(cr, cg, cb, 0));
      grad.addColorStop(0.2, rgba(cr, cg, cb, bandAlpha * 0.6));
      grad.addColorStop(0.5, rgba(cr, cg, cb, bandAlpha));
      grad.addColorStop(0.8, rgba(cr, cg, cb, bandAlpha * 0.5));
      grad.addColorStop(1, rgba(cr, cg, cb, 0));
      ctx.fillStyle = grad;
      ctx.fill();
    }

    // Bright spine
    this._drawSpine(ctx, w, h, t, ghost, m, inten, breath, sc);
  }

  _drawSpine(ctx, w, h, t, ghost, m, inten, breath, sc) {
    var ph = this._auroraPhase;
    ctx.beginPath();
    var segs = 80;
    var pts = [];
    for (var s = 0; s <= segs; s++) {
      var sx = s / segs;
      var n = fbm(sx * 4.5 + ph * 1.3, ph * 0.5 + 3.3, 5);
      var py = (0.38 + n * 0.07 + breath * 0.008) * h;
      var px = sx * w;
      pts.push({ x: px, y: py });
    }

    // Smooth path
    ctx.moveTo(pts[0].x, pts[0].y);
    for (var s = 1; s < pts.length; s++) {
      var cpx = (pts[s-1].x + pts[s].x) / 2;
      var cpy = (pts[s-1].y + pts[s].y) / 2;
      ctx.quadraticCurveTo(pts[s-1].x, pts[s-1].y, cpx, cpy);
    }

    // Wide soft glow
    ctx.strokeStyle = rgba(m.r, m.g, m.b, (0.02 + inten * 0.05) * ghost);
    ctx.lineWidth = (25 + inten * 50) * sc;
    ctx.stroke();

    // Medium glow
    ctx.strokeStyle = rgba(m.r + 30, m.g + 30, m.b + 30, (0.04 + inten * 0.1) * ghost);
    ctx.lineWidth = (6 + inten * 12) * sc;
    ctx.stroke();

    // Thin bright line
    ctx.strokeStyle = rgba(
      clamp(m.r + 80, 0, 255), clamp(m.g + 80, 0, 255), clamp(m.b + 80, 0, 255),
      (0.15 + inten * 0.45) * ghost
    );
    ctx.lineWidth = (1 + inten * 2.5) * sc;
    ctx.stroke();
  }

  _drawSparks(ctx, w, h, t, ghost, m, inten) {
    // Trim excess sparks
    while (this._sparks.length > 120) this._sparks.shift();

    for (var i = this._sparks.length - 1; i >= 0; i--) {
      var p = this._sparks[i];
      p.life++;
      p.phase += 0.06;
      p.x += p.vx + Math.sin(p.phase * 0.4 + i) * 0.00008;
      p.y += p.vy;

      if (p.life > p.maxLife) {
        // Respawn
        if (this._sparks.length > 90) { this._sparks.splice(i, 1); continue; }
        var np = this._newSpark(); for (var k in np) p[k] = np[k];
        continue;
      }

      if (p.x < -0.05 || p.x > 1.05 || p.y < 0 || p.y > 1) {
        var np = this._newSpark(); for (var k in np) p[k] = np[k];
        continue;
      }

      var lifeFrac = p.life / p.maxLife;
      var fadeIn = Math.min(1, lifeFrac * 6);
      var fadeOut = lifeFrac > 0.7 ? Math.max(0, 1 - (lifeFrac - 0.7) / 0.3) : 1;
      var alpha = p.maxAlpha * fadeIn * fadeOut * (0.4 + inten * 0.6) * ghost;
      if (alpha < 0.01) continue;

      var px = p.x * w, py = p.y * h;
      var sz = p.size * (0.7 + 0.3 * Math.sin(p.phase));

      // Glow
      var gr = ctx.createRadialGradient(px, py, 0, px, py, sz * 5);
      gr.addColorStop(0, rgba(m.r + 50, m.g + 50, m.b + 50, alpha * 0.3));
      gr.addColorStop(1, rgba(m.r, m.g, m.b, 0));
      ctx.beginPath(); ctx.arc(px, py, sz * 5, 0, TAU);
      ctx.fillStyle = gr; ctx.fill();

      // Bright core
      ctx.beginPath(); ctx.arc(px, py, sz * 0.5, 0, TAU);
      ctx.fillStyle = rgba(255, 255, 255, alpha * 0.6);
      ctx.fill();
    }
  }

  _drawRipples(ctx, w, h, ghost) {
    var cx = w * 0.5, cy = h * 0.4;
    for (var i = this._ripples.length - 1; i >= 0; i--) {
      var rp = this._ripples[i];
      rp.t += 2;
      rp.alpha -= 0.006;
      if (rp.alpha <= 0) { this._ripples.splice(i, 1); continue; }
      var r = rp.t * Math.min(w, h) * 0.005;

      // Elliptical spread
      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(2.2, 0.5);
      ctx.beginPath(); ctx.arc(0, 0, r, 0, TAU);
      ctx.restore();
      ctx.strokeStyle = rgba(rp.rgb[0], rp.rgb[1], rp.rgb[2], rp.alpha * 0.3 * ghost);
      ctx.lineWidth = 2 + rp.alpha * 3;
      ctx.stroke();

      // Inner brighter
      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(2.2, 0.5);
      ctx.beginPath(); ctx.arc(0, 0, r * 0.7, 0, TAU);
      ctx.restore();
      ctx.strokeStyle = rgba(rp.rgb[0], rp.rgb[1], rp.rgb[2], rp.alpha * 0.15 * ghost);
      ctx.lineWidth = 6 + rp.alpha * 8;
      ctx.stroke();
    }
  }

  _drawHeart(ctx, w, h, t, ghost, m, inten, breath, sc) {
    var cx = w * 0.5, cy = h * 0.4;
    var heartR = (35 + inten * 35 + breath * 6) * sc;

    // Very wide ambient glow
    var g0 = ctx.createRadialGradient(cx, cy, 0, cx, cy, heartR * 6);
    g0.addColorStop(0, rgba(m.r, m.g, m.b, (0.04 + inten * 0.06) * ghost));
    g0.addColorStop(0.3, rgba(m.r, m.g, m.b, (0.015 + inten * 0.02) * ghost));
    g0.addColorStop(1, rgba(m.r, m.g, m.b, 0));
    ctx.beginPath(); ctx.arc(cx, cy, heartR * 6, 0, TAU);
    ctx.fillStyle = g0; ctx.fill();

    // Mid glow
    var g1 = ctx.createRadialGradient(cx, cy, 0, cx, cy, heartR * 2.5);
    g1.addColorStop(0, rgba(m.r + 30, m.g + 30, m.b + 30, (0.1 + inten * 0.2) * ghost));
    g1.addColorStop(0.5, rgba(m.r, m.g, m.b, (0.03 + inten * 0.05) * ghost));
    g1.addColorStop(1, rgba(m.r, m.g, m.b, 0));
    ctx.beginPath(); ctx.arc(cx, cy, heartR * 2.5, 0, TAU);
    ctx.fillStyle = g1; ctx.fill();

    // Bright core
    var g2 = ctx.createRadialGradient(cx, cy, 0, cx, cy, heartR * 0.8);
    g2.addColorStop(0, rgba(255, 255, 255, (0.25 + inten * 0.45 + breath * 0.08) * ghost));
    g2.addColorStop(0.2, rgba(m.r + 60, m.g + 60, m.b + 60, (0.15 + inten * 0.25) * ghost));
    g2.addColorStop(1, rgba(m.r, m.g, m.b, 0));
    ctx.beginPath(); ctx.arc(cx, cy, heartR * 0.8, 0, TAU);
    ctx.fillStyle = g2; ctx.fill();

    // White-hot center dot
    var dotR = heartR * 0.08;
    var g3 = ctx.createRadialGradient(cx, cy, 0, cx, cy, dotR);
    g3.addColorStop(0, rgba(255, 255, 255, (0.8 + breath * 0.2) * ghost));
    g3.addColorStop(1, rgba(255, 255, 255, 0));
    ctx.beginPath(); ctx.arc(cx, cy, dotR, 0, TAU);
    ctx.fillStyle = g3; ctx.fill();
  }

  _drawText(ctx, w, h, t, ghost, m, inten, sc) {
    var cx = w * 0.5;
    var baseY = h * 0.4;

    // ATHENA name
    if (this._nameReveal < 6) this._nameReveal += 0.015;
    var nameA = Math.min(1, this._nameReveal) * (0.06 + inten * 0.09 + Math.sin(t * 0.005) * 0.015);
    ctx.textAlign = 'center';
    ctx.font = '200 ' + (48 * sc) + 'px Inter,sans-serif';
    ctx.fillStyle = rgba(clamp(m.r+60,0,255), clamp(m.g+60,0,255), clamp(m.b+60,0,255), nameA * ghost);
    ctx.fillText('ATHENA', cx, baseY + 95 * sc);

    // Pipeline stage
    if (this.pipelineStage) {
      ctx.font = '300 ' + (9 * sc) + "px 'JetBrains Mono',monospace";
      ctx.fillStyle = rgba(m.r, m.g, m.b, 0.22 * ghost);
      ctx.fillText(this.pipelineStage.toUpperCase(), cx, baseY + 112 * sc);
    }

    // Status typewriter
    if (this._statusReveal < this._statusChars.length) this._statusReveal += 0.15;
    var revealed = this._statusChars.slice(0, Math.floor(this._statusReveal)).join('');
    var blink = Math.floor(t * 0.06) % 2 === 0 && this._statusReveal < this._statusChars.length;
    ctx.font = '500 ' + (10 * sc) + "px 'JetBrains Mono',monospace";
    ctx.fillStyle = rgba(m.r, m.g, m.b, (0.35 + 0.1 * Math.sin(t * 0.02)) * ghost);
    ctx.fillText(revealed + (blink ? '\u2588' : ''), cx, baseY + 130 * sc);

    // Budget critical
    if (this.usageCrit) {
      ctx.font = '600 ' + (8 * sc) + 'px Inter,sans-serif';
      ctx.fillStyle = rgba(239, 68, 68, (0.3 + 0.25 * Math.sin(t * 0.12)) * ghost);
      ctx.fillText('\u26A0 BUDGET CRITICAL', cx, baseY + 148 * sc);
    }
  }

  _drawBudget(ctx, w, h, t, ghost, m, sc) {
    var us = spring(this._dispUsed, this.tokenBudget.used, this._usedVel, 0.06, 0.75);
    this._dispUsed = us.v; this._usedVel = us.vel;
    var pct = Math.min(this._dispUsed / Math.max(this.tokenBudget.total, 1), 1);
    if (pct < 0.005) return;

    var cx = w * 0.5;
    var barW = 130 * sc, barH = 2;
    var by = h * 0.4 + 162 * sc;

    ctx.fillStyle = rgba(255,255,255, 0.03 * ghost);
    ctx.fillRect(cx - barW/2, by, barW, barH);

    var bRGB = pct > 0.9 ? [239,68,68] : pct > 0.7 ? [234,179,8] : [60,200,140];
    ctx.fillStyle = rgba(bRGB[0], bRGB[1], bRGB[2], 0.4 * ghost);
    ctx.fillRect(cx - barW/2, by, barW * pct, barH);

    ctx.textAlign = 'center';
    ctx.font = '400 ' + (6 * sc) + "px 'JetBrains Mono',monospace";
    ctx.fillStyle = rgba(m.r, m.g, m.b, 0.18 * ghost);
    ctx.fillText(Math.round(this._dispUsed) + ' / ' + this.tokenBudget.total, cx, by + 13 * sc);
  }

  _drawGlitch(ctx, w, h) {
    var it = this._glitchI;
    var n = 3 + Math.floor(Math.random() * 5);
    for (var i = 0; i < n; i++) {
      var y = Math.random() * h, sh = 2 + Math.random() * 18 * it;
      var off = (Math.random() - 0.5) * 30 * it;
      try {
        var img = ctx.getImageData(0, Math.floor(y), Math.floor(w), Math.floor(Math.max(1, sh)));
        ctx.putImageData(img, Math.floor(off), Math.floor(y));
      } catch(e) {}
    }
    if (Math.random() < it * 0.3) {
      ctx.fillStyle = rgba(239, 68, 68, it * 0.06);
      ctx.fillRect(0, 0, w, h);
    }
  }
}
