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
    "t-cell": { accent: "#7de3f0" }, // cyan  — cross-source pairwise mentions
    "rel":    { accent: "#e8c873" }, // gold  — relational binding (temporal variants)
    "neg-t":  { accent: "#9d8bef" }, // indigo — inverse correlation (v2)
    "red":    { accent: "#e8736b" }  // red   — repair / near-duplicate detection
  };

  // Globe travel cadence — slow + contemplative. Each hop is a 3-phase
  // sequence: CHARGE (lightning trace extends src->tgt) → TRAVEL (globe
  // crosses, trace dims) → PAUSE (globe rests at tgt).
  var CHARGE_MS = 700;        // trace extends source -> target
  var TRAVEL_MS = 3000;       // globe travels along extended trace
  var PAUSE_MIN_MS = 1000;    // settle at destination (min)
  var PAUSE_MAX_MS = 9000;    // settle at destination (max) — total cycle 4-12s
  var BRIDGE_PAUSE_MS = 800;  // shorter pause after a fresh-edge bridge fire

  // Pause-on-interaction: runner state machine freezes when the operator is
  // actively rotating/zooming/panning the camera or hovering a node. Resumes
  // RUNNER_RESUME_DELAY_MS after last interaction. Lets the operator inspect
  // a frozen scene without ball-lightning motion distracting.
  var RUNNER_RESUME_DELAY_MS = 3000;

  // Pulse durations on new-edge events (the edge itself + its endpoints)
  var EDGE_PULSE_MS = 1800;
  var EDGE_ENDPOINT_PULSE_MS = 2400;

  // Local rebalance after a new edge — partial alpha-target reheat that
  // decays back to rest. Half the default server cycle (120s).
  var MOTION_DURATION_MS = 60_000;
  var MOTION_ALPHA_TARGET = 0.12;

  // Four-tier zoom-driven resolution gates. Camera distance from origin
  // determines which node kinds are visible:
  //   level 0 (> RES_FAR_THRESHOLD):          folders only
  //   level 1 (> RES_MEDIUM_FAR_THRESHOLD):   + files
  //   level 2 (> RES_MEDIUM_THRESHOLD):       + composites / section_anchors
  //                                             / conversation_segments / null
  //                                             (hand-curated concept nodes)
  //   level 3 (closer):                       + atoms (paragraphs / findings
  //                                             / observations / chat_turns)
  //
  // Splitting files from mid-kinds gives a true medium-far view: the
  // document scaffold becomes visible before per-section noise. Operator
  // picks which file/region to enter before atoms come into view.
  // Spatial-hierarchy navigation per the pin's v0.5 direction.
  // Thresholds calibrated for Memory-scale data (~36K nodes spread over
  // ~10k-unit radius from origin after force layout). Testbench data
  // (~2K nodes) lands well inside level 3 at default zoom, which is fine.
  var RES_FAR_THRESHOLD = 30000;        // > this: level 0 (folders only)
  var RES_MEDIUM_FAR_THRESHOLD = 15000; // > this: level 1 (+ files)
  var RES_MEDIUM_THRESHOLD = 7000;      // > this: level 2 (+ mid kinds)
                                        // closer: level 3 (+ atoms, all)

  var FOLDER_KINDS = { folder: 1, directory: 1 };
  var FILE_KINDS = { file: 1 };
  // Mid-class node kinds — visible at level 2+. Concept-grade nodes
  // (finding / observation / section_anchor / composite), segment
  // anchors, and post-merge `page` consolidations live here so they
  // stay visible when you zoom past atom detail. Hand-curated null-kind
  // nodes also surface at level 2+ (handled in nodeEffectiveAlpha via
  // the `kind === null` branch in kindFadeThreshold).
  // (Note: `page` consolidation is subtractive — fragments are removed
  // and their content absorbed into the page node directly.)
  var MID_KINDS = {
    composite: 1, section_anchor: 1, conversation_segment: 1,
    finding: 1, observation: 1, page: 1, document_section: 1
  };

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

    var allNodes = data.nodes.map(function (n) {
      var out = {
        id: n.id,
        label: n.label || n.id,
        cluster: n.cluster || "uncategorized",
        weight: typeof n.weight === "number" ? n.weight : 1.0,
        archived: !!n.archived,
        phase: n.phase || (n.archived ? "archived" : "current"),
        tags: n.tags || [],
        meta: n.meta || {}  // preserved for tier classification
      };
      if (cachedPositions) {
        var p = cachedPositions[n.id];
        if (p && p.length >= 3) {
          out.x = p[0]; out.y = p[1]; out.z = p[2];
        }
      }
      return out;
    });
    var allLinks = (data.edges || []).map(function (e) {
      return { source: e.source, target: e.target, kind: e.kind || "related" };
    });

    // Tier-progressive loading: structural shell first (folders + file
    // anchors + composites + section_anchors + concept-only nodes).
    // Atoms (paragraph / finding / observation) batch-load in chunks
    // after the shell settles. Prevents browser crash on 35K-node
    // initial graphData() — the force simulation can't sustain that
    // many nodes from a cold start.
    var ATOM_KINDS_SET = { paragraph: 1, finding: 1, observation: 1 };
    var structuralNodes = [];
    var atomNodes = [];
    var nodeIdSet = {};  // ids of currently-loaded nodes (for link filtering)
    allNodes.forEach(function (n) {
      var k = n.meta.kind;
      if (k && ATOM_KINDS_SET[k]) {
        atomNodes.push(n);
      } else {
        structuralNodes.push(n);
        nodeIdSet[n.id] = true;
      }
    });
    function linksForLoadedNodes(loadedIdSet) {
      return allLinks.filter(function (l) {
        var s = (typeof l.source === "object" ? l.source.id : l.source);
        var t = (typeof l.target === "object" ? l.target.id : l.target);
        return loadedIdSet[s] && loadedIdSet[t];
      });
    }

    // Start with just structural nodes — small, fast initial render.
    var nodes = structuralNodes;
    var links = linksForLoadedNodes(nodeIdSet);

    // ----------------------------------------------------------------
    // Search state: highlight labels/tags/clusters that match the input,
    // dim everything else. Match → full color + size bump (glow via val).
    // No-match → desaturated gray, ~12% opacity. Empty term restores
    // normal rendering. Pre-built haystack per node to keep keystroke
    // filtering O(n) instead of O(n × strchk).
    // ----------------------------------------------------------------
    var searchMatchSet = null;  // null = inactive; Set<id> when filtering
    var SEARCH_DIM_RGBA = "rgba(70, 72, 80, 0.15)";
    var clusterLabelById = {};
    (data.clusters || []).forEach(function (c) {
      clusterLabelById[c.id] = (c.label || c.id || "").toLowerCase();
    });
    var searchIndex = allNodes.map(function (n) {
      var clusterLabel = clusterLabelById[n.cluster] || (n.cluster || "");
      return {
        id: n.id,
        hay: (
          (n.label || "") + " " +
          (n.tags || []).join(" ") + " " +
          clusterLabel
        ).toLowerCase()
      };
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

    // Smooth-fade resolution gate. Each kind has a "fade threshold" — the
    // camera distance at which it starts fading out. Within FADE_WIDTH
    // around that threshold, opacity ramps 1→0 linearly. Below the band
    // the node is full opacity; above, completely invisible (so the
    // renderer can skip it via nodeVisibility for perf). Wider FADE_WIDTH
    // = softer transitions but more redraws during zoom.
    var FADE_WIDTH = 3500;
    var currentCameraDistance = 0;
    function kindFadeThreshold(kind) {
      if (FOLDER_KINDS[kind]) return Infinity;          // never fades
      if (FILE_KINDS[kind])   return RES_FAR_THRESHOLD;
      if (MID_KINDS[kind] || kind === null)
                              return RES_MEDIUM_FAR_THRESHOLD;
      return RES_MEDIUM_THRESHOLD;                       // atoms (paragraph/chat_turn)
    }
    function nodeEffectiveAlpha(n) {
      var meta = n.meta || {};
      var threshold = kindFadeThreshold(meta.kind || null);
      if (threshold === Infinity) return 1.0;
      var halfWidth = FADE_WIDTH * 0.5;
      var d = currentCameraDistance;
      if (d <= threshold - halfWidth) return 1.0;
      if (d >= threshold + halfWidth) return 0.0;
      return 1.0 - (d - (threshold - halfWidth)) / FADE_WIDTH;
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
        // Search active + non-match: heavy dim. Bypasses pulse/base color
        // so the matched-vs-rest contrast is immediate and unambiguous.
        if (searchMatchSet && !searchMatchSet.has(n.id)) {
          return SEARCH_DIM_RGBA;
        }
        var base;
        var g = nodeGlow[n.id];
        if (!g || !g.until) {
          base = baseNodeColor(n, clusterColors);
        } else {
          var now = Date.now();
          if (g.until <= now) {
            delete nodeGlow[n.id];
            base = baseNodeColor(n, clusterColors);
          } else {
            var pal = RUNNER_PALETTE[g.runner] || RUNNER_PALETTE["t-cell"];
            var frac = (g.until - now) / EDGE_ENDPOINT_PULSE_MS;
            base = blendHex(baseNodeColor(n, clusterColors), pal.accent, frac);
          }
        }
        // Apply smooth tier-fade. Nodes inside their tier band stay full
        // color; those near the boundary fade gradually instead of popping.
        var alpha = nodeEffectiveAlpha(n);
        if (alpha >= 0.995) return base;
        return applyAlpha(base, alpha);
      })
      .nodeVal(function (n) {
        var base = Math.max(1, n.weight * n.weight * 4);
        // Search-match size bump — "glow" effect via larger sphere.
        if (searchMatchSet && searchMatchSet.has(n.id)) {
          return base * 2.5;
        }
        return base;
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
        // Skip rendering when fully outside fade band (perf). Inside the
        // band, return true and let nodeColor's alpha do the visual fade.
        return nodeEffectiveAlpha(n) > 0.02;
      })
      .linkVisibility(function (e) {
        // Endpoint may be either object ref or id string depending on
        // simulation state. Both forms supported.
        var s = (e.source && typeof e.source === "object") ? e.source : nodeIndex[e.source];
        var t = (e.target && typeof e.target === "object") ? e.target : nodeIndex[e.target];
        if (!s || !t) return false;
        return nodeEffectiveAlpha(s) > 0.02 && nodeEffectiveAlpha(t) > 0.02;
      })
      .onNodeClick(function (n) {
        if (!n || !n.id) return;
        window.location.href = "nodes/" + encodeURIComponent(n.id) + ".html";
      });

    // Default cooldown — initial layout settles and freezes. Cluster motion
    // only resumes when a new edge fires (brief alpha-target reheat).
    var alphaSettleTimeout = null;

    // Tier-progressive atom loader. Fires once after the structural
    // shell settles; appends atoms in batches with brief delays so the
    // force simulation can keep up. Each batch reheats partially via the
    // existing alpha-target machinery rather than re-cold-starting the
    // whole layout. Idempotent (only loads atoms once even if engine
    // stops multiple times).
    var ATOM_BATCH_SIZE = 2000;
    var ATOM_BATCH_DELAY_MS = 500;
    var atomLoadStarted = false;
    function loadAtomBatch(startIdx) {
      var endIdx = Math.min(startIdx + ATOM_BATCH_SIZE, atomNodes.length);
      var batch = atomNodes.slice(startIdx, endIdx);
      batch.forEach(function (n) { nodeIdSet[n.id] = true; });
      var current = graph.graphData();
      var newNodes = current.nodes.concat(batch);
      var newLinks = linksForLoadedNodes(nodeIdSet);
      graph.graphData({ nodes: newNodes, links: newLinks });
      // Rebuild nodeIndex incrementally so runner walks find new atoms
      batch.forEach(function (n) { nodeIndex[n.id] = n; });
      if (endIdx < atomNodes.length) {
        setTimeout(function () { loadAtomBatch(endIdx); }, ATOM_BATCH_DELAY_MS);
      }
    }

    // Position cache: persist on simulation stop so the next page load
    // restores the settled layout instantly instead of re-running force
    // simulation from random initial positions. With 35K-node lattices
    // the savings is measured in seconds-per-refresh.
    if (typeof graph.onEngineStop === "function") {
      graph.onEngineStop(function () {
        // First-time hook: structural shell has settled → start loading atoms.
        if (!atomLoadStarted && atomNodes.length > 0) {
          atomLoadStarted = true;
          setTimeout(function () { loadAtomBatch(0); }, ATOM_BATCH_DELAY_MS);
        }
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
    // Layout modes — five preset positionings. Each mode assigns
    // n.fx/fy/fz to pin nodes; "globe" clears them so the default
    // force-directed solver runs free. Switching reheats the alpha
    // target briefly so the transition animates instead of snapping.
    // ----------------------------------------------------------------
    function nodeHash(id) {
      // Tiny deterministic 0..1 hash so positions are stable across reloads
      var h = 2166136261;
      for (var i = 0; i < id.length; i++) {
        h ^= id.charCodeAt(i);
        h = (h * 16777619) >>> 0;
      }
      return h / 4294967296;
    }
    function clearFixedPositions() {
      for (var i = 0; i < allNodes.length; i++) {
        delete allNodes[i].fx;
        delete allNodes[i].fy;
        delete allNodes[i].fz;
      }
    }
    function groupByCluster() {
      var by = Object.create(null);
      for (var i = 0; i < allNodes.length; i++) {
        var n = allNodes[i];
        (by[n.cluster] = by[n.cluster] || []).push(n);
      }
      return by;
    }
    function layoutGalaxy() {
      // Logarithmic spiral arms — one arm per cluster, flat disc + slight Z thickness
      var clusters = data.clusters || [];
      var armCount = Math.max(2, clusters.length);
      var clusterToArm = {};
      clusters.forEach(function (c, i) { clusterToArm[c.id] = i % armCount; });
      var byCluster = groupByCluster();
      Object.keys(byCluster).forEach(function (cid) {
        var arm = (cid in clusterToArm) ? clusterToArm[cid] : 0;
        var armAngle = (arm / armCount) * Math.PI * 2;
        var ns = byCluster[cid];
        ns.forEach(function (n, idx) {
          var t = idx / Math.max(1, ns.length);
          var r = 200 + t * 2400;
          var theta = armAngle + t * Math.PI * 3.5 + (nodeHash(n.id) - 0.5) * 0.15;
          n.fx = r * Math.cos(theta);
          n.fy = (nodeHash(n.id + ":z") - 0.5) * 80;
          n.fz = r * Math.sin(theta);
        });
      });
    }
    // ----- shared BFS over the graph: builds adj + depth + parent maps
    function bfsTree(rootCount) {
      // rootCount=1 → single root (highest-inbound)
      // rootCount=2 → two roots (top two by inbound), each grows its own subtree
      var inDeg = {};
      var adj = {};
      for (var i = 0; i < allLinks.length; i++) {
        var e = allLinks[i];
        var s = (e.source && e.source.id) || e.source;
        var t = (e.target && e.target.id) || e.target;
        inDeg[t] = (inDeg[t] || 0) + 1;
        (adj[s] = adj[s] || []).push(t);
        (adj[t] = adj[t] || []).push(s);
      }
      var ranked = allNodes.slice().sort(function (a, b) {
        return (inDeg[b.id] || 0) - (inDeg[a.id] || 0);
      });
      var roots = [];
      for (var r = 0; r < rootCount && r < ranked.length; r++) {
        roots.push(ranked[r].id);
      }
      if (!roots.length && allNodes.length) roots.push(allNodes[0].id);

      var depth = {}, parent = {}, rootOf = {};
      roots.forEach(function (rid) {
        depth[rid] = 0;
        parent[rid] = null;
        rootOf[rid] = rid;
      });
      // Multi-source BFS — fair share between roots
      var queue = roots.slice();
      while (queue.length) {
        var nid = queue.shift();
        var nbrs = adj[nid] || [];
        for (var k = 0; k < nbrs.length; k++) {
          var m = nbrs[k];
          if (!(m in depth)) {
            depth[m] = depth[nid] + 1;
            parent[m] = nid;
            rootOf[m] = rootOf[nid];
            queue.push(m);
          }
        }
      }
      return { adj: adj, depth: depth, parent: parent, rootOf: rootOf, roots: roots };
    }

    function layoutTree() {
      // Real 3D tree — vertical trunk + branches growing up + outward.
      // Each child placed at parent_pos + (mostly-upward unit vector ×
      // branch_length). Branches taper with depth.
      var tree = bfsTree(1);
      var depth = tree.depth, parent = tree.parent;
      var rootId = tree.roots[0];
      var nodeById = {};
      allNodes.forEach(function (n) { nodeById[n.id] = n; });

      var pos = {};
      if (rootId && rootId in nodeById) {
        pos[rootId] = { x: 0, y: -1400, z: 0 };
        nodeById[rootId].fx = 0; nodeById[rootId].fy = -1400; nodeById[rootId].fz = 0;
      }

      // Disconnected nodes — drop in a low ground cloud
      var disconnected = [];
      // Sort connected non-root nodes by BFS depth so parents resolve first
      var connected = [];
      allNodes.forEach(function (n) {
        if (n.id === rootId) return;
        if (n.id in depth) connected.push(n);
        else disconnected.push(n);
      });
      connected.sort(function (a, b) { return depth[a.id] - depth[b.id]; });

      var BRANCH_LEN = 380;
      connected.forEach(function (n) {
        var pp = pos[parent[n.id]];
        if (!pp) {
          disconnected.push(n);
          return;
        }
        // Mostly-upward direction with lateral angular spread
        var lateralAng = nodeHash(n.id) * Math.PI * 2;
        var upBias = 0.55 + nodeHash(n.id + ":u") * 0.35;       // [0.55, 0.9] up
        var lateralAmt = Math.sqrt(Math.max(0, 1 - upBias * upBias));
        var dirX = lateralAmt * Math.cos(lateralAng);
        var dirY = upBias;
        var dirZ = lateralAmt * Math.sin(lateralAng);
        var d = depth[n.id];
        var len = BRANCH_LEN * Math.max(0.35, 1.0 - d * 0.04);
        var np = {
          x: pp.x + dirX * len,
          y: pp.y + dirY * len,
          z: pp.z + dirZ * len
        };
        pos[n.id] = np;
        n.fx = np.x; n.fy = np.y; n.fz = np.z;
      });
      // Stragglers — drift cloud below the trunk
      disconnected.forEach(function (n) {
        n.fx = (nodeHash(n.id) - 0.5) * 1400;
        n.fy = -1500 + (nodeHash(n.id + ":y") - 0.5) * 200;
        n.fz = (nodeHash(n.id + ":z") - 0.5) * 1400;
      });
    }

    function layoutBrain() {
      // Bilateral dendritic / vascular network — two "brainstem" roots
      // (top-2 inbound), each grows a fractal branching subtree biased
      // outward from the midline. Z-flattened so the envelope reads
      // bean-shaped rather than spherical.
      var tree = bfsTree(2);
      var depth = tree.depth, parent = tree.parent, rootOf = tree.rootOf;
      var roots = tree.roots;
      var rootL = roots[0] || null;
      var rootR = roots[1] || rootL;
      var nodeById = {};
      allNodes.forEach(function (n) { nodeById[n.id] = n; });

      // Assign each root a hemisphere
      var hemiOfRoot = {};
      if (rootL) hemiOfRoot[rootL] = -1;
      if (rootR && rootR !== rootL) hemiOfRoot[rootR] = 1;

      var pos = {};
      var ROOT_Y = -200;
      if (rootL && rootL in nodeById) {
        pos[rootL] = { x: -700, y: ROOT_Y, z: 0 };
        nodeById[rootL].fx = pos[rootL].x; nodeById[rootL].fy = pos[rootL].y; nodeById[rootL].fz = pos[rootL].z;
      }
      if (rootR && rootR !== rootL && rootR in nodeById) {
        pos[rootR] = { x: 700, y: ROOT_Y, z: 0 };
        nodeById[rootR].fx = pos[rootR].x; nodeById[rootR].fy = pos[rootR].y; nodeById[rootR].fz = pos[rootR].z;
      }

      var connected = [];
      var disconnected = [];
      allNodes.forEach(function (n) {
        if (n.id === rootL || n.id === rootR) return;
        if (n.id in depth) connected.push(n);
        else disconnected.push(n);
      });
      connected.sort(function (a, b) { return depth[a.id] - depth[b.id]; });

      var BRANCH_LEN = 260;
      connected.forEach(function (n) {
        var pp = pos[parent[n.id]];
        if (!pp) { disconnected.push(n); return; }
        var hh = hemiOfRoot[rootOf[n.id]] || 0;
        // Direction: random spherical, then bias outward from midline
        // (x sign of hemisphere) and slightly upward, then Z-flatten.
        var theta = nodeHash(n.id) * Math.PI * 2;
        var phi = Math.acos(2 * nodeHash(n.id + ":p") - 1);
        var dirX = Math.sin(phi) * Math.cos(theta) + hh * 0.4;
        var dirY = Math.sin(phi) * Math.sin(theta) + 0.15;
        var dirZ = Math.cos(phi) * 0.55;        // bean: thinner in Z
        var mag = Math.sqrt(dirX*dirX + dirY*dirY + dirZ*dirZ) || 1;
        dirX /= mag; dirY /= mag; dirZ /= mag;
        var d = depth[n.id];
        var len = BRANCH_LEN * Math.max(0.45, 1.0 - d * 0.05);
        var np = {
          x: pp.x + dirX * len,
          y: pp.y + dirY * len,
          z: pp.z + dirZ * len
        };
        pos[n.id] = np;
        n.fx = np.x; n.fy = np.y; n.fz = np.z;
      });
      disconnected.forEach(function (n) {
        // Drift cloud below — assigned to a hemisphere by hash
        var hh = (nodeHash(n.id) < 0.5) ? -1 : 1;
        n.fx = hh * 900 + (nodeHash(n.id) - 0.5) * 400;
        n.fy = -800 + (nodeHash(n.id + ":y") - 0.5) * 300;
        n.fz = (nodeHash(n.id + ":z") - 0.5) * 280;
      });
    }
    function layoutFlower() {
      // Radial petals — one cluster per petal. Petal narrows at base,
      // widens toward tip; slight arch above/below the equatorial plane.
      var byCluster = groupByCluster();
      var cids = Object.keys(byCluster);
      var petalCount = Math.max(1, cids.length);
      cids.forEach(function (cid, i) {
        var ang = (i / petalCount) * Math.PI * 2;
        var ns = byCluster[cid];
        ns.forEach(function (n, idx) {
          var t = (idx + 0.5) / ns.length;          // 0 base → 1 tip
          var r = 250 + t * 2000;
          var spread = 0.18 + 0.55 * t;             // wider near tip
          var transAng = (nodeHash(n.id) - 0.5) * spread;
          var bow = Math.sin(t * Math.PI) * 350;    // arch shape
          var pa = ang + transAng;
          n.fx = r * Math.cos(pa);
          n.fy = bow * (0.35 + (nodeHash(n.id + ":y") - 0.5) * 0.4);
          n.fz = r * Math.sin(pa);
        });
      });
    }
    function applyLayout(modeName) {
      if (modeName === "globe")        clearFixedPositions();
      else if (modeName === "galaxy")  layoutGalaxy();
      else if (modeName === "tree")    layoutTree();
      else if (modeName === "brain")   layoutBrain();
      else if (modeName === "flower")  layoutFlower();
      // Re-seat the graph so the simulator picks up fx/fy/fz changes
      graph.graphData(graph.graphData());
      // Reheat briefly so the transition animates
      if (typeof graph.d3AlphaTarget === "function") {
        graph.d3AlphaTarget(0.18);
        if (alphaSettleTimeout) clearTimeout(alphaSettleTimeout);
        alphaSettleTimeout = setTimeout(function () {
          graph.d3AlphaTarget(0);
          alphaSettleTimeout = null;
        }, 4000);
      }
      if (typeof graph.refresh === "function") graph.refresh();
    }
    (function wireLayoutSelector() {
      var sel = document.getElementById("lattice-layout");
      if (!sel) return;
      sel.addEventListener("change", function () { applyLayout(sel.value); });
    })();

    // ----------------------------------------------------------------
    // Search wiring — input → debounce → rebuild matchSet → graph.refresh()
    // ----------------------------------------------------------------
    (function wireSearch() {
      var input = document.getElementById("lattice-search");
      var countEl = document.getElementById("lattice-search-count");
      if (!input) return;
      var debounceTimer = null;
      function applySearch(rawTerm) {
        var term = (rawTerm || "").trim().toLowerCase();
        if (!term) {
          searchMatchSet = null;
          if (countEl) countEl.textContent = "";
        } else {
          searchMatchSet = new Set();
          for (var i = 0; i < searchIndex.length; i++) {
            if (searchIndex[i].hay.indexOf(term) !== -1) {
              searchMatchSet.add(searchIndex[i].id);
            }
          }
          if (countEl) {
            var n = searchMatchSet.size;
            countEl.textContent = n + " match" + (n === 1 ? "" : "es");
          }
        }
        if (typeof graph.refresh === "function") graph.refresh();
      }
      input.addEventListener("input", function () {
        var v = input.value;
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () { applySearch(v); }, 150);
      });
      // Escape clears search
      input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          input.value = "";
          applySearch("");
        }
      });
    })();

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

    // Pause-on-interaction state — set by control + hover handlers below,
    // consumed by animLoop's per-runner pause-gate. Phase clocks are shifted
    // forward on resume so the in-flight phase finishes its remaining time
    // budget instead of jump-resetting.
    var lastInteractionAt = 0;
    function markInteraction() {
      lastInteractionAt = Date.now();
    }
    function isRunnersPaused() {
      if (!lastInteractionAt) return false;
      return (Date.now() - lastInteractionAt) < RUNNER_RESUME_DELAY_MS;
    }

    // Wire camera interaction → pause. OrbitControls fires 'start' on
    // mousedown/touchstart and 'change' on every camera motion. Both attach
    // so wheel-zoom (which only fires 'change') and click-drag (which fires
    // 'start' then repeated 'change') both pause runners.
    if (typeof graph.controls === "function") {
      var orbitControls = graph.controls();
      if (orbitControls && typeof orbitControls.addEventListener === "function") {
        orbitControls.addEventListener("start", markInteraction);
        orbitControls.addEventListener("change", markInteraction);
      }
    }
    // Node hover → pause. Brief hover counts as inspection intent.
    if (typeof graph.onNodeHover === "function") {
      graph.onNodeHover(function (node) {
        if (node) markInteraction();
      });
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
      var pausedNow = isRunnersPaused();
      Object.keys(runnerState).forEach(function (rname) {
        var st = runnerState[rname];
        var globe = globes[rname];
        var trace = traces[rname];
        if (!st || !globe || !trace) return;

        // Pause-on-interaction: freeze runner state machine. Phase clocks
        // are shifted forward on resume so the in-flight phase finishes
        // its remaining time budget instead of jump-resetting.
        if (pausedNow) {
          if (!st._pausedAt) st._pausedAt = now;
          return;
        } else if (st._pausedAt) {
          var pauseDur = now - st._pausedAt;
          st.phaseStart += pauseDur;
          if (st.pauseUntil > 0) st.pauseUntil += pauseDur;
          st._pausedAt = 0;
        }

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

      // Smooth-fade resolution gate. Camera distance is sampled each frame;
      // when it changes by >150 units we refresh so the alpha-gradient
      // re-evaluates. Threshold keeps redraws bounded — zoom is mouse-paced
      // so this is plenty smooth visually without burning frames.
      var cam = graph.cameraPosition && graph.cameraPosition();
      if (cam) {
        var dist = Math.hypot(cam.x || 0, cam.y || 0, cam.z || 0);
        if (Math.abs(dist - currentCameraDistance) > 150) {
          currentCameraDistance = dist;
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
  // All runners share the same white-hot center; only the halos differ
  // (cyan / gold / indigo / red).
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

  // Multiply opacity into either #RRGGBB or rgba(r,g,b,a). Used by the
  // smooth tier-fade so an already-pulsed node (which is rgba) still
  // dims correctly through the fade band.
  function applyAlpha(color, alpha) {
    var rgbaM = /^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)$/.exec(color);
    if (rgbaM) {
      var newA = +rgbaM[4] * alpha;
      return "rgba(" + rgbaM[1] + "," + rgbaM[2] + "," + rgbaM[3] + "," + newA.toFixed(3) + ")";
    }
    var c = hexToRgb(color);
    if (!c) return color;
    return "rgba(" + c.r + "," + c.g + "," + c.b + "," + alpha.toFixed(3) + ")";
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
