/* Topology — cây phân tầng Core → Switch access → AP (dagre, top-down) */
(function (global) {
  "use strict";

  var POLL_MS = 60000;
  var SSE_DEBOUNCE_MS = 2000;
  var dagreReady = false;

  function ensureDagre() {
    if (dagreReady) return true;
    if (global.cytoscape && global.cytoscapeDagre) {
      try {
        global.cytoscape.use(global.cytoscapeDagre);
        dagreReady = true;
      } catch (e) { /* đã register hoặc lỗi → fallback breadthfirst */ }
    }
    return dagreReady;
  }

  function buildQuery(apiUrl, filterAc, filterSwitch) {
    var params = [];
    if (filterAc && filterAc.value) params.push("ac=" + encodeURIComponent(filterAc.value));
    if (filterSwitch && filterSwitch.value) params.push("switch=" + encodeURIComponent(filterSwitch.value));
    return params.length ? apiUrl + "?" + params.join("&") : apiUrl;
  }

  function cytoscapeStyle() {
    return [
      {
        selector: "node[type='core']",
        style: {
          shape: "round-rectangle",
          width: "label",
          height: "label",
          padding: "10px",
          "background-color": "#dbeafe",
          "border-width": 3,
          "border-color": "#1d4ed8",
          label: "data(label)",
          "font-size": 13,
          "font-weight": "bold",
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "wrap",
          "text-max-width": 170,
          "text-overflow-wrap": "anywhere",
        },
      },
      {
        selector: "node[type='core'][online='false']",
        style: { "border-color": "#dc2626", "background-color": "#fee2e2" },
      },
      {
        selector: "node[type='switch']",
        style: {
          shape: "round-rectangle",
          width: "label",
          height: "label",
          padding: "9px",
          "background-color": "#e0e7ff",
          "border-width": 2,
          "border-color": "#4f46e5",
          label: "data(label)",
          "font-size": 11,
          "font-weight": "bold",
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "wrap",
          "text-max-width": 150,
          "text-overflow-wrap": "anywhere",
        },
      },
      {
        selector: "node[type='switch'][online='false']",
        style: { "border-color": "#dc2626", "background-color": "#fee2e2" },
      },
      {
        selector: "node[type='orphan-group']",
        style: {
          shape: "round-rectangle",
          width: "label",
          height: "label",
          padding: "9px",
          "background-color": "#f3f4f6",
          "border-width": 2,
          "border-color": "#9ca3af",
          "border-style": "dashed",
          label: "data(label)",
          "font-size": 11,
          "font-weight": "bold",
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "wrap",
          "text-max-width": 150,
          "text-overflow-wrap": "anywhere",
        },
      },
      {
        selector: "node[type='ap']",
        style: {
          shape: "round-rectangle",
          width: 104,
          height: 46,
          "background-color": "#dcfce7",
          "border-width": 2,
          "border-color": "#16a34a",
          label: "data(label)",
          "font-size": 9,
          "line-height": 1.25,
          "text-wrap": "wrap",
          "text-max-width": 98,
          "text-valign": "center",
          "text-halign": "center",
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
        selector: "edge[type='uplink']",
        style: {
          width: 2.5,
          "line-color": "#6366f1",
          "target-arrow-color": "#6366f1",
          "target-arrow-shape": "triangle",
          "curve-style": "taxi",
          "taxi-direction": "downward",
          "taxi-turn": "30px",
          // Nhãn = cổng downlink phía parent (CORE). Đặt ở đoạn DỌC sát switch con
          // (target) để mỗi nhãn tản theo từng switch, không dồn lên đoạn ngang
          // dùng chung dưới CORE (gây đè nét + đè nhau).
          "target-label": "data(label)",
          "target-text-offset": 30,
          "font-size": 9,
          color: "#3730a3",
          "text-background-color": "#ffffff",
          "text-background-opacity": 0.92,
          "text-background-padding": "2px",
          "text-background-shape": "roundrectangle",
          "text-border-width": 1,
          "text-border-color": "#c7d2fe",
          "text-border-opacity": 1,
        },
      },
      {
        selector: "edge[type='ap']",
        style: {
          width: 1.5,
          "line-color": "#86efac",
          "target-arrow-color": "#86efac",
          "target-arrow-shape": "triangle",
          "arrow-scale": 0.8,
          "curve-style": "taxi",
          "taxi-direction": "downward",
          "taxi-turn": "20px",
        },
      },
      {
        selector: "edge[type='ap'][online='false']",
        style: { "line-color": "#fca5a5", "target-arrow-color": "#fca5a5" },
      },
      {
        selector: "edge[inferred='true']",
        style: {
          "line-style": "dashed",
          "line-color": "#cbd5e1",
          "target-arrow-color": "#cbd5e1",
        },
      },
      {
        selector: "node.topo-offline-pulse",
        style: { "overlay-opacity": 0.3, "overlay-color": "#dc2626" },
      },
      {
        selector: "node.hidden-offline-filter",
        style: { display: "none" },
      },
      {
        selector: ":selected",
        style: { "border-width": 3, "overlay-opacity": 0.12 },
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
      runLayout();
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
      if (opts.layoutHint) {
        if (meta.switch_filter) {
          opts.layoutHint.textContent = "Đang xem 1 switch — Core trên cùng, AP bên dưới.";
        } else if (meta.core_name) {
          opts.layoutHint.textContent =
            "Phân tầng cây: " + meta.core_name + " (core) → switch (trunk) → AP. Nhãn trên cạnh = cổng trunk.";
        } else if ((meta.switch_count || 0) > 6) {
          opts.layoutHint.textContent = "Nhiều switch — chọn filter Switch để dễ nhìn.";
        } else {
          opts.layoutHint.textContent = "Core → switch access → AP (đường nối switch → AP).";
        }
      }
    }

    function showPanel(node) {
      if (!opts.panel) return;
      var d = node.data();
      var title = d.full_label || d.label || "—";
      opts.panelTitle.textContent = title;
      var lines = [];
      if (d.type === "ap") {
        if (d.mac) lines.push("<div><strong>MAC:</strong> " + d.mac + "</div>");
        if (d.ip) lines.push("<div><strong>IP:</strong> " + d.ip + "</div>");
        if (d.switch_name) {
          lines.push("<div><strong>Switch:</strong> " + d.switch_name + "</div>");
          lines.push("<div><strong>Port:</strong> " + (d.switch_port || "—") + "</div>");
        } else if (d.orphan) {
          lines.push('<div class="text-warning">Chưa map — chưa biết switch/port</div>');
        }
        lines.push("<div><strong>Client:</strong> " + (d.client_count != null ? d.client_count : "—") + "</div>");
        lines.push("<div><strong>Trạng thái:</strong> " +
          (d.online === false || d.online === "false"
            ? '<span class="text-danger">Offline</span>'
            : '<span class="text-success">Online</span>') + "</div>");
        if (lastMeta.ac_id) {
          lines.push('<div class="mt-2"><a href="' + opts.wlanDetailBase + lastMeta.ac_id + '/">WLAN AC</a></div>');
        }
      } else if (d.type === "core" || d.type === "switch") {
        lines.push('<div class="badge ' + (d.type === "core" ? "bg-primary" : "bg-secondary") + ' mb-1">' +
          (d.type === "core" ? "Core switch" : "Switch access") + "</div>");
        if (d.ip) lines.push("<div><strong>IP:</strong> " + d.ip + "</div>");
        if (d.location) lines.push("<div><strong>Vị trí:</strong> " + d.location + "</div>");
        if (d.ap_count != null) lines.push("<div><strong>AP:</strong> " + d.ap_count + "</div>");
        if (d.detail_url) {
          lines.push('<div class="mt-2"><a href="' + d.detail_url + '">Chi tiết switch</a></div>');
        }
      }
      opts.panelBody.innerHTML = lines.join("");
      opts.panel.classList.add("visible");
    }

    function runLayout() {
      if (!cy || cy.nodes().length === 0) return;
      var eles = cy.elements(":visible");
      var useDagre = ensureDagre();
      var layoutOpts = useDagre
        ? {
            name: "dagre",
            rankDir: "TB",
            nodeSep: 22,
            edgeSep: 8,
            rankSep: 75,
            ranker: "tight-tree",
            fit: true,
            padding: 35,
          }
        : {
            name: "breadthfirst",
            directed: true,
            spacingFactor: 1.1,
            fit: true,
            padding: 35,
          };
      eles.layout(layoutOpts).run();
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
            if (d.inferred !== undefined) d.inferred = d.inferred ? "true" : "false";
          });
          if (!cy) {
            cy = cytoscape({
              container: opts.container,
              elements: elements,
              style: cytoscapeStyle(),
              minZoom: 0.12,
              maxZoom: 2.5,
              wheelSensitivity: 0.25,
              boxSelectionEnabled: false,
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
          setTimeout(runLayout, 80);
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
    if (opts.btnFit) opts.btnFit.addEventListener("click", function () {
      if (cy) cy.fit(undefined, 40);
    });
    if (opts.panelClose && opts.panel) {
      opts.panelClose.addEventListener("click", function () {
        opts.panel.classList.remove("visible");
      });
    }

    loadGraph().then(function () {
      schedulePoll();
      try {
        if (global.Realtime && global.Realtime.connectSSE) {
          global.Realtime.connectSSE(opts.sseUrl, onSseMetrics, function () {});
        }
      } catch (e) { /* ignore */ }
    });

    // Cho phép module khác (topology_links.js) vẽ lại ngay sau khi thêm/xoá link.
    return { reload: loadGraph };
  }

  global.Topology = { init: init };
})(window);
