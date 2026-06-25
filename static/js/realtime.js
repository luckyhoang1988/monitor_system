/* Realtime SSE helper — dùng chung cho dashboard index và trang chi tiết.
 * connectSSE mở 1 EventSource, parse event "metrics", và gọi onFail (chuyển
 * sang polling) khi kết nối hỏng liên tục. EventSource tự reconnect; chỉ khi
 * thất bại nhiều lần mới đóng hẳn và fallback.
 */
(function (global) {
  "use strict";

  function connectSSE(url, onMetrics, onFail) {
    if (!("EventSource" in global)) {
      if (onFail) onFail();
      return null;
    }
    var es = new EventSource(url, { withCredentials: true });
    var failures = 0;

    es.addEventListener("metrics", function (e) {
      failures = 0;
      try {
        onMetrics(JSON.parse(e.data));
      } catch (err) {
        /* payload hỏng — bỏ qua */
      }
    });
    es.addEventListener("open", function () {
      failures = 0;
    });
    es.onerror = function () {
      // EventSource tự reconnect. Sau vài lần fail liên tiếp → bỏ cuộc, fallback.
      failures += 1;
      if (failures >= 4) {
        es.close();
        if (onFail) onFail();
      }
    };
    return es;
  }

  function statusBadge(online) {
    return online
      ? '<span class="badge badge-online status-dot rounded-pill px-2">On</span>'
      : '<span class="badge badge-offline status-dot rounded-pill px-2">Off</span>';
  }

  // Cập nhật badge trạng thái của 1 hàng thiết bị trên dashboard index, tại chỗ.
  function updateFleetRow(payload) {
    if (!payload || payload.device_id == null) return;
    var row = document.querySelector(
      'tr.js-device-row[data-device-id="' + payload.device_id + '"]'
    );
    if (!row) return;
    var cell = row.querySelector(".js-status-cell");
    if (cell) cell.innerHTML = statusBadge(payload.online);
  }

  global.Realtime = {
    connectSSE: connectSSE,
    statusBadge: statusBadge,
    updateFleetRow: updateFleetRow,
  };
})(window);
