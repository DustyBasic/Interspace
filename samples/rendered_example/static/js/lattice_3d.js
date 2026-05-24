/* Interspace 3D lattice viewer (prototype).
 *
 * Uses 3d-force-graph (Three.js wrapper) to render nodes/edges in 3D space.
 * Same data source as the 2D lattice (#lattice-data); same click-to-navigate.
 * Camera: drag = rotate, scroll = zoom, right-click+drag = pan.
 *
 * v0.1 scope: spatial render + cluster coloring + click navigation.
 * Filters (search/tag/time slider) deliberately deferred — establish the
 * spatial primitive first.
 */
(function () {
  "use strict";

  var FALLBACK_COLOR = "#888";

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

    // Shape data for 3d-force-graph: nodes need id; links use source/target.
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

    var graph = ForceGraph3D()(container)
      .graphData({ nodes: nodes, links: links })
      .backgroundColor(getComputedStyle(document.body).backgroundColor || "#0d0e11")
      .nodeId("id")
      .nodeLabel(function (n) {
        var phase = n.phase === "current" ? "" : " [" + n.phase + "]";
        return n.label + phase;
      })
      .nodeColor(function (n) {
        var c = clusterColors[n.cluster] || FALLBACK_COLOR;
        // Foundation nodes dimmer; archived dimmer still
        if (n.archived) return dim(c, 0.4);
        if (n.phase === "foundation") return dim(c, 0.7);
        return c;
      })
      .nodeVal(function (n) {
        // 3d-force-graph maps val to sphere volume; sqrt for visual linearity
        return Math.max(1, n.weight * n.weight * 4);
      })
      .nodeOpacity(0.9)
      .linkColor(function () { return "rgba(180, 180, 180, 0.35)"; })
      .linkWidth(0.5)
      .linkDirectionalArrowLength(2.5)
      .linkDirectionalArrowRelPos(0.85)
      .linkDirectionalArrowColor(function () { return "rgba(140, 140, 140, 0.6)"; })
      .onNodeClick(function (n) {
        if (!n || !n.id) return;
        window.location.href = "nodes/" + encodeURIComponent(n.id) + ".html";
      });

    // Auto-resize with window
    function resize() {
      graph.width(container.clientWidth);
      graph.height(container.clientHeight);
    }
    window.addEventListener("resize", resize);
    setTimeout(resize, 0);
  }

  // Convert a hex color and an opacity-like factor (0-1) to an rgba() string
  // suitable for 3d-force-graph nodeColor (it accepts rgba strings).
  function dim(hex, factor) {
    var m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    var r = (n >> 16) & 0xff;
    var g = (n >> 8) & 0xff;
    var b = n & 0xff;
    return "rgba(" + r + "," + g + "," + b + "," + factor.toFixed(2) + ")";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
