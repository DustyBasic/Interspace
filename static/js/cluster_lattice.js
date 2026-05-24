/* Interspace cluster mini-lattice — Cytoscape.js.
 *
 * Renders a scoped graph for a single cluster page: only that cluster's nodes
 * and the edges where both endpoints are in this cluster.
 *
 * Reads from #cluster-data; mounts on #cluster-lattice. Click → ../nodes/<id>.html
 * because cluster pages always live one directory down from root.
 */
(function () {
  "use strict";

  function readData() {
    var el = document.getElementById("cluster-data");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (e) {
      console.error("[interspace] failed to parse #cluster-data JSON:", e);
      return null;
    }
  }

  function init() {
    var container = document.getElementById("cluster-lattice");
    if (!container) return;
    var data = readData();
    if (!data) return;
    if (typeof cytoscape !== "function") {
      console.error("[interspace] cytoscape global missing on cluster page");
      return;
    }
    if (!data.nodes || data.nodes.length === 0) {
      container.style.display = "none";
      return;
    }

    var color = data.cluster_color || "#888";

    var elements = [];
    var nodePhase = {};
    data.nodes.forEach(function (n) {
      var nodeData = {
        id: n.id,
        label: n.label || n.id,
        weight: typeof n.weight === "number" ? n.weight : 1.0,
        color: color,
        archived: !!n.archived,
        phase: n.phase || (n.archived ? "archived" : "current")
      };
      if (typeof n.ts === "number") nodeData.ts = n.ts;
      var nodeClasses = "";
      if (nodeData.archived) nodeClasses = "archived";
      else if (nodeData.phase === "foundation") nodeClasses = "foundation";
      nodePhase[n.id] = nodeData.phase;
      elements.push({ group: "nodes", data: nodeData, classes: nodeClasses });
    });
    (data.edges || []).forEach(function (e, i) {
      var srcPhase = nodePhase[e.source] || "current";
      var tgtPhase = nodePhase[e.target] || "current";
      var edgeClass = "";
      if (srcPhase === "archived" || tgtPhase === "archived") edgeClass = "archived";
      else if (srcPhase === "foundation" || tgtPhase === "foundation") edgeClass = "foundation";
      elements.push({
        group: "edges",
        data: {
          id: "ce" + i,
          source: e.source,
          target: e.target,
          kind: e.kind || "related",
          weight: typeof e.weight === "number" ? e.weight : 1.0
        },
        classes: edgeClass
      });
    });

    var hasEdges = (data.edges || []).length > 0;
    var layout = hasEdges
      ? {
          name: "cose",
          animate: false,
          nodeRepulsion: 6000,
          idealEdgeLength: 80,
          gravity: 0.3,
          numIter: 1000,
          padding: 20
        }
      : { name: "circle", animate: false, padding: 20 };

    var cy = cytoscape({
      container: container,
      elements: elements,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            "label": "data(label)",
            "font-size": "10px",
            "font-family": "system-ui, sans-serif",
            "color": "#1a1a1a",
            "text-valign": "bottom",
            "text-margin-y": 5,
            "text-outline-color": "#fff",
            "text-outline-width": 2,
            "width": "mapData(weight, 0, 3, 16, 48)",
            "height": "mapData(weight, 0, 3, 16, 48)",
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
            "arrow-scale": 0.8,
            "width": "mapData(weight, 0, 3, 1, 3)",
            "label": "data(kind)",
            "font-size": "8px",
            "color": "#666",
            "text-rotation": "autorotate",
            "text-background-color": "#fff",
            "text-background-opacity": 0.8,
            "text-background-padding": 2
          }
        },
        {
          selector: ".time-hidden",
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
          selector: "node.document-parent",
          style: {
            "shape": "round-rectangle",
            "background-color": "data(color)",
            "background-opacity": 0.18,
            "border-color": "data(color)",
            "border-width": 1,
            "border-opacity": 0.7,
            "border-style": "dashed",
            "label": "data(label)",
            "font-size": "10px",
            "color": "data(color)",
            "text-valign": "top",
            "text-halign": "center",
            "text-margin-y": -4,
            "padding": "12px",
            "compound-sizing-wrt-labels": "include",
            "events": "no"
          }
        }
      ],
      layout: layout,
      minZoom: 0.05,
      maxZoom: 3
    });

    cy.on("tap", "node", function (evt) {
      var n = evt.target;
      if (n.data("isParent")) return;  // synthetic containers
      var id = n.data("id");
      if (id) {
        window.location.href = "../nodes/" + encodeURIComponent(id) + ".html";
      }
    });

    wireZoomControls(container, cy);
    wireTimeSlider(cy);
  }

  function wireTimeSlider(cy) {
    var slider = document.getElementById("cluster-time");
    if (!slider) return;
    var label = document.getElementById("cluster-time-label");
    var status = document.getElementById("cluster-time-status");

    function apply() {
      var cutoff = Number(slider.value);
      cy.batch(function () {
        var visible = 0;
        cy.nodes().forEach(function (n) {
          var ts = n.data("ts");
          var hide = (typeof ts === "number") && ts > cutoff;
          n.toggleClass("time-hidden", hide);
          if (!hide) visible++;
        });
        cy.edges().forEach(function (e) {
          var srcHidden = e.source().hasClass("time-hidden");
          var tgtHidden = e.target().hasClass("time-hidden");
          e.toggleClass("time-hidden", srcHidden || tgtHidden);
        });
        if (status) {
          var total = cy.nodes().length;
          status.textContent = visible === total
            ? ""
            : "Showing " + visible + " of " + total + " nodes.";
        }
      });
    }

    // Restore shared cutoff (set by main lattice or a sibling cluster page).
    var stored = readSharedCutoff();
    if (stored !== null) {
      var clamped = Math.min(Math.max(stored, Number(slider.min)), Number(slider.max));
      slider.value = String(clamped);
      if (label) label.textContent = formatDate(clamped);
    }

    slider.addEventListener("input", function () {
      var cutoff = Number(slider.value);
      if (label) label.textContent = formatDate(cutoff);
      writeSharedCutoff(cutoff);
      apply();
    });

    apply();
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

  function formatDate(ms) {
    var d = new Date(ms);
    var y = d.getUTCFullYear();
    var m = String(d.getUTCMonth() + 1).padStart(2, "0");
    var day = String(d.getUTCDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
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
