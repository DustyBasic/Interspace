/* Interspace lattice viewer — Cytoscape.js v3.30.2.
 *
 * Reads the input data from the inline <script id="lattice-data"> JSON block,
 * builds the graph, wires the controls bar (search / tag chips / color-by),
 * and click → navigate.
 */
(function () {
  "use strict";

  var TAG_PALETTE = [
    "#4f7cac", "#c97b63", "#7aa974", "#9b7aa9",
    "#d4a55a", "#5a9aa8", "#b56576", "#6d6875",
    "#8a9a5b", "#a26769", "#4a6c6f", "#7d5a50"
  ];
  var FALLBACK_COLOR = "#888";

  function readData() {
    var el = document.getElementById("lattice-data");
    if (!el) {
      console.error("[interspace] #lattice-data script element missing");
      return null;
    }
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (e) {
      console.error("[interspace] failed to parse #lattice-data JSON:", e);
      return null;
    }
  }

  function buildClusterColorMap(clusters) {
    var map = {};
    (clusters || []).forEach(function (c, i) {
      map[c.id] = c.color || TAG_PALETTE[i % TAG_PALETTE.length];
    });
    return map;
  }

  function buildTagColorMap(nodes) {
    var seen = {};
    var i = 0;
    nodes.forEach(function (n) {
      (n.tags || []).forEach(function (t) {
        if (!(t in seen)) {
          seen[t] = TAG_PALETTE[i % TAG_PALETTE.length];
          i++;
        }
      });
    });
    return seen;
  }

  function colorForNode(node, mode, clusterColors, tagColors) {
    if (mode === "tag") {
      var firstTag = (node.tags || [])[0];
      return firstTag ? (tagColors[firstTag] || FALLBACK_COLOR) : FALLBACK_COLOR;
    }
    return clusterColors[node.cluster] || FALLBACK_COLOR;
  }

  function toElements(data, clusterColors, tagColors, colorBy) {
    var els = [];
    var nodePhase = {};
    data.nodes.forEach(function (n) {
      var nodeData = {
        id: n.id,
        label: n.label || n.id,
        cluster: n.cluster || "uncategorized",
        tags: n.tags || [],
        weight: typeof n.weight === "number" ? n.weight : 1.0,
        color: colorForNode(n, colorBy, clusterColors, tagColors),
        archived: !!n.archived,
        phase: n.phase || (n.archived ? "archived" : "current")
      };
      if (typeof n.ts === "number") nodeData.ts = n.ts;
      var nodeClasses = "";
      if (nodeData.archived) nodeClasses = "archived";
      else if (nodeData.phase === "foundation") nodeClasses = "foundation";
      nodePhase[n.id] = nodeData.phase;
      els.push({ group: "nodes", data: nodeData, classes: nodeClasses });
    });
    (data.edges || []).forEach(function (e, i) {
      var srcPhase = nodePhase[e.source] || "current";
      var tgtPhase = nodePhase[e.target] || "current";
      var edgeClass = "";
      // Archived dominates; foundation second; current is plain.
      if (srcPhase === "archived" || tgtPhase === "archived") edgeClass = "archived";
      else if (srcPhase === "foundation" || tgtPhase === "foundation") edgeClass = "foundation";
      els.push({
        group: "edges",
        data: {
          id: "e" + i,
          source: e.source,
          target: e.target,
          kind: e.kind || "related",
          weight: typeof e.weight === "number" ? e.weight : 1.0
        },
        classes: edgeClass
      });
    });
    return els;
  }

  function init() {
    var container = document.getElementById("lattice");
    if (!container) {
      console.error("[interspace] #lattice container missing");
      return;
    }
    var data = readData();
    if (!data) return;
    if (typeof cytoscape !== "function") {
      console.error("[interspace] cytoscape global missing");
      return;
    }

    var clusterColors = buildClusterColorMap(data.clusters);
    var tagColors = buildTagColorMap(data.nodes);

    var state = {
      search: "",
      activeTags: new Set(),
      colorBy: "cluster",
      timeCutoffMs: null,
      includeArchived: true
    };

    var cy = cytoscape({
      container: container,
      elements: toElements(data, clusterColors, tagColors, state.colorBy),
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            "label": "data(label)",
            "font-size": "11px",
            "font-family": "system-ui, sans-serif",
            "color": "#1a1a1a",
            "text-valign": "bottom",
            "text-margin-y": 6,
            "text-outline-color": "#fff",
            "text-outline-width": 2,
            "width": "mapData(weight, 0, 3, 18, 60)",
            "height": "mapData(weight, 0, 3, 18, 60)",
            "border-width": 1,
            "border-color": "#333"
          }
        },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier",
            "line-color": "#bbb",
            "target-arrow-color": "#888",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.9,
            "width": "mapData(weight, 0, 3, 1, 4)",
            "label": "data(kind)",
            "font-size": "9px",
            "color": "#666",
            "text-rotation": "autorotate",
            "text-background-color": "#fff",
            "text-background-opacity": 0.8,
            "text-background-padding": 2
          }
        },
        {
          selector: ".filtered-out",
          style: { "display": "none" }
        },
        {
          selector: "node.archived",
          style: {
            "opacity": 0.4,
            "border-style": "dashed",
            "border-color": "#666"
          }
        },
        {
          selector: "edge.archived",
          style: {
            "opacity": 0.4,
            "line-style": "dashed"
          }
        },
        {
          selector: "node.foundation",
          style: {
            "opacity": 0.7,
            "border-style": "dotted",
            "border-width": 2,
            "border-color": "#555"
          }
        },
        {
          selector: "edge.foundation",
          style: {
            "opacity": 0.55,
            "line-style": "dotted"
          }
        },
        {
          selector: "node:selected",
          style: { "border-width": 3, "border-color": "#222" }
        }
      ],
      layout: {
        name: "cose",
        animate: false,
        nodeRepulsion: 8000,
        idealEdgeLength: 90,
        edgeElasticity: 100,
        gravity: 0.25,
        numIter: 1500,
        padding: 30
      },
      minZoom: 0.2,
      maxZoom: 3
    });

    cy.on("tap", "node", function (evt) {
      var id = evt.target.data("id");
      if (id) {
        window.location.href = "nodes/" + encodeURIComponent(id) + ".html";
      }
    });

    wireZoomControls(container, cy);

    function nodeMatches(nodeData) {
      if (!state.includeArchived && nodeData.archived) return false;
      var label = (nodeData.label || nodeData.id || "").toLowerCase();
      var tags = nodeData.tags || [];
      var id = (nodeData.id || "").toLowerCase();
      if (state.search) {
        var q = state.search;
        var hit =
          label.indexOf(q) !== -1 ||
          id.indexOf(q) !== -1 ||
          tags.some(function (t) { return t.toLowerCase().indexOf(q) !== -1; });
        if (!hit) return false;
      }
      if (state.activeTags.size > 0) {
        var match = tags.some(function (t) { return state.activeTags.has(t); });
        if (!match) return false;
      }
      if (state.timeCutoffMs !== null && typeof nodeData.ts === "number") {
        if (nodeData.ts > state.timeCutoffMs) return false;
      }
      return true;
    }

    function applyFilters() {
      cy.batch(function () {
        var visible = 0;
        cy.nodes().forEach(function (n) {
          var ok = nodeMatches(n.data());
          n.toggleClass("filtered-out", !ok);
          if (ok) visible++;
        });
        cy.edges().forEach(function (e) {
          var srcOk = !e.source().hasClass("filtered-out");
          var tgtOk = !e.target().hasClass("filtered-out");
          e.toggleClass("filtered-out", !(srcOk && tgtOk));
        });
        renderStatus(visible);
      });
      var timeSlider = document.getElementById("lattice-time");
      var atMaxTime = !timeSlider || state.timeCutoffMs === null ||
        Number(state.timeCutoffMs) === Number(timeSlider.max);
      var anyFilter = state.search || state.activeTags.size > 0 || !atMaxTime;
      var clearBtn = document.getElementById("lattice-clear");
      if (clearBtn) clearBtn.hidden = !anyFilter;
    }

    function formatDate(ms) {
      var d = new Date(ms);
      var y = d.getUTCFullYear();
      var m = String(d.getUTCMonth() + 1).padStart(2, "0");
      var day = String(d.getUTCDate()).padStart(2, "0");
      return y + "-" + m + "-" + day;
    }

    function applyColoring() {
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var d = n.data();
          var c = colorForNode(d, state.colorBy, clusterColors, tagColors);
          n.data("color", c);
        });
      });
    }

    function renderStatus(visible) {
      var el = document.getElementById("lattice-status");
      if (!el) return;
      var total = cy.nodes().length;
      if (visible === total) {
        el.textContent = "";
      } else {
        el.textContent = "Showing " + visible + " of " + total + " nodes.";
      }
    }

    // --- Wire controls ---

    var search = document.getElementById("lattice-search");
    if (search) {
      search.addEventListener("input", function () {
        state.search = (search.value || "").trim().toLowerCase();
        applyFilters();
      });
    }

    var chips = document.getElementById("lattice-tag-chips");
    if (chips) {
      chips.addEventListener("click", function (evt) {
        var btn = evt.target.closest(".tag-chip");
        if (!btn) return;
        var tag = btn.getAttribute("data-tag");
        if (!tag) return;
        if (state.activeTags.has(tag)) {
          state.activeTags.delete(tag);
          btn.setAttribute("aria-pressed", "false");
          btn.classList.remove("tag-chip--active");
        } else {
          state.activeTags.add(tag);
          btn.setAttribute("aria-pressed", "true");
          btn.classList.add("tag-chip--active");
        }
        applyFilters();
      });
    }

    var colorBy = document.getElementById("lattice-color-by");
    if (colorBy) {
      colorBy.addEventListener("change", function () {
        state.colorBy = colorBy.value || "cluster";
        applyColoring();
      });
    }

    var archiveToggle = document.getElementById("lattice-include-archived");
    if (archiveToggle) {
      state.includeArchived = !!archiveToggle.checked;
      archiveToggle.addEventListener("change", function () {
        state.includeArchived = !!archiveToggle.checked;
        applyFilters();
      });
    }

    var timeSlider = document.getElementById("lattice-time");
    var timeLabel = document.getElementById("lattice-time-label");
    if (timeSlider) {
      var stored = readSharedCutoff();
      if (stored !== null) {
        var clamped = Math.min(Math.max(stored, Number(timeSlider.min)), Number(timeSlider.max));
        timeSlider.value = String(clamped);
        if (timeLabel) timeLabel.textContent = formatDate(clamped);
      }
      state.timeCutoffMs = Number(timeSlider.value);
      timeSlider.addEventListener("input", function () {
        state.timeCutoffMs = Number(timeSlider.value);
        if (timeLabel) timeLabel.textContent = formatDate(state.timeCutoffMs);
        writeSharedCutoff(state.timeCutoffMs);
        applyFilters();
      });
      applyFilters();
    }

    var clearBtn = document.getElementById("lattice-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        state.search = "";
        state.activeTags.clear();
        if (search) search.value = "";
        if (chips) {
          chips.querySelectorAll(".tag-chip--active").forEach(function (b) {
            b.classList.remove("tag-chip--active");
            b.setAttribute("aria-pressed", "false");
          });
        }
        if (timeSlider) {
          timeSlider.value = timeSlider.max;
          state.timeCutoffMs = Number(timeSlider.max);
          if (timeLabel) timeLabel.textContent = formatDate(state.timeCutoffMs);
          clearSharedCutoff();
        }
        if (archiveToggle) {
          archiveToggle.checked = true;
          state.includeArchived = true;
        }
        applyFilters();
      });
    }
  }

  // Shared time-cutoff across the main lattice and any cluster mini-lattices.
  // sessionStorage = per-tab; closing the tab resets to default (max).
  var SHARED_CUTOFF_KEY = "interspace.timeCutoffMs";
  function readSharedCutoff() {
    try {
      var raw = window.sessionStorage.getItem(SHARED_CUTOFF_KEY);
      if (raw == null) return null;
      var n = Number(raw);
      return Number.isFinite(n) ? n : null;
    } catch (e) { return null; }
  }
  function writeSharedCutoff(ms) {
    try { window.sessionStorage.setItem(SHARED_CUTOFF_KEY, String(ms)); } catch (e) {}
  }
  function clearSharedCutoff() {
    try { window.sessionStorage.removeItem(SHARED_CUTOFF_KEY); } catch (e) {}
  }

  function wireZoomControls(container, cy) {
    var wrap = container.parentElement;
    if (!wrap) return;
    var buttons = wrap.querySelectorAll(".lattice-zoom__btn");
    if (!buttons.length) return;
    var ZOOM_FACTOR = 1.5;
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.getAttribute("data-zoom");
        if (mode === "in") {
          cy.zoom(cy.zoom() * ZOOM_FACTOR);
        } else if (mode === "out") {
          cy.zoom(cy.zoom() / ZOOM_FACTOR);
        } else if (mode === "fit") {
          cy.fit(undefined, 25);
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
