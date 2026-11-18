
    // 页面不持有令牌：Authorization 由 nginx 反代时在服务端注入；
    // 本地直开静态文件调试时该值为空，查询请求会被后端 401 拒绝
    const GATEWAY_TOKEN = "";

    // 请求超时上限（毫秒）：后端单次编排有总预算（默认 100s），前端兜底避免
    // 上游/网关挂起时按钮无限 loading；轮询总时长上限防止任务永久 queued
    const FETCH_TIMEOUT_JOBS_SUBMIT = 30000;
    const FETCH_TIMEOUT_JOB_POLL = 30000;
    const FETCH_TIMEOUT_SYNC_RUN = 180000;
    // 日历操作含学校登录/快照重建（L3 回退真实登录），上界放宽到 60s
    const FETCH_TIMEOUT_CALENDAR = 60000;
    const JOB_POLL_MAX_DURATION = 8 * 60 * 1000;

    // 组合外部 signal（取消旧查询/返回按钮）与超时定时器：外部 abort 保持
    // AbortError 语义（静默返回），超时转为 TimeoutError（错误卡可区分文案）
    function fetchWithTimeout(url, options, timeoutMs) {
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
      const external = options && options.signal;
      if (external) {
        if (external.aborted) controller.abort();
        else external.addEventListener("abort", function () {
          controller.abort();
        }, { once: true });
      }
      const opts = Object.assign({}, options, { signal: controller.signal });
      return fetch(url, opts).then(
        function (resp) {
          clearTimeout(timer);
          return resp;
        },
        function (err) {
          clearTimeout(timer);
          if (timedOut) {
            const timeoutError = new Error("请求超时（" + Math.round(timeoutMs / 1000) + " 秒无响应）");
            timeoutError.name = "TimeoutError";
            throw timeoutError;
          }
          throw err;
        }
      );
    }

    const themeToggle = document.getElementById("themeToggle");
    const SUN_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
    const MOON_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    function applyTheme(theme, persist) {
      document.documentElement.classList.toggle("dark", theme === "dark");
      themeToggle.innerHTML = theme === "dark" ? SUN_ICON : MOON_ICON;
      themeToggle.setAttribute("aria-label", theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
      var bg = getComputedStyle(document.documentElement).getPropertyValue("--eduquery-bg").trim();
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta && bg) meta.setAttribute("content", bg);
      if (persist) {
        try { localStorage.setItem("eduquery-theme", theme); } catch (e) {}
      }
    }
    themeToggle.addEventListener("click", function () {
      var target = document.documentElement.classList.contains("dark") ? "light" : "dark";
      // E3 主题水波：View Transitions 从按钮位置圆形展开（不支持的浏览器静默降级）
      var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!document.startViewTransition || reduceMotion) {
        applyTheme(target, true);
        return;
      }
      var rect = themeToggle.getBoundingClientRect();
      var x = rect.left + rect.width / 2;
      var y = rect.top + rect.height / 2;
      var radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
      var transition = document.startViewTransition(function () {
        applyTheme(target, true);
      });
      transition.ready.then(function () {
        document.documentElement.animate(
          {
            clipPath: [
              "circle(0px at " + x + "px " + y + "px)",
              "circle(" + radius + "px at " + x + "px " + y + "px)"
            ]
          },
          { duration: 420, easing: "ease-out", pseudoElement: "::view-transition-new(root)" }
        );
      }).catch(function () { /* 转场被打断时静默 */ });
    });
    applyTheme(document.documentElement.classList.contains("dark") ? "dark" : "light", false);

    const form = document.getElementById("queryForm");
    const usernameInput = document.getElementById("username");
    const usernameError = document.getElementById("usernameError");
    const passwordError = document.getElementById("passwordError");
    usernameInput.addEventListener("input", function () {
      this.value = this.value.replace(/\D/g, "").slice(0, 10);
      if (!usernameError.classList.contains("hidden")) clearFieldError(usernameInput, usernameError);
    });
    usernameInput.addEventListener("blur", function () {
      if (this.value) validateUsernameField(false);
    });
    const passwordInput = document.getElementById("password");
    passwordInput.addEventListener("input", function () {
      if (!passwordError.classList.contains("hidden")) clearFieldError(passwordInput, passwordError);
    });
    passwordInput.addEventListener("blur", function () {
      if (this.value) validatePasswordField(false);
    });
    const optionInput = document.getElementById("option");
    const semestersInput = document.getElementById("semesters");
    const weeksInput = document.getElementById("weeks");
    const md2pdfInput = document.getElementById("md2pdf");
    const checkInput = document.getElementById("check");
    const includeAllInput = document.getElementById("includeAll");
    const submitBtn = document.getElementById("submitBtn");
    const submitSpinner = document.getElementById("submitSpinner");
    const submitBtnText = document.getElementById("submitBtnText");
    const skeleton = document.getElementById("skeleton");
    const statusBox = document.getElementById("status");
    const progressTrack = document.getElementById("progressTrack");
    const progressSteps = document.getElementById("progressSteps");
    const resultCard = document.getElementById("resultCard");
    const runIdMeta = document.getElementById("runIdMeta");
    const runIdValue = document.getElementById("runIdValue");
    const runIdCopy = document.getElementById("runIdCopy");
    const runIdCopyState = document.getElementById("runIdCopyState");
    const resultHtml = document.getElementById("resultHtml");
    const resultBox = document.getElementById("result");
    const resultTitle = document.getElementById("resultTitle");
    const resultOverview = document.getElementById("resultOverview");
    const layout = document.querySelector(".layout");
    const serviceStatusBox = document.getElementById("serviceStatus");
    const statusTooltip = document.getElementById("statusTooltip");
    const toastWrap = document.getElementById("toastWrap");
    const offlineBar = document.getElementById("offlineBar");

    let queryController = null;
    let resultContext = { option: "", semesters: "", weeks: "all" };
    const historyTrigger = document.getElementById("historyTrigger");
    const historyMenu = document.getElementById("historyMenu");
    const historyClear = document.getElementById("historyClear");
    const historySection = document.getElementById("historySection");

    // Cookie 工具：历史查询记录（不含密码）
    const HISTORY_KEY = "dify_query_results_v3";
    const MAX_HISTORY = 5;
    const MAX_CACHE_AGE_MS = 6 * 60 * 60 * 1000;

    function setCookie(name, value, days) {
      const date = new Date();
      date.setTime(date.getTime() + days * 24 * 60 * 60 * 1000);
      document.cookie = name + "=" + encodeURIComponent(value) +
        "; expires=" + date.toUTCString() + "; path=/; SameSite=Lax" +
        (window.location.protocol === "https:" ? "; Secure" : "");
    }

    function getCookie(name) {
      const m = document.cookie.match(new RegExp(
        "(?:^|; )" + name.replace(/([.$?*|{}()\[\]\\\/\+^])/g, "\\$1") + "=([^;]*)"
      ));
      return m ? decodeURIComponent(m[1]) : null;
    }

    function loadHistory() {
      try {
        const raw = getCookie(HISTORY_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr : [];
      } catch (e) {
        return [];
      }
    }

    function recordLabel(r) {
      let label = (r.username || "?") + " · " + (r.option || "");
      if (r.semesters) label += " · " + r.semesters;
      if (r.weeks && r.weeks !== "all") label += " · 周" + r.weeks;
      if (r.md2pdf) label += " · PDF";
      if (r.check) label += " · 分析";
      return label;
    }

    function storeResultText(id, text) {
      try { localStorage.setItem("dify_query_result_" + id, text); } catch (e) {}
    }

    function getResultText(id) {
      try { return localStorage.getItem("dify_query_result_" + id) || ""; } catch (e) { return ""; }
    }

    function removeResultText(id) {
      try { localStorage.removeItem("dify_query_result_" + id); } catch (e) {}
    }

    function stripLoginNote(text) {
      if (!text) return text;
      var lines = String(text).split("\n");
      var out = [];
      var skipping = false;
      lines.forEach(function (line) {
        if (line.indexOf("> 🔗 免密登录教务系统") === 0) {
          skipping = true;
          return;
        }
        if (skipping) {
          if (line.indexOf("> ") === 0) return;
          skipping = false;
        }
        // 回看历史时同样隐藏课表下载链接与其令牌提示（下载码短时有效）
        if (/^\[点击下载课表（(?:PDF|Word) 文件）\]\(/.test(line)) return;
        if (line.indexOf("> ⚠️ 此链接含个人令牌，请勿外传。") === 0) return;
        out.push(line);
      });
      return out.join("\n").replace(/^\s+/, "");
    }

    function saveResultRecord(record) {
      let arr = loadHistory();
      const dupIndex = arr.findIndex(function (r) {
        return r.username === record.username &&
          r.option === record.option &&
          r.semesters === record.semesters &&
          r.weeks === record.weeks;
      });
      if (dupIndex !== -1) {
        removeResultText(arr[dupIndex].id);
        arr.splice(dupIndex, 1);
      }
      const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      storeResultText(id, stripLoginNote(String(record.resultText || "")));
      arr.unshift({
        id: id,
        savedAt: Date.now(),
        username: record.username,
        option: record.option,
        semesters: record.semesters || "",
        weeks: record.weeks,
        md2pdf: !!record.md2pdf,
        check: !!record.check,
        isMarkdown: !!record.isMarkdown
      });
      if (arr.length > MAX_HISTORY) {
        arr.slice(MAX_HISTORY).forEach(function (r) { removeResultText(r.id); });
        arr = arr.slice(0, MAX_HISTORY);
      }
      setCookie(HISTORY_KEY, JSON.stringify(arr), 30);
    }

    function pruneExpiredHistory() {
      const now = Date.now();
      const original = loadHistory();
      const valid = original.filter(function (r) {
        return typeof r.savedAt === "number" && now - r.savedAt < MAX_CACHE_AGE_MS;
      });
      if (valid.length !== original.length) {
        original.filter(function (r) { return valid.indexOf(r) === -1; })
          .forEach(function (r) { removeResultText(r.id); });
        setCookie(HISTORY_KEY, JSON.stringify(valid), 30);
      }
      return valid;
    }

    function sanitizeStoredHistory() {
      pruneExpiredHistory().forEach(function (r) {
        const raw = getResultText(r.id);
        const clean = stripLoginNote(raw);
        if (clean !== raw) storeResultText(r.id, clean);
      });
    }

    function renderHistorySelect() {
      const arr = loadHistory();
      historySection.classList.toggle("hidden", arr.length === 0);
      historyClear.disabled = arr.length === 0;
      const current = historyTrigger.getAttribute("data-label") || "";
      historyMenu.innerHTML = arr.map(function (r, i) {
        const label = recordLabel(r);
        // 与 initDropdown 契约一致：选中项同步 aria-selected，屏幕阅读器可感知当前值
        const selected = current && label === current ? "true" : "false";
        return '<button type="button" class="dropdown-item" role="option" aria-selected="' + selected + '" data-index="' + i + '">' +
          label.replace(/"/g, "&quot;") + "</button>";
      }).join("");
      if (current && arr.some(function (r) { return recordLabel(r) === current; })) {
        historyTrigger.textContent = current;
      } else {
        historyTrigger.textContent = "— 选择历史查询结果 —";
        historyTrigger.removeAttribute("data-label");
      }
    }

    function displaySavedRecord(rec) {
      if (!rec) return;
      resultContext = {
        option: rec.option || "",
        semesters: rec.semesters || "",
        weeks: rec.weeks || "all"
      };
      resultTitle.textContent = rec.option ? rec.option + "查询结果" : "查询结果";
      clearLockTimer();
      clearTimeout(resultTimer);
      skeleton.hidden = true;
      skeleton.setAttribute("aria-hidden", "true");
      resultCard.classList.remove("hidden");
      resultCard.classList.add("is-growing");
      layout.classList.add("has-result");
      resultCard.classList.remove("fade-in");
      resultCard.classList.add("fade-out");
      resultTimer = setTimeout(function () {
        const text = getResultText(rec.id) || rec.resultText || "";
        let note = "";
        if (rec.savedAt) {
          const mins = Math.max(0, Math.round((Date.now() - rec.savedAt) / 60000));
          note = '<div class="badge badge-warning">已加载本地缓存 · 更新于 ' +
            (mins < 1 ? "刚刚" : mins + " 分钟前") + "</div>";
        }
        if (rec.isMarkdown) {
          setRunId(rec.runId);
          resultHtml.classList.remove("hidden");
          resultBox.classList.add("hidden");
          resultHtml.innerHTML = note + renderResultMarkdown(stripLoginNote(text), {});
          applyRenderedElementStyles(resultHtml);
        } else {
          resultHtml.classList.add("hidden");
          resultBox.classList.remove("hidden");
          resultBox.textContent = text || "（无返回内容）";
        }
        resultCard.classList.remove("fade-out");
        resultCard.classList.add("fade-in");
        resultTitle.focus({ preventScroll: true });
      }, 180);
    }

    historyTrigger.addEventListener("click", function (e) {
      e.stopPropagation();
      const willOpen = historyMenu.classList.contains("hidden");
      document.querySelectorAll(".dropdown-menu").forEach(function (m) {
        m.classList.add("hidden");
      });
      document.querySelectorAll(".dropdown-trigger").forEach(function (t) {
        t.setAttribute("aria-expanded", "false");
      });
      if (willOpen) {
        historyMenu.classList.remove("hidden");
        historyTrigger.setAttribute("aria-expanded", "true");
      }
    });

    function selectHistoryItem(item) {
      const arr = loadHistory();
      const rec = item ? arr[item.getAttribute("data-index")] : null;
      if (rec) {
        usernameInput.value = rec.username || "";
        displaySavedRecord(rec);
        const label = recordLabel(rec);
        historyTrigger.textContent = label;
        historyTrigger.setAttribute("data-label", label);
        renderHistorySelect();
      }
      historyMenu.classList.add("hidden");
      historyTrigger.setAttribute("aria-expanded", "false");
    }

    function openHistoryMenu() {
      document.querySelectorAll(".dropdown-menu").forEach(function (m) {
        m.classList.add("hidden");
      });
      document.querySelectorAll(".dropdown-trigger").forEach(function (t) {
        t.setAttribute("aria-expanded", "false");
      });
      historyMenu.classList.remove("hidden");
      historyTrigger.setAttribute("aria-expanded", "true");
      const first = historyMenu.querySelector(".dropdown-item");
      if (first) first.focus();
    }

    historyMenu.addEventListener("click", function (e) {
      const item = e.target.closest ? e.target.closest(".dropdown-item") : null;
      if (!item) return;
      selectHistoryItem(item);
    });

    historyTrigger.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (historyMenu.classList.contains("hidden")) openHistoryMenu();
      } else if (e.key === "Escape") {
        historyMenu.classList.add("hidden");
        historyTrigger.setAttribute("aria-expanded", "false");
        historyTrigger.focus();
      }
    });

    historyMenu.addEventListener("keydown", function (e) {
      const list = Array.prototype.slice.call(historyMenu.querySelectorAll(".dropdown-item"));
      if (!list.length) return;
      const idx = list.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        list[(idx + 1) % list.length].focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        list[(idx - 1 + list.length) % list.length].focus();
      } else if (e.key === "Home") {
        e.preventDefault();
        list[0].focus();
      } else if (e.key === "End") {
        e.preventDefault();
        list[list.length - 1].focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        const item = document.activeElement && document.activeElement.closest
          ? document.activeElement.closest(".dropdown-item") : null;
        if (item) selectHistoryItem(item);
      } else if (e.key === "Escape") {
        e.preventDefault();
        historyMenu.classList.add("hidden");
        historyTrigger.setAttribute("aria-expanded", "false");
        historyTrigger.focus();
      } else if (e.key === "Tab") {
        historyMenu.classList.add("hidden");
        historyTrigger.setAttribute("aria-expanded", "false");
      }
    });

    historyClear.addEventListener("click", function () {
      const keys = [];
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.indexOf("dify_query_result_") === 0) {
          keys.push(key);
        }
      }
      keys.forEach(function (key) { localStorage.removeItem(key); });
      setCookie(HISTORY_KEY, JSON.stringify([]), 30);
      renderHistorySelect();
      historyTrigger.textContent = "— 选择历史查询结果 —";
      historyTrigger.removeAttribute("data-label");
      historySection.classList.add("hidden");
      historyClear.disabled = true;
      showToast("success", "历史记录已清空");
    });

    function showStatus(message, ok) {
      statusBox.textContent = message;
      statusBox.classList.remove("hidden", "notice-success", "notice-error", "notice-warning", "notice-info");
      statusBox.classList.add(ok ? "notice-success" : "notice-error");
    }

    function setFieldError(input, errorNode, message) {
      input.setAttribute("aria-invalid", "true");
      input.classList.add("input-invalid");
      errorNode.textContent = message;
      errorNode.classList.remove("hidden");
    }

    function clearFieldError(input, errorNode) {
      input.removeAttribute("aria-invalid");
      input.classList.remove("input-invalid");
      errorNode.textContent = "";
      errorNode.classList.add("hidden");
    }

    function validateUsernameField(showRequired) {
      const value = usernameInput.value.trim();
      let message = "";
      if (!value && showRequired) message = "请输入 10 位数字学号";
      else if (value && !/^\d{10}$/.test(value)) message = "学号应为 10 位数字";
      if (message) {
        setFieldError(usernameInput, usernameError, message);
        return false;
      }
      clearFieldError(usernameInput, usernameError);
      return true;
    }

    function validatePasswordField(showRequired) {
      if (!passwordInput.value && showRequired) {
        setFieldError(passwordInput, passwordError, "请输入教务系统密码");
        return false;
      }
      clearFieldError(passwordInput, passwordError);
      return true;
    }

    function validateCredentials() {
      const usernameValid = validateUsernameField(true);
      const passwordValid = validatePasswordField(true);
      if (!usernameValid) {
        usernameInput.focus();
        return false;
      }
      if (!passwordValid) {
        passwordInput.focus();
        return false;
      }
      return true;
    }

    function queryButtonLabel() {
      return "查询" + optionInput.value;
    }

    function syncOptionSeg() {
      const isSchedule = optionInput.value === "课表";
      document.querySelectorAll(".query-choice").forEach(function (btn) {
        const active = btn.getAttribute("data-option") === optionInput.value;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
      });
      document.getElementById("weeksField").classList.toggle("hidden", !isSchedule);
      document.getElementById("gradeOptions").classList.toggle("hidden", isSchedule);
      document.getElementById("queryScopeHint").textContent = isSchedule
        ? "当前学期 · 全部周次"
        : "当前学期";
      // 「全部学期 / 最近 N 学期」仅成绩可用：切到课表时复位，避免把多学期参数透传给课表链路
      document.querySelectorAll("#semestersMenu .grades-only-item").forEach(function (item) {
        item.classList.toggle("hidden", isSchedule);
      });
      if (isSchedule && /^(all|recent:\d+)$/.test(semestersInput.value)) {
        semestersInput.value = "";
        semestersDropdown.sync();
      }
      if (!submitBtn.disabled) submitBtnText.textContent = queryButtonLabel();
    }

    function initDropdown(triggerId, menuId, inputId, defaultLabel) {
      const trigger = document.getElementById(triggerId);
      const menu = document.getElementById(menuId);
      const input = document.getElementById(inputId);

      function items() {
        return Array.prototype.slice.call(menu.querySelectorAll(".dropdown-item"));
      }

      function positionMenu() {
        // 触发器贴近视口底部时向上翻转，彻底避免菜单越界（含 fixed 页脚区域）；
        // 两侧都放不下时选空间更大的一侧并限高，交给菜单内部滚动
        menu.classList.remove("drop-up");
        menu.style.maxHeight = "";
        const rect = trigger.getBoundingClientRect();
        const gap = 8;          // 与 .dropdown-menu 的 top/bottom 偏移一致
        const margin = 12;      // 视口边缘安全余量
        const spaceBelow = window.innerHeight - rect.bottom;
        const spaceAbove = rect.top;
        const menuH = menu.offsetHeight;   // hidden 已移除，可测量
        if (menuH + gap + margin <= spaceBelow) return;   // 下方放得下，维持向下
        if (spaceAbove > spaceBelow) {
          menu.classList.add("drop-up");
          if (menuH + gap + margin > spaceAbove) {
            menu.style.maxHeight = Math.max(160, spaceAbove - gap - margin) + "px";
          }
        } else if (menuH + gap + margin > spaceBelow) {
          menu.style.maxHeight = Math.max(160, spaceBelow - gap - margin) + "px";
        }
      }

      function openMenu() {
        hideStatusTooltip();   // 悬浮详情(z-60)不得压住下拉选项(z-30@卡内)
        document.querySelectorAll(".dropdown-menu").forEach(function (m) {
          m.classList.add("hidden");
        });
        document.querySelectorAll(".dropdown-trigger").forEach(function (t) {
          t.setAttribute("aria-expanded", "false");
        });
        menu.classList.remove("hidden");
        positionMenu();
        trigger.setAttribute("aria-expanded", "true");
        const current = items().find(function (item) {
          return item.getAttribute("data-value") === input.value;
        }) || items()[0];
        if (current) current.focus();
      }

      function closeMenu(refocus) {
        menu.classList.add("hidden");
        menu.classList.remove("drop-up");
        menu.style.maxHeight = "";
        trigger.setAttribute("aria-expanded", "false");
        if (refocus) trigger.focus();
      }

      function selectItem(item) {
        input.value = item.getAttribute("data-value");
        syncLabel();
        closeMenu(true);
      }

      function syncLabel() {
        let found = false;
        items().forEach(function (item) {
          const active = item.getAttribute("data-value") === input.value;
          item.setAttribute("role", "option");
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
          if (active) {
            trigger.textContent = item.textContent.trim();
            found = true;
          }
        });
        if (!found) trigger.textContent = defaultLabel;
      }

      trigger.addEventListener("click", function (e) {
        e.stopPropagation();
        if (menu.classList.contains("hidden")) openMenu();
        else closeMenu(true);
      });

      trigger.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (menu.classList.contains("hidden")) openMenu();
        } else if (e.key === "Escape") {
          closeMenu(true);
        }
      });

      menu.addEventListener("keydown", function (e) {
        const list = items();
        if (!list.length) return;
        const idx = list.indexOf(document.activeElement);
        if (e.key === "ArrowDown") {
          e.preventDefault();
          list[(idx + 1) % list.length].focus();
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          list[(idx - 1 + list.length) % list.length].focus();
        } else if (e.key === "Home") {
          e.preventDefault();
          list[0].focus();
        } else if (e.key === "End") {
          e.preventDefault();
          list[list.length - 1].focus();
        } else if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          const item = document.activeElement && document.activeElement.closest
            ? document.activeElement.closest(".dropdown-item") : null;
          if (item) selectItem(item);
        } else if (e.key === "Escape") {
          e.preventDefault();
          closeMenu(true);
        } else if (e.key === "Tab") {
          closeMenu(false);
        }
      });

      menu.addEventListener("click", function (e) {
        const item = e.target.closest ? e.target.closest(".dropdown-item") : null;
        if (!item) return;
        selectItem(item);
      });

      syncLabel();
      return { sync: syncLabel };
    }

    const semestersDropdown = initDropdown("semestersTrigger", "semestersMenu", "semesters", "默认当前学期");
    const weeksDropdown = initDropdown("weeksTrigger", "weeksMenu", "weeks", "全部");

    document.addEventListener("click", function () {
      document.querySelectorAll(".dropdown-menu").forEach(function (m) {
        m.classList.add("hidden");
      });
      document.querySelectorAll(".dropdown-trigger").forEach(function (t) {
        t.setAttribute("aria-expanded", "false");
      });
    });

    let progressStepStates = [];
    let currentJobPhase = null;

    function progressStepsFor(payload) {
      const steps = [
        { phase: "login", label: "登录验证" },
        { phase: "queued", label: "排队等待" },
        { phase: "dispatching", label: "等待槽位" },
        { phase: "querying", label: payload.option === "课表" ? "查询课表" : "查询成绩" }
      ];
      if (payload.option === "成绩" && payload.check) {
        steps.push({ phase: "analyzing", label: "成绩分析" });
      }
      if (payload.md2pdf) {
        steps.push({ phase: "generating_pdf", label: "生成 PDF" });
      }
      steps.push({ phase: "done", label: "完成" });
      return steps;
    }

    function renderJobProgress(phase, payload, jobState, position) {
      const normalizedPhase = phase || "queued";
      const steps = progressStepsFor(payload);
      if (normalizedPhase === "queued" && position) {
        steps[1].label = "第" + position + "位";
      }
      const currentIndex = Math.max(0, steps.findIndex(function (step) {
        return step.phase === normalizedPhase;
      }));
      const failed = jobState === "failed";
      const done = jobState === "success" || normalizedPhase === "done";
      const wasHidden = progressTrack.classList.contains("hidden");
      const states = steps.map(function (step, index) {
        if (index < currentIndex || done) return "completed";
        return index === currentIndex ? (failed ? "failed" : "active") : "pending";
      });
      const entering = states.map(function (state, index) {
        return wasHidden || !progressStepStates[index] || progressStepStates[index] !== state;
      });
      const labelsChanged = steps.some(function (step, index) {
        const node = progressSteps.children[index];
        const label = node && node.querySelector(".query-step-label");
        return !label || label.textContent !== step.label;
      });
      const needRebuild = wasHidden || states.length !== progressStepStates.length ||
        entering.some(Boolean) || labelsChanged;
      if (needRebuild) {
        progressSteps.innerHTML = steps.map(function (step, index) {
          const state = states[index];
          const enterClass = entering[index] ? " query-step-enter" : "";
          const ariaCurrent = state === "active" ? "step" : "false";
          const dot = state === "active"
            ? '<span class="query-step-dot"><span class="spinner"></span></span>'
            : '<span class="query-step-dot"></span>';
          return [
            '<div class="query-step ', state, enterClass, '" aria-current="', ariaCurrent, '"',
            ' data-step-index="', wasHidden ? index : 0, '">',
            dot,
            '<span class="query-step-label"></span>',
            '</div>'
          ].join("");
        }).join("");
        progressSteps.querySelectorAll(".query-step-label").forEach(function (node, index) {
          node.textContent = steps[index].label;
        });
        progressSteps.querySelectorAll(".query-step").forEach(function (node, index) {
          node.style.setProperty("--step-index", String(wasHidden ? index : 0));
        });
      }
      progressTrack.classList.remove("hidden");
      progressTrack.classList.toggle("query-progress-failed", failed);
      progressTrack.classList.toggle("query-progress-done", done && !failed);
      currentJobPhase = normalizedPhase;
      progressStepStates = states;
      progressSteps.style.setProperty("--step-font-size", "12px");
    }

    function hideJobProgress() {
      progressTrack.classList.add("hidden");
      progressTrack.classList.remove("query-progress-failed", "query-progress-done");
      progressSteps.innerHTML = "";
      progressStepStates = [];
      currentJobPhase = null;
    }

    function markJobProgressFailed(payload) {
      if (progressTrack.classList.contains("hidden")) return;
      renderJobProgress(currentJobPhase || "querying", payload, "failed", 0);
    }

    function setRunId(value) {
      const runId = String(value || "");
      clearTimeout(runIdCopyTimer);
      runIdCopyState.textContent = "复制";
      runIdCopy.classList.remove("copied");
      if (runId) {
        runIdMeta.classList.remove("hidden");
        runIdValue.textContent = runId;
        runIdValue.title = runId;
        runIdCopy.title = "点击复制运行ID";
      } else {
        runIdMeta.classList.add("hidden");
        runIdValue.textContent = "—";
        runIdValue.removeAttribute("title");
        runIdCopy.removeAttribute("title");
      }
    }

    async function copyRunId() {
      const runId = runIdValue.textContent.trim();
      if (!runId || runId === "—") return;
      let copied = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(runId);
          copied = true;
        } else {
          const helper = document.createElement("textarea");
          helper.value = runId;
          helper.setAttribute("readonly", "readonly");
          helper.className = "sr-only";
          document.body.appendChild(helper);
          helper.select();
          copied = document.execCommand("copy");
          helper.remove();
        }
      } catch (error) {
        copied = false;
      }
      runIdCopyState.textContent = copied ? "已复制" : "复制失败";
      runIdCopy.classList.toggle("copied", copied);
      clearTimeout(runIdCopyTimer);
      runIdCopyTimer = window.setTimeout(function () {
        runIdCopyState.textContent = "复制";
        runIdCopy.classList.remove("copied");
      }, 1600);
    }

    const TOAST_ICONS = {
      success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="m9 12 2 2 4-4"></path></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg>',
      info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4"></path><path d="M12 8h.01"></path></svg>'
    };
    function showToast(type, message) {
      const icon = TOAST_ICONS[type] || TOAST_ICONS.info;
      const el = document.createElement("div");
      el.className = "toast toast-" + type + " toast-enter";
      el.setAttribute("role", type === "error" ? "alert" : "status");
      el.innerHTML =
        '<span class="toast-icon toast-icon-pop">' + icon + "</span>" +
        '<span class="toast-message">' + escapeHtml(message) + "</span>" +
        '<span class="toast-progress"></span>';
      toastWrap.appendChild(el);
      setTimeout(function () {
        el.classList.remove("toast-enter");
        el.classList.add("toast-leave");
        setTimeout(function () { el.remove(); }, 220);
      }, 3000);
    }

    function showOfflineBar(savedAt) {
      let time = "";
      if (savedAt) {
        const mins = Math.max(0, Math.round((Date.now() - savedAt) / 60000));
        time = " (" + (mins < 1 ? "刚刚" : mins + " 分钟前") + ")";
      }
      offlineBar.innerHTML = '<span class="status-dot status-invalid" aria-hidden="true"></span>当前离线，显示的是上次同步的数据' + time +
        ' <button type="button" class="btn btn-ghost js-retry-query">手动刷新</button>';
      offlineBar.classList.remove("hidden");
    }

    function hideOfflineBar() {
      offlineBar.classList.add("hidden");
    }

    function setButtonLoading(loading) {
      submitBtn.disabled = loading;
      submitSpinner.classList.toggle("hidden", !loading);
      submitBtnText.textContent = loading ? "正在查询..." : queryButtonLabel();
    }

    const ALERT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg>';
    const INBOX_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path></svg>';
    const CLOUD_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"></path></svg>';


    let lockTimer = 0;
    function clearLockTimer() {
      clearInterval(lockTimer);
      lockTimer = 0;
    }

    function showStateCard(icon, title, desc, withRetry, kind, extraHtml) {
      clearLockTimer();
      clearTimeout(resultTimer);
      setRunId("");
      skeleton.hidden = true;
      skeleton.setAttribute("aria-hidden", "true");
      resultBox.classList.add("hidden");
      resultHtml.classList.remove("hidden");
      resultCard.classList.add("is-growing");
      resultCard.classList.remove("hidden");
      layout.classList.add("has-result");
      const variantClass = kind === "error" ? " empty-state--error" : "";
      const description = desc ? renderStateDescription(desc) : "";
      resultHtml.innerHTML =
        '<div class="empty-state' + variantClass + '">' +
        '<div class="empty-state-art" aria-hidden="true">' + icon + "</div>" +
        '<p class="empty-state-text">' + escapeHtml(title) + "</p>" +
        description +
        (extraHtml || "") +
        (withRetry ? '<button type="button" class="empty-state-action btn btn-primary js-retry-query">重试</button>' : "") +
        "</div>";
      applyRenderedElementStyles(resultHtml);
      renderStateOverview(kind, title, desc || "");
      resultCard.classList.remove("fade-in");
      resultCard.classList.add("fade-in");
    }

    function containsMarkdownSyntax(text) {
      return /(^|\n)[ \t]*(?:[-*][ \t]|\d+\.[ \t]|#{1,6}[ \t]|>[ \t])/.test(text) ||
        /\*\*[^*\n]+\*\*/.test(text) ||
        /\[[^\]\n]+\]\([^)\n]+\)/.test(text) ||
        /`[^`\n]+`/.test(text);
    }

    function renderStateDescription(text) {
      if (!containsMarkdownSyntax(text)) {
        return '<p class="empty-state-text">' + escapeHtml(text) + "</p>";
      }
      return '<div class="empty-state-detail">' + renderMarkdown(text) + "</div>";
    }

    function showErrorCard(message) {
      showStateCard(ALERT_SVG, "数据加载失败", message, true, "error");
      showStatus("查询失败", false);
    }

    function showEmptyState() {
      showStateCard(INBOX_SVG, "没有找到相关记录", "请尝试更换学期或查询项目", false);
    }

    function formatLockCountdown(remainingMs) {
      const totalSeconds = Math.max(1, Math.ceil(remainingMs / 1000));
      const days = Math.floor(totalSeconds / 86400);
      const hours = Math.floor((totalSeconds % 86400) / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      const two = function (value) { return value < 10 ? "0" + value : String(value); };
      if (days > 0) return days + " 天 " + two(hours) + ":" + two(minutes) + ":" + two(seconds);
      return two(hours) + ":" + two(minutes) + ":" + two(seconds);
    }

    function showLockCard(lockUntil) {
      const displayTime = escapeHtml(String(lockUntil || ""));
      const desc = "学校端限制解除前不要重试，避免继续延长锁定。";
      showStateCard(
        ALERT_SVG,
        "学校账号临时锁定",
        desc,
        true,
        "error",
        '<p class="empty-state-meta" aria-live="polite"><span data-lock-countdown>预计解除时间：' + displayTime + "</span></p>"
      );
      const countdown = resultHtml.querySelector("[data-lock-countdown]");
      const retry = resultHtml.querySelector(".js-retry-query");
      if (!countdown || !retry) return;
      const target = new Date(String(lockUntil || "").replace(" ", "T"));
      if (Number.isNaN(target.getTime())) {
        countdown.textContent = "预计解除时间：" + displayTime;
        retry.textContent = "重试查询";
        retry.disabled = false;
        return;
      }
      const renderCountdown = function () {
        const remaining = target.getTime() - Date.now();
        if (remaining > 0) {
          countdown.textContent = "预计解除时间：" + displayTime +
            " · 剩余 " + formatLockCountdown(remaining);
          retry.textContent = "解除后可重试";
          retry.disabled = true;
          return;
        }
        countdown.textContent = "预计解除时间：" + displayTime + " · 已到，可重试";
        retry.textContent = "重试查询";
        retry.disabled = false;
        clearLockTimer();
      };
      renderCountdown();
      if (target.getTime() > Date.now()) {
        lockTimer = window.setInterval(renderCountdown, 1000);
      }
    }

    function looksEmptyMarkdown(text) {
      if (!text || !text.trim()) return true;
      const pipeLines = text.split("\n").filter(function (line) {
        return line.trim().indexOf("|") === 0;
      });
      if (pipeLines.length >= 1 && pipeLines.length <= 2) return true;
      if (tableRowsAllEmpty(text)) return true;
      const plain = text.replace(/\|/g, " ").replace(/-{3,}/g, " ").trim();
      if (!plain) return true;
      const tokens = plain.split(/\s+/).filter(Boolean);
      if (tokens.length) {
        const restOnly = tokens.every(function (token) {
          return /^\d+$/.test(token) || /^休息[～~]?$/.test(token);
        });
        if (restOnly) return true;
      }
      return false;
    }

    function countMarkdownTableRows(text) {
      if (!text) return 0;
      const lines = String(text).split("\n").filter(function (line) {
        return line.trim().indexOf("|") === 0;
      });
      return Math.max(0, lines.length - 2);
    }

    function tableRowsAllEmpty(text) {
      const lines = text.split("\n").filter(function (line) {
        return line.trim().indexOf("|") === 0;
      });
      if (lines.length < 3) return false;
      let meaningful = 0;
      for (let i = 2; i < lines.length; i++) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map(function (c) {
          return c.trim();
        });
        const rest = cells.length > 1 ? cells.slice(1) : cells;
        if (rest.some(function (c) {
          return c && c !== "休息" && c !== "休息～";
        })) {
          meaningful++;
        }
      }
      return meaningful === 0;
    }

    function updateUrlParams(payload) {
      const params = new URLSearchParams();
      if (payload.option) params.set("option", payload.option);
      if (payload.semesters) params.set("semesters", payload.semesters);
      if (payload.weeks && payload.weeks !== "all") params.set("weeks", payload.weeks);
      if (payload.md2pdf) params.set("md2pdf", "1");
      if (payload.check) params.set("check", "1");
      if (payload.include_all) params.set("include_all", "1");
      const qs = params.toString();
      history.replaceState(null, "", qs ? "?" + qs : location.pathname);
    }

    function restoreUrlParams() {
      const params = new URLSearchParams(location.search);
      if (params.has("option")) optionInput.value = params.get("option");
      if (params.has("semesters")) semestersInput.value = params.get("semesters");
      if (params.has("weeks")) weeksInput.value = params.get("weeks");
      md2pdfInput.checked = params.get("md2pdf") === "1";
      checkInput.checked = params.get("check") === "1";
      if (includeAllInput) includeAllInput.checked = params.get("include_all") === "1";
      syncOptionSeg();
      semestersDropdown.sync();
      weeksDropdown.sync();
    }

    document.querySelectorAll(".query-choice").forEach(function (btn) {
      btn.addEventListener("click", function () {
        optionInput.value = this.getAttribute("data-option");
        syncOptionSeg();
      });
    });

    let serviceStatusSeq = 0;
    function renderServiceStatus() {
      const requestSeq = ++serviceStatusSeq;
      fetch("/api/v1/service-status", { headers: authHeaders() })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (requestSeq !== serviceStatusSeq) return;
          const body = data && data.data ? data.data : {};
          const stats = body.stats || {};
          const results = body.results || [];
          const acceptedTotal = Number(stats.accepted_total || 0);
          if (!results.length) {
            serviceStatusBox.classList.add("hidden");
            return;
          }
          // API 返回最新在前；状态条按时间从右到左展示。
          const segments = results.slice().reverse().map(function (item) {
            const cls = item ? (item.success ? "success" : "fail") : "";
            const attrs = item
              ? ' data-success="' + (item.success ? "1" : "0") +
                '" data-kind="' + escapeHtml(item.kind || (item.success ? "success" : "business_error")) +
                '" data-time="' + escapeHtml(item.time || "") + '"'
              : "";
            return '<span class="status-seg ' + cls + '"' + attrs + "></span>";
          });
          const total = results.length;
          const okCount = stats.success_count != null
            ? Number(stats.success_count)
            : results.filter(function (item) { return item.success; }).length;
          const failCount = total - okCount;
          const availability = stats.availability != null
            ? Number(stats.availability)
            : (total ? Math.round(okCount / total * 1000) / 10 : 0);
          const isMobile = window.matchMedia && window.matchMedia("(max-width: 600px)").matches;
          let barHtml;
          if (isMobile) {
            const okPct = total ? Math.round(okCount / total * 100) : 0;
            const failPct = total ? Math.round(failCount / total * 100) : 0;
            barHtml = '<div class="status-bar-mobile">' +
              '<span class="seg-success" data-width="' + okPct + '"></span>' +
              '<span class="seg-fail" data-width="' + failPct + '"></span></div>';
          } else {
            barHtml = '<div class="status-bar">' + segments.join("") + "</div>";
          }
          let availClass = "avail-good";
          if (availability < 70) availClass = "avail-bad";
          else if (availability < 90) availClass = "avail-mid";
          serviceStatusBox.classList.remove("hidden");
          serviceStatusBox.innerHTML =
            '<div class="status-title">服务状态 · 已受理 ' + acceptedTotal + ' 次</div>' +
            barHtml +
            '<div class="status-summary">' +
            '<span class="' + availClass + '">最近 ' + total + ' 次可用性 ' +
            availability.toFixed(1) + "%</span> · " + okCount + " 成功 · " + failCount + " 失败</div>";
          serviceStatusBox.querySelectorAll("[data-width]").forEach(function (node) {
            node.style.width = node.dataset.width + "%";
          });
        })
        .catch(function () {
          serviceStatusBox.classList.add("hidden");
        });
    }

    function hideStatusTooltip() {
      statusTooltip.classList.add("hidden");
    }

    // 返回顶部浮钮：页面没有可回滚距离时隐藏（短页会悬浮在表单字段上，
    // 见评审 focus-not-obscured 项）；visibility:hidden 同步移出焦点与无障碍树
    function syncBackToTopVisibility() {
      var btn = document.querySelector(".back-to-top");
      if (!btn) return;
      var scrollable = document.documentElement.scrollHeight >
        window.innerHeight + 8;
      btn.classList.toggle("back-to-top-hidden", !scrollable);
    }

    function positionStatusTooltip(event) {
      statusTooltip.classList.remove("hidden");
      const pad = 12;
      let x = event.clientX + pad;
      let y = event.clientY + pad;
      if (x + statusTooltip.offsetWidth > window.innerWidth - 4) {
        x = event.clientX - statusTooltip.offsetWidth - pad;
      }
      if (y + statusTooltip.offsetHeight > window.innerHeight - 4) {
        y = event.clientY - statusTooltip.offsetHeight - pad;
      }
      statusTooltip.style.left = Math.max(4, x) + "px";
      statusTooltip.style.top = Math.max(4, y) + "px";
    }

    function showStatusTooltip(event) {
      const seg = event.target.closest ? event.target.closest(".status-seg") : null;
      if (!seg) return;
      const time = seg.getAttribute("data-time");
      if (!time) return;
      const ok = seg.getAttribute("data-success") === "1";
      const kind = seg.getAttribute("data-kind") || "";
      let text = ok ? "成功" : "失败";
      if (kind === "no_data") text = "成功 · 无数据";
      else if (kind === "business_error") text = "失败 · 业务错误";
      else if (kind === "upstream_error") text = "失败 · 上游错误";
      statusTooltip.textContent = text + " · " + time;
      positionStatusTooltip(event);
    }

    serviceStatusBox.addEventListener("mouseover", function (event) {
      showStatusTooltip(event);
    });
    // mousemove 高频触发且 positionStatusTooltip 读取 offsetWidth/Height 会强制
    // 回流：rAF 合帧节流，每帧最多定位一次
    let tooltipPositioningFrame = 0;
    serviceStatusBox.addEventListener("mousemove", function (event) {
      if (statusTooltip.classList.contains("hidden")) return;
      const clientX = event.clientX;
      const clientY = event.clientY;
      if (tooltipPositioningFrame) return;
      tooltipPositioningFrame = window.requestAnimationFrame(function () {
        tooltipPositioningFrame = 0;
        positionStatusTooltip({ clientX: clientX, clientY: clientY });
      });
    });
    serviceStatusBox.addEventListener("mouseleave", hideStatusTooltip);
    serviceStatusBox.addEventListener("touchstart", function (event) {
      if (!event.touches.length) return;
      const touch = event.touches[0];
      const fakeEvent = { clientX: touch.clientX, clientY: touch.clientY, target: event.target };
      if (!statusTooltip.classList.contains("hidden")) {
        hideStatusTooltip();
      } else {
        showStatusTooltip(fakeEvent);
      }
    }, { passive: true });

    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function inlineFormat(text) {
      let html = escapeHtml(text);
      html = html.replace(
        /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );
      html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/(^|[^"'=])(https?:\/\/[^\s<>"']+)/g, function (m, pre, url) {
        url = url.replace(/[.,;:!?]+$/, "");
        return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
      });
      return html;
    }

    const SAFE_TAGS = new Set([
      "a", "abbr", "b", "blockquote", "br", "code", "del", "div", "em",
      "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
      "li", "ol", "p", "pre", "s", "span", "strong", "table", "tbody",
      "td", "th", "thead", "tr", "ul"
    ]);
    const SAFE_ATTRS = new Set([
      "href", "src", "alt", "title", "color", "face", "size", "align",
      "colspan", "rowspan", "width", "height", "target", "rel"
    ]);
    const URL_ATTRS = new Set(["href", "src"]);

    function sanitizeTag(rawTag) {
      const match = rawTag.match(
        /^<\/?([a-zA-Z][a-zA-Z0-9]*)((?:\s+[a-zA-Z-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>$/
      );
      if (!match) return escapeHtml(rawTag);
      const tag = match[1].toLowerCase();
      if (!SAFE_TAGS.has(tag)) return escapeHtml(rawTag);
      if (rawTag.startsWith("</")) return "</" + tag + ">";

      let attrs = "";
      const attrRe = /([a-zA-Z-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let m;
      while ((m = attrRe.exec(match[2]))) {
        const name = m[1].toLowerCase();
        const value = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : "");
        if (!SAFE_ATTRS.has(name)) continue;
        if (URL_ATTRS.has(name) && !/^(https?:|mailto:|#|\/)/i.test(value)) continue;
        attrs += " " + name + '="' + escapeHtml(value) + '"';
      }
      return "<" + tag + attrs + ">";
    }

    function renderInline(text) {
      const parts = String(text).split(/(<[^>]+>)/g);
      let html = "";
      for (const part of parts) {
        if (/^<\/?[a-zA-Z][^>]*>$/.test(part)) {
          html += sanitizeTag(part);
        } else {
          html += inlineFormat(part);
        }
      }
      return html;
    }

    function isTableSeparator(line) {
      return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes("-");
    }

    // ---- 课表移动端按天视图：把「节次 × 星期」大表转置为按天卡片 ----
    // 桌面端（≥760px）由 CSS 显示原表、隐藏 dayview；小屏反之。
    const SCHEDULE_DAY_CHARS = ["一", "二", "三", "四", "五", "六", "日"];

    function scheduleDayOfHeader(text) {
      const compact = String(text).replace(/\s/g, "");
      for (let i = 0; i < SCHEDULE_DAY_CHARS.length; i++) {
        if (compact.indexOf("星期" + SCHEDULE_DAY_CHARS[i]) !== -1 ||
            compact.indexOf("周" + SCHEDULE_DAY_CHARS[i]) !== -1) return i;
      }
      return -1;
    }

    // ---- 教学周上下文（同类课表产品标配：本周定位/今天列/进行中节次）----
    // 数据来自公开校历基线 /api/v1/calendars/config（periods + terms，非机密）。
    // 任何一步不可解析都返回 null：下游高亮/快捷项/徽章全部静默降级。
    function parseDateOnly(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || "").trim());
      if (!match) return null;
      const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
      return isNaN(date.getTime()) ? null : date;
    }

    function parseClockMinutes(value) {
      const match = /^(\d{1,2}):(\d{2})$/.exec(String(value || "").trim());
      if (!match) return null;
      return Number(match[1]) * 60 + Number(match[2]);
    }

    function resolveTeachingContext(now, config) {
      if (!config || !config.terms || !config.periods) return null;
      const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      let hit = null;
      const semesters = Object.keys(config.terms);
      for (let i = 0; i < semesters.length; i++) {
        const term = config.terms[semesters[i]] || {};
        const monday = parseDateOnly(term.monday);
        const weeks = Number(term.weeks);
        if (!monday || !Number.isFinite(weeks) || weeks < 1) continue;
        const week = Math.floor((dayStart.getTime() - monday.getTime()) / 604800000) + 1;
        if (week < 1 || week > weeks) continue;
        hit = { semester: semesters[i], week: week };
        break;
      }
      if (!hit) return null;
      const dayIdx = (dayStart.getDay() + 6) % 7;   // 0=周一
      const minutes = now.getHours() * 60 + now.getMinutes();
      const periodKeys = Object.keys(config.periods).sort(function (a, b) {
        return Number(a) - Number(b);
      });
      let periodNumber = 0;
      for (let i = 0; i < periodKeys.length; i++) {
        const window = config.periods[periodKeys[i]];
        const start = parseClockMinutes(window && window[0]);
        const end = parseClockMinutes(window && window[1]);
        if (start == null || end == null) continue;
        periodNumber += 1;
        if (minutes >= start && minutes < end) {
          return {
            semester: hit.semester,
            week: hit.week,
            dayIdx: dayIdx,
            periodNumber: periodNumber,
            periodText: "第" + periodKeys[i] + "节"
          };
        }
      }
      // 课间/课前/课后：学期与周仍可判定，节次为空
      return {
        semester: hit.semester,
        week: hit.week,
        dayIdx: dayIdx,
        periodNumber: 0,
        periodText: ""
      };
    }

    // 查询周参数是否覆盖某个周次：all / 单值 / a-b 区间 / 逗号混合
    function queryCoversWeek(weeksValue, week) {
      const raw = String(weeksValue == null ? "all" : weeksValue).trim();
      if (!raw || raw === "all") return true;
      const parts = raw.split(/[,，]/);
      for (let i = 0; i < parts.length; i++) {
        const seg = parts[i].trim();
        if (!seg) continue;
        const range = seg.match(/^(\d+)\s*[-~至到]\s*(\d+)$/);
        if (range) {
          if (week >= Number(range[1]) && week <= Number(range[2])) return true;
        } else if (Number(seg) === week) {
          return true;
        }
      }
      return false;
    }

    // 节次标签 → 节次序号（"第3节" → 3；无法解析返回 0）
    function periodNumberOf(text) {
      const match = /第(\d+)节/.exec(String(text || "").replace(/\s/g, ""));
      return match ? Number(match[1]) : 0;
    }

    function buildScheduleDayView(shell) {
      const table = shell.querySelector("table");
      if (!table || shell.dataset.dayviewReady === "1") return;
      const headCells = Array.from(table.querySelectorAll("thead th"));
      if (headCells.length < 3) return;
      const colDays = headCells.map(function (th) {
        return scheduleDayOfHeader(th.textContent);
      });
      if (!colDays.some(function (day) { return day >= 0; })) return;

      const byDay = [[], [], [], [], [], [], []];
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        const cells = Array.from(tr.querySelectorAll("td"));
        const period = cells.length ? cells[0].textContent.trim() : "";
        cells.forEach(function (td, idx) {
          const day = colDays[idx];
          if (day < 0 || !period) return;
          const html = td.innerHTML.trim();
          if (!html || html === "&nbsp;") return;
          byDay[day].push({ period: period, html: html });
        });
      });

      const view = document.createElement("div");
      view.className = "schedule-dayview";
      // 教学周上下文：渲染时解析一次（切换星期 tab 复用同一结论）
      const ctx = calendarConfig ? resolveTeachingContext(new Date(), calendarConfig) : null;
      const tabs = document.createElement("div");
      tabs.className = "schedule-dayview-tabs";
      tabs.setAttribute("role", "tablist");
      tabs.setAttribute("aria-label", "按星期查看课表");
      const panel = document.createElement("div");
      panel.className = "schedule-dayview-panel";
      const tabButtons = [];

      function select(day) {
        tabButtons.forEach(function (btn, idx) {
          btn.classList.toggle("active", idx === day);
          btn.setAttribute("aria-selected", idx === day ? "true" : "false");
        });
        panel.innerHTML = "";
        const items = byDay[day] || [];
        if (!items.length) {
          const empty = document.createElement("p");
          empty.className = "schedule-dayview-empty";
          empty.textContent = "该日没有排课";
          panel.appendChild(empty);
          return;
        }
        items.forEach(function (item) {
          // 单元格行内结构（已过 sanitize）：课程名 <br> 教师 <br> 教室 <br> 周次
          const lines = item.html.split(/<br\s*\/?>/i)
            .map(function (line) { return line.trim(); }).filter(Boolean);
          const card = document.createElement("div");
          card.className = "schedule-day-card";
          // 教学周上下文：当天视图里正在上的节次卡高亮（公开校历可解析时）
          if (calendarConfig && ctx && ctx.periodNumber &&
              day === ctx.dayIdx && periodNumberOf(item.period) === ctx.periodNumber) {
            card.classList.add("is-now");
          }
          const head = document.createElement("div");
          head.className = "schedule-day-head";
          const period = document.createElement("span");
          period.className = "schedule-day-period";
          period.textContent = item.period;
          const name = document.createElement("span");
          name.className = "schedule-day-name";
          name.textContent = lines.shift() || "（未命名课程）";
          head.appendChild(period);
          head.appendChild(name);
          card.appendChild(head);
          if (lines.length) {
            const meta = document.createElement("div");
            meta.className = "schedule-day-meta";
            lines.forEach(function (line) {
              const span = document.createElement("span");
              span.textContent = line;
              meta.appendChild(span);
            });
            card.appendChild(meta);
          }
          panel.appendChild(card);
        });
      }

      for (let day = 0; day < SCHEDULE_DAY_CHARS.length; day++) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "schedule-dayview-tab";
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", "false");
        btn.textContent = "周" + SCHEDULE_DAY_CHARS[day];
        btn.addEventListener("click", function () { select(day); });
        tabs.appendChild(btn);
        tabButtons.push(btn);
      }
      tabs.addEventListener("keydown", function (event) {
        const current = tabButtons.findIndex(function (btn) {
          return btn.classList.contains("active");
        });
        if (event.key === "ArrowRight") {
          event.preventDefault();
          select((current + 1) % SCHEDULE_DAY_CHARS.length);
          tabButtons[(current + 1) % SCHEDULE_DAY_CHARS.length].focus();
        } else if (event.key === "ArrowLeft") {
          event.preventDefault();
          select((current + SCHEDULE_DAY_CHARS.length - 1) % SCHEDULE_DAY_CHARS.length);
          tabButtons[(current + SCHEDULE_DAY_CHARS.length - 1) % SCHEDULE_DAY_CHARS.length].focus();
        }
      });
      view.appendChild(tabs);
      view.appendChild(panel);
      shell.parentNode.insertBefore(view, shell);
      shell.dataset.dayviewReady = "1";
      // 默认选中今天；今天没课时选中第一个有课的日子
      const today = (new Date().getDay() + 6) % 7;
      const firstBusy = byDay.findIndex(function (items) { return items.length > 0; });
      select(byDay[today] && byDay[today].length ? today
        : (firstBusy >= 0 ? firstBusy : today));
    }

    // ---- 免密登录提示：渐进披露的紧凑条 ----
    // 三行引用块（链接行 + 超长兼容性说明行 + 令牌警示行）在结果区占 ~140px，
    // 把查询内容挤出首屏；这里保留链接 CTA 与「短时有效·勿外传」提示，
    // 长说明收进「使用须知」折叠，并移除课表尾部与之重复的单行令牌警示。
    function enhanceLoginNote(root) {
      root.querySelectorAll("blockquote").forEach(function (bq) {
        if (bq.dataset.loginNote === "1") return;
        const raw = bq.innerHTML || "";
        if (raw.indexOf("免密登录教务系统") === -1) return;
        const lines = raw.split(/<br\s*\/?>/i)
          .map(function (line) { return line.trim(); }).filter(Boolean);
        const probe = document.createElement("template");
        probe.innerHTML = lines.shift() || "";
        const source = probe.content || probe;
        const link = source.querySelector ? source.querySelector("a") : null;
        if (!link || !link.getAttribute("href")) return;
        // 跳转码 TTL：从服务端渲染文案提取「N 秒内有效」（单一来源），
        // 缺省 60s；起点为结果渲染时刻（与服务端 mint 时刻误差 <1s）
        const ttlMatch = (bq.textContent || "").match(/(\d+)\s*秒内有效/);
        const ttlSeconds = ttlMatch ? Math.max(1, parseInt(ttlMatch[1], 10)) : 60;
        bq.dataset.loginNote = "1";
        bq.className = "login-note";
        bq.innerHTML = "";
        const main = document.createElement("div");
        main.className = "login-note-main";
        const title = document.createElement("span");
        title.className = "login-note-title";
        title.textContent = "免密登录教务系统";
        const anchor = document.createElement("a");
        anchor.className = "login-note-link";
        anchor.href = link.getAttribute("href");
        anchor.rel = "noopener";
        anchor.textContent = link.textContent || "点击进入";
        const hint = document.createElement("span");
        hint.className = "login-note-hint";
        main.appendChild(title);
        main.appendChild(anchor);
        main.appendChild(hint);
        bq.appendChild(main);
        // 倒计时：剩余秒数实时更新，过期后置灰链接并出现「重新获取链接」入口
        // （过期码无法续期是安全设计；重新获取需重新认证——POST /api/v1/jump/renew）
        let remain = ttlSeconds;
        let timer = 0;
        const renderCountdown = function () {
          if (!bq.isConnected) {
            clearInterval(timer);
            return;
          }
          if (remain > 0) {
            hint.textContent = "剩余 " + remain + " 秒有效 · 请尽快使用 · 请勿外传";
          } else {
            hint.textContent = "链接已过期 · 可重新获取（需重新输入密码）";
            bq.classList.add("expired");
            renewBtn.hidden = false;
            clearInterval(timer);
          }
          remain -= 1;
        };
        const startCountdown = function (ttl) {
          remain = Math.max(1, ttl);
          bq.classList.remove("expired");
          renewBtn.hidden = true;
          clearInterval(timer);
          renderCountdown();
          timer = window.setInterval(renderCountdown, 1000);
        };
        // ---- 重新获取链接（重新认证签发）----
        // 过期码无法证明持有者身份，不提供无认证续期；重新获取走
        // POST /api/v1/jump/renew（学号 + 密码重新登录，LOGIN_REUSE 命中时不打学校）
        const actions = document.createElement("div");
        actions.className = "login-note-actions";
        const renewBtn = document.createElement("button");
        renewBtn.type = "button";
        renewBtn.className = "btn btn-ghost btn-sm";
        renewBtn.hidden = true;
        renewBtn.textContent = "重新获取链接";
        actions.appendChild(renewBtn);
        const renewRow = document.createElement("div");
        renewRow.className = "login-note-renew hidden";
        const renewLabel = document.createElement("label");
        renewLabel.className = "login-note-renew-label";
        renewLabel.textContent = "教务系统密码";
        const renewInput = document.createElement("input");
        renewInput.className = "input";
        renewInput.type = "password";
        renewInput.autocomplete = "off";
        renewInput.placeholder = "重新输入教务系统密码";
        renewInput.setAttribute("aria-label", "教务系统密码");
        renewLabel.appendChild(renewInput);
        const renewSubmit = document.createElement("button");
        renewSubmit.type = "button";
        renewSubmit.className = "btn btn-primary btn-sm";
        renewSubmit.textContent = "签发新链接";
        const renewCancel = document.createElement("button");
        renewCancel.type = "button";
        renewCancel.className = "btn btn-ghost btn-sm";
        renewCancel.textContent = "取消";
        const renewErr = document.createElement("span");
        renewErr.className = "login-note-renew-error hidden";
        renewErr.setAttribute("role", "alert");
        renewRow.appendChild(renewLabel);
        renewRow.appendChild(renewInput);
        renewRow.appendChild(renewSubmit);
        renewRow.appendChild(renewCancel);
        renewRow.appendChild(renewErr);
        bq.appendChild(actions);
        bq.appendChild(renewRow);
        renewBtn.addEventListener("click", function () {
          renewRow.classList.toggle("hidden");
          actions.classList.toggle("expanded", !renewRow.classList.contains("hidden"));
          if (!renewRow.classList.contains("hidden")) renewInput.focus();
        });
        renewCancel.addEventListener("click", function () {
          renewRow.classList.add("hidden");
          actions.classList.remove("expanded");
          renewErr.classList.add("hidden");
          renewInput.value = "";
        });
        const showRenewError = function (message) {
          renewErr.textContent = message;
          renewErr.classList.remove("hidden");
        };
        renewSubmit.addEventListener("click", async function () {
          if (renewSubmit.disabled) return;
          const username = (usernameInput.value || "").trim();
          const password = renewInput.value;
          if (!username) {
            showRenewError("缺少学号：请返回查询页确认学号后重试");
            return;
          }
          if (!password) {
            showRenewError("请输入教务系统密码");
            renewInput.focus();
            return;
          }
          renewSubmit.disabled = true;
          renewSubmit.setAttribute("aria-busy", "true");
          renewSubmit.textContent = "签发中…";
          renewErr.classList.add("hidden");
          try {
            const resp = await fetch("/api/v1/jump/renew", {
              method: "POST",
              headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
              body: JSON.stringify({ username: username, password: password })
            });
            const text = await resp.text();
            let envelope;
            try { envelope = JSON.parse(text); } catch (e) { envelope = null; }
            if (!resp.ok || !envelope || envelope.success === false) {
              const code = envelope && envelope.code;
              const messages = {
                "school.cas.invalid_credentials": "账号或密码错误，请核对后重试",
                "request.rate_limited": "操作过于频繁，请稍后再试",
                "school.upstream.error": "教务系统登录暂不可用，请稍后重试",
                "school.session.expired": "学校端会话已失效，请稍后重试"
              };
              showRenewError((envelope && envelope.message) ||
                messages[code] || ("重新获取失败（" + resp.status + "），请稍后重试"));
              return;
            }
            const data = envelope.data || {};
            if (!data.login_url) {
              showRenewError("新链接签发异常，请重新发起查询");
              return;
            }
            anchor.href = data.login_url;
            anchor.rel = "noopener";
            const noteText = data.note || "";
            const noteTtl = noteText.match(/(\d+)\s*秒内有效/);
            startCountdown(noteTtl ? Math.max(1, parseInt(noteTtl[1], 10)) : 60);
            renewInput.value = "";
            renewRow.classList.add("hidden");
            actions.classList.remove("expanded");
            showToast("success", "新免密链接已生成，请尽快使用");
          } catch (e) {
            showRenewError("网络请求失败，请稍后重试");
          } finally {
            renewSubmit.disabled = false;
            renewSubmit.removeAttribute("aria-busy");
            renewSubmit.textContent = "签发新链接";
          }
        });
        startCountdown(ttlSeconds);
        if (lines.length) {
          // 「使用须知」收进第一行（手动折叠：button + aria-expanded + 显式显隐，
          // 不用 details——其内容隐匿机制与 display:contents 类布局手法冲突会
          // 泄漏收起态内容）；展开时 body 独占一行，内容来自已过 sanitize 的渲染产物
          const more = document.createElement("div");
          more.className = "login-note-more";
          const summary = document.createElement("button");
          summary.type = "button";
          summary.className = "login-note-summary";
          summary.textContent = "使用须知";
          summary.setAttribute("aria-expanded", "false");
          const body = document.createElement("div");
          body.className = "login-note-body hidden";
          lines.forEach(function (line) {
            const p = document.createElement("p");
            p.innerHTML = line;   // 内容来自已过 sanitize_markdown 的渲染产物
            body.appendChild(p);
          });
          summary.addEventListener("click", function () {
            const open = body.classList.toggle("hidden") === false;
            more.classList.toggle("open", open);
            summary.setAttribute("aria-expanded", String(open));
          });
          more.appendChild(summary);
          more.appendChild(body);
          main.appendChild(more);
        }
      });
      // 课表尾部的单行令牌警示与紧凑条内提示重复：仅在紧凑条存在时移除
      if (root.querySelector("blockquote.login-note")) {
        root.querySelectorAll("blockquote:not(.login-note)").forEach(function (bq) {
          const text = (bq.textContent || "").replace(/\s+/g, "");
          if (/此链接含个人令牌，请勿外传/.test(text) && text.length < 40) bq.remove();
        });
      }
    }

    // 课表下载链接 CTA 化：结果区的主要后续动作（导出 PDF）用胶囊按钮呈现，
    // 与免密跳转同一视觉语言；仅匹配本站导出地址，不误伤普通外链
    function enhanceDownloadCta(root) {
      root.querySelectorAll(".markdown-body a[href]").forEach(function (link) {
        if (link.classList.contains("md-cta")) return;
        const href = link.getAttribute("href") || "";
        if (href.indexOf("/get_schedule/export") === -1) return;
        link.classList.add("md-cta");
      });
    }

    // 课表时间上下文：今天列头高亮 + 正在上的节次行高亮。
    // 仅当公开校历基线可解析出教学周、且查询周参数覆盖本周时标记，
    // 避免对历史周次/聚合数据做误导性高亮；重放历史记录同样按其 weeks 判定。
    function enhanceScheduleToday(root) {
      if (!calendarConfig) return;
      const ctx = resolveTeachingContext(new Date(), calendarConfig);
      if (!ctx) return;
      if (!queryCoversWeek(resultContext.weeks, ctx.week)) return;
      root.querySelectorAll(".table-shell--schedule").forEach(function (shell) {
        const table = shell.querySelector("table");
        if (!table || shell.dataset.todayReady === "1") return;
        shell.dataset.todayReady = "1";
        const headCells = Array.from(table.querySelectorAll("thead th"));
        let todayCol = -1;
        headCells.forEach(function (th, idx) {
          if (scheduleDayOfHeader(th.textContent) === ctx.dayIdx) todayCol = idx;
        });
        // 0 为节次表头列；命中的是星期列才标记
        if (todayCol > 0) headCells[todayCol].classList.add("is-today");
        if (ctx.periodText) {
          table.querySelectorAll("tbody tr").forEach(function (tr) {
            const cells = tr.querySelectorAll("td");
            if (cells.length && periodNumberOf(cells[0].textContent) === ctx.periodNumber) {
              tr.classList.add("is-now");
            }
          });
        }
      });
    }

    function applyRenderedElementStyles(root) {
      root.querySelectorAll("[data-score]").forEach(function (node) {
        node.style.width = node.dataset.score + "%";
      });
      root.querySelectorAll("[data-width]").forEach(function (node) {
        node.style.width = node.dataset.width + "%";
      });
      root.querySelectorAll(".table-shell--schedule").forEach(buildScheduleDayView);
      enhanceScheduleToday(root);
      enhanceLoginNote(root);
      enhanceDownloadCta(root);
    }

    function renderMarkdown(text) {
      const lines = String(text).replace(/\r\n/g, "\n").split("\n");
      const html = [];
      let i = 0;

      function renderList(startIndex, listType) {
        const topLevelRe = listType === "ol" ? /^\d+\.\s+/ : /^[-*]\s+/;
        const nestedRe = /^[ \t]+(?:[-*]|\d+\.)\s+/;
        let cursor = startIndex;
        const items = [];

        while (cursor < lines.length && topLevelRe.test(lines[cursor].trim())) {
          let content = lines[cursor].trim().replace(topLevelRe, "");
          cursor++;
          const children = [];
          while (cursor < lines.length && nestedRe.test(lines[cursor])) {
            children.push(lines[cursor].trim().replace(/^(?:[-*]|\d+\.)\s+/, ""));
            cursor++;
          }
          items.push("<li>" + renderInline(content) +
            (children.length
              ? "<ul>" + children.map(function (item) { return "<li>" + renderInline(item) + "</li>"; }).join("") + "</ul>"
              : "") +
            "</li>");
        }

        return {
          html: "<" + listType + ">" + items.join("") + "</" + listType + ">",
          next: cursor
        };
      }

      while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();

        if (!trimmed) { i++; continue; }

        if (trimmed.startsWith("```")) {
          const buf = [];
          i++;
          while (i < lines.length && !lines[i].trim().startsWith("```")) {
            buf.push(lines[i]);
            i++;
          }
          i++;
          html.push("<pre><code>" + escapeHtml(buf.join("\n")) + "</code></pre>");
          continue;
        }

        if (trimmed.startsWith("|")) {
          const rows = [];
          while (i < lines.length && lines[i].trim().startsWith("|")) {
            rows.push(lines[i].trim());
            i++;
          }
          if (rows.length >= 2 && isTableSeparator(rows[1])) {
            const cells = function (row) {
              return row.split("|").slice(1, -1).map(function (cell) {
                return cell.trim();
              });
            };
            const rawHeaders = cells(rows[0]);
            const headers = rawHeaders.map(function (h) { return h.toLowerCase(); });
            const scoreIdx = headers.findIndex(function (h) {
              return h.indexOf("成绩") !== -1 || h.indexOf("分数") !== -1 || h === "score";
            });
            const typeIdx = headers.findIndex(function (h) {
              return h.indexOf("课程类别") !== -1 || h.indexOf("类型") !== -1 || h === "course type";
            });
            function decorateCell(cell, colIdx) {
              const plain = cell.trim();
              const header = headers[colIdx] || "";
              const label = escapeHtml(rawHeaders[colIdx] || header);
              const semanticClasses = [];
              if (/课程/.test(header)) semanticClasses.push("cell-course");
              if (/学期/.test(header)) semanticClasses.push("cell-semester");
              if (/GPA/i.test(header)) semanticClasses.push("cell-gpa");
              if (/学分/.test(header)) semanticClasses.push("cell-credit");
              if (/备注/.test(header)) semanticClasses.push("cell-note");
              if (colIdx === scoreIdx) semanticClasses.push("cell-score");
              if (colIdx === typeIdx) semanticClasses.push("cell-type");
              const semanticClass = semanticClasses.length ? " " + semanticClasses.join(" ") : "";
              if (colIdx === scoreIdx) {
                if (plain === "--" || plain === "—" || /未发布|暂无成绩/.test(plain)) {
                  return '<td data-label="' + label + '" class="score-unpub' + semanticClass + '" title="成绩尚未发布">--</td>';
                }
                const num = parseFloat(plain.replace(/[^\d.]/g, ""));
                if (!isNaN(num)) {
                  let cls = "score-low";
                  if (num >= 90) cls = "score-high";
                  else if (num >= 75) cls = "score-mid";
                  const pct = Math.min(100, Math.max(0, num));
                  return '<td data-label="' + label + '" class="' + cls + semanticClass + '">' +
                    renderInline(cell) +
                    '<div class="score-bar"><i data-score="' + pct + '"></i></div></td>';
                }
              }
              if (colIdx === typeIdx) {
                let cls = "other";
                if (plain.indexOf("必修") !== -1) cls = "required";
                else if (plain.indexOf("选修") !== -1 || plain.indexOf("任选") !== -1) cls = "elective";
                else if (plain.indexOf("实践") !== -1 || plain.indexOf("实验") !== -1 || plain.indexOf("实训") !== -1) cls = "practice";
                return '<td data-label="' + label + '" class="' + semanticClasses.join(" ") + '"><span class="badge ' + cls + '">' + renderInline(cell) + "</span></td>";
              }
              return '<td data-label="' + label + '" class="' + semanticClasses.join(" ") + '">' + renderInline(cell) + "</td>";
            }
            const tableClass = scoreIdx !== -1 ? "table-shell table-shell--grades" : "table-shell table-shell--schedule";
            html.push('<div class="' + tableClass + '"><table aria-label="查询结果表格"><thead><tr>');
            cells(rows[0]).forEach(function (cell) {
              html.push("<th>" + renderInline(cell) + "</th>");
            });
            html.push("</tr></thead>");
            if (rows.length > 2) {
              html.push("<tbody>");
              rows.slice(2).forEach(function (row, rowIdx) {
                html.push('<tr style="--row-i:' + rowIdx + '">');
                cells(row).forEach(function (cell, colIdx) {
                  html.push(decorateCell(cell, colIdx));
                });
                html.push("</tr>");
              });
              html.push("</tbody>");
            }
            html.push("</table></div>");
          } else {
            html.push("<p>" + renderInline(rows.join("<br>")) + "</p>");
          }
          continue;
        }

        const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
        if (heading) {
          const level = heading[1].length;
          html.push("<h" + level + ">" + renderInline(heading[2]) + "</h" + level + ">");
          i++;
          continue;
        }

        if (trimmed.startsWith(">")) {
          const buf = [];
          while (i < lines.length && lines[i].trim().startsWith(">")) {
            buf.push(lines[i].trim().replace(/^>\s?/, ""));
            i++;
          }
          html.push("<blockquote>" + buf.map(renderInline).join("<br>") + "</blockquote>");
          continue;
        }

        if (/^[-*]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
          const list = renderList(i, /^\d+\.\s+/.test(trimmed) ? "ol" : "ul");
          html.push(list.html);
          i = list.next;
          continue;
        }

        if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
          html.push("<hr>");
          i++;
          continue;
        }

        const buf = [line];
        i++;
        while (i < lines.length) {
          const next = lines[i].trim();
          if (!next || /^(#{1,6}\s|>|[-*]\s|\d+\.\s|\||```|(-{3,}|\*{3,})$)/.test(next)) {
            break;
          }
          buf.push(lines[i]);
          i++;
        }
        html.push("<p>" + renderInline(buf.join(" ")) + "</p>");
      }

      return html.join("\n");
    }

    function extractMarkdownValue(body) {
      if (!body || typeof body !== "object") return null;
      let primary = null;
      let fallback = null;
      const extraLinks = [];
      const extraMarkdown = [];
      const seenUrls = new Set();
      const seenMarkdown = new Set();

      function extractMarkdownLinks(text) {
        const links = [];
        const re = /\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g;
        let m;
        while ((m = re.exec(text))) {
          links.push(m[0]);
        }
        return links;
      }

      function pushLink(link) {
        const url = typeof link === "string" ? link : link.url;
        if (!/^https?:\/\//i.test(url) || seenUrls.has(url)) return;
        seenUrls.add(url);
        extraLinks.push(link);
      }

      function walk(node, inOutputs) {
        if (!node || typeof node !== "object") return;
        if (Array.isArray(node)) {
          for (const item of node) {
            if (item && typeof item === "object" && (item.url || item.download_url)) {
              const url = String(item.url || item.download_url || "");
              if (/^https?:\/\//i.test(url)) {
                pushLink({
                  name: String(item.filename || item.name || "下载文件"),
                  url: url
                });
              }
            }
            walk(item, inOutputs);
          }
          return;
        }
        for (const key of Object.keys(node)) {
          const value = node[key];
          const childOutputs = inOutputs || key === "outputs";
          if (typeof value === "string" && value.trim()) {
            if (key === "output_text") primary = primary || value;
            else if (key === "output") fallback = fallback || value;
            else if ((key === "result" || key === "markdown") && !primary && !fallback) {
              fallback = value;
            } else if (inOutputs && ["files", "download_url", "file_url", "url"].indexOf(key) !== -1) {
              const s = value.trim();
              const markdownLinks = extractMarkdownLinks(s);
              if (markdownLinks.length) {
                for (const link of markdownLinks) {
                  if (!seenMarkdown.has(link)) {
                    seenMarkdown.add(link);
                    extraMarkdown.push(link);
                  }
                }
              } else if (/^https?:\/\//i.test(s)) {
                pushLink(s);
              }
            }
          } else if (value && typeof value === "object") {
            walk(value, childOutputs);
          }
        }
      }

      walk(body, false);
      const parts = [];
      if (primary) parts.push(primary);
      if (
        fallback &&
        fallback !== primary &&
        (primary === null || primary.indexOf(fallback) === -1)
      ) {
        parts.push(fallback);
      }
      for (const link of extraLinks) {
        if (typeof link === "string") {
          parts.push("[下载文件](" + link + ")");
        } else {
          parts.push("[" + link.name + "](" + link.url + ")");
        }
      }
      parts.push.apply(parts, extraMarkdown);
      return parts.length ? parts.join("\n\n") : null;
    }

    const RESULT_OVERVIEW_ICONS = {
      success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="m9 12 2 2 4-4"></path></svg>',
      warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><path d="M12 9v4M12 17h.01"></path></svg>',
      info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4M12 8h.01"></path></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="m15 9-6 6M9 9l6 6"></path></svg>'
    };

    function resultMeta(body) {
      if (!body || typeof body !== "object") return {};
      return body.meta || {};
    }

    function parseResultTable(text) {
      const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
      for (let i = 0; i < lines.length; i++) {
        if (!lines[i].trim().startsWith("|")) continue;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          rows.push(lines[i].trim());
          i++;
        }
        if (rows.length < 3 || !isTableSeparator(rows[1])) continue;
        const cells = function (row) {
          return row.split("|").slice(1, -1).map(function (cell) { return cell.trim(); });
        };
        return {
          headers: cells(rows[0]),
          rows: rows.slice(2).map(cells)
        };
      }
      return null;
    }

    function gradeOverviewFacts(text) {
      const table = parseResultTable(text);
      if (!table) return [];
      const scoreIdx = table.headers.findIndex(function (header) {
        return /成绩|分数|score/i.test(header);
      });
      if (scoreIdx === -1) return [];
      const scores = table.rows.map(function (row) {
        const raw = String(row[scoreIdx] || "").replace(/<br\s*\/?>/gi, " ");
        const value = parseFloat(raw.replace(/[^\d.]/g, ""));
        return Number.isFinite(value) ? value : null;
      }).filter(function (value) { return value !== null; });
      if (!scores.length) return [];
      const average = scores.reduce(function (sum, value) { return sum + value; }, 0) / scores.length;
      return [
        { label: "已发布", value: scores.length + " 门" },
        { label: "平均分", value: average.toFixed(1) },
        { label: "最高分", value: Math.max.apply(null, scores).toFixed(0) }
      ];
    }

    function renderResultOverview(state, kicker, title, description, facts) {
      const icon = RESULT_OVERVIEW_ICONS[state] || RESULT_OVERVIEW_ICONS.info;
      const factItems = facts || [];
      if (state === "success") {
        // 成功态走单行紧凑条：结论 + 关键数值芯片。不再渲染 kicker/描述
        // （与结果标题、概览描述三重冗余），把首屏高度还给真正的查询结果。
        const chips = factItems.map(function (fact) {
          return '<span class="result-fact-chip"><span>' + escapeHtml(fact.label) +
            "</span><strong>" + escapeHtml(fact.value) + "</strong></span>";
        }).join("");
        resultOverview.className =
          "result-overview result-overview--success result-overview--compact";
        resultOverview.innerHTML =
          '<span class="result-overview-icon">' + icon + "</span>" +
          '<div class="result-overview-copy">' +
            '<strong class="result-overview-title">' + escapeHtml(title) + "</strong>" +
            (chips ? '<span class="result-facts-inline">' + chips + "</span>" : "") +
          "</div>";
        resultOverview.classList.remove("hidden");
        return;
      }
      const safeFacts = factItems.map(function (fact) {
        return '<div class="result-fact"><span>' + escapeHtml(fact.label) +
          "</span><strong>" + escapeHtml(fact.value) + "</strong></div>";
      }).join("");
      resultOverview.className = "result-overview result-overview--" + state;
      resultOverview.innerHTML =
        '<span class="result-overview-icon">' + icon + "</span>" +
        '<div class="result-overview-copy">' +
          '<span class="result-overview-kicker">' + escapeHtml(kicker || "查询结果") + "</span>" +
          '<strong class="result-overview-title">' + escapeHtml(title) + "</strong>" +
          '<p class="result-overview-desc">' + escapeHtml(description) + "</p>" +
        "</div>" +
        (safeFacts
          ? '<div class="result-facts' + (factItems.length === 3 ? " result-facts--three" : "") + '">' +
            safeFacts + "</div>"
          : "");
      resultOverview.classList.remove("hidden");
    }

    function renderStateOverview(kind, title, description) {
      renderResultOverview(
        kind === "error" ? "error" : "info",
        resultContext.option ? resultContext.option + "查询" : "查询结果",
        title,
        description,
        resultContext.option ? [
          { label: "学期", value: resultContext.semesters || "当前学期" },
          { label: "范围", value: resultContext.weeks === "all" ? "全部周次" : resultContext.weeks }
        ] : []
      );
    }

    function parseDiagnosticReport(text) {
      const lines = String(text || "").split("\n");
      const categoryLine = lines.find(function (line) {
        return /问题分类：/.test(line);
      }) || "";
      const kindLine = lines.find(function (line) {
        return /^\s*-\s*环节：/.test(line);
      }) || "";
      const evidenceLine = lines.find(function (line) {
        return /^\s*-\s*判断依据：/.test(line);
      }) || "";
      const category = categoryLine.replace(/.*问题分类：/, "").replace(/\*/g, "").trim();
      if (!category) return null;
      const userStart = lines.findIndex(function (line) { return /用户可自行处理：/.test(line); });
      const adminStart = lines.findIndex(function (line) { return /需要管理员\/运维处理：/.test(line); });
      const actions = function (start, end) {
        if (start === -1) return [];
        return lines.slice(start + 1, end === -1 ? lines.length : end)
          .map(function (line) {
            const match = line.match(/^\s*-\s+(.+)$/);
            return match ? match[1].trim() : "";
          })
          .filter(function (item) { return item && item !== "无"; });
      };
      return {
        category: category,
        kind: kindLine.replace(/.*环节：/, "").trim() || "查询",
        evidence: evidenceLine.replace(/.*判断依据：/, "").trim(),
        userActions: actions(userStart, adminStart),
        adminActions: actions(adminStart, -1)
      };
    }

    function humanizeDiagnosticEvidence(evidence) {
      const status = String(evidence || "").match(/HTTP\s*(\d{3})/i);
      if (/暂时不可用|请稍后重试|上游服务/.test(evidence) && status && /^5/.test(status[1])) {
        return "学校服务暂时不可用（HTTP " + status[1] + "）";
      }
      return String(evidence || "服务未返回可用的诊断信息").slice(0, 220);
    }

    function renderDiagnosticReport(report) {
      const userItems = report.userActions.length
        ? report.userActions.map(function (item) { return "<li>" + escapeHtml(item) + "</li>"; }).join("")
        : "<li>稍后重新查询</li>";
      const adminItems = report.adminActions.length
        ? report.adminActions.map(function (item) { return "<li>" + escapeHtml(item) + "</li>"; }).join("")
        : "<li>无需额外处理</li>";
      return '<div class="diagnostic-report">' +
        '<section class="diagnostic-guidance" aria-labelledby="diagnosticGuideTitle">' +
          '<span class="diagnostic-guidance-icon" aria-hidden="true">' + RESULT_OVERVIEW_ICONS.info + "</span>" +
          "<div>" +
            '<h3 id="diagnosticGuideTitle">你可以先这样处理</h3>' +
            '<ul class="diagnostic-action-list">' + userItems + "</ul>" +
          "</div>" +
        "</section>" +
        '<details class="diagnostic-technical">' +
          "<summary>查看诊断信息</summary>" +
          '<dl class="diagnostic-facts">' +
            "<div><dt>发生环节</dt><dd>" + escapeHtml(report.kind) + "</dd></div>" +
            "<div><dt>系统判断</dt><dd>" + escapeHtml(humanizeDiagnosticEvidence(report.evidence)) + "</dd></div>" +
          "</dl>" +
          '<div class="diagnostic-admin"><strong>管理员核对项</strong><ul>' + adminItems + "</ul></div>" +
        "</details>" +
      "</div>";
    }

    // 诊断报告的统一出口：状态卡 + 诊断正文。renderResultMarkdown（markdown 内嵌
    // 报告）与 showResult 的失败信封兜底（data.diagnosis）共用，保证失败查询
    // 永远呈现友好报告而非裸 JSON。
    function renderDiagnosisOutcome(report) {
      renderResultOverview(
        /账号或密码/.test(report.category) ? "error" : "warning",
        (resultContext.option || report.kind) + "查询",
        /账号或密码/.test(report.category)
          ? "登录信息需要确认"
          : (/暂时不可用|服务未开放/.test(humanizeDiagnosticEvidence(report.evidence))
            ? "学校服务暂不可用"
            : "本次查询未完成"),
        /账号或密码/.test(report.category)
          ? "请重新核对学校统一身份认证账号和密码后再试。"
          : "学校服务这次没有正常返回，请稍后重新查询；不要连续快速重试。",
        [
          { label: "环节", value: report.kind },
          { label: "状态", value: (/HTTP\s*\d{3}/i.exec(report.evidence) || ["未完成"])[0] }
        ]
      );
      return renderDiagnosticReport(report);
    }

    function renderResultMarkdown(markdown, body) {
      const report = parseDiagnosticReport(markdown);
      if (report) {
        return renderDiagnosisOutcome(report);
      }
      const meta = resultMeta(body);
      const option = resultContext.option || "查询";
      const facts = option === "成绩" ? gradeOverviewFacts(markdown) : [
        { label: "学期", value: resultContext.semesters || "当前学期" },
        { label: "范围", value: resultContext.weeks === "all" ? "全部周次" : resultContext.weeks }
      ];
      // 教学周上下文徽章（同类课表产品标配）：公开校历基线可解析且查询覆盖本周时
      // 追加「教学周」事实；只陈述位置事实（第 N 周/星期X/进行中节次），不列课程清单，
      // 避免对全周聚合表做误导性结论
      if (option !== "成绩" && calendarConfig) {
        const teachingCtx = resolveTeachingContext(new Date(), calendarConfig);
        if (teachingCtx && queryCoversWeek(resultContext.weeks, teachingCtx.week)) {
          let teachingValue = "第 " + teachingCtx.week + " 周 · 星期" + SCHEDULE_DAY_CHARS[teachingCtx.dayIdx];
          if (teachingCtx.periodText) teachingValue += " · " + teachingCtx.periodText + "进行中";
          facts.push({ label: "教学周", value: teachingValue });
        }
      }
      renderResultOverview(
        "success",
        option + "查询",
        option === "成绩" ? "成绩查询完成" : "课表查询完成",
        option === "成绩"
          ? (facts.length ? "已汇总本次查询结果，成绩概览只统计已发布的数值成绩。" : "查询已完成，本次结果中没有可汇总的已发布成绩。")
          : "课程安排已整理完成，可在下方查看完整课表。",
        facts
      );
      return renderMarkdown(markdown);
    }

    let resultTimer = 0;
    let runIdCopyTimer = 0;
    function showResult(statusCode, body) {
      clearLockTimer();
      clearTimeout(resultTimer);
      const markdown = extractMarkdownValue(body);
      setRunId(body && typeof body === "object"
        ? ((body.meta && body.meta.trace_id) || body.run_id || "") : "");
      skeleton.hidden = true;
      resultCard.classList.add("is-growing");
      skeleton.setAttribute("aria-hidden", "true");
      resultCard.classList.remove("hidden");
      layout.classList.add("has-result");
      resultCard.classList.remove("fade-in");
      resultCard.classList.add("fade-out");
      if (body && typeof body === "object" && body.meta) {
        calendarShowResultBlock(body.meta);
      } else if (calendarEls.block) {
        calendarEls.block.classList.add("hidden");
      }
      resultTimer = setTimeout(function () {
        if (markdown !== null) {
          resultHtml.classList.remove("hidden");
          resultBox.classList.add("hidden");
          resultHtml.innerHTML = renderResultMarkdown(markdown, body);
          applyRenderedElementStyles(resultHtml);
        } else {
          // 失败信封（{kind, diagnosis}）没有 markdown output：优先渲染
          // 确定性诊断报告，仅在报告缺失/不可解析时回退原始 JSON 文本。
          const report = body && typeof body === "object" && body.data &&
            body.data.diagnosis && body.data.diagnosis.report
            ? parseDiagnosticReport(String(body.data.diagnosis.report))
            : null;
          if (report) {
            resultBox.classList.add("hidden");
            resultHtml.classList.remove("hidden");
            resultHtml.innerHTML = renderDiagnosisOutcome(report);
            applyRenderedElementStyles(resultHtml);
          } else {
            resultHtml.classList.add("hidden");
            resultBox.classList.remove("hidden");
            const text = typeof body === "string" ? body : JSON.stringify(body, null, 2);
            resultBox.textContent = "HTTP " + statusCode + "\n\n" + text;
          }
        }
        resultCard.classList.remove("fade-out");
        resultCard.classList.add("fade-in");
      }, 200);
    }

    const DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" x2="12" y1="15" y2="3"></line></svg>';

    function pdfFilename(username) {
      const d = new Date();
      const pad = function (n) { return n < 10 ? "0" + n : String(n); };
      return "成绩单_" + username + "_" + d.getFullYear() + pad(d.getMonth() + 1) +
        pad(d.getDate()) + "_" + pad(d.getHours()) + pad(d.getMinutes()) +
        pad(d.getSeconds()) + ".pdf";
    }

    function downloadPdfData(pdfBase64, username) {
      if (!pdfBase64) return false;
      try {
        const bin = atob(pdfBase64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const blob = new Blob([bytes], { type: "application/pdf" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = pdfFilename(username);
        document.body.appendChild(a);
        a.click();
        // 延迟清理：过早移除锚点/回收 blob 会中断浏览器的下载
        setTimeout(function () {
          a.remove();
          URL.revokeObjectURL(url);
        }, 60000);
        return true;
      } catch (err) {
        return false;
      }
    }

    function autoDownloadPdf(pdfBase64, username) {
      downloadPdfData(pdfBase64, username);
    }

    function renderPdfAction(pdfBase64, username) {
      const box = document.getElementById("pdfActions");
      if (!box) return;
      box.classList.remove("hidden");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-primary btn-sm";
      btn.innerHTML = DOWNLOAD_ICON + "<span>下载 PDF 文档</span>";
      btn.addEventListener("click", function () {
        if (downloadPdfData(pdfBase64, username)) showToast("success", "PDF 已开始下载");
        else showToast("error", "PDF 生成失败，请重试");
      });
      box.innerHTML = "";
      box.appendChild(btn);
    }

    function hidePdfAction() {
      const box = document.getElementById("pdfActions");
      if (box) {
        box.classList.add("hidden");
        box.innerHTML = "";
      }
    }

    function authHeaders() {
      // 令牌由 nginx 反代时注入 Authorization 头，页面不持有令牌
      return GATEWAY_TOKEN ? { "Authorization": "Bearer " + GATEWAY_TOKEN } : {};
    }

    let querySeq = 0;
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (submitBtn.disabled) return;
      if (!validateCredentials()) return;
      const payload = {
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        option: optionInput.value,
        weeks: weeksInput.value,
        md2pdf: md2pdfInput.checked,
        check: checkInput.checked
      };
      if (payload.option === "成绩" && includeAllInput && includeAllInput.checked) {
        payload.include_all = true;
      }
      if (semestersInput.value) {
        payload.semesters = semestersInput.value;
      }
      resultContext = {
        option: payload.option,
        semesters: payload.semesters || "",
        weeks: payload.weeks
      };

      payload.username = String(payload.username);

      const seq = ++querySeq;
      if (queryController) queryController.abort();
      queryController = new AbortController();

      setButtonLoading(true);
      hideOfflineBar();
      hidePdfAction();
      clearLockTimer();
      clearTimeout(resultTimer);
      // 同步装载标记：240ms 骨架屏定时器据此判断「仍在装载」——若结果先于
      // 定时器返回（快速失败/限流 429 等），finally 已移除该标记，定时器
      // 直接放弃，不再把已渲染的结果覆盖回骨架屏（骨架屏常驻 bug 的根因）。
      resultCard.setAttribute("aria-busy", "true");
      // B1 提交让位：表单卡先失焦淡出（240ms），再切结果态让骨架屏淡入 —— 空间连续性
      document.body.classList.add("querying");
      form.classList.add("is-yielding");
      window.setTimeout(function () {
        if (seq !== querySeq || !resultCard.hasAttribute("aria-busy")) return;
        skeleton.hidden = false;
        skeleton.setAttribute("aria-hidden", "false");
        resultHtml.classList.add("hidden");
        resultBox.classList.add("hidden");
        resultCard.classList.remove("hidden");
        resultCard.classList.add("is-growing");
        resultCard.setAttribute("aria-busy", "true");
        resultTitle.textContent = payload.option + "查询结果";
        layout.classList.add("has-result");
      }, 240);
      updateUrlParams(payload);
      const savedScrollY = window.scrollY;
      renderJobProgress("login", payload, "running", 0);
      let rowCount = 0;
      try {
        let resp;
        let data;
        const jobSubmitResp = await fetchWithTimeout("/api/v1/run/jobs", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
          body: JSON.stringify(payload),
          signal: queryController.signal
        }, FETCH_TIMEOUT_JOBS_SUBMIT);
        let jobSubmitText = await jobSubmitResp.text();
        let jobSubmitData;
        try { jobSubmitData = JSON.parse(jobSubmitText); } catch (err) { jobSubmitData = jobSubmitText; }
        const jobSubmitPayload = jobSubmitData && jobSubmitData.data ? jobSubmitData.data : null;
        // 统一语义：以稳定错误码判断降级（异步未启用/排队服务不可用时回退同步查询）
        const jobFeatureUnavailable = jobSubmitResp.status === 404 ||
          (jobSubmitResp.status === 503 &&
            (jobSubmitData && jobSubmitData.code) in {
              "capacity.jobs_disabled": true,
              "capacity.queue_unavailable": true
            });

        if (jobSubmitResp.ok && jobSubmitPayload && jobSubmitPayload.job_id) {
          renderJobProgress("queued", payload, "queued", jobSubmitPayload.position || 0);
          const pollDeadline = Date.now() + JOB_POLL_MAX_DURATION;
          let terminalJob = null;
          while (seq === querySeq && !queryController.signal.aborted) {
            await new Promise(function (resolve) { setTimeout(resolve, 2000); });
            if (seq !== querySeq || queryController.signal.aborted) return;
            if (Date.now() > pollDeadline) {
              // 轮询上限：任务长期无终态时给出明确出路，不让按钮无限 loading
              markJobProgressFailed(payload);
              showErrorCard("查询任务等待超时（超过 8 分钟无结果），请稍后重试");
              showToast("error", "查询任务超时");
              showStatus("查询失败", false);
              renderServiceStatus();
              return;
            }
            const pollResp = await fetchWithTimeout("/api/v1/run/jobs/" + encodeURIComponent(jobSubmitPayload.job_id), {
              headers: authHeaders(),
              cache: "no-store",
              signal: queryController.signal
            }, FETCH_TIMEOUT_JOB_POLL);
            const pollText = await pollResp.text();
            let pollData;
            try { pollData = JSON.parse(pollText); } catch (err) { pollData = pollText; }
            if (pollResp.status === 404) {
              markJobProgressFailed(payload);
              showErrorCard("任务不存在或已过期，请重新查询");
              showToast("error", "查询任务已过期");
              showStatus("查询失败", false);
              renderServiceStatus();
              return;
            }
            if (!pollResp.ok) {
              throw new Error((pollData && pollData.message) || ("HTTP " + pollResp.status));
            }
            const jobPayload = pollData && pollData.data ? pollData.data : null;
            if (!jobPayload || !jobPayload.state) {
              // 非 JSON/非信封响应：不读 null.phase 抛 TypeError 被误报为网络失败
              throw new Error((pollData && pollData.message) || "查询任务状态异常，请重试");
            }
            if (jobPayload.state === "queued") {
              renderJobProgress(
                jobPayload.phase || "queued", payload, jobPayload.state, jobPayload.position);
            } else if (jobPayload.state === "running") {
              renderJobProgress(jobPayload.phase || "querying", payload, jobPayload.state, 0);
            } else {
              terminalJob = jobPayload;
              renderJobProgress(
                terminalJob.phase || (terminalJob.result && terminalJob.result.success ? "done" : "querying"),
                payload,
                terminalJob.state,
                terminalJob.position
              );
              break;
            }
          }
          if (!terminalJob) return;
          data = terminalJob.result ||
            { success: false, code: "internal.error", message: "查询任务失败", data: { kind: "internal" }, meta: {} };
          resp = new Response(JSON.stringify(data), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          });
        } else if (jobSubmitResp.ok || jobFeatureUnavailable) {
          hideJobProgress();
          resp = await fetchWithTimeout("/api/v1/run", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
            body: JSON.stringify(payload),
            signal: queryController.signal
          }, FETCH_TIMEOUT_SYNC_RUN);
          const text = await resp.text();
          try { data = JSON.parse(text); } catch (err) { data = text; }
        } else {
          resp = jobSubmitResp;
          data = jobSubmitData;
        }

        let businessFail = false;
        // 统一语义信封：{success, code, message, data:{kind, output?, pdf_base64?, diagnosis?}, meta}
        const env = data && typeof data === "object" && "success" in data ? data : null;
        const envPayload = env && env.data && typeof env.data === "object" ? env.data : null;
        const envMeta = env && env.meta ? env.meta : null;
        if (!resp.ok) {
          markJobProgressFailed(payload);
          const diagnosis = envPayload && envPayload.diagnosis ? envPayload.diagnosis : null;
          const lockUntil = envMeta && envMeta.lock_until ? envMeta.lock_until : null;
          if (lockUntil) {
            showLockCard(lockUntil);
            showToast("error", "学校账号临时锁定");
          } else {
            const message = (env && env.message) || "HTTP " + resp.status;
            if (diagnosis && diagnosis.report) {
              // 展示后端确定性分类的友好报告（无密码/session/token）
              showResult(resp.status, env || {});
            } else {
              showErrorCard(message);
              showToast("error", "查询失败");
            }
          }
        } else {
          const meta = envMeta;
          const markdown = extractMarkdownValue(envPayload || data);
          const kind = envPayload ? (envPayload.kind || "") : "";
          businessFail = Boolean(env && env.success === false);
          const noData = kind === "grades_empty" ||
            (markdown !== null && (/尚未发布|暂无|没有找到|未查询到|无课/.test(markdown) || looksEmptyMarkdown(markdown)));
          if (businessFail) {
            markJobProgressFailed(payload);
            hidePdfAction();
            if (meta && meta.lock_until) {
              showLockCard(meta.lock_until);
            } else if (markdown !== null) {
              showResult(resp.status, env || {});
            } else {
              showErrorCard((env && env.message) || "查询失败，请检查学号/密码或稍后重试");
            }
            if (markdown === null && !(meta && meta.lock_until)) {
              showToast("error", (env && env.message) || "查询失败");
            }
          } else if (noData) {
            hidePdfAction();
            if (markdown !== null && /尚未发布/.test(markdown)) {
              showStateCard(CLOUD_SVG, "成绩尚未发布", "成绩公布后会自动显示在这里", false);
            } else {
              showEmptyState();
            }
          } else {
            showResult(resp.status, env || data);
            rowCount = markdown !== null ? countMarkdownTableRows(markdown) : 0;
            if (payload.md2pdf && envPayload && envPayload.pdf_base64) {
              renderPdfAction(envPayload.pdf_base64, payload.username);
              autoDownloadPdf(envPayload.pdf_base64, payload.username);
            }
            saveResultRecord({
              username: payload.username,
              option: payload.option,
              semesters: payload.semesters || "",
              weeks: payload.weeks,
              md2pdf: payload.md2pdf,
              check: payload.check,
              runId: (meta && meta.trace_id) || "",
              isMarkdown: markdown !== null,
              resultText: markdown !== null
                ? stripLoginNote(markdown)
                : (typeof data === "string" ? data : JSON.stringify(data, null, 2))
            });
            renderHistorySelect();
            hideOfflineBar();
          }
        }
        renderServiceStatus();
        const queryOk = resp.ok && !businessFail;
        showStatus(
          queryOk
            ? "查询完成" + (rowCount ? " · 共找到 " + rowCount + " 条记录" : "")
            : "查询失败",
          queryOk
        );
      } catch (err) {
        if (err && err.name === "AbortError") return;
        markJobProgressFailed(payload);
        const cached = pruneExpiredHistory().find(function (r) {
          return r.username === payload.username &&
            r.option === payload.option &&
            (r.semesters || "") === (payload.semesters || "");
        });
        if (cached) {
          displaySavedRecord(cached);
          showOfflineBar(cached.savedAt);
          showStatus("网络异常，已展示缓存数据", false);
          showToast("info", "网络异常，已展示本地缓存数据");
        } else if (err && err.name === "TimeoutError") {
          // 超时与网络失败区分文案：超时通常是学校侧/上游缓慢，建议稍后重试
          hideOfflineBar();
          showErrorCard(err.message + "，请稍后重试");
          showToast("error", "查询超时");
        } else {
          hideOfflineBar();
          showErrorCard("网络请求失败，请稍后重试");
          showToast("error", "网络请求失败");
        }
        renderServiceStatus();
      } finally {
        if (seq === querySeq) {
          queryController = null;
          document.body.classList.remove("querying");
          form.classList.remove("is-yielding");
          resultCard.removeAttribute("aria-busy");
          setButtonLoading(false);
          if (layout.classList.contains("has-result")) {
            // 结果态布局与表单态差异大：旧滚动位置可能落在结果卡下方空白处，
            // 仅当结果标题不在视口内时才滚动到结果卡，可见时保持原位不打扰
            const titleRect = resultTitle.getBoundingClientRect();
            const titleVisible = titleRect.top >= 0 && titleRect.top < window.innerHeight;
            if (!titleVisible) {
              const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
              resultCard.scrollIntoView({
                behavior: reduceMotion ? "auto" : "smooth",
                block: "start"
              });
            }
            resultTitle.focus({ preventScroll: true });
          } else {
            window.scrollTo(0, savedScrollY);
          }
        }
      }
    });

    document.getElementById("resetBtn").addEventListener("click", function () {
      usernameInput.value = "";
      passwordInput.value = "";
      optionInput.value = "课表";
      semestersInput.value = "";
      weeksInput.value = "all";
      md2pdfInput.checked = false;
      checkInput.checked = false;
      if (includeAllInput) includeAllInput.checked = false;
      passwordInput.type = "password";
      document.getElementById("togglePassword").textContent = "显示";
      document.getElementById("togglePassword").setAttribute("aria-pressed", "false");
      clearFieldError(usernameInput, usernameError);
      clearFieldError(passwordInput, passwordError);
      historyTrigger.textContent = "— 选择历史查询结果 —";
      historyTrigger.removeAttribute("data-label");
      historyMenu.classList.add("hidden");
      historyTrigger.setAttribute("aria-expanded", "false");
      history.replaceState(null, "", location.pathname);
      syncOptionSeg();
      semestersDropdown.sync();
      weeksDropdown.sync();
      resultCard.classList.remove("is-growing");
      resultCard.classList.add("hidden");
      resultTitle.textContent = "查询结果";
      resultOverview.classList.add("hidden");
      resultOverview.textContent = "";
      resultContext = { option: "", semesters: "", weeks: "all" };
      layout.classList.remove("has-result");
      statusBox.classList.add("hidden");
      hideOfflineBar();
      hidePdfAction();
    });

    document.getElementById("backToQuery").addEventListener("click", function () {
      if (submitBtn.disabled && queryController) queryController.abort();
      clearTimeout(resultTimer);
      resultCard.classList.remove("is-growing");
      resultCard.classList.add("hidden");
      layout.classList.remove("has-result");
      statusBox.classList.add("hidden");
      hideOfflineBar();
      hidePdfAction();
      window.scrollTo(0, 0);
      usernameInput.focus({ preventScroll: true });
    });

    document.getElementById("togglePassword").addEventListener("click", function () {
      const show = passwordInput.type === "password";
      passwordInput.type = show ? "text" : "password";
      this.textContent = show ? "隐藏" : "显示";
      this.setAttribute("aria-pressed", show ? "true" : "false");
    });

    runIdCopy.addEventListener("click", function () {
      copyRunId().catch(function () {
        runIdCopyState.textContent = "复制失败";
        runIdCopy.classList.remove("copied");
      });
    });

    document.addEventListener("mousedown", function (event) {
      const btn = event.target.closest ? event.target.closest(".btn-primary, .btn-ghost") : null;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const span = document.createElement("span");
      span.className = "btn-ripple";
      span.style.width = size + "px";
      span.style.height = size + "px";
      span.style.left = event.clientX - rect.left - size / 2 + "px";
      span.style.top = event.clientY - rect.top - size / 2 + "px";
      btn.appendChild(span);
      setTimeout(function () { span.remove(); }, 600);
    });

    document.addEventListener("click", function (event) {
      const retry = event.target.closest ? event.target.closest(".js-retry-query") : null;
      if (!retry || retry.disabled) return;
      event.preventDefault();
      const form = document.getElementById("queryForm");
      if (!form) return;
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    });


    // ===== 网络日历订阅（页脚入口 + 结果区区块共用一套状态）=====
    var calendarEls = {
      entry: document.getElementById("calendarEntry"),
      modal: document.getElementById("calendarModal"),
      close: document.getElementById("calendarClose"),
      dismiss: document.getElementById("calendarDismiss"),
      username: document.getElementById("calendarUsername"),
      password: document.getElementById("calendarPassword"),
      semester: document.getElementById("calendarSemester"),
      interval: document.getElementById("calendarInterval"),
      check: document.getElementById("calendarCheck"),
      useForm: document.getElementById("calendarUseForm"),
      state: document.getElementById("calendarState"),
      urlBox: document.getElementById("calendarUrlBox"),
      url: document.getElementById("calendarUrl"),
      reveal: document.getElementById("calendarReveal"),
      copy: document.getElementById("calendarCopy"),
      enable: document.getElementById("calendarEnable"),
      refresh: document.getElementById("calendarRefreshNow"),
      rotate: document.getElementById("calendarRotate"),
      stop: document.getElementById("calendarStop"),
      block: document.getElementById("calendarBlock"),
      guideSection: document.getElementById("calendarGuideSection"),
      guideDevice: document.getElementById("calendarGuideDevice"),
      guideReach: document.getElementById("calendarGuideReach"),
      guideWebview: document.getElementById("calendarGuideWebview"),
      guidePrimary: document.getElementById("calendarGuidePrimary"),
      guideSteps: document.getElementById("calendarGuideSteps"),
      guideMore: document.getElementById("calendarGuideMore"),
      guideTip: document.getElementById("calendarGuideTip"),
      guidePrivacy: document.getElementById("calendarGuidePrivacy"),
      qrButton: document.getElementById("calendarQrButton"),
      qrBox: document.getElementById("calendarQrBox"),
      qrCanvas: document.getElementById("calendarQrCanvas"),
      qrStatus: document.getElementById("calendarQrStatus")
    };
    var calendarState = { state: "", feedUrl: "", verifiedToken: "", semester: "" };
    var calendarFocus = null;
    // 学期下拉：直接复用首页查询的同一份学期选项（克隆菜单项，避免两处维护）
    function syncCalendarSemesterOptions() {
      var source = document.getElementById("semestersMenu");
      var target = document.getElementById("calendarSemesterMenu");
      if (!source || !target) return;
      var clone = source.cloneNode(true);
      clone.removeAttribute("id");
      target.innerHTML = clone.innerHTML;
      Array.prototype.forEach.call(target.querySelectorAll(".grades-only-item"), function (item) {
        item.remove();   // 日历学期不需要「全部学期 / 最近 N 学期」这类成绩专用项
      });
    }
    syncCalendarSemesterOptions();
    var calendarSemesterDropdown = initDropdown(
      "calendarSemesterTrigger", "calendarSemesterMenu", "calendarSemester", "默认当前学期");

    function calendarSyncSemester(value) {
      if (value) calendarEls.semester.value = value;
      calendarSemesterDropdown.sync();
    }
    var calendarConfig = null;
    var calendarUnavailable = false;   // 本次部署未启用网络日历（/config 返回 404）
    var calendarUrlVisible = false;

    function calendarMaskUrl(url) {
      if (!url) return "";
      var i = url.lastIndexOf("/");
      if (i < 0) return "••••••";
      return url.slice(0, i + 1) + "••••••••.ics";
    }

    function calendarFill(fill) {
      if (!fill) return;
      if (!calendarEls.username.value) calendarEls.username.value = usernameInput.value.trim();
      if (!calendarEls.password.value) calendarEls.password.value = passwordInput.value;
      if (!calendarEls.semester.value) {
        calendarEls.semester.value = semestersInput.value || (calendarConfig && calendarConfig.default_semester) || "";
      }
    }

    function calendarDisableEntry() {
      // 部署未启用 CALENDAR_ENABLED：日历路由不存在（404），此时不应再露出入口，
      // 否则用户点开只会看到加载失败（本轮线上日志即为此情况）。
      calendarUnavailable = true;
      if (calendarEls.entry) calendarEls.entry.classList.add("hidden");
      if (calendarEls.block) calendarEls.block.classList.add("hidden");
      if (calendarEls.modal) calendarEls.modal.classList.add("hidden");
    }

    function calendarFilterSemesterOptions() {
      var allowed = (calendarConfig && calendarConfig.semesters) || [];
      var menu = document.getElementById("calendarSemesterMenu");
      if (!menu || !allowed.length) return;
      Array.prototype.forEach.call(menu.querySelectorAll(".dropdown-item"), function (item) {
        var value = item.getAttribute("data-value") || "";
        if (value && allowed.indexOf(value) === -1) item.remove();
      });
    }

    // 首页学期下拉动态化：后台「校历校准」保存后立即反映到首页选项；
    // 未启用日历或拉取失败时保留 HTML 静态兜底列表。
    function refreshSemesterOptions() {
      var menu = document.getElementById("semestersMenu");
      if (!menu || !calendarConfig) return;
      var configured = (calendarConfig && calendarConfig.semesters) || [];
      if (!configured.length) return;
      var frag = document.createDocumentFragment();
      var keep = menu.querySelector('.dropdown-item[data-value=""]');
      if (keep) frag.appendChild(keep);
      // 后端学期为升序，界面按最新在前展示；textContent 赋值防注入
      configured.slice().reverse().forEach(function (sem) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "dropdown-item";
        btn.setAttribute("data-value", sem);
        btn.textContent = sem;
        frag.appendChild(btn);
      });
      [["all", "全部学期"], ["recent:3", "最近 3 学期"]].forEach(function (pair) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "dropdown-item grades-only-item";
        btn.setAttribute("data-value", pair[0]);
        btn.textContent = pair[1];
        frag.appendChild(btn);
      });
      menu.innerHTML = "";
      menu.appendChild(frag);
      syncCalendarSemesterOptions();
      semestersDropdown.sync();
      calendarSemesterDropdown.sync();
    }

    // 周数下拉快捷项：公开校历基线可解析出当前教学周时，紧跟「全部周次」插入
    // 「第 N 周（本周）」；默认选中值保持「全部周次」不变（全学期聚合仍有价值）。
    // 静态兜底列表可能已含同值「第 N 周」项，二者语义一致；后台校准学期后
    // 旧快捷项按标记移除重建。config 拉取失败/未启用日历时静默降级。
    function refreshWeekOptions() {
      const menu = document.getElementById("weeksMenu");
      if (!menu || !calendarConfig) return;
      const ctx = resolveTeachingContext(new Date(), calendarConfig);
      const stale = menu.querySelector(".dropdown-item--today");
      if (stale) stale.remove();
      if (!ctx || ctx.week < 1 || ctx.week > 20) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dropdown-item dropdown-item--today";
      btn.setAttribute("data-value", String(ctx.week));
      btn.textContent = "第 " + ctx.week + " 周（本周）";
      const keep = menu.querySelector('.dropdown-item[data-value="all"]');
      if (keep) keep.insertAdjacentElement("afterend", btn);
      else menu.insertBefore(btn, menu.firstChild);
      weeksDropdown.sync();
    }

    async function calendarLoadConfig() {
      if (calendarConfig) return calendarConfig;
      if (calendarUnavailable) return null;
      try {
        var resp = await fetchWithTimeout("/api/v1/calendars/config", { headers: authHeaders() }, FETCH_TIMEOUT_CALENDAR);
        if (resp.status === 404) { calendarDisableEntry(); return null; }
        if (resp.ok) {
          var body = await resp.json();
          calendarConfig = body && body.data ? body.data : null;
          calendarFilterSemesterOptions();
          refreshSemesterOptions();
          refreshWeekOptions();
        }
      } catch (e) { /* 网络异常时保持入口可用，仅缺少默认学期 */ }
      return calendarConfig;
    }

    function calendarRender() {
      var st = calendarState.state || "";
      var labels = { active: "已开启", reauth_needed: "订阅用的是旧密码，需重新授权",
                     none: "该学号当前学期未开启", locked: "订阅已暂停", expired: "订阅已失效" };
      var text = st ? (labels[st] || st) : "尚未查询状态";
      if (st === "active" || st === "reauth_needed") {
        text += " · 每 " + Math.round((calendarState.refreshInterval || 43200) / 3600) + " 小时自动刷新";
        if (calendarState.lastRefreshAt) text += " · 上次刷新 " + calendarState.lastRefreshAt;
      }
      var inferred = calendarConfig && calendarConfig.inferred_periods && calendarConfig.inferred_periods.length
        ? "（第 " + calendarConfig.inferred_periods.join("、") + " 节时间为推算值，待学校官方确认）" : "";
      calendarEls.state.textContent = text + inferred;
      calendarEls.state.dataset.state = st || "none";
      var hasUrl = Boolean(calendarState.feedUrl);
      calendarEls.urlBox.classList.toggle("hidden", !hasUrl);
      if (hasUrl) {
        calendarEls.url.value = calendarUrlVisible ? calendarState.feedUrl : calendarMaskUrl(calendarState.feedUrl);
        calendarEls.reveal.textContent = calendarUrlVisible ? "隐藏" : "显示";
      }
      var isEnabled = st === "active" || st === "reauth_needed";
      var enableRow = document.getElementById("calendarEnableRow");
      var manageRow = document.getElementById("calendarManageRow");
      if (enableRow) enableRow.classList.toggle("hidden", isEnabled);
      if (manageRow) manageRow.classList.toggle("hidden", !isEnabled);
      calendarEls.enable.textContent = st === "reauth_needed" ? "用当前密码重新授权" : "一键开启订阅";
      calendarEls.rotate.classList.toggle("hidden", !hasUrl);
      calendarEls.refresh.classList.toggle("hidden", !hasUrl);
      if (calendarEls.block && !calendarEls.block.classList.contains("hidden")) {
        calendarEls.block.textContent = "网络日历订阅：" + text +
          (hasUrl ? " · 已生成订阅地址，可在下方订阅面板中复制或关闭。" : " · 可一键开启。");
      }
      calendarGuideRender();
    }

    async function calendarRequest(path, body, method) {
      var resp = await fetchWithTimeout(path, {
        method: method || "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
        body: body ? JSON.stringify(body) : undefined
      }, FETCH_TIMEOUT_CALENDAR);
      var data = null;
      try { data = await resp.json(); } catch (e) { data = null; }
      if (!resp.ok) {
        // 统一信封：message 为人类可读摘要，code 为稳定错误码
        var message = (data && data.message) || ("HTTP " + resp.status);
        var err = new Error(message);
        err.code = data && data.code ? data.code : "";
        throw err;
      }
      return (data && data.data) || {};
    }

    function calendarPayload() {
      return {
        username: (calendarEls.username.value || "").trim(),
        password: calendarEls.password.value,
        semester: (calendarEls.semester.value || "").trim(),
        verified_token: calendarState.verifiedToken || null
      };
    }

    function calendarGuard() {
      var payload = calendarPayload();
      if (!/^\d{10}$/.test(payload.username)) { calendarEls.state.textContent = "学号必须为 10 位数字"; return null; }
      if (!payload.password) { calendarEls.state.textContent = "请填写教务系统密码"; return null; }
      if (!payload.semester) {
        var fallback = calendarConfig && calendarConfig.default_semester;
        if (fallback) {
          calendarSyncSemester(fallback);
          payload.semester = fallback;
        } else {
          calendarEls.state.textContent = "正在加载学期列表，请稍后再试";
          calendarLoadConfig();
          return null;
        }
      }
      return payload;
    }

    function calendarSyncInterval(seconds) {
      if (!seconds || !calendarEls.interval) return;
      calendarEls.interval.value = String(seconds);
      document.querySelectorAll("#calendarIntervalGroup .seg-btn").forEach(function (item) {
        var match = Number(item.dataset.interval) === Number(seconds);
        item.classList.toggle("active", match);
        item.setAttribute("aria-pressed", match ? "true" : "false");
      });
    }

    function calendarApply(data) {
      calendarState.state = data.state || "";
      calendarState.feedUrl = data.feed_url || "";
      calendarState.semester = data.semester || calendarState.semester;
      calendarState.refreshInterval = data.refresh_interval;
      calendarState.lastRefreshAt = data.last_refresh_at;
      if (data.verified_token) calendarState.verifiedToken = data.verified_token;
      if (data.refresh_interval) calendarSyncInterval(data.refresh_interval);
      if (data.semester) calendarSyncSemester(data.semester);
      calendarRender();
    }

    async function calendarCheck() {
      var payload = calendarGuard(); if (!payload) return;
      calendarEls.state.textContent = "查询中…";
      try { calendarApply(await calendarRequest("/api/v1/calendars/status", payload)); }
      catch (e) { calendarEls.state.textContent = e.message; }
    }

    async function calendarEnable() {
      var payload = calendarGuard(); if (!payload) return;
      payload.refresh_interval = Number(calendarEls.interval.value || 43200);
      payload.reauthorize = calendarState.state === "reauth_needed";
      calendarEls.state.textContent = "开启中（需要登录学校并生成首份课表）…";
      try {
        var data = await calendarRequest("/api/v1/calendars", payload);
        calendarState.state = "active";
        calendarState.feedUrl = data.feed_url || "";
        calendarState.semester = data.semester || payload.semester;
        calendarState.refreshInterval = data.refresh_interval;
        calendarRender();
      } catch (e) { calendarEls.state.textContent = e.message; }
    }

    async function calendarRefreshNow() {
      var payload = calendarGuard(); if (!payload) return;
      calendarEls.state.textContent = "刷新中…";
      try {
        var data = await calendarRequest("/api/v1/calendars/refresh", payload);
        calendarEls.state.textContent = data.skipped
          ? ("距上次刷新过近，稍后再试（可于 " + (data.next_allowed_at || "") + " 后）")
          : ("已刷新" + (data.changed ? "，课表有更新" : "，课表无变化"));
      } catch (e) { calendarEls.state.textContent = e.message; }
    }

    async function calendarRotate() {
      var payload = calendarGuard(); if (!payload) return;
      if (!window.confirm("轮换后旧订阅地址立即失效，需要在日历软件中重新添加。继续？")) return;
      try {
        var data = await calendarRequest("/api/v1/calendars/rotate", payload);
        calendarState.feedUrl = data.feed_url || "";
        calendarUrlVisible = false;     // 新地址仍先掩码展示，避免就地暴露长期密钥
        calendarRender();
        calendarEls.state.textContent = "订阅地址已轮换：旧地址立即失效，请在日历软件中重新添加";
      } catch (e) { calendarEls.state.textContent = e.message; }
    }

    async function calendarStop() {
      var payload = calendarGuard(); if (!payload) return;
      if (!window.confirm("关闭后将立即删除加密凭据与课表快照，订阅地址失效；已同步到日历软件的事件需在客户端自行删除。确认关闭？")) return;
      try {
        await calendarRequest("/api/v1/calendars", payload, "DELETE");
        calendarState.state = "none"; calendarState.feedUrl = ""; calendarUrlVisible = false;
        calendarRender();
        calendarEls.state.textContent = "订阅已关闭，凭据与快照已删除";
      } catch (e) { calendarEls.state.textContent = e.message; }
    }

    function calendarCopy() {
      if (!calendarState.feedUrl) return;
      navigator.clipboard.writeText(calendarState.feedUrl).then(function () {
        calendarEls.copy.textContent = "已复制";
        window.setTimeout(function () { calendarEls.copy.textContent = "复制"; }, 1200);
      }).catch(function () { calendarEls.state.textContent = "复制失败，请点「显示」后手动复制"; });
    }

    // ===== 订阅指引：识别当前设备与内置浏览器，给出按平台的一键导入 =====
    var calendarGuide = { env: null, reach: null, qrFor: "" };

    function calendarDetectGuide() {
      var ua = navigator.userAgent || "";
      var low = ua.toLowerCase();
      var raw = String((navigator.userAgentData && navigator.userAgentData.platform) ||
                       navigator.platform || "").toLowerCase();
      var platform = "unknown";
      var touchMac = /macintosh|mac os x/.test(low) && (navigator.maxTouchPoints || 0) > 1;
      if (/iphone|ipod/.test(low)) platform = "ios";
      else if (/ipad/.test(low) || touchMac) platform = "ipados";
      else if (/android/.test(low)) platform = "android";
      else if (/mac os x|macintosh/.test(low) || /^mac/.test(raw)) platform = "macos";
      else if (/windows/.test(low) || /^win/.test(raw)) platform = "windows";
      else if (/linux|cros/.test(low) || /linux|cros|x11/.test(raw)) platform = "linux";
      var env = {
        platform: platform,
        platformLabel: { ios: "iPhone", ipados: "iPad", android: "Android", macos: "macOS",
                         windows: "Windows", linux: "Linux", unknown: "未知系统" }[platform],
        browserLabel: "浏览器",
        webview: "",
        mobile: platform === "ios" || platform === "ipados" || platform === "android",
        canShare: typeof navigator.share === "function" && typeof File === "function" &&
                  typeof navigator.canShare === "function"
      };
      var webviews = [[/micromessenger/i, "微信"], [/wxwork/i, "企业微信"], [/\sqq\//i, "QQ"],
                      [/dingtalk/i, "钉钉"], [/alipayclient/i, "支付宝"], [/weibo/i, "微博"],
                      [/\blark\b/i, "飞书"]];
      for (var i = 0; i < webviews.length; i++) {
        if (webviews[i][0].test(ua)) { env.webview = webviews[i][1]; break; }
      }
      var browsers = [[/edgios|edga\/|edg\//i, "Edge"], [/crios|chrome\//i, "Chrome"],
                      [/fxios|firefox\//i, "Firefox"], [/safari\//i, "Safari"]];
      for (var j = 0; j < browsers.length; j++) {
        if (browsers[j][0].test(ua)) { env.browserLabel = browsers[j][1]; break; }
      }
      if (env.webview) env.browserLabel = env.webview + "内置浏览器";
      return env;
    }

    // 订阅地址可达性：日历客户端是"自己去取"地址，因此 loopback 只有本机可用，
    // 内网地址只有同一局域网可用 —— 必须在使用前提示，避免扫码/云端订阅静默失败。
    function calendarGuideReachability(feedUrl) {
      var host = "";
      try {
        host = String(new URL(feedUrl).hostname || "").toLowerCase();
      } catch (e) {
        return { kind: "unknown", note: "" };
      }
      host = host.replace(/^\[|\]$/g, "");      // IPv6 字面量在 URL 里带方括号
      var parts = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
      var a = parts ? Number(parts[1]) : -1;
      var b = parts ? Number(parts[2]) : -1;
      var isLoopback = host === "localhost" || host === "::1" || host === "0.0.0.0" || a === 127;
      if (isLoopback) {
        return { kind: "loopback",
          note: "当前订阅地址是本机地址（" + host + "）：只有这台电脑能取到课表，手机扫码、" +
                "其他设备或云端日历（Google/Outlook）都会失败。真机验收请把 PUBLIC_BASE_URL 改为" +
                "本机局域网地址（如 http://192.168.x.x:8000）或 HTTPS 域名后重启编排。" };
      }
      var isPrivate = (a === 10) || (a === 192 && b === 168) ||
        (a === 172 && b >= 16 && b <= 31) || (a === 169 && b === 254) ||
        /\.local$/.test(host) || /^f[cd][0-9a-f]{2}:/.test(host);
      if (isPrivate) {
        return { kind: "private",
          note: "当前订阅地址是内网地址（" + host + "）：手机需连接同一 Wi-Fi 才能取到课表，" +
                "离开该网络或交给 Google/Outlook 云端抓取会失败；生产请改用 HTTPS 域名。" };
      }
      return { kind: "public", note: "" };
    }

    // 各平台可用方式：本地方式（webcal / 复制 / 下载 / 分享 / 扫码）不经过第三方服务器；
    // Google、Outlook 网页版会把订阅地址交给对应服务端抓取，必须显式提示。
    function calendarGuideActions(env, feedUrl) {
      var webcal = feedUrl.replace(/^https?:\/\//i, "webcal://");
      var google = "https://calendar.google.com/calendar/render?cid=" + encodeURIComponent(feedUrl);
      var outlook = "https://outlook.live.com/calendar/0/addfromweb?url=" + encodeURIComponent(feedUrl) +
                    "&name=" + encodeURIComponent("我的课表");
      var copy = { key: "copy", label: "复制订阅地址", kind: "copy" };
      var download = { key: "download", label: "下载 .ics（一次性导入）", kind: "download",
                       tip: "下载的是当前课表快照：导入后不会自动更新，需要自动更新请用订阅方式。" };
      var share = { key: "share", label: "分享给已安装的日历 App", kind: "share" };
      var apple = { key: "apple", label: "试着用系统日历打开", kind: "link", href: webcal,
                    scheme: "webcal", attempt: true,
                    tip: "webcal:// 是可选尝试：部分系统会把它当一次性导入、要求 HTTPS，" +
                         "或在本地地址上直接报错；失败请用「复制订阅地址」手动订阅。" };
      var outlookApp = { key: "outlook-app", label: "试着用 Outlook 打开", kind: "link", href: webcal,
                         scheme: "webcal", attempt: true,
                         tip: "同样走 webcal://；未安装 Outlook 时会交给系统日历，失败请手动订阅。" };
      var thunderbird = { key: "thunderbird", label: "试着用 Thunderbird 打开", kind: "link", href: webcal,
                          scheme: "webcal", attempt: true,
                          tip: "同样走 webcal://；失败请在 Thunderbird 里手动新建网络日历。" };
      var googleAction = { key: "google", label: "Google 日历（第三方抓取）", kind: "link", href: google,
                           external: true, warn: "订阅地址会提交给 Google 服务器抓取；Google 约每天同步一次。" };
      var outlookAction = { key: "outlook-web", label: "Outlook 网页版（第三方抓取）", kind: "link",
                            href: outlook, external: true,
                            warn: "订阅地址会提交给微软服务器抓取；Outlook 同步间隔常 ≥3 小时。" };
      var plan = {
        ios: {
          primary: [copy],
          more: [apple, googleAction, share, download],
          steps: ["先点「复制订阅地址」",
                  "在 iPhone/iPad 上：设置 → 日历 → 账户 → 添加账户 → 其他 → 添加已订阅的日历，粘贴地址并保存",
                  "「试着用系统日历打开」是可选快捷方式：若提示导入错误或要求 HTTPS，请按上一步手动订阅"]
        },
        ipados: {
          primary: [copy],
          more: [apple, googleAction, share, download],
          steps: ["先点「复制订阅地址」",
                  "设置 → 日历 → 账户 → 添加账户 → 其他 → 添加已订阅的日历，粘贴地址并保存",
                  "本地/内网地址只能在同一网络内订阅，离开该网络会同步失败"]
        },
        macos: {
          primary: [copy],
          more: [apple, outlookApp, thunderbird, googleAction, download],
          steps: ["先点「复制订阅地址」",
                  "Mac 上：日历 → 文件 → 新建日历订阅…，粘贴地址后选「订阅」",
                  "「试着用系统日历打开」为可选快捷方式：webcal 深链在本地地址或缺 HTTPS 时可能报导入错误"]
        },
        android: {
          primary: [copy],
          more: [googleAction, share, download],
          steps: ["先点「复制订阅地址」",
                  "Google 日历网页版：其他日历 → 通过网址添加（需要地址公网可达）",
                  "华为/小米/OPPO/vivo 等系统日历：用「分享给已安装的日历 App」，或在 App 的「订阅/网络日历」入口粘贴地址"]
        },
        windows: {
          primary: [copy],
          more: [outlookApp, outlookAction, googleAction, share, download],
          steps: ["先点「复制订阅地址」",
                  "Outlook 桌面端：添加日历 → 从 Internet 订阅，粘贴地址",
                  "「试着用 Outlook 打开」走 webcal://，部分环境会直接报导入错误，失败请手动订阅"]
        },
        linux: {
          primary: [copy],
          more: [thunderbird, googleAction, download],
          steps: ["先点「复制订阅地址」",
                  "Thunderbird：日历 → 新建日历 → 在网络上 → 粘贴地址",
                  "GNOME 日历 / Evolution：新建日历 → 从 URL 添加"]
        },
        unknown: {
          primary: [copy],
          more: [apple, googleAction, outlookAction, download],
          steps: ["先点「复制订阅地址」",
                  "在日历 App 中选择「订阅 / 添加网络日历 / 通过网址添加」后粘贴地址",
                  "Apple 设备与 Outlook 也可先试上面的 webcal 快捷方式，失败就手动订阅"]
        }
      }[env.platform];
      if (env.webview) {
        plan.steps = ["当前在" + env.webview + "内置浏览器中：webcal 深链基本会被拦截，" +
                      "请复制地址后到系统日历 App 里手动订阅，或点右上角「…」→「在浏览器中打开」",
                      plan.steps[1] || plan.steps[0]];
      }
      plan.tip = "webcal:// 只是「试试交给系统日历」的快捷方式：不同系统可能把它当作一次性导入、" +
        "要求 HTTPS，或对本地/内网地址直接报错 —— 手动「复制订阅地址」再添加网络日历最稳。";
      plan.privacy = "本地方式（系统日历、复制、下载、分享、扫码）不会把订阅地址交给第三方；" +
        "Google 与 Outlook 网页版会让对应服务端抓取该地址 —— 订阅地址即长期密钥，请自行判断是否接受。";
      return plan;
    }

    function calendarWatchDeepLink(label) {
      var left = document.visibilityState === "hidden";
      function mark() { if (document.visibilityState === "hidden") left = true; }
      document.addEventListener("visibilitychange", mark);
      window.setTimeout(function () {
        document.removeEventListener("visibilitychange", mark);
        if (!left && document.visibilityState === "visible" && calendarEls.guideTip) {
          calendarEls.guideTip.textContent = "没有检测到系统打开「" + label +
            "」：当前设备可能没有对应日历 App，请改用下面的其他方式。";
        }
      }, 1500);
    }

    async function calendarShare() {
      if (!calendarState.feedUrl) return;
      if (!(navigator.share && navigator.canShare && typeof File === "function")) {
        calendarEls.guideTip.textContent = "当前浏览器不支持系统分享，请改用「复制订阅地址」或「下载 .ics」。";
        return;
      }
      calendarEls.guideTip.textContent = "正在读取课表快照…";
      try {
        var resp = await fetch(calendarState.feedUrl, { cache: "no-store" });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        var blob = await resp.blob();
        var file = new File([blob], "schedule.ics", { type: "text/calendar" });
        if (!navigator.canShare({ files: [file] })) {
          calendarEls.guideTip.textContent = "当前浏览器不支持分享文件，请改用「下载 .ics」后用日历 App 打开。";
          return;
        }
        calendarEls.guideTip.textContent = "分享的是课表快照文件（不含订阅地址），系统会列出已安装的日历 App。";
        await navigator.share({ files: [file], title: "我的课表" });
      } catch (e) {
        calendarEls.guideTip.textContent = (e && e.name === "AbortError")
          ? "已取消分享。" : "分享失败：请改用「下载 .ics」或「复制订阅地址」。";
      }
    }

    function calendarQrReset() {
      calendarGuide.qrFor = "";
      if (calendarEls.qrBox) calendarEls.qrBox.classList.add("hidden");
      if (calendarEls.qrStatus) calendarEls.qrStatus.textContent = "";
      if (calendarEls.qrCanvas) {
        var ctx = calendarEls.qrCanvas.getContext("2d");
        if (ctx) ctx.clearRect(0, 0, calendarEls.qrCanvas.width, calendarEls.qrCanvas.height);
        calendarEls.qrCanvas.width = 1;
        calendarEls.qrCanvas.height = 1;
      }
    }

    function calendarDrawQr(canvas, data) {
      var size = Number(data && data.size) || 0;
      var rows = (data && data.rows) || [];
      var quiet = Number((data && data.quiet_zone) || 4);
      if (!canvas || !size || rows.length !== size) throw new Error("二维码数据不完整");
      var total = size + quiet * 2;
      var module = Math.max(2, Math.floor(220 / total));
      var dim = total * module;
      var dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
      canvas.width = dim * dpr;
      canvas.height = dim * dpr;
      var ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // 二维码必须保持最高对比度，这里固定纯黑/纯白（不参与主题令牌）
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, dim, dim);
      ctx.fillStyle = "#000000";
      for (var r = 0; r < size; r++) {
        var row = String(rows[r] || "");
        for (var c = 0; c < size; c++) {
          if (row.charAt(c) === "1") {
            ctx.fillRect((c + quiet) * module, (r + quiet) * module, module, module);
          }
        }
      }
      canvas.setAttribute("aria-label", "订阅地址二维码（版本 " + (data.version || "?") +
        "，纠错等级 " + (data.ecc || "M") + "）");
    }

    async function calendarQrShow() {
      var payload = calendarGuard();
      if (!payload) return;
      calendarEls.qrBox.classList.remove("hidden");
      calendarEls.qrStatus.textContent = "正在本地生成二维码…";
      try {
        var data = await calendarRequest("/api/v1/calendars/qr", payload);
        calendarDrawQr(calendarEls.qrCanvas, data);
        calendarGuide.qrFor = calendarState.feedUrl;
        var reach = calendarGuide.reach || calendarGuideReachability(calendarState.feedUrl);
        calendarEls.qrStatus.textContent =
          "用手机相机扫码即可订阅（" + (data.scheme || "webcal") + "://）；二维码包含订阅密钥，请勿截图转发。" +
          (reach.kind === "loopback" ? "当前地址仅本机可达，手机扫码会失败（见上方提示）。" :
           (reach.kind === "private" ? "手机需连接同一 Wi-Fi。" : ""));
      } catch (e) {
        calendarQrReset();
        calendarEls.qrBox.classList.remove("hidden");
        calendarEls.qrStatus.textContent = e.message;
      }
    }

    // 订阅地址是长期密钥：渲染期只生成按钮，URL 在用户点击时才写入 DOM / 发起导航，
    // 避免链接预取、浏览器扩展或安全扫描器提前拿到地址。
    function calendarGuideButton(action, primary) {
      var control = document.createElement("button");
      control.type = "button";
      control.className = "btn " + (primary ? "btn-primary" : "btn-ghost");
      control.textContent = action.label;
      if (action.tip) control.title = action.tip;
      if (action.kind === "link") control.addEventListener("click", function () { calendarGuideOpen(action); });
      else if (action.kind === "download") control.addEventListener("click", calendarGuideDownload);
      else if (action.kind === "copy") control.addEventListener("click", calendarCopy);
      else if (action.kind === "share") control.addEventListener("click", calendarShare);
      return control;
    }

    function calendarGuideOpen(action) {
      if (action.external &&
          !window.confirm(action.warn + "\n\n订阅地址等同于长期访问凭据，确定继续？")) {
        return;
      }
      if (action.external) {
        window.open(action.href, "_blank", "noopener,noreferrer");
        return;
      }
      if (action.attempt) {
        calendarEls.guideTip.textContent = "已尝试用 webcal:// 唤起系统日历；若出现导入错误、订阅失败或没有反应，" +
          "请改用「复制订阅地址」在日历 App 里手动添加网络日历。";
      }
      calendarWatchDeepLink(action.label);
      window.location.href = action.href;      // webcal:// 交给系统日历
    }

    function calendarGuideDownload() {
      var link = document.createElement("a");
      link.href = calendarState.feedUrl;
      link.download = "schedule.ics";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    function calendarGuideRender() {
      if (!calendarEls.guideSection) return;
      var hasUrl = Boolean(calendarState.feedUrl);
      calendarEls.guideSection.classList.toggle("hidden", !hasUrl);
      if (calendarGuide.qrFor && calendarGuide.qrFor !== calendarState.feedUrl) calendarQrReset();
      if (!hasUrl) {
        calendarEls.guidePrimary.textContent = "";
        calendarEls.guideSteps.textContent = "";
        calendarEls.guideMore.textContent = "";
        calendarEls.guideWebview.classList.add("hidden");
        calendarEls.guideReach.classList.add("hidden");
        return;
      }
      var env = calendarDetectGuide();
      calendarGuide.env = env;
      calendarEls.guideDevice.textContent = "已识别：" + env.platformLabel + " · " + env.browserLabel;
      var reach = calendarGuideReachability(calendarState.feedUrl);
      calendarGuide.reach = reach;
      calendarEls.guideReach.textContent = reach.note;
      calendarEls.guideReach.classList.toggle("hidden", !reach.note);
      var plan = calendarGuideActions(env, calendarState.feedUrl);
      calendarEls.guideWebview.textContent = env.webview
        ? ("当前在" + env.webview + "内置浏览器中打开，可能无法唤起日历 App：点右上角「…」→「在浏览器中打开」，或直接用手机相机扫码。")
        : "";
      calendarEls.guideWebview.classList.toggle("hidden", !env.webview);
      calendarEls.guidePrimary.textContent = "";
      plan.primary.forEach(function (action) {
        calendarEls.guidePrimary.appendChild(calendarGuideButton(action, true));
      });
      calendarEls.guideMore.textContent = "";
      plan.more.forEach(function (action) {
        if (action.kind === "share" && !env.canShare) return;
        calendarEls.guideMore.appendChild(calendarGuideButton(action, false));
      });
      calendarEls.guideSteps.textContent = "";
      plan.steps.forEach(function (step) {
        var li = document.createElement("li");
        li.textContent = step;
        calendarEls.guideSteps.appendChild(li);
      });
      calendarEls.guideTip.textContent = plan.tip || "";
      calendarEls.guidePrivacy.textContent = plan.privacy || "";
    }

    function calendarOpen() {
      if (calendarUnavailable) return;
      calendarFocus = document.activeElement;
      calendarUrlVisible = false;      // 面板每次打开都回到掩码状态，需显式点「显示」
      calendarFill(true);
      calendarLoadConfig().then(function (cfg) {
        if (!calendarEls.semester.value && cfg && cfg.default_semester) {
          calendarSyncSemester(cfg.default_semester);
        }
      });
      calendarEls.modal.classList.remove("hidden");
      calendarRender();
      calendarEls.close.focus();
    }

    function calendarClosePanel() {
      var wasOpen = !calendarEls.modal.classList.contains("hidden");
      calendarEls.modal.classList.add("hidden");
      calendarQrReset();          // 订阅密钥不进截图/演示残留：关闭即清除已生成的二维码
      if (wasOpen && calendarFocus && document.contains(calendarFocus)) {
        calendarFocus.focus();
      }
      calendarFocus = null;
    }

    function calendarShowResultBlock(meta) {
      if (!calendarEls.block) return;
      var info = meta && meta.calendar;
      if (!info) { calendarEls.block.classList.add("hidden"); return; }
      calendarState.state = info.state || "";
      calendarState.feedUrl = info.feed_url || "";
      calendarState.verifiedToken = info.verified_token || "";
      calendarState.semester = info.semester || "";
      calendarState.refreshInterval = info.refresh_interval;
      calendarState.lastRefreshAt = info.last_refresh_at;
      calendarEls.block.classList.remove("hidden");
      calendarEls.block.textContent = "";
      if (info.refresh_interval) calendarSyncInterval(info.refresh_interval);
      if (info.semester) calendarSyncSemester(info.semester);
      var text = document.createElement("span");
      text.className = "calendar-block-text";
      var label = info.state === "active" ? "已开启网络日历订阅"
        : (info.state === "reauth_needed" ? "订阅存在但用的是旧密码，可重新授权" : "尚未开启网络日历订阅");
      text.textContent = "网络日历 · " + label + "（" + (info.semester || "") + "）";
      var open = document.createElement("button");
      open.type = "button"; open.className = "btn btn-ghost"; open.textContent = "打开订阅面板";
      open.addEventListener("click", calendarOpen);
      calendarEls.block.appendChild(text);
      calendarEls.block.appendChild(open);
    }

    if (calendarEls.entry) calendarEls.entry.addEventListener("click", calendarOpen);
    if (calendarEls.close) calendarEls.close.addEventListener("click", calendarClosePanel);
    if (calendarEls.dismiss) calendarEls.dismiss.addEventListener("click", calendarClosePanel);
    if (calendarEls.modal) calendarEls.modal.addEventListener("click", function (event) {
      if (event.target === calendarEls.modal) calendarClosePanel();
    });
    if (calendarEls.modal) calendarEls.modal.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        calendarClosePanel();
        return;
      }
      if (event.key !== "Tab") return;
      var focusable = Array.prototype.slice.call(calendarEls.modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter(function (node) {
        return node.getClientRects().length > 0 && !node.classList.contains("hidden");
      });
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    if (calendarEls.useForm) calendarEls.useForm.addEventListener("click", function () {
      calendarEls.username.value = usernameInput.value.trim();
      calendarEls.password.value = passwordInput.value;
      calendarSyncSemester(semestersInput.value || (calendarConfig && calendarConfig.default_semester) || "");
      calendarEls.state.textContent = "已复用查询表单中的学号与密码（仅保存在当前页面内存）";
    });
    document.querySelectorAll("#calendarIntervalGroup .seg-btn").forEach(function (button) {
      button.addEventListener("click", function () {
        document.querySelectorAll("#calendarIntervalGroup .seg-btn").forEach(function (item) {
          item.classList.toggle("active", item === button);
          item.setAttribute("aria-pressed", item === button ? "true" : "false");
        });
        if (calendarEls.interval) calendarEls.interval.value = button.dataset.interval;
      });
    });
    if (calendarEls.check) calendarEls.check.addEventListener("click", calendarCheck);
    if (calendarEls.enable) calendarEls.enable.addEventListener("click", calendarEnable);
    if (calendarEls.refresh) calendarEls.refresh.addEventListener("click", calendarRefreshNow);
    if (calendarEls.rotate) calendarEls.rotate.addEventListener("click", calendarRotate);
    if (calendarEls.stop) calendarEls.stop.addEventListener("click", calendarStop);
    if (calendarEls.copy) calendarEls.copy.addEventListener("click", calendarCopy);
    if (calendarEls.qrButton) calendarEls.qrButton.addEventListener("click", calendarQrShow);
    if (calendarEls.reveal) calendarEls.reveal.addEventListener("click", function () {
      calendarUrlVisible = !calendarUrlVisible; calendarRender();
    });

    calendarLoadConfig();            // 部署未启用日历时隐藏入口（404 探测）
    sanitizeStoredHistory();
    renderServiceStatus();
    renderHistorySelect();
    restoreUrlParams();
    syncBackToTopVisibility();
    // PWA：注册 Service Worker（离线兜底 + 可安装）；仅安全上下文生效。
    // 三重防御：属性缺失（http 不安全上下文）、register 缺失（部分内置 WebView
    // 暴露残缺对象）、register 拒绝 —— 任一情况都静默降级，绝不阻断初始化
    // （此行位于 IIFE 尾部，抛错会让其后的初始化全部中断）。
    try {
      if (window.location.protocol === "https:" ||
          ["localhost", "127.0.0.1"].indexOf(window.location.hostname) !== -1) {
        if (navigator.serviceWorker && typeof navigator.serviceWorker.register === "function") {
          navigator.serviceWorker.register("/sw.js")
            .catch(function () { /* 静默降级 */ });
        }
      }
    } catch (e) { /* 静默降级 */ }
    // 在线/离线感知：连接状态变化即同步离线条（与查询失败降级共用同一 UI，
    // showOfflineBar() 无参调用 = 尚无本地缓存，只提示离线）
    function syncOnlineState() {
      if (navigator.onLine === false) {
        showOfflineBar();
      } else if (!offlineBar.classList.contains("hidden")) {
        hideOfflineBar();
        showToast("success", "网络已恢复");
      }
    }
    window.addEventListener("offline", syncOnlineState);
    window.addEventListener("online", syncOnlineState);
    if (navigator.onLine === false) showOfflineBar();
    // P1 氛围微视差：栅格 0.06 / 前景 0.12（仅装饰层、位移封顶 140px）；
    // prefers-reduced-motion 下完全跳过。巨层的 will-change 已常驻（CSS 侧
    // 桌面 + 允许动效时声明），不再用 body.is-parallaxing 动态挂摘——
    // 动态切换会让每次滚动触发全层重新栅格化、停止后层回退，即「滑动频闪」根因。
    // is-scrolling：滚动期间暂停纯装饰常驻动画（CSS 侧 animation-play-state），
    // 停止 300ms 恢复——滚动帧合成器只处理位移
    var parallaxReduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    var parallaxTicking = false;
    var parallaxScrollingTimer = 0;
    function applyParallax() {
      parallaxTicking = false;
      if (parallaxReduce.matches) return;
      var y = window.scrollY;
      var grid = document.querySelector(".tech-grid");
      var fore = document.querySelector(".tech-ambience-fore");
      if (grid) grid.style.transform = "translate3d(0," + Math.min(y * 0.06, 140).toFixed(1) + "px,0)";
      if (fore) fore.style.transform = "translate3d(0," + Math.min(y * 0.12, 140).toFixed(1) + "px,0)";
      document.body.classList.add("is-scrolling");
      clearTimeout(parallaxScrollingTimer);
      parallaxScrollingTimer = window.setTimeout(function () {
        document.body.classList.remove("is-scrolling");
      }, 300);
    }
    window.addEventListener("scroll", function () {
      if (!parallaxTicking) {
        parallaxTicking = true;
        window.requestAnimationFrame(applyParallax);
      }
    }, { passive: true });
    window.addEventListener("scroll", hideStatusTooltip, { passive: true });
    // renderServiceStatus 每次触发都发 fetch：resize 高频连发时防抖合并，
    // 拖拽窗口/旋转屏幕只请求一次；纯 DOM 同步的两项保持即时执行
    let serviceStatusResizeTimer = 0;
    window.addEventListener("resize", function () {
      hideStatusTooltip();
      syncBackToTopVisibility();
      clearTimeout(serviceStatusResizeTimer);
      serviceStatusResizeTimer = window.setTimeout(renderServiceStatus, 200);
    }, { passive: true });
    // 内容高度变化（结果卡渲染/收起）也要重估“是否有可回滚距离”
    if (window.ResizeObserver && document.body) {
      new ResizeObserver(syncBackToTopVisibility).observe(document.body);
    }
    requestAnimationFrame(function () {
      var shell = document.querySelector(".wrap");
      if (shell) shell.classList.add("page-enter");
    });
  
