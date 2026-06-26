/* ChartRange — bộ chọn khung thời gian dùng chung cho các trang chi tiết.
 *
 * Giữ dropdown preset (1h/6h/24h/7d) + thêm option "custom" mở 2 ô datetime-local
 * (Từ/Đến) và nút Xem. Trả về query string để gắn vào API metrics.
 *
 * Dùng:
 *   ChartRange.init({selectId, customWrapId, fromId, toId, applyBtnId, onChange});
 *   fetch(`${API}?${ChartRange.query()}`);
 */
(function () {
  "use strict";

  var state = { sel: null, wrap: null, from: null, to: null };

  // Format Date → 'YYYY-MM-DDTHH:mm' cho input datetime-local (giờ địa phương).
  function toLocalInput(d) {
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function isCustom() {
    return state.sel && state.sel.value === "custom";
  }

  var ChartRange = {
    init: function (opts) {
      state.sel = document.getElementById(opts.selectId);
      state.wrap = document.getElementById(opts.customWrapId);
      state.from = document.getElementById(opts.fromId);
      state.to = document.getElementById(opts.toId);
      var applyBtn = document.getElementById(opts.applyBtnId);
      var onChange = opts.onChange || function () {};

      // Giá trị mặc định 2 ô: Đến = bây giờ, Từ = 24h trước.
      if (state.from && state.to && !state.from.value && !state.to.value) {
        var now = new Date();
        var dayAgo = new Date(now.getTime() - 24 * 3600 * 1000);
        state.to.value = toLocalInput(now);
        state.from.value = toLocalInput(dayAgo);
      }

      var toggleWrap = function () {
        if (!state.wrap) return;
        state.wrap.classList.toggle("d-none", !isCustom());
      };
      toggleWrap();

      if (state.sel) {
        state.sel.addEventListener("change", function () {
          toggleWrap();
          // Đổi sang preset → vẽ lại ngay; sang custom → chờ bấm Xem.
          if (!isCustom()) onChange();
        });
      }
      if (applyBtn) {
        applyBtn.addEventListener("click", function () {
          if (!(state.from && state.to && state.from.value && state.to.value)) return;
          // Người dùng chọn Từ > Đến → hoán đổi để luôn lọc đúng khoảng
          // (chuỗi datetime-local 'YYYY-MM-DDTHH:mm' so sánh chuỗi = so sánh thời gian).
          if (state.from.value > state.to.value) {
            var tmp = state.from.value;
            state.from.value = state.to.value;
            state.to.value = tmp;
          }
          onChange();
        });
      }
    },

    // Trả về query string: 'from=…&to=…' khi custom, ngược lại 'range=…'.
    query: function () {
      if (isCustom() && state.from && state.to && state.from.value && state.to.value) {
        return "from=" + encodeURIComponent(state.from.value) +
          "&to=" + encodeURIComponent(state.to.value);
      }
      return "range=" + encodeURIComponent(state.sel ? state.sel.value : "1h");
    },

    // true khi đang ở preset realtime (1h/6h/24h) — để SSE auto-refresh.
    isLive: function () {
      if (isCustom()) return false;
      var v = state.sel ? state.sel.value : "1h";
      return v === "1h" || v === "6h" || v === "24h";
    },
  };

  window.ChartRange = ChartRange;
})();
