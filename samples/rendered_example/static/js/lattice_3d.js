/* Interspace 3D lattice viewer.
 *
 * Uses 3d-force-graph (Three.js wrapper) to render nodes/edges in 3D space.
 * Click any sphere to open its detail page. Camera: drag = rotate, scroll =
 * zoom, right-click+drag = pan.
 *
 * Live runner layer (when the server is started with --live):
 *   - Subscribes to /api/runners/stream (SSE)
 *   - Three ball-lightning globes (cool-white core + colored halo + fringe)
 *     traverse the lattice physically — interpolating along edges between
 *     source and target nodes. T-cell cyan / REL gold / NEG-T indigo.
 *   - Visual cadence: jittered 2-6s per hop (travel + pause). Independent
 *     of the server discovery clock so the lattice always feels alive.
 *   - When the server broadcasts a new edge discovery, the responsible
 *     runner snaps to the source endpoint and travels along the new bridge
 *     to the target. New edge pulses bright; the affected region rebalances
 *     via a brief alpha-target reheat (half of server cycle).
 *
 * Three motion clocks, decoupled:
 *   - Discovery clock (server, ~2min per runner) — actual semantic effect
 *   - Runner visual clock (client, 2-6s per hop) — ambient liveness signal
 *   - Cluster motion clock (event-triggered, 60s settle) — local rebalance
 */
