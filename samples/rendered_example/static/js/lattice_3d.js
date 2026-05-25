/* Interspace 3D lattice viewer.
 *
 * Uses 3d-force-graph (Three.js wrapper) to render nodes/edges in 3D space.
 * Click any sphere to open its detail page. Camera: drag = rotate, scroll =
 * zoom, right-click+drag = pan.
 *
 * Live runner layer (when the server is started with --live):
 *   - Subscribes to /api/runners/stream (SSE)
 *   - Three runner sprites (T-cell cyan / REL gold / NEG-T indigo) wander
 *     the lattice on a jittered 1-3s visual clock — independent of the
 *     server discovery cycle so the lattice always feels alive
 *   - When the server broadcasts a new edge discovery, the responsible
 *     runner "flies to" the edge endpoints, the new edge pulses bright,
 *     and the cluster reshuffles via continuous force simulation
 */
(function () {
  "use strict";

  var FALLBACK_COLOR = "#888";

  // Runner palette — shades of white with a single accent per runner
  var RUNNER_PALETTE = {
    "t-cell": { accent: "#7de3f0", trail: "#cdebef" }, // cyan
    "rel":    { accent: "#e8c873", trail: "#f0e2bd" }, // gold
    "neg-t":  { accent: "#9d8bef", trail: "#cfc7f0" }  // indigo
  };

  // Visual clock — jittered range for runner step cadence (ms)
  var STEP_MIN_MS = 1000;
  var STEP_MAX_MS = 3000;

  // Glow / pulse durations (ms)
  var NODE_PULSE_MS = 900;
  var EDGE_PULSE_MS = 1800;

  function readData() {
    var el = document.getElementById("lattice-data");
    if (!el) {
      console.error("[interspace3d] #lattice-data missing");
      return null;
    }
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (e) {
      console.error("[interspace3d] failed to parse #lattice-data JSON:", e);
      return null;
    }
  }

  function buildClusterColorMap(clusters) {
    var map = {};
    (clusters || []).forEach(function (c) {
      map[c.id] = c.color || FALLBACK_COLOR;
    });
    return map;
  }

  function init() {
    var container = document.getElementById("lattice-3d");
    if (!container) return;
    var data = readData();
    if (!data) return;
    if (typeof ForceGraph3D !== "function") {
      console.error("[interspace3d] ForceGraph3D global missing — was 3d-force-graph.min.js loaded?");
      container.textContent = "3D renderer failed to load.";
      return;
    }

    var clusterColors = buildClusterColorMap(data.clusters);

    var nodes = data.nodes.map(function (n) {
      return {
        id: n.id,
        label: n.label || n.id,
        cluster: n.cluster || "uncategorized",
        weight: typeof n.weight === "number" ? n.weight : 1.0,
        archived: !!n.archived,
        phase: n.phase || (n.archived ? "archived" : "current"),
        tags: n.tags || []
      };
    });
    var links = (data.edges || []).map(function (e) {
      return { source: e.source, target: e.target, kind: e.kind || "related" };
    });

    // Per-node visual-state map used to drive runner glow.
    // glowUntil = ms epoch when current glow fades; runner = which palette to use.
    var nodeGlow = Object.create(null);

    // Per-edge visual-state map (keyed by "src|tgt|kind"). Used for fresh-edge
    // pulse — bright white-cyan-gold-indigo flash that fades to default over
    // EDGE_PULSE_MS.
    var edgePulse = Object.create(null);
    function edgeKey(e) {
      var s = (e.source && e.source.id) || e.source;
      var t = (e.target && e.target.id) || e.target;
      return s + "|" + t + "|" + (e.kind || "related");
    }

    var graph = ForceGraph3D()(container)
      .graphData({ nodes: nodes, links: links })
      .backgroundColor(getComputedStyle(document.body).backgroundColor || "#0d0e11")
      .nodeId("id")
      .nodeLabel(function (n) {
        var phase = n.phase === "current" ? "" : " [" + n.phase + "]";
        return n.label + phase;
      })
      .nodeColor(function (n) {
        var now = Date.now();
        var g = nodeGlow[n.id];
        if (g && g.until > now) {
          // Blend: accent over base, weighted by remaining time.
          var frac = (g.until - now) / NODE_PULSE_MS;
          var pal = RUNNER_PALETTE[g.runner] || RUNNER_PALETTE["t-cell"];
          return blendHex(baseNodeColor(n, clusterColors), pal.accent, Math.min(1, frac));
        }
        return baseNodeColor(n, clusterColors);
      })
      .nodeVal(function (n) {
        return Math.max(1, n.weight * n.weight * 4);
      })
      .nodeOpacity(0.9)
      .linkColor(function (e) {
        var now = Date.now();
        var key = edgeKey(e);
        var p = edgePulse[key];
        if (p && p.until > now) {
          var frac = (p.until - now) / EDGE_PULSE_MS;
          var pal = RUNNER_PALETTE[p.runner] || RUNNER_PALETTE["t-cell"];
          return rgbaWithAlpha(pal.accent, 0.4 + frac * 0.55);
        }
        return "rgba(200, 200, 200, 0.55)";
      })
      .linkWidth(function (e) {
        var key = edgeKey(e);
        var p = edgePulse[key];
        if (p && p.until > Date.now()) {
          var frac = (p.until - Date.now()) / EDGE_PULSE_MS;
          return 1.5 + frac * 2.5;
        }
        return 1.5;
      })
      .linkOpacity(0.6)
      .linkDirectionalArrowLength(3.5)
      .linkDirectionalArrowRelPos(0.9)
      .linkDirectionalArrowColor(function () { return "rgba(170, 170, 170, 0.85)"; })
      .onNodeClick(function (n) {
        if (!n || !n.id) return;
        window.location.href = "nodes/" + encodeURIComponent(n.id) + ".html";
      });

    // Keep the simulation slightly warm so cluster reshuffles after edge
    // additions are visible rather than instantaneous freezes.
    if (typeof graph.cooldownTicks === "function") {
      graph.cooldownTicks(Infinity).cooldownTime(Infinity);
    }
    if (typeof graph.d3AlphaDecay === "function") {
      graph.d3AlphaDecay(0.005); // slower decay = more ambient motion
    }

    function resize() {
      graph.width(container.clientWidth);
      graph.height(container.clientHeight);
    }
    window.addEventListener("resize", resize);
    setTimeout(resize, 0);

    wireZoomControls(container, graph);

    // ----------------------------------------------------------------
    // Live runner layer — visual clock + SSE subscription
    // ----------------------------------------------------------------
    var nodeIndex = Object.create(null);
    graph.graphData().nodes.forEach(function (n) { nodeIndex[n.id] = n; });

    var runnerState = {
      "t-cell": { currentId: pickRandomNodeId(graph), nextStepAt: 0 },
      "rel":    { currentId: pickRandomNodeId(graph), nextStepAt: 0 },
      "neg-t":  { currentId: pickRandomNodeId(graph), nextStepAt: 0 }
    };

    function pickRandomNodeId(g) {
      var ns = g.graphData().nodes;
      if (!ns.length) return null;
      return ns[(Math.random() * ns.length) | 0].id;
    }

    function pickWalkTarget(g, fromId) {
      // Prefer a neighbor; fall back to a random node if no edges out.
      var ls = g.graphData().links;
      var neighbors = [];
      for (var i = 0; i < ls.length; i++) {
        var l = ls[i];
        var s = (l.source && l.source.id) || l.source;
        var t = (l.target && l.target.id) || l.target;
        if (s === fromId) neighbors.push(t);
        else if (t === fromId) neighbors.push(s);
      }
      if (neighbors.length) {
        return neighbors[(Math.random() * neighbors.length) | 0];
      }
      return pickRandomNodeId(g);
    }

    // Visual clock — runs always, regardless of SSE state.
    function visualClock() {
      var now = Date.now();
      Object.keys(runnerState).forEach(function (rname) {
        var st = runnerState[rname];
        if (now < st.nextStepAt) return;
        // Step: pick a new target, glow the node, schedule next step
        var nextId = pickWalkTarget(graph, st.currentId);
        if (nextId) {
          st.currentId = nextId;
          nodeGlow[nextId] = { runner: rname, until: now + NODE_PULSE_MS };
        }
        st.nextStepAt = now + STEP_MIN_MS + Math.random() * (STEP_MAX_MS - STEP_MIN_MS);
      });
      // Trigger re-render so glow updates draw.
      if (typeof graph.refresh === "function") graph.refresh();
      window.requestAnimationFrame(visualClock);
    }
    window.requestAnimationFrame(visualClock);

    // SSE subscription — connects when server has --live enabled.
    var statusEl = document.getElementById("lattice-runner-status");
    var runnersBar = document.getElementById("lattice-runners");
    if (window.EventSource) {
      try {
        var es = new EventSource("/api/runners/stream");
        es.addEventListener("hello", function () {
          if (runnersBar) runnersBar.hidden = false;
          if (statusEl) statusEl.textContent = "live";
        });
        es.addEventListener("error", function () {
          // Server not in --live mode (404), or connection dropped. Keep
          // visual clock running so the lattice still feels alive.
          if (statusEl) statusEl.textContent = "offline";
        });
        es.onmessage = function (ev) {
          var msg = null;
          try { msg = JSON.parse(ev.data || "{}"); } catch (e) { return; }
          if (msg.type === "edge_added" && msg.edge) {
            handleEdgeAdded(msg.runner || "t-cell", msg.edge);
          } else if (msg.type === "cycle_complete" && statusEl) {
            statusEl.textContent = "live · +" + msg.added + " from " + msg.runner;
            window.setTimeout(function () {
              if (statusEl) statusEl.textContent = "live";
            }, 3000);
          }
        };
      } catch (e) {
        if (statusEl) statusEl.textContent = "offline";
      }
    }

    function handleEdgeAdded(runnerName, edge) {
      // Skip if endpoints unknown to this lattice (shouldn't happen since
      // server is reading the same lattice.html).
      if (!nodeIndex[edge.source] || !nodeIndex[edge.target]) return;
      var current = graph.graphData();
      // Avoid duplicate (server is the source of truth but the diff check
      // there should already handle this — defensive).
      var key = edge.source + "|" + edge.target + "|" + (edge.kind || "related");
      for (var i = 0; i < current.links.length; i++) {
        var l = current.links[i];
        var sk = ((l.source && l.source.id) || l.source) + "|" +
                 ((l.target && l.target.id) || l.target) + "|" +
                 (l.kind || "related");
        if (sk === key) return;
      }
      // Append link; reuse existing node refs.
      current.links.push({
        source: edge.source,
        target: edge.target,
        kind: edge.kind || "related"
      });
      graph.graphData(current);
      edgePulse[key] = { runner: runnerName, until: Date.now() + EDGE_PULSE_MS };

      // Snap responsible runner to one endpoint, glow both endpoints.
      var st = runnerState[runnerName];
      if (st) {
        st.currentId = edge.source;
        st.nextStepAt = Date.now() + STEP_MIN_MS;
      }
      nodeGlow[edge.source] = { runner: runnerName, until: Date.now() + NODE_PULSE_MS };
      nodeGlow[edge.target] = { runner: runnerName, until: Date.now() + NODE_PULSE_MS * 1.5 };

      // Re-warm simulation so cluster reshuffle is visible.
      if (typeof graph.d3ReheatSimulation === "function") {
        graph.d3ReheatSimulation();
      }
    }
  }

  function baseNodeColor(n, clusterColors) {
    var c = clusterColors[n.cluster] || FALLBACK_COLOR;
    if (n.archived) return dim(c, 0.4);
    if (n.phase === "foundation") return dim(c, 0.7);
    return c;
  }

  function wireZoomControls(container, graph) {
    var wrap = container.parentElement;
    if (!wrap) return;
    var buttons = wrap.querySelectorAll(".lattice-zoom__btn");
    if (!buttons.length) return;
    var ZOOM_FACTOR = 0.7;
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.getAttribute("data-zoom");
        if (mode === "fit") {
          graph.zoomToFit(500, 40);
          return;
        }
        var cam = graph.cameraPosition();
        if (!cam) return;
        var factor = mode === "in" ? ZOOM_FACTOR : 1 / ZOOM_FACTOR;
        graph.cameraPosition(
          { x: cam.x * factor, y: cam.y * factor, z: cam.z * factor },
          null,
          300
        );
      });
    });
  }

  // ---------- color utilities ----------
  function dim(hex, factor) {
    var m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    var r = (n >> 16) & 0xff;
    var g = (n >> 8) & 0xff;
    var b = n & 0xff;
    return "rgba(" + r + "," + g + "," + b + "," + factor.toFixed(2) + ")";
  }

  function hexToRgb(s) {
    var m = /^#?([0-9a-f]{6})$/i.exec(s);
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return { r: (n >> 16) & 0xff, g: (n >> 8) & 0xff, b: n & 0xff };
  }

  function rgbaWithAlpha(hex, alpha) {
    var c = hexToRgb(hex);
    if (!c) return hex;
    return "rgba(" + c.r + "," + c.g + "," + c.b + "," + alpha.toFixed(2) + ")";
  }

  // Blend base color toward `accent` by `frac` (0..1). base can be hex or rgba string.
  function blendHex(base, accent, frac) {
    var bc, ba;
    var rgbaM = /^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)$/.exec(base);
    if (rgbaM) {
      bc = { r: +rgbaM[1], g: +rgbaM[2], b: +rgbaM[3] };
      ba = +rgbaM[4];
    } else {
      bc = hexToRgb(base) || { r: 136, g: 136, b: 136 };
      ba = 1.0;
    }
    var ac = hexToRgb(accent) || bc;
    var f = Math.max(0, Math.min(1, frac));
    var r = Math.round(bc.r * (1 - f) + ac.r * f);
    var g = Math.round(bc.g * (1 - f) + ac.g * f);
    var b = Math.round(bc.b * (1 - f) + ac.b * f);
    return "rgba(" + r + "," + g + "," + b + "," + ba.toFixed(2) + ")";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
