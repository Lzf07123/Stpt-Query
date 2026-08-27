(function () {
  "use strict";

  var container = document.getElementById("healthStatus");
  if (!container) return;

  var pills = Array.prototype.slice.call(container.querySelectorAll(".health-pill"));
  var labels = ["本站", "查询代理", "学校服务"];

  function render(index, state) {
    var pill = pills[index];
    if (!pill) return;
    var text = state === "up" ? "正常"
      : state === "degraded" ? "降级"
      : state === "down" ? "异常"
      : "未知";
    pill.className = "health-pill " + state;
    pill.title = labels[index] + "：" + text;
    pill.innerHTML = '<i class="health-dot"></i><span>' +
      labels[index] + " · " + text + "</span>";
  }

  function refresh() {
    render(0, "up");
    render(1, "unknown");
    render(2, "unknown");
    fetch("/health/public", { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) {
      render(1, data.proxy && data.proxy.status || "unknown");
      render(2, data.school && data.school.status || "unknown");
    }).catch(function () {
      render(1, "unknown");
      render(2, "unknown");
    });
  }

  refresh();
  window.setInterval(refresh, 30000);
})();
