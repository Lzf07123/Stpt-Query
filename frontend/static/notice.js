(function () {
  "use strict";

  const bar = document.getElementById("noticeBar");
  const track = document.getElementById("noticeTrack");
  const text = document.getElementById("noticeText");
  const historyButton = document.getElementById("noticeHistory");
  const historyModal = document.getElementById("noticeHistoryModal");
  const historyClose = document.getElementById("noticeHistoryClose");
  const historyDismiss = document.getElementById("noticeHistoryDismiss");
  const historyList = document.getElementById("noticeHistoryList");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  let notices = [];
  let index = 0;
  let rotateTimer = null;
  let historyFocus = null;

  function formatTime(value) {
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) return "时间未知";
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function stopRotation() {
    if (rotateTimer) window.clearTimeout(rotateTimer);
    rotateTimer = null;
  }

  function measureFirstCharacter(item) {
    if (!text.firstChild) return 0;
    const range = document.createRange();
    range.setStart(text.firstChild, 0);
    range.setEnd(text.firstChild, 1);
    const rect = range.getBoundingClientRect();
    const width = rect.right - rect.left;
    if (Number.isFinite(width) && width > 0) return width;
    return Math.max(10, text.scrollWidth / Math.max(1, item.content.length));
  }

  function renderNotice() {
    const item = notices[index];
    if (!item) return;
    bar.classList.toggle("notice-bar-warning", item.level === "warning");
    text.textContent = item.content;
    text.removeAttribute("style");
    track.classList.remove("is-scrolling", "is-static");
    track.innerHTML = "";
    track.append(text);
    window.requestAnimationFrame(function () {
      const overflow = text.scrollWidth > track.clientWidth + 2;
      if (overflow) {
        const firstCharWidth = measureFirstCharacter(item);
        const start = Math.max(0, track.clientWidth - firstCharWidth - 1);
        const distance = start + text.scrollWidth;
        const duration = Math.max(10, Math.min(30, distance / 90));
        text.style.setProperty("--notice-start", start + "px");
        text.style.setProperty("--notice-duration", duration + "s");
        track.classList.add("is-scrolling");
        scheduleRotation((duration + 0.25) * 1000);
      } else {
        track.classList.add("is-static");
        scheduleRotation();
      }
    });
  }

  function renderStaticList() {
    track.innerHTML = "";
    track.classList.remove("is-scrolling", "is-static");
    track.classList.add("is-static-list");
    bar.classList.toggle("notice-bar-warning", notices.some(function (item) {
      return item.level === "warning";
    }));
    notices.forEach(function (item) {
      const row = document.createElement("div");
      row.className = "notice-static-item";
      row.setAttribute("role", "listitem");
      row.textContent = item.content;
      track.appendChild(row);
    });
  }

  function scheduleRotation(delay = 8000) {
    if (notices.length < 2) return;
    stopRotation();
    rotateTimer = window.setTimeout(function () {
      index = (index + 1) % notices.length;
      renderNotice();
    }, Math.max(1000, delay));
  }

  function renderActive() {
    stopRotation();
    track.classList.remove("is-static-list");
    if (reducedMotion.matches) {
      renderStaticList();
      return;
    }
    index = 0;
    renderNotice();
  }

  function createHistoryItem(item) {
    const row = document.createElement("article");
    row.className = "notice-history-item";
    const badge = document.createElement("span");
    badge.className = "badge " + (item.level === "warning" ? "badge-warning" : "badge-primary");
    badge.textContent = item.level === "warning" ? "重要" : "普通";
    const content = document.createElement("p");
    content.textContent = item.content;
    const time = document.createElement("small");
    time.textContent = formatTime(item.published_at) + " 发布 · " + formatTime(item.archived_at) + " 下线";
    row.append(badge, content, time);
    return row;
  }

  async function openHistory() {
    historyFocus = document.activeElement;
    historyModal.classList.remove("hidden");
    historyList.textContent = "加载中";
    historyClose.focus();
    try {
      const response = await fetch("/notices/history?limit=50", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      historyList.innerHTML = "";
      const items = Array.isArray(payload.notices) ? payload.notices : [];
      if (!items.length) {
        historyList.textContent = "暂无历史通知";
        return;
      }
      items.forEach(function (item) {
        historyList.appendChild(createHistoryItem(item));
      });
    } catch (error) {
      historyList.textContent = "通知历史暂时不可用";
    }
  }

  function closeHistory() {
    historyModal.classList.add("hidden");
    if (historyFocus && document.contains(historyFocus)) historyFocus.focus();
  }

  async function loadNotices() {
    if (!bar || !track || !text) return;
    try {
      const response = await fetch("/notices/active", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      notices = Array.isArray(payload.notices) ? payload.notices : [];
      if (!notices.length) return;
      bar.classList.remove("hidden");
      renderActive();
    } catch (error) {
      bar.classList.add("hidden");
    }
  }

  if (historyButton) historyButton.addEventListener("click", openHistory);
  if (historyClose) historyClose.addEventListener("click", closeHistory);
  if (historyDismiss) historyDismiss.addEventListener("click", closeHistory);
  if (historyModal) historyModal.addEventListener("click", function (event) {
    if (event.target === historyModal) closeHistory();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !historyModal.classList.contains("hidden")) closeHistory();
  });
  if (reducedMotion.addEventListener) reducedMotion.addEventListener("change", renderActive);

  loadNotices();
})();