(function () {
  "use strict";

  var FALLBACK_COLOR = "#888";

  // Runner palette — shades of white core, accent on the halo/fringe
  var RUNNER_PALETTE = {
    "t-cell": { accent: "#7de3f0" }, // cyan
    "rel":    { accent: "#e8c873" }, // gold
    "neg-t":  { accent: "#9d8bef" }  // indigo
  };

  // Globe travel cadence — slow + contemplative. Each hop is a 3-phase
  // sequence: CHARGE (lightning trace extends src->tgt) → TRAVEL (globe
  // crosses, trace dims) → PAUSE (globe rests at tgt).
  var CHARGE_MS = 700;        // trace extends source -> target
  var TRAVEL_MS = 3000;       // globe travels along extended trace
  var PAUSE_MIN_MS = 1000;    // settle at destination (min)
  var PAUSE_MAX_MS = 9000;    // settle at destination (max) — total cycle 4-12s
  var BRIDGE_PAUSE_MS = 800;  // shorter pause after a fresh-edge bridge fire

  // Pulse durations on new-edge events (the edge itself + its endpoints)
  var EDGE_PULSE_MS = 1800;
  var EDGE_ENDPOINT_PULSE_MS = 2400;

  // Local rebalance after a new edge — partial alpha-target reheat that
  // decays back to rest. Half the default server cycle (120s).
  var MOTION_DURATION_MS = 60_000;
  var MOTION_ALPHA_TARGET = 0.12;

  // Zoom-driven resolution gates. Camera distance from origin determines
  // which node kinds are visible — distant zoom hides atom-level detail
  // (paragraphs / findings / observations) and reveals only structural
  // anchors (folders, files). Zooming in unmasks finer granularity.
  // Spatial-hierarchy navigation per the pin's v0.5 direction.
  //
  // Thresholds are deliberately generous so typical "browsing" zooms stay
  // at full detail; gates only kick in when the operator intentionally
  // zooms out to see the whole structure.
  var RES_FAR_THRESHOLD = 10000;    // beyond this: folders only
  var RES_MEDIUM_THRESHOLD = 4500;  // beyond this: + files / composites / section_anchors / hand-curated concept nodes
                                    // closer than this: everything visible

  // Atom-class node kinds — hidden at far/medium zoom
  var ATOM_KINDS = { paragraph: 1, finding: 1, observation: 1 };
  // Mid-class node kinds — visible at medium zoom
  var MID_KINDS = { file: 1, composite: 1, section_anchor: 1, directory: 1 };

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

    // Cached node positions — restored on load so the lattice doesn't
    // re-layout from scratch every refresh / click / rotate. Saved on
    // simulation stop after each settle. Keyed by URL path so each
    // rendered dataset has its own cache.
    var POSITION_CACHE_KEY =
      "interspace.lattice.positions:" + window.location.pathname;
    var cachedPositions = null;
    try {
      var rawCache = window.localStorage.getItem(POSITION_CACHE_KEY);
      if (rawCache) cachedPositions = JSON.parse(rawCache);
    } catch (e) {
      cachedPositions = null;
    }

    var nodes = data.nodes.map(function (n) {
      var out = {
        id: n.id,
        label: n.label || n.id,
        cluster: n.cluster || "uncategorized",
        weight: typeof n.weight === "number" ? n.weight : 1.0,
        archived: !!n.archived,
        phase: n.phase || (n.archived ? "archived" : "current"),
        tags: n.tags || []
      };
      if (cachedPositions) {
        var p = cachedPositions[n.id];
        if (p && p.length >= 3) {
          // Seed positions so the simulation starts already-settled.
          out.x = p[0]; out.y = p[1]; out.z = p[2];
        }
      }
      return out;
    });
    var links = (data.edges || []).map(function (e) {
      return { source: e.source, target: e.target, kind: e.kind || "related" };
    });

    // Endpoint glow (TRANSIENT only — used for new-edge pulses, not for
    // runner presence). Runners themselves are Three.js globe meshes.
    var nodeGlow = Object.create(null);

    // Per-edge pulse map keyed by "src|tgt|kind".
    var edgePulse = Object.create(null);
    function edgeKey(e) {
      var s = (e.source && e.source.id) || e.source;
      var t = (e.target && e.target.id) || e.target;
      return s + "|" + t + "|" + (e.kind || "related");
    }

    // Resolution gate state must be declared BEFORE graph constructor —
    // nodeVisibility/linkVisibility callbacks reference these at evaluation
    // time, and var declarations are hoisted (so the symbol exists) but the
    // assignment is not (so it'd read undefined → undefined>=2 is false →
    // everything except folders gets filtered on first render).
    var currentResLevel = 2;  // start fully visible until camera moves
    function resolutionLevelFromDistance(d) {
      if (d > RES_FAR_THRESHOLD) return 0;
      if (d > RES_MEDIUM_THRESHOLD) return 1;
      return 2;
    }
    function isVisibleAtResolution(n, level) {
      if (level >= 2) return true;
      var meta = n.meta || {};
      var kind = meta.kind || null;
      if (kind === "folder") return true;
      if (level >= 1) {
        if (kind && MID_KINDS[kind]) return true;
        if (kind === null) return true; // hand-curated concept-only nodes
        return false;
      }
      return false;
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
        var g = nodeGlow[n.id];
        if (!g || !g.until) return baseNodeColor(n, clusterColors);
        var now = Date.now();
        if (g.until <= now) {
          delete nodeGlow[n.id];
          return baseNodeColor(n, clusterColors);
        }
        var pal = RUNNER_PALETTE[g.runner] || RUNNER_PALETTE["t-cell"];
        var frac = (g.until - now) / EDGE_ENDPOINT_PULSE_MS;
        return blendHex(baseNodeColor(n, clusterColors), pal.accent, frac);
      })
      .nodeVal(function (n) {
        return Math.max(1, n.weight * n.weight * 4);
      })
      .nodeOpacity(0.9)
      .linkColor(function (e) {
        var key = edgeKey(e);
        var p = edgePulse[key];
        if (!p || p.until <= Date.now()) {
          if (p) delete edgePulse[key];
          return "rgba(200, 200, 200, 0.55)";
        }
        var frac = (p.until - Date.now()) / EDGE_PULSE_MS;
        var pal = RUNNER_PALETTE[p.runner] || RUNNER_PALETTE["t-cell"];
        return rgbaWithAlpha(pal.accent, 0.4 + frac * 0.55);
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
      .nodeVisibility(function (n) {
        return isVisibleAtResolution(n, currentResLevel);
      })
      .linkVisibility(function (e) {
        // Endpoint may be either object ref or id string depending on
        // simulation state. Both forms supported.
        var s = (e.source && typeof e.source === "object") ? e.source : nodeIndex[e.source];
        var t = (e.target && typeof e.target === "object") ? e.target : nodeIndex[e.target];
        if (!s || !t) return false;
        return isVisibleAtResolution(s, currentResLevel) &&
               isVisibleAtResolution(t, currentResLevel);
      })
      .onNodeClick(function (n) {
        if (!n || !n.id) return;
        window.location.href = "nodes/" + encodeURIComponent(n.id) + ".html";
      });

    // Default cooldown — initial layout settles and freezes. Cluster motion
    // only resumes when a new edge fires (brief alpha-target reheat).
    var alphaSettleTimeout = null;

    // Position cache: persist on simulation stop so the next page load
    // restores the settled layout instantly instead of re-running force
    // simulation from random initial positions. With 35K-node lattices
    // the savings is measured in seconds-per-refresh.
    if (typeof graph.onEngineStop === "function") {
      graph.onEngineStop(function () {
        try {
          var snap = {};
          graph.graphData().nodes.forEach(function (n) {
            if (typeof n.x === "number" && typeof n.y === "number") {
              // Round to 2 decimals to keep cache size reasonable.
              snap[n.id] = [
                Math.round(n.x * 100) / 100,
                Math.round(n.y * 100) / 100,
                Math.round((n.z || 0) * 100) / 100
              ];
            }
          });
          window.localStorage.setItem(
            POSITION_CACHE_KEY,
            JSON.stringify(snap)
          );
        } catch (e) {
          // Quota exceeded or storage disabled — silently continue.
        }
      });
    }

    // If we restored cached positions, skip the warmup pass so the
    // simulation accepts them as already-settled instead of doing a
    // pre-layout tick burst that visibly reshuffles everything.
    if (cachedPositions) {
      if (typeof graph.warmupTicks === "function") graph.warmupTicks(0);
      // Very low cooldown so we don't run a full settle from cached state;
      // simulation still alive enough that d3AlphaTarget(0.12) reheats
      // for new-edge events still produce local rebalance.
      if (typeof graph.cooldownTicks === "function") graph.cooldownTicks(20);
    }

    // (Resolution gate state hoisted above the graph constructor — see top.)

    function resize() {
      graph.width(container.clientWidth);
      graph.height(container.clientHeight);
    }
    window.addEventListener("resize", resize);
    setTimeout(resize, 0);

    wireZoomControls(container, graph);

    // ----------------------------------------------------------------
    // Runner globes (ball-lightning style: white core + colored halo)
    // ----------------------------------------------------------------
    var nodeIndex = Object.create(null);
    graph.graphData().nodes.forEach(function (n) { nodeIndex[n.id] = n; });

    var THREE = window.THREE;
    var globes = {};
    var traces = {};
    var runnerState = {};
    if (THREE && typeof graph.scene === "function") {
      var scene = graph.scene();
      Object.keys(RUNNER_PALETTE).forEach(function (rname) {
        var globe = makeRunnerGlobe(THREE, RUNNER_PALETTE[rname]);
        globe.visible = false;
        scene.add(globe);
        globes[rname] = globe;

        var trace = makeRunnerTrace(THREE);
        scene.add(trace);
        traces[rname] = trace;

        var sid = pickRandomNodeId(graph);
        var tid = pickWalkTarget(graph, sid);
        runnerState[rname] = {
          sourceId: sid,
          targetId: tid,
          phase: "charging",
          phaseStart: Date.now(),
          pauseUntil: 0
        };
      });
    } else {
      console.warn("[interspace3d] window.THREE missing — runner globes disabled");
    }

    function pickRandomNodeId(g) {
      var ns = g.graphData().nodes;
      if (!ns.length) return null;
      return ns[(Math.random() * ns.length) | 0].id;
    }

    // Tunable: probability per hop of teleporting to a random node anywhere
    // in the lattice instead of walking a direct neighbor. Higher = larger,
    // more variable jumps; lower = tighter local walks.
    var TELEPORT_PROB = 0.3;

    function pickWalkTarget(g, fromId) {
      // Long-hop random teleport — visually striking, less predictable
      if (Math.random() < TELEPORT_PROB) {
        return pickRandomNodeId(g);
      }
      // Otherwise walk a direct neighbor
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

    function nodePos(nid) {
      var n = nodeIndex[nid];
      if (!n) return null;
      if (typeof n.x !== "number") return null;
      return { x: n.x, y: n.y, z: n.z || 0 };
    }

    function hasActiveEdgePulses(now) {
      for (var k in edgePulse) {
        if (edgePulse[k].until > now) return true;
      }
      for (var nid in nodeGlow) {
        if (nodeGlow[nid].until && nodeGlow[nid].until > now) return true;
      }
      return false;
    }

    // Animation loop — drives the 3-phase runner state machine
    // (charging -> traveling -> pausing) per runner, plus triggers
    // graph.refresh() only when edge/endpoint pulses are actively fading.
    function animLoop() {
      var now = Date.now();
      Object.keys(runnerState).forEach(function (rname) {
        var st = runnerState[rname];
        var globe = globes[rname];
        var trace = traces[rname];
        if (!st || !globe || !trace) return;

        var src = nodePos(st.sourceId);
        var tgt = nodePos(st.targetId);
        if (!src || !tgt) {
          globe.visible = false;
          trace.visible = false;
          return;
        }
        globe.visible = true;

        var elapsed = now - st.phaseStart;

        if (st.phase === "charging") {
          // Lightning trace extends source -> target. Globe stays at source.
          var f = Math.min(1, elapsed / CHARGE_MS);
          var endPoint = {
            x: src.x + (tgt.x - src.x) * f,
            y: src.y + (tgt.y - src.y) * f,
            z: src.z + (tgt.z - src.z) * f
          };
          updateTraceEndpoints(trace, src, endPoint);
          trace.material.opacity = 0.85 * f + 0.05;
          trace.visible = true;
          globe.position.set(src.x, src.y, src.z);
          if (elapsed >= CHARGE_MS) {
            st.phase = "traveling";
            st.phaseStart = now;
          }
        } else if (st.phase === "traveling") {
          // Globe travels src -> tgt with ease-in-out; trace stays full length
          // but dims as the globe progresses (current → faded).
          var t = Math.min(1, elapsed / TRAVEL_MS);
          var e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
          globe.position.set(
            src.x + (tgt.x - src.x) * e,
            src.y + (tgt.y - src.y) * e,
            src.z + (tgt.z - src.z) * e
          );
          updateTraceEndpoints(trace, src, tgt);
          trace.material.opacity = Math.max(0, 0.85 * (1 - t));
          trace.visible = trace.material.opacity > 0.02;
          if (elapsed >= TRAVEL_MS) {
            st.phase = "pausing";
            st.phaseStart = now;
            st.pauseUntil = now + PAUSE_MIN_MS +
                            Math.random() * (PAUSE_MAX_MS - PAUSE_MIN_MS);
            trace.visible = false;
          }
        } else { // pausing
          globe.position.set(tgt.x, tgt.y, tgt.z);
          trace.visible = false;
          if (now >= st.pauseUntil) {
            // Pick next neighbor + start charging next hop.
            st.sourceId = st.targetId;
            st.targetId = pickWalkTarget(graph, st.sourceId);
            st.phase = "charging";
            st.phaseStart = now;
          }
        }
      });

      // Resolution-gate transition detection. Camera-distance change
      // crossing a tier threshold triggers a single refresh so
      // nodeVisibility / linkVisibility filters re-evaluate.
      var cam = graph.cameraPosition && graph.cameraPosition();
      if (cam) {
        var dist = Math.hypot(cam.x || 0, cam.y || 0, cam.z || 0);
        var lvl = resolutionLevelFromDistance(dist);
        if (lvl !== currentResLevel) {
          currentResLevel = lvl;
          if (typeof graph.refresh === "function") graph.refresh();
        }
      }

      if (hasActiveEdgePulses(now)) {
        if (typeof graph.refresh === "function") graph.refresh();
      }

      window.requestAnimationFrame(animLoop);
    }
    window.requestAnimationFrame(animLoop);

    // ----------------------------------------------------------------
    // SSE subscription
    // ----------------------------------------------------------------
    var statusEl = document.getElementById("lattice-runner-status");
    function setStatus(state, text) {
      if (!statusEl) return;
      statusEl.setAttribute("data-state", state);
      statusEl.textContent = text;
    }
    setStatus("connecting", "connecting…");

    if (window.EventSource) {
      try {
        var es = new EventSource("/api/runners/stream");
        es.addEventListener("hello", function () {
          setStatus("live", "live");
        });
        es.addEventListener("error", function () {
          setStatus("offline", "offline (static mode)");
        });
        es.onmessage = function (ev) {
          var msg = null;
          try { msg = JSON.parse(ev.data || "{}"); } catch (e) { return; }
          if (msg.type === "edge_added" && msg.edge) {
            handleEdgeAdded(msg.runner || "t-cell", msg.edge);
          } else if (msg.type === "cycle_complete") {
            setStatus("live", "live · +" + msg.added + " from " + msg.runner);
            window.setTimeout(function () { setStatus("live", "live"); }, 3500);
          }
        };
      } catch (e) {
        setStatus("offline", "offline");
      }
    }

    function handleEdgeAdded(runnerName, edge) {
      if (!nodeIndex[edge.source] || !nodeIndex[edge.target]) return;
      var current = graph.graphData();
      var key = edge.source + "|" + edge.target + "|" + (edge.kind || "related");
      for (var i = 0; i < current.links.length; i++) {
        var l = current.links[i];
        var sk = ((l.source && l.source.id) || l.source) + "|" +
                 ((l.target && l.target.id) || l.target) + "|" +
                 (l.kind || "related");
        if (sk === key) return;
      }
      current.links.push({
        source: edge.source,
        target: edge.target,
        kind: edge.kind || "related"
      });
      graph.graphData(current);

      var now = Date.now();
      edgePulse[key] = { runner: runnerName, until: now + EDGE_PULSE_MS };
      nodeGlow[edge.source] = {
        runner: runnerName,
        until: now + EDGE_ENDPOINT_PULSE_MS
      };
      nodeGlow[edge.target] = {
        runner: runnerName,
        until: now + EDGE_ENDPOINT_PULSE_MS * 1.2
      };

      // Snap the responsible runner to fly the new bridge — charging
      // phase first (lightning extends along the new edge), then travel.
      var st = runnerState[runnerName];
      if (st) {
        st.sourceId = edge.source;
        st.targetId = edge.target;
        st.phase = "charging";
        st.phaseStart = now;
      }

      // Brief local rebalance — partial alpha-target reheat that settles
      // back to rest after MOTION_DURATION_MS.
      if (typeof graph.d3AlphaTarget === "function") {
        graph.d3AlphaTarget(MOTION_ALPHA_TARGET);
        if (alphaSettleTimeout) clearTimeout(alphaSettleTimeout);
        alphaSettleTimeout = setTimeout(function () {
          graph.d3AlphaTarget(0);
          alphaSettleTimeout = null;
        }, MOTION_DURATION_MS);
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

  // ---------- runner globe construction ----------
  // Ball-lightning aesthetic: bright cool-white core + accent-tinted halos.
  // All three runners share the same white-hot center; only the halos differ
  // (cyan / gold / indigo) — Dusty's call.
  function makeRunnerGlobe(THREE, palette) {
    var group = new THREE.Group();
    var core = new THREE.Mesh(
      new THREE.SphereGeometry(5, 16, 16),
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 1.0
      })
    );
    var halo = new THREE.Mesh(
      new THREE.SphereGeometry(11, 18, 18),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(palette.accent),
        transparent: true,
        opacity: 0.55,
        blending: THREE.AdditiveBlending,
        depthWrite: false
      })
    );
    var fringe = new THREE.Mesh(
      new THREE.SphereGeometry(20, 20, 20),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(palette.accent),
        transparent: true,
        opacity: 0.18,
        blending: THREE.AdditiveBlending,
        depthWrite: false
      })
    );
    group.add(core);
    group.add(halo);
    group.add(fringe);
    return group;
  }

  // ---------- lightning trace construction ----------
  // Bright white line that extends source -> target during the CHARGE phase,
  // stays at full length during TRAVEL while dimming, then disappears at PAUSE.
  // Two-point line with mutable BufferAttribute so we update endpoints in place.
  function makeRunnerTrace(THREE) {
    var geom = new THREE.BufferGeometry();
    geom.setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array(6), 3)
    );
    var mat = new THREE.LineBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false
    });
    var line = new THREE.Line(geom, mat);
    line.visible = false;
    return line;
  }

  function updateTraceEndpoints(trace, src, tgt) {
    var pos = trace.geometry.getAttribute("position");
    pos.setXYZ(0, src.x, src.y, src.z);
    pos.setXYZ(1, tgt.x, tgt.y, tgt.z);
    pos.needsUpdate = true;
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
