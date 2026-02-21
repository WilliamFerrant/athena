/**
 * living-diagram.js — Athena Orchestration Diagram
 *
 * Renders a hub-and-spoke diagram in #livingDiagram (inside .bridge-layer).
 * State is managed by LivingDiagram singleton and exposed via:
 *   LD.setAgentState(agentId, state, task?)
 *   LD.setGlobalState(state)
 *   LD.setRunnerOnline(bool)
 *   LD.setHealthStatus('green'|'warn'|'incident')
 *   LD.setFocusedProject(name)
 *   LD.setLastEvent(text)
 *
 * All positions are computed as percentages of the container, then
 * converted to px on resize. SVG lines connect hub ↔ spokes.
 */

(function () {
  'use strict';

  // ── Agent definitions ────────────────────────────────────────────────────
  const AGENTS = [
    { id: 'manager',    label: 'MANAGER'    },
    { id: 'frontend',   label: 'FRONTEND'   },
    { id: 'backend',    label: 'BACKEND'    },
    { id: 'tester',     label: 'TESTER'     },
    { id: 'security',   label: 'SECURITY'   },
  ];

  // State idle timeout — agents revert to IDLE after 60s without an event
  const IDLE_TIMEOUT_MS = 60_000;

  // ── Internal state ───────────────────────────────────────────────────────
  const _state = {
    global:         'idle',       // 'idle'|'active'|'degraded'|'error'
    runnerOnline:   false,
    healthStatus:   'green',      // 'green'|'warn'|'incident'
    focusedProject: null,
    lastEvent:      '',
    agents: Object.fromEntries(
      AGENTS.map(a => [a.id, { state: 'idle', task: '', lastUpdated: 0 }])
    ),
  };

  // Idle-timeout handles per agent
  const _idleTimers = {};

  // DOM element refs (populated by init)
  let _container = null;
  let _svg = null;
  let _nodesEl = null;
  let _hubEl = null;
  let _agentEls = {};   // agentId → node div
  let _spokeEls = {};   // agentId → <line> SVG el
  let _flowEls  = {};   // agentId → <circle> SVG el (flow particle)
  let _flowAnims = {};  // agentId → { rAF id, progress, direction }

  let _resizeObs = null;

  // ── Public API ───────────────────────────────────────────────────────────
  const LD = {
    /**
     * Set the state and optional task label for an agent spoke.
     * @param {string} agentId  - e.g. 'backend'
     * @param {string} state    - 'idle'|'planning'|'executing'|'reviewing'|'waiting'|'error'|'done'
     * @param {string} [task]   - short task description
     * @param {string} [dir]    - 'out' (hub→agent) | 'in' (agent→hub) | null
     */
    setAgentState(agentId, state, task = '', dir = null) {
      const s = _state.agents[agentId];
      if (!s) return;
      s.state = state;
      s.task  = task || '';
      s.lastUpdated = Date.now();

      // Reset idle timer
      clearTimeout(_idleTimers[agentId]);
      if (state !== 'idle') {
        _idleTimers[agentId] = setTimeout(() => {
          LD.setAgentState(agentId, 'idle');
        }, IDLE_TIMEOUT_MS);
      }

      _renderAgentNode(agentId);
      _updateSpoke(agentId, state, dir);
      _updateGlobalState();
    },

    /** Reset all agents to idle (e.g. mission complete). */
    resetAllAgents(state = 'idle') {
      AGENTS.forEach(a => LD.setAgentState(a.id, state));
    },

    setGlobalState(state) {
      _state.global = state;
      _renderHub();
    },

    setRunnerOnline(online) {
      _state.runnerOnline = online;
      _renderHub();
    },

    setHealthStatus(status) {
      _state.healthStatus = status;
      _renderHub();
    },

    setFocusedProject(name) {
      _state.focusedProject = name || null;
      _renderHub();
    },

    setLastEvent(text) {
      _state.lastEvent = text || '';
      _renderHub();
    },

    /** Initialise the diagram inside containerEl. Idempotent. */
    init(containerEl) {
      if (_container) return; // already initialised
      _container = containerEl;
      _build();
      _layout();
      _resizeObs = new ResizeObserver(_layout);
      _resizeObs.observe(_container);
    },

    destroy() {
      if (_resizeObs) _resizeObs.disconnect();
      Object.values(_idleTimers).forEach(clearTimeout);
      Object.values(_flowAnims).forEach(a => cancelAnimationFrame(a.raf));
      if (_container) _container.innerHTML = '';
      _container = null;
    },
  };

  // ── Build DOM ────────────────────────────────────────────────────────────
  function _build() {
    _container.innerHTML = '';

    // SVG for spoke lines + flow particles
    _svg = _el('svg', { id: 'ldSvg', 'aria-hidden': 'true' });

    // Node layer (absolute-positioned divs)
    _nodesEl = _el('div', { class: 'ld-nodes' });

    _container.appendChild(_svg);
    _container.appendChild(_nodesEl);

    // Build ATHENA hub card
    _hubEl = _buildHubCard();
    _nodesEl.appendChild(_hubEl);

    // Build spoke lines + agent cards
    AGENTS.forEach(ag => {
      // SVG line
      const line = _svgEl('line', { class: 'ld-spoke', id: `ld-spoke-${ag.id}` });
      _svg.appendChild(line);
      _spokeEls[ag.id] = line;

      // SVG flow particle
      const dot = _svgEl('circle', { class: 'ld-flow', id: `ld-flow-${ag.id}`, r: '3' });
      _svg.appendChild(dot);
      _flowEls[ag.id] = dot;

      // Agent node card
      const card = _buildAgentCard(ag);
      _nodesEl.appendChild(card);
      _agentEls[ag.id] = card;
    });
  }

  function _buildHubCard() {
    const node = _el('div', { class: 'ld-node hub', id: 'ld-hub', 'data-agent': 'athena' });
    node.innerHTML = `
      <div class="ld-node-header">
        <span class="ld-node-name">ATHENA</span>
        <span class="ld-badge idle" id="ld-global-badge-inline">IDLE</span>
      </div>
      <div class="ld-hub-meta">
        <div class="ld-meta-row">
          <span class="ld-meta-key">PROJECT</span>
          <span class="ld-meta-val" id="ld-meta-project">—</span>
        </div>
        <div class="ld-meta-row">
          <span class="ld-meta-key">RUNNER</span>
          <span class="ld-meta-val offline" id="ld-meta-runner">OFFLINE</span>
        </div>
        <div class="ld-meta-row">
          <span class="ld-meta-key">HEALTH</span>
          <span class="ld-meta-val" id="ld-meta-health">—</span>
        </div>
        <div class="ld-meta-row" id="ld-meta-event-row" style="display:none">
          <span class="ld-meta-key">LAST</span>
          <span class="ld-meta-val" id="ld-meta-event"></span>
        </div>
      </div>
    `;
    return node;
  }

  function _buildAgentCard(ag) {
    const node = _el('div', {
      class: 'ld-node',
      id: `ld-node-${ag.id}`,
      'data-agent': ag.id,
    });
    node.innerHTML = `
      <div class="ld-node-header">
        <span class="ld-node-name">${ag.label}</span>
        <span class="ld-badge idle" id="ld-badge-${ag.id}">IDLE</span>
      </div>
      <div class="ld-node-task" id="ld-task-${ag.id}"></div>
    `;
    return node;
  }

  // ── Layout (positions) ───────────────────────────────────────────────────
  /**
   * Compute hub + spoke positions.
   * Hub is at center. Agents are placed in a ring, evenly distributed.
   * Angles start at top (-90°) and go clockwise.
   */
  function _layout() {
    if (!_container) return;
    const W = _container.offsetWidth;
    const H = _container.offsetHeight;
    if (!W || !H) return;

    const cx = W / 2;
    const cy = H / 2;

    // Radius from center to agent node centers (as fraction of smaller dim)
    const radius = Math.min(W, H) * 0.32;

    // Place hub
    _hubEl.style.left = cx + 'px';
    _hubEl.style.top  = cy + 'px';

    const n = AGENTS.length;
    AGENTS.forEach((ag, i) => {
      // Evenly distribute, start from top (-90°)
      const angle = (Math.PI * 2 * i / n) - Math.PI / 2;
      const ax = cx + Math.cos(angle) * radius;
      const ay = cy + Math.sin(angle) * radius;

      const card = _agentEls[ag.id];
      card.style.left = ax + 'px';
      card.style.top  = ay + 'px';

      // SVG spoke: from hub center to agent center
      // Account for card half-width being 60px (120px / 2)
      const spoke = _spokeEls[ag.id];
      spoke.setAttribute('x1', cx);
      spoke.setAttribute('y1', cy);
      spoke.setAttribute('x2', ax);
      spoke.setAttribute('y2', ay);

      // Store endpoint for flow particles
      _flowEls[ag.id]._hub = { x: cx, y: cy };
      _flowEls[ag.id]._agent = { x: ax, y: ay };
    });
  }

  // ── Render helpers ───────────────────────────────────────────────────────
  function _renderHub() {
    if (!_hubEl) return;

    // Global state badge (inline in header)
    const badge = document.getElementById('ld-global-badge-inline');
    if (badge) {
      badge.textContent = _state.global.toUpperCase();
      badge.className = `ld-badge ${_state.global}`;
    }

    // Project
    const proj = document.getElementById('ld-meta-project');
    if (proj) proj.textContent = _state.focusedProject || '—';

    // Runner
    const runner = document.getElementById('ld-meta-runner');
    if (runner) {
      runner.textContent = _state.runnerOnline ? 'ONLINE' : 'OFFLINE';
      runner.className   = `ld-meta-val ${_state.runnerOnline ? 'online' : 'offline'}`;
    }

    // Health
    const health = document.getElementById('ld-meta-health');
    if (health) {
      const map = { green: 'ALL GREEN', warn: 'WARN', incident: 'INCIDENT' };
      health.textContent = map[_state.healthStatus] || _state.healthStatus.toUpperCase();
      health.className = `ld-meta-val ${_state.healthStatus === 'green' ? '' : _state.healthStatus === 'warn' ? 'warn' : 'offline'}`;
    }

    // Last event
    const evRow = document.getElementById('ld-meta-event-row');
    const evEl  = document.getElementById('ld-meta-event');
    if (evRow && evEl) {
      if (_state.lastEvent) {
        evRow.style.display = '';
        evEl.textContent = _state.lastEvent;
      } else {
        evRow.style.display = 'none';
      }
    }

    // Hub border colour based on global state
    if (_state.global === 'error') {
      _hubEl.style.borderColor = 'var(--red)';
    } else if (_state.global === 'degraded') {
      _hubEl.style.borderColor = '#c8a96e';
    } else if (_state.global === 'active') {
      _hubEl.style.borderColor = 'var(--accent)';
    } else {
      _hubEl.style.borderColor = '';
    }
  }

  function _renderAgentNode(agentId) {
    const s = _state.agents[agentId];
    if (!s) return;

    const badge = document.getElementById(`ld-badge-${agentId}`);
    const taskEl = document.getElementById(`ld-task-${agentId}`);

    if (badge) {
      badge.textContent = s.state.toUpperCase();
      badge.className   = `ld-badge ${s.state}`;
    }
    if (taskEl) {
      taskEl.textContent = s.task || '';
    }

    // Node border
    const card = _agentEls[agentId];
    if (card) {
      if (s.state === 'executing') {
        card.style.borderColor = 'var(--accent)';
      } else if (s.state === 'error') {
        card.style.borderColor = 'var(--red)';
      } else if (s.state === 'reviewing') {
        card.style.borderColor = '#a3c78a';
      } else if (s.state === 'planning') {
        card.style.borderColor = '#c8a96e';
      } else {
        card.style.borderColor = '';
      }
    }
  }

  function _updateSpoke(agentId, state, dir) {
    const spoke = _spokeEls[agentId];
    if (!spoke) return;

    const active = state !== 'idle' && state !== 'waiting';
    spoke.classList.toggle('active', active);

    // Start / stop flow particle
    if (active) {
      const direction = dir || (state === 'reviewing' ? 'in' : 'out');
      _startFlow(agentId, direction);
    } else {
      _stopFlow(agentId);
    }
  }

  // ── Flow particle animation ──────────────────────────────────────────────
  function _startFlow(agentId, direction) {
    if (_flowAnims[agentId]) return; // already running

    const dot   = _flowEls[agentId];
    if (!dot) return;

    let progress = 0; // 0..1
    const speed  = 0.008; // fraction of path per frame

    function step() {
      progress += speed;
      if (progress > 1) progress = 0;

      const hub   = dot._hub   || { x: 0, y: 0 };
      const agent = dot._agent || { x: 0, y: 0 };

      let t = progress;
      let sx, sy;

      if (direction === 'out') {
        sx = hub.x   + (agent.x - hub.x)   * t;
        sy = hub.y   + (agent.y - hub.y)   * t;
      } else {
        sx = agent.x + (hub.x   - agent.x) * t;
        sy = agent.y + (hub.y   - agent.y) * t;
      }

      // Fade in/out at edges
      const fade = t < 0.1 ? t / 0.1 : t > 0.9 ? (1 - t) / 0.1 : 1;

      dot.setAttribute('cx', sx);
      dot.setAttribute('cy', sy);
      dot.style.opacity = fade * 0.85;

      _flowAnims[agentId].raf = requestAnimationFrame(step);
    }

    _flowAnims[agentId] = { raf: null };
    _flowAnims[agentId].raf = requestAnimationFrame(step);
  }

  function _stopFlow(agentId) {
    if (_flowAnims[agentId]) {
      cancelAnimationFrame(_flowAnims[agentId].raf);
      delete _flowAnims[agentId];
    }
    const dot = _flowEls[agentId];
    if (dot) dot.style.opacity = '0';
    const spoke = _spokeEls[agentId];
    if (spoke) spoke.classList.remove('active');
  }

  // ── Derived global state ─────────────────────────────────────────────────
  function _updateGlobalState() {
    const anyActive = AGENTS.some(a => {
      const s = _state.agents[a.id].state;
      return s !== 'idle' && s !== 'waiting';
    });
    const anyError = AGENTS.some(a => _state.agents[a.id].state === 'error');

    let newGlobal;
    if (anyError || _state.healthStatus === 'incident') {
      newGlobal = 'error';
    } else if (!_state.runnerOnline || _state.healthStatus === 'warn') {
      newGlobal = anyActive ? 'active' : 'degraded';
    } else if (anyActive) {
      newGlobal = 'active';
    } else {
      newGlobal = 'idle';
    }

    if (newGlobal !== _state.global) {
      _state.global = newGlobal;
      _renderHub();
    }
  }

  // ── Utilities ────────────────────────────────────────────────────────────
  function _el(tag, attrs = {}, text = '') {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text) el.textContent = text;
    return el;
  }

  function _svgEl(tag, attrs = {}) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  // ── Export ───────────────────────────────────────────────────────────────
  window.LD = LD;
})();
