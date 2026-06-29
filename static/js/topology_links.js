/* Topology Links — thêm/xoá link topology thủ công (admin) qua modal Bootstrap. */
(function (global) {
  "use strict";

  function getCsrfToken() {
    var el = document.querySelector("[name=csrfmiddlewaretoken]");
    if (el) return el.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function el(id) { return document.getElementById(id); }

  function reloadGraph() {
    if (global.topoInstance && global.topoInstance.reload) {
      global.topoInstance.reload();
    }
  }

  function init(opts) {
    var kindRadios = document.querySelectorAll("input[name='linkKind']");
    var localDevice = el("linkLocalDevice");
    var localPort = el("linkLocalPort");
    var localPortManual = el("linkLocalPortManual");
    var localPortText = el("linkLocalPortText");
    var apFields = el("apFields");
    var switchFields = el("switchFields");
    var linkAp = el("linkAp");
    var apManual = el("linkApManual");
    var apManualFields = el("apManualFields");
    var apMac = el("linkApMac");
    var apName = el("linkApName");
    var remoteDevice = el("linkRemoteDevice");
    var remotePort = el("linkRemotePort");
    var saveBtn = el("linkSaveBtn");
    var alertBox = el("linkAlert");
    var modalEl = el("linkModal");
    var manageBody = el("manageLinksBody");

    function currentKind() {
      var r = document.querySelector("input[name='linkKind']:checked");
      return r ? r.value : "ap";
    }

    function showAlert(msg) {
      alertBox.textContent = msg;
      alertBox.classList.remove("d-none");
    }
    function clearAlert() { alertBox.classList.add("d-none"); }

    function toggleKind() {
      var ap = currentKind() === "ap";
      apFields.classList.toggle("d-none", !ap);
      switchFields.classList.toggle("d-none", ap);
    }

    function loadPorts(devId) {
      localPort.innerHTML = '<option value="">— Đang tải —</option>';
      localPort.disabled = true;
      if (!devId) { localPort.innerHTML = '<option value="">— Chọn switch trước —</option>'; return; }
      fetch(opts.portsUrl + "?device=" + encodeURIComponent(devId), { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var ports = d.ports || [];
          if (!ports.length) {
            localPort.innerHTML = '<option value="">— Không có cổng (nhập tay) —</option>';
          } else {
            localPort.innerHTML = '<option value="">— Chọn cổng —</option>';
            ports.forEach(function (p) {
              var o = document.createElement("option");
              o.value = p.name;
              var tag = p.is_uplink ? " [uplink]" : (p.port_mode ? " [" + p.port_mode + "]" : "");
              o.textContent = p.name + tag;
              localPort.appendChild(o);
            });
          }
          localPort.disabled = false;
        })
        .catch(function () { localPort.innerHTML = '<option value="">— Lỗi tải cổng —</option>'; });
    }

    function loadAps() {
      linkAp.innerHTML = '<option value="">— Đang tải —</option>';
      var acVal = opts.filterAc && opts.filterAc.value ? opts.filterAc.value : "";
      var url = opts.apsUrl + (acVal ? "?ac=" + encodeURIComponent(acVal) : "");
      fetch(url, { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var aps = d.aps || [];
          if (!aps.length) {
            linkAp.innerHTML = '<option value="">— AC chưa có AP (nhập tay) —</option>';
            return;
          }
          linkAp.innerHTML = '<option value="">— Chọn AP —</option>';
          aps.forEach(function (a) {
            var o = document.createElement("option");
            o.value = a.mac || "";
            o.dataset.name = a.name || "";
            var flag = a.mapped ? " ✓đã map" : " ·chưa map";
            o.textContent = (a.name || a.mac || "AP") + flag;
            linkAp.appendChild(o);
          });
        })
        .catch(function () { linkAp.innerHTML = '<option value="">— Lỗi tải AP —</option>'; });
    }

    function gatherPayload() {
      var kind = currentKind();
      var port = localPortManual.checked ? localPortText.value.trim() : localPort.value;
      var payload = {
        kind: kind,
        local_device: localDevice.value,
        local_port: port,
      };
      if (kind === "ap") {
        if (apManual.checked) {
          payload.remote_ap_mac = apMac.value.trim();
          payload.remote_ap_name = apName.value.trim();
        } else {
          var opt = linkAp.options[linkAp.selectedIndex];
          payload.remote_ap_mac = linkAp.value;
          payload.remote_ap_name = opt ? (opt.dataset.name || "") : "";
        }
      } else {
        payload.remote_device = remoteDevice.value;
        payload.remote_port = remotePort.value.trim();
      }
      return payload;
    }

    function save() {
      clearAlert();
      var payload = gatherPayload();
      saveBtn.disabled = true;
      fetch(opts.linksUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(payload),
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok || !res.d.success) {
            showAlert(res.d.message || "Lưu thất bại.");
            return;
          }
          reloadGraph();
          if (modalEl && global.bootstrap) {
            var m = global.bootstrap.Modal.getInstance(modalEl) || new global.bootstrap.Modal(modalEl);
            m.hide();
          }
        })
        .catch(function () { showAlert("Lỗi kết nối."); })
        .finally(function () { saveBtn.disabled = false; });
    }

    function loadManageList() {
      manageBody.innerHTML = '<tr><td colspan="6" class="text-center text-muted small py-3">Đang tải…</td></tr>';
      fetch(opts.linksUrl, { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var links = d.links || [];
          if (!links.length) {
            manageBody.innerHTML = '<tr><td colspan="6" class="text-center text-muted small py-3">Chưa có link thủ công.</td></tr>';
            return;
          }
          manageBody.innerHTML = "";
          links.forEach(function (l) {
            var tr = document.createElement("tr");
            var kindBadge = l.kind === "ap"
              ? '<span class="badge bg-success-subtle text-success">AP</span>'
              : '<span class="badge bg-primary-subtle text-primary">Switch</span>';
            tr.innerHTML =
              "<td>" + kindBadge + "</td>" +
              "<td class='small'>" + esc(l.local_device) + "</td>" +
              "<td class='small'>" + esc(l.local_port) + "</td>" +
              "<td class='small'>" + esc(l.remote) + "</td>" +
              "<td class='small'>" + esc(l.remote_port || "—") + "</td>" +
              "<td class='text-end'><button class='btn btn-sm btn-outline-danger btn-del-link' data-id='" + l.id + "'><i class='bi bi-trash'></i></button></td>";
            manageBody.appendChild(tr);
          });
        })
        .catch(function () {
          manageBody.innerHTML = '<tr><td colspan="6" class="text-center text-danger small py-3">Lỗi tải danh sách.</td></tr>';
        });
    }

    function esc(s) {
      var div = document.createElement("div");
      div.textContent = s == null ? "" : String(s);
      return div.innerHTML;
    }

    function deleteLink(id, btn) {
      if (!global.confirm("Xoá liên kết thủ công này?")) return;
      btn.disabled = true;
      fetch(opts.linksUrl + id + "/", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRFToken": getCsrfToken() },
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.success) { loadManageList(); reloadGraph(); }
          else { btn.disabled = false; global.alert(d.message || "Xoá thất bại."); }
        })
        .catch(function () { btn.disabled = false; global.alert("Lỗi kết nối."); });
    }

    // --- Event wiring ---
    Array.prototype.forEach.call(kindRadios, function (r) { r.addEventListener("change", toggleKind); });
    localDevice.addEventListener("change", function () { loadPorts(localDevice.value); });
    localPortManual.addEventListener("change", function () {
      var manual = localPortManual.checked;
      localPortText.classList.toggle("d-none", !manual);
      localPort.classList.toggle("d-none", manual);
    });
    apManual.addEventListener("change", function () {
      var manual = apManual.checked;
      apManualFields.classList.toggle("d-none", !manual);
      linkAp.classList.toggle("d-none", manual);
    });
    saveBtn.addEventListener("click", save);

    if (modalEl) {
      modalEl.addEventListener("show.bs.modal", function () {
        clearAlert();
        toggleKind();
        loadAps();
      });
    }

    var manageModal = el("manageLinksModal");
    if (manageModal) {
      manageModal.addEventListener("show.bs.modal", loadManageList);
    }
    if (manageBody) {
      manageBody.addEventListener("click", function (e) {
        var btn = e.target.closest(".btn-del-link");
        if (btn) deleteLink(btn.dataset.id, btn);
      });
    }
  }

  global.TopologyLinks = { init: init };
})(window);
