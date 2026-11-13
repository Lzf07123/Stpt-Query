(function () {
  "use strict";

  const bar = document.getElementById("noticeBar");
  const track = document.getElementById("noticeTrack");
  const text = document.getElementById("noticeText");
  const historyButton = document.getElementById("noticeHistory");
  const toggleButton = document.getElementById("noticeToggle");
  const historyModal = document.getElementById("noticeHistoryModal");
  const historyClose = document.getElementById("noticeHistoryClose");
  const historyDismiss = document.getElementById("noticeHistoryDismiss");
  const historyList = document.getElementById("noticeHistoryList");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const PAUSE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5v14M15 5v14"></path></svg>';
  const PLAY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 5v14l11-7L8 5z"></path></svg>';

  let notices = [];
  let index = 0;
  let rotateTimer = null;
  let historyFocus = null;
  let rotationPaused = false;   // 用户主动暂停（控件）
  let hoverPause = false;       // 悬停/焦点临时暂停，离开后自动恢复

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
    if (rotationPaused || hoverPause) return;
    rotateTimer = window.setTimeout(function () {
      index = (index + 1) % notices.length;
      renderNotice();
    }, Math.max(1000, delay));
  }

  function setPaused(paused) {
    rotationPaused = paused;
    if (paused) stopRotation();
    else scheduleRotation();
    if (toggleButton) {
      toggleButton.setAttribute("aria-pressed", paused ? "true" : "false");
      toggleButton.setAttribute("aria-label", paused ? "恢复通知轮播" : "暂停通知轮播");
      toggleButton.innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
    }
  }

  function setHoverPause(active) {
    if (hoverPause === active) return;
    hoverPause = active;
    if (active) stopRotation();
    else scheduleRotation();
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
      const response = await fetch("/api/v1/notices/history?limit=50", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const envelope = await response.json();
      const payload = envelope && envelope.data ? envelope.data : {};
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
      const response = await fetch("/api/v1/notices/active", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const envelope = await response.json();
      const payload = envelope && envelope.data ? envelope.data : {};
      notices = Array.isArray(payload.notices) ? payload.notices : [];
      if (!notices.length) return;
      bar.classList.remove("hidden");
      if (toggleButton) toggleButton.hidden = notices.length < 2;
      renderActive();
    } catch (error) {
      bar.classList.add("hidden");
    }
  }

  if (historyButton) historyButton.addEventListener("click", openHistory);
  if (toggleButton) toggleButton.addEventListener("click", function () {
    setPaused(!rotationPaused);
  });
  if (bar) {
    // 轮播控件契约：悬停或键盘焦点进入即停止自动切换，离开后恢复；
    // 用户主动暂停（控件）不受悬停状态影响
    bar.addEventListener("mouseenter", function () { setHoverPause(true); });
    bar.addEventListener("mouseleave", function () { setHoverPause(false); });
    bar.addEventListener("focusin", function () { setHoverPause(true); });
    bar.addEventListener("focusout", function (event) {
      if (!bar.contains(event.relatedTarget)) setHoverPause(false);
    });
  }
  if (historyClose) historyClose.addEventListener("click", closeHistory);
  if (historyDismiss) historyDismiss.addEventListener("click", closeHistory);
  if (historyModal) historyModal.addEventListener("click", function (event) {
    if (event.target === historyModal) closeHistory();
  });
  if (historyModal) historyModal.addEventListener("keydown", function (event) {
    if (event.key !== "Tab") return;
    const focusable = Array.prototype.slice.call(historyModal.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(function (node) { return !node.classList.contains("hidden"); });
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !historyModal.classList.contains("hidden")) closeHistory();
  });
  if (reducedMotion.addEventListener) reducedMotion.addEventListener("change", renderActive);

  loadNotices();
})();
