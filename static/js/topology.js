/* Topology AP ↔ Switch — Cytoscape.js */
(function (global) {
  "use strict";

  var POLL_MS = 60000;
  var SSE_DEBOUNCE_MS = 2000;

  function buildQuery(apiUrl, filterAc, filterSwitch) {
    var params = [];
    if (filterAc && filterAc.value) params.push("ac=" + encodeURIComponent(filterAc.value));
    if (filterSwitch && filterSwitch.value) params.push("switch=" + encodeURIComponent(filterSwitch.value));
    return params.length ? apiUrl + "?" + params.join("&") : apiUrl;
  }

  function cytoscapeStyle() {
    return [
      {
        selector: "node[type='switch']",
        style: {
          shape: "round-rectangle",
          width: 120,
          height: 44,
          "background-color": "#dbeafe",
          "border-width": 3,
          "border-color": "#2563eb",
          label: "data(label)",
          "font-size": 11,
          "text-wrap": "wrap",
          "text-max-width": 110,
          "text-valign": "center",
          "text-halign": "center",
        },
      },
      {
        selector: "node[type='switch'][online='false']",
        style: { "border-color": "#dc2626", "background-color": "#fee2e2" },
      },
      {
        selector: "node[type='ap']",
        style: {
          shape: "ellipse",
          width: 56,
          height: 56,
          "background-color": "#dcfce7",
          "border-width": 2,
          "border-color": "#16a34a",
          label: "data(label)",
          "font-size": 9,
          "text-wrap": "wrap",
          "text-max-width": 70,
          "text-valign": "bottom",
          "text-margin-y": 6,
        },
      },
      {
        selector: "node[type='ap'][online='false']",
        style: {
          "background-color": "#fee2e2",
          "border-color": "#dc2626",
        },
      },
      {
        selector: "node[type='ap'][orphan='true']",
        style: {
          "background-color": "#f3f4f6",
          "border-color": "#9ca3af",
          "border-style": "dashed",
        },
      },
      {
        selector: "node.topo-offline-pulse",
        style: { "overlay-opacity": 0.25, "overlay-color": "#dc2626" },
      },
      {
        selector: "node:hidden-offline-filter",
        style: { display: "none" },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "line-color": "#94a3b8",
          "target-arrow-color": "#94a3b8",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(label)",
          "font-size": 8,
          "text-rotation": "autorotate",
          "text-margin-y": -8,
        },
      },
      {
        selector: "edge[confirmed='false']",
        style: { "line-style": "dashed", "line-color": "#d97706" },
      },
      {
        selector: ":selected",
        style: {
          "border-width": 4,
          "overlay-opacity": 0.15,
        },
      },
    ];
  }

  function init(opts) {
    var cy = null;
    var pollTimer = null;
    var sseDebounce = null;
    var offlineOnly = false;
    var lastMeta = {};

    function applyOfflineFilter() {
      if (!cy) return;
      cy.nodes("[type='ap']").forEach(function (n) {
        if (offlineOnly && n.data("online") !== false && n.data("online") !== "false") {
          n.addClass("hidden-offline-filter");
        } else {
          n.removeClass("hidden-offline-filter");
        }
      });
    }

    function updatePulseClasses() {
      if (!cy) return;
      cy.nodes("[type='ap']").forEach(function (n) {
        var off = n.data("online") === false || n.data("online") === "false";
        if (off) n.addClass("topo-offline-pulse");
        else n.removeClass("topo-offline-pulse");
      });
    }

    function updateMeta(meta) {
      lastMeta = meta || {};
      if (opts.metaApTotal) opts.metaApTotal.textContent = meta.ap_total != null ? meta.ap_total : "—";
      if (opts.metaApMapped) opts.metaApMapped.textContent = meta.ap_mapped != null ? meta.ap_mapped : "—";
      if (opts.metaApOffline) opts.metaApOffline.textContent = meta.ap_offline != null ? meta.ap_offline : "—";
      if (opts.metaApUnmapped) opts.metaApUnmapped.textContent = meta.ap_unmapped != null ? meta.ap_unmapped : "—";
      if (opts.metaUpdated && meta.generated_at) {
        opts.metaUpdated.textContent = "Cập nhật: " + meta.generated_at.slice(11, 19);
      }
    }

    function showPanel(node) {
      if (!opts.panel) return;
      var d = node.data();
      opts.panelTitle.textContent = d.label || "—";
      var lines = [];
      if (d.type === "ap") {
        if (d.mac) lines.push("<div><strong>MAC:</strong> " + d.mac + "</div>");
        if (d.ip) lines.push("<div><strong>IP:</strong> " + d.ip + "</div>");
        if (d.switch_name) {
          lines.push("<div><strong>Switch:</strong> " + d.switch_name + "</div>");
          lines.push("<div><strong>Port:</strong> " + (d.switch_port || "—") + "</div>");
        } else if (d.orphan) {
          lines.push('<div class="text-warning">Chưa map LLDP — chưa biết switch/port</div>');
        }
        lines.push("<div><strong>Client:</strong> " + (d.client_count != null ? d.client_count : "—") + "</div>");
        lines.push("<div><strong>Trạng thái:</strong> " +
          (d.online === false || d.online === "false"
            ? '<span class="text-danger">Offline</span>'
            : '<span class="text-success">Online</span>') + "</div>");
        if (lastMeta.ac_id) {
          var url = opts.wlanDetailBase + lastMeta.ac_id + "/";
          lines.push('<div class="mt-2"><a href="' + url + '">Xem trên WLAN AC</a></div>');
        }
      } else if (d.type === "switch") {
        if (d.ip) lines.push("<div><strong>IP:</strong> " + d.ip + "</div>");
        if (d.location) lines.push("<div><strong>Vị trí:</strong> " + d.location + "</div>");
        lines.push("<div><strong>Trạng thái:</strong> " +
          (d.online === false || d.online === "false" ? "Offline" : "Online") + "</div>");
        if (d.detail_url) {
          lines.push('<div class="mt-2"><a href="' + d.detail_url + '">Chi tiết switch</a></div>');
        }
      }
      opts.panelBody.innerHTML = lines.join("");
      opts.panel.classList.add("visible");
    }

    function runLayout() {
      if (!cy || cy.nodes().length === 0) return;
      cy.layout({
        name: "breadthfirst",
        directed: true,
        padding: 40,
        spacingFactor: 1.2,
        roots: cy.nodes("[type='switch']"),
      }).run();
    }

    function loadGraph() {
      var url = buildQuery(opts.apiUrl, opts.filterAc, opts.filterSwitch);
      return fetch(url, { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var elements = (data.nodes || []).concat(data.edges || []);
          elements.forEach(function (el) {
            var d = el.data || {};
            if (d.online !== undefined) d.online = d.online ? "true" : "false";
            if (d.confirmed !== undefined) d.confirmed = d.confirmed ? "true" : "false";
            if (d.orphan !== undefined) d.orphan = d.orphan ? "true" : "false";
          });
          if (!cy) {
            cy = cytoscape({
              container: opts.container,
              elements: elements,
              style: cytoscapeStyle(),
              minZoom: 0.2,
              maxZoom: 3,
              wheelSensitivity: 0.3,
            });
            cy.on("tap", "node", function (evt) {
              showPanel(evt.target);
            });
            cy.on("tap", function (evt) {
              if (evt.target === cy && opts.panel) opts.panel.classList.remove("visible");
            });
          } else {
            cy.json({ elements: elements });
          }
          updateMeta(data.meta);
          updatePulseClasses();
          applyOfflineFilter();
          runLayout();
        })
        .catch(function (err) {
          console.warn("Topology load failed:", err);
        });
    }

    function schedulePoll() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(loadGraph, POLL_MS);
    }

    function onSseMetrics(payload) {
      if (payload && payload.device_type === "wlan_controller") {
        if (sseDebounce) clearTimeout(sseDebounce);
        sseDebounce = setTimeout(loadGraph, SSE_DEBOUNCE_MS);
      }
      if (payload && payload.device_type === "switch" && cy) {
        var nid = "sw-" + payload.device_id;
        var node = cy.getElementById(nid);
        if (node.length) {
          node.data("online", payload.online ? "true" : "false");
        }
      }
    }

    if (opts.filterAc) opts.filterAc.addEventListener("change", loadGraph);
    if (opts.filterSwitch) opts.filterSwitch.addEventListener("change", loadGraph);
    if (opts.filterOfflineOnly) {
      opts.filterOfflineOnly.addEventListener("change", function () {
        offlineOnly = opts.filterOfflineOnly.checked;
        applyOfflineFilter();
      });
    }
    if (opts.btnRelayout) opts.btnRelayout.addEventListener("click", runLayout);
    if (opts.panelClose && opts.panel) {
      opts.panelClose.addEventListener("click", function () {
        opts.panel.classList.remove("visible");
      });
    }

    loadGraph().then(function () {
      schedulePoll();
      try {
        if (global.Realtime && global.Realtime.connectSSE) {
          global.Realtime.connectSSE(opts.sseUrl, onSseMetrics, function () {
            /* SSE fail — poll 60s vẫn chạy */
          });
        }
      } catch (e) { /* ignore */ }
    });
  }

  global.Topology = { init: init };
})(window);
