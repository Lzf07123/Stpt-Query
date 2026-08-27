(function () {
  "use strict";

  var TOKEN_KEY = "eduquery-admin-token";
  var SUN_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  var MOON_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function $(id) { return document.getElementById(id); }
  var elements = {
    loginForm: $("loginForm"), tokenInput: $("adminToken"),
    loginMessage: $("loginMessage"), app: $("adminApp"), logout: $("logoutBtn"),
    tabLogs: $("tabLogs"), tabMetrics: $("tabMetrics"), logsPanel: $("logsPanel"),
    metricsPanel: $("metricsPanel"), filter: $("logFilter"), exportBtn: $("exportBtn"),
    filterToggle: $("filterToggle"),
    autoLogs: $("autoLogs"), autoMetrics: $("autoMetrics"),
    body: $("logsBody"), prev: $("prevPage"), next: $("nextPage"), pageInfo: $("pageInfo"),
    detailPanel: $("logDetailPanel"), detailBack: $("detailBack"), detailCopy: $("detailCopy"),
    detailMeta: $("detailMeta"), detailResult: $("detailResult"), detailKind: $("detailKind"),
    detailElapsed: $("detailElapsed"), detailRun: $("detailRun"), detailFields: $("detailFields"),
    detailScopes: $("detailScopes"), relatedStatus: $("relatedStatus"), relatedBody: $("relatedBody"),
    detailJson: $("detailJson"),
    total: $("statTotal"), success: $("statSuccess"), failure: $("statFailure"),
    source: $("statSource"), themeToggle: $("themeToggle"),
    memoryValue: $("memoryValue"), stackMemoryInfo: $("stackMemoryInfo"),
    stackMemoryTotal: $("stackMemoryTotal"), stackServices: $("stackServices"),
    trendMax: $("trendMax"), cpuChart: $("cpuChart"),
    trendPeak: $("trendPeak"), trendAverage: $("trendAverage"), trendSamples: $("trendSamples"),
    metricsUpdated: $("metricsUpdated"), metricsNotice: $("metricsNotice"), metricsRefresh: $("metricsRefresh"),
    rss: $("rssValue"), threadInfo: $("threadInfo"), disk: $("diskValue"),
    diskPath: $("diskPath"), network: $("networkValue"), uptime: $("uptimeValue"),
    services: $("serviceList"),
    requestCount: $("requestCount"), requestSuccessRate: $("requestSuccessRate"),
    requestP95: $("requestP95"), analysisCount: $("analysisCount"),
    analysisTokens: $("analysisTokens"), hostCpu: $("hostCpu"), hostCpuMeta: $("hostCpuMeta"),
    hostMemory: $("hostMemory"), hostMemoryMeta: $("hostMemoryMeta"), hostLoad: $("hostLoad"),
    hostLoadMeta: $("hostLoadMeta"), hostUptime: $("hostUptime"), hostSource: $("hostSource"),
    hostDiskRead: $("hostDiskRead"), hostDiskReadMeta: $("hostDiskReadMeta"),
    hostDiskWrite: $("hostDiskWrite"), hostDiskWriteMeta: $("hostDiskWriteMeta"),
    hostNetIn: $("hostNetIn"), hostNetInMeta: $("hostNetInMeta"), hostNetOut: $("hostNetOut"),
    hostNetOutMeta: $("hostNetOutMeta"), storageFile: $("storageFile"),
    storageGrowth: $("storageGrowth"), storageBackups: $("storageBackups"),
    storageFree: $("storageFree"), storageTotal: $("storageTotal"), storageState: $("storageState"),
    storageSource: $("storageSource")
  };

  var state = {
    logs: [], offset: 0, autoLogsTimer: null, autoMetricsTimer: null,
    detail: null, detailScope: "", relatedLogs: [], relatedRequest: 0,
    metricHistory: [], metricsLoading: false
  };

  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function setToken(value) {
    try {
      if (value) sessionStorage.setItem(TOKEN_KEY, value);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }

  async function request(path) {
    var response;
    try {
      response = await fetch("/admin/api/" + path, {
        headers: { Authorization: "Bearer " + getToken() },
        cache: "no-store"
      });
    } catch (error) {
      throw new Error("网络连接失败");
    }
    var payload = null;
    try { payload = await response.json(); } catch (e) {}
    if (!response.ok) {
      if (response.status === 404) throw new Error("凭据无效");
      if (response.status === 401 || response.status === 403) throw new Error("凭据无效");
      throw new Error(payload && payload.detail ? String(payload.detail) : "请求失败");
    }
    return payload;
  }

  function applyTheme(theme) {
    document.documentElement.classList.toggle("dark", theme === "dark");
    if (elements.themeToggle) elements.themeToggle.innerHTML = theme === "dark" ? SUN_ICON : MOON_ICON;
    var background = getComputedStyle(document.documentElement).getPropertyValue("--eduquery-bg").trim();
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta && background) meta.setAttribute("content", background);
    try { localStorage.setItem("eduquery-theme", theme); } catch (e) {}
  }

  function setupTheme() {
    if (!elements.themeToggle) return;
    applyTheme(document.documentElement.classList.contains("dark") ? "dark" : "light");
    elements.themeToggle.addEventListener("click", function () {
      applyTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char];
    });
  }

  function formatBytes(bytes, digits) {
    if (bytes == null || !isFinite(Number(bytes))) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var value = Number(bytes);
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return value.toFixed(index === 0 ? 0 : digits == null ? 1 : digits) + " " + units[index];
  }

  function formatTime(value) {
    if (!value) return "—";
    var date = new Date(value);
    return isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  }

  function setValue(element, value, suffix) {
    var text = value == null ? "—" : String(value) + (suffix || "");
    if (element.textContent === text) return;
    element.textContent = text;
    element.classList.remove("metric-value-refresh");
    void element.offsetWidth;
    element.classList.add("metric-value-refresh");
  }

  function setLoadState(element, percent) {
    var value = Number(percent);
    element.classList.toggle("warning", value >= 80 && value < 95);
    element.classList.toggle("danger", value >= 95);
  }

  function formatRate(value) {
    return value == null ? "—" : formatBytes(value) + "/s";
  }

  function formatUptime(seconds) {
    if (seconds == null) return "—";
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    return days > 0 ? days + " 天 " + hours + " 小时"
      : hours > 0 ? hours + " 小时 " + minutes + " 分钟"
      : minutes + " 分钟";
  }

  function renderApplication(payload) {
    var application = payload && payload.application ? payload.application : {};
    setValue(elements.requestCount, application.requests);
    setValue(elements.requestSuccessRate,
      application.success_rate == null ? null : application.success_rate + "%");
    setValue(elements.requestP95, application.elapsed_p95_ms, " ms");
    setValue(elements.analysisCount, application.analysis_count);
    setValue(elements.analysisTokens, application.analysis_token_total);
  }

  function renderStorage(latest, services) {
    var storage = latest && latest.storage ? latest.storage : {};
    var disk = storage.disk || {};
    var fileStatus = services && services.file_log;
    setValue(elements.storageFile, storage.file_bytes != null ? formatBytes(storage.file_bytes) : null);
    setValue(elements.storageGrowth,
      storage.file_growth_bytes_per_second == null ? null : formatRate(storage.file_growth_bytes_per_second) + " 增长");
    setValue(elements.storageBackups, storage.backup_count);
    setValue(elements.storageFree, disk.free_bytes != null ? formatBytes(disk.free_bytes) : null);
    setValue(elements.storageTotal, disk.total_bytes != null ? "总计 " + formatBytes(disk.total_bytes) : null);
    setValue(elements.storageState, storage.available ? "可写入" : "不可用");
    var labels = { ok: "文件通道正常", disabled: "文件通道未启用", error: "文件通道异常" };
    elements.storageSource.textContent = labels[fileStatus] || "状态未知";
  }

  function renderHost(latest) {
    var host = latest && latest.host ? latest.host : {};
    var memory = host.memory || {};
    var load = host.load || {};
    var disk = host.disk || {};
    var network = host.network || {};
    setValue(elements.hostCpu, host.cpu_percent, "%");
    setLoadState(elements.hostCpu, host.cpu_percent);
    setValue(elements.hostCpuMeta, host.cpu_count == null ? null : host.cpu_count + " 逻辑核心");
    setValue(elements.hostMemory, memory.percent, "%");
    elements.hostMemoryMeta.textContent = memory.used_bytes != null && memory.total_bytes != null
      ? formatBytes(memory.used_bytes) + " / " + formatBytes(memory.total_bytes) : "—";
    setValue(elements.hostLoad, load.one_minute);
    elements.hostLoadMeta.textContent = load.five_minutes != null && load.fifteen_minutes != null
      ? "5m " + load.five_minutes + " · 15m " + load.fifteen_minutes : "—";
    setValue(elements.hostUptime, host.uptime_seconds == null ? null : formatUptime(host.uptime_seconds));
    elements.hostSource.textContent = host.source === "host_proc"
      ? (host.scope === "docker_vm" ? "宿主 /proc · Docker VM" : "宿主 /proc") : "容器内核视图";
    setValue(elements.hostDiskRead, disk.read_iops);
    elements.hostDiskReadMeta.textContent = disk.read_bytes_per_second == null
      ? "—" : formatRate(disk.read_bytes_per_second);
    setValue(elements.hostDiskWrite, disk.write_iops);
    elements.hostDiskWriteMeta.textContent = disk.write_bytes_per_second == null
      ? "—" : formatRate(disk.write_bytes_per_second);
    setValue(elements.hostNetIn, network.received_bytes_per_second == null
      ? null : formatRate(network.received_bytes_per_second));
    elements.hostNetInMeta.textContent = network.received_bytes != null
      ? "累计 " + formatBytes(network.received_bytes) : "—";
    setValue(elements.hostNetOut, network.sent_bytes_per_second == null
      ? null : formatRate(network.sent_bytes_per_second));
    elements.hostNetOutMeta.textContent = network.sent_bytes != null
      ? "累计 " + formatBytes(network.sent_bytes) : "—";
  }

  function renderOrchestration(latest) {
    var stack = latest && latest.orchestration && latest.orchestration.memory
      ? latest.orchestration.memory : {};
    var sourceLabels = { cgroup: "cgroup 实测", process_rss: "RSS 估算", unavailable: "不可用" };
    setValue(elements.memoryValue, stack.memory_bytes == null ? null : formatBytes(stack.memory_bytes));
    setValue(elements.stackMemoryTotal, stack.memory_bytes == null ? null : formatBytes(stack.memory_bytes));
    elements.stackMemoryInfo.textContent = stack.memory_bytes == null ? "等待采样"
      : stack.discovered_services + "/" + stack.expected_services + " 服务 · " + (sourceLabels[stack.source] || stack.source);
    var names = {
      "format-service": "编排后端",
      "get-infomation-service": "查询代理",
      "frontend": "前端入口"
    };
    var services = stack.services || {};
    elements.stackServices.innerHTML = Object.keys(names).map(function (key) {
      var service = services[key] || {};
      var state = service.memory_bytes == null ? "muted" : "success";
      return '<div class="stack-service">' +
        '<span>' + names[key] + '</span>' +
        '<strong>' + (service.memory_bytes == null ? "—" : formatBytes(service.memory_bytes)) + '</strong>' +
        '<small>' + (service.process_count || 0) + ' 进程</small>' +
        '<i class="badge badge-' + state + '">' + (service.source === "cgroup" ? "实测" : service.source === "process_rss" ? "估算" : "缺失") + '</i>' +
        '</div>';
    }).join("");
  }

  async function activate(token) {
    setToken(token);
    setActiveTab("metrics", false);
    await Promise.all([loadLogs(), loadMetrics()]);
    elements.loginForm.closest(".admin-layout").hidden = true;
    elements.app.hidden = false;
    startTimers();
  }

  function setupCustomSelects() {
    document.querySelectorAll(".custom-select").forEach(function (control) {
      if (control.dataset.customSelectReady === "true") return;
      control.dataset.customSelectReady = "true";
      var source = control.querySelector("select, input[type=hidden]");
      var trigger = control.querySelector(".custom-select-trigger");
      var menu = control.querySelector(".custom-select-menu");
      var options = Array.from(control.querySelectorAll(".custom-select-option"));
      var activeIndex = 0;
      if (!source || !trigger || !menu || options.length === 0) return;

      function render() {
        var selected = options.find(function (option) {
          return option.dataset.value === String(source.value);
        }) || options[0];
        activeIndex = options.indexOf(selected);
        trigger.querySelector(".custom-select-value").textContent = selected.textContent;
        options.forEach(function (option) {
          var isSelected = option === selected;
          option.classList.toggle("is-selected", isSelected);
          option.setAttribute("aria-selected", String(isSelected));
        });
      }

      function close() {
        control.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
        menu.hidden = true;
      }

      function open() {
        document.querySelectorAll(".custom-select.is-open").forEach(function (item) {
          item.classList.remove("is-open");
          item.querySelector(".custom-select-trigger").setAttribute("aria-expanded", "false");
          item.querySelector(".custom-select-menu").hidden = true;
        });
        control.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
        menu.hidden = false;
        setActive(activeIndex);
      }

      function setActive(index) {
        activeIndex = (index + options.length) % options.length;
        options.forEach(function (option, optionIndex) {
          option.classList.toggle("is-active", optionIndex === activeIndex);
        });
        var active = options[activeIndex];
        trigger.setAttribute("aria-activedescendant", active.id);
        active.focus();
        active.scrollIntoView({ block: "nearest" });
      }

      function moveActive(offset) {
        if (menu.hidden) open();
        setActive(activeIndex + offset);
      }

      function select(option) {
        source.value = option.dataset.value;
        source.dispatchEvent(new Event("change", { bubbles: true }));
        render();
        close();
        trigger.focus();
      }

      trigger.addEventListener("click", function () {
        if (menu.hidden) open(); else close();
      });
      trigger.addEventListener("keydown", function (event) {
        if (event.key === "ArrowDown") { event.preventDefault(); moveActive(1); }
        else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(-1); }
        else if (event.key === "Home") { event.preventDefault(); if (menu.hidden) open(); setActive(0); }
        else if (event.key === "End") { event.preventDefault(); if (menu.hidden) open(); setActive(options.length - 1); }
        else if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (menu.hidden) open(); else select(options[activeIndex]);
        } else if (event.key === "Escape") close();
      });
      menu.addEventListener("keydown", function (event) {
        if (event.key === "ArrowDown") { event.preventDefault(); moveActive(1); }
        else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(-1); }
        else if (event.key === "Home") { event.preventDefault(); setActive(0); }
        else if (event.key === "End") { event.preventDefault(); setActive(options.length - 1); }
        else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(options[activeIndex]); }
        else if (event.key === "Escape") { event.preventDefault(); close(); trigger.focus(); }
      });
      options.forEach(function (option, index) {
        option.addEventListener("click", function () { select(option); });
        option.addEventListener("mousemove", function () { setActive(index); });
      });
      document.addEventListener("click", function (event) {
        if (!control.contains(event.target)) close();
      });
      source.addEventListener("change", render);
      if (elements.filter) elements.filter.addEventListener("reset", function () { window.setTimeout(render, 0); });
      render();
    });
  }

  function setActiveTab(tab, reload) {
    var isMetrics = tab === "metrics";
    elements.tabMetrics.classList.toggle("active", isMetrics);
    elements.tabLogs.classList.toggle("active", !isMetrics);
    elements.tabMetrics.setAttribute("aria-current", isMetrics ? "page" : "false");
    elements.tabLogs.setAttribute("aria-current", !isMetrics ? "page" : "false");
    elements.metricsPanel.hidden = !isMetrics;
    elements.logsPanel.hidden = isMetrics;
    if (isMetrics && reload !== false) loadMetrics().catch(function () {});
  }

  function stopTimers() {
    window.clearInterval(state.autoLogsTimer);
    window.clearInterval(state.autoMetricsTimer);
    state.autoLogsTimer = null;
    state.autoMetricsTimer = null;
  }

  function startTimers() {
    stopTimers();
    state.autoLogsTimer = window.setInterval(function () {
      if (elements.autoLogs.checked && document.visibilityState === "visible") loadLogs().catch(function () {});
    }, 10000);
    state.autoMetricsTimer = window.setInterval(function () {
      if (elements.autoMetrics.checked && document.visibilityState === "visible") loadMetrics().catch(function () {});
    }, 5000);
  }

  function selectedFilters(form) {
    var data = new FormData(form);
    var query = new URLSearchParams();
    ["keyword", "kind", "option", "success", "time_from", "time_to", "limit"].forEach(function (key) {
      var value = data.get(key);
      if (value == null || String(value).trim() === "") return;
      if (key.indexOf("time_") === 0) {
        var date = new Date(String(value));
        query.set(key, isNaN(date.getTime()) ? String(value) : date.toISOString());
      } else {
        query.set(key, String(value));
      }
    });
    query.set("offset", String(state.offset));
    return query.toString();
  }

  async function loadLogs() {
    var query = selectedFilters(elements.filter);
    var payload = await request("query-logs?" + query);
    state.logs = payload.logs || [];
    renderLogs(payload);
  }

  function formatDetailValue(value) {
    if (value == null || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "number") return String(value);
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }

  function detailLabel(key) {
    var labels = {
      event: "事件", time: "时间", run_id: "运行ID", client_ip: "客户端 IP",
      username: "学号", option: "查询项目", semesters: "学期", weeks: "周数",
      md2pdf: "导出 PDF", check: "成绩校验", success: "是否成功", kind: "分类",
      elapsed_ms: "耗时（毫秒）", message: "消息", analysis: "成绩分析",
      analysis_usage: "Token用量", response_summary: "响应信息"
    };
    return labels[key] || key;
  }

  function detailScopes(item) {
    return [
      { key: "run_id", label: "同一运行ID", value: item.run_id },
      { key: "username", label: "同一学号", value: item.username },
      { key: "client_ip", label: "同一IP", value: item.client_ip },
      { key: "kind", label: "同一分类", value: item.kind }
    ].filter(function (scope) { return String(scope.value || "").trim() !== ""; });
  }

  function renderLogDetail() {
    var item = state.detail;
    if (!item) return;
    var time = formatTime(item.time);
    elements.detailMeta.textContent = time + " · " + (item.event || "query");
    elements.detailResult.innerHTML = '<span class="' + (item.success ? "badge badge-success" : "badge badge-danger") + '">' + (item.success ? "成功" : "失败") + "</span>";
    elements.detailKind.textContent = item.kind || "unknown";
    elements.detailElapsed.textContent = item.elapsed_ms != null ? item.elapsed_ms + " ms" : "—";
    elements.detailRun.textContent = item.run_id || "—";
    elements.detailRun.title = item.run_id || "";

    var keys = Object.keys(item).sort(function (left, right) {
      var order = ["event", "time", "success", "kind", "response_summary", "analysis", "analysis_usage", "elapsed_ms", "run_id", "username", "option", "semesters", "weeks", "md2pdf", "check", "client_ip", "message"];
      return order.indexOf(left) - order.indexOf(right);
    });
    elements.detailFields.innerHTML = keys.map(function (key) {
      return '<div><dt>' + escapeHtml(detailLabel(key)) + "</dt><dd>" + escapeHtml(formatDetailValue(item[key])) + "</dd></div>";
    }).join("");
    elements.detailJson.textContent = JSON.stringify(item, null, 2);
    renderDetailScopes(item);
  }

  function renderDetailScopes(item) {
    var scopes = detailScopes(item);
    if (!state.detailScope && scopes.length) state.detailScope = scopes[0].key;
    elements.detailScopes.innerHTML = scopes.map(function (scope) {
      return '<button class="scope-btn' + (scope.key === state.detailScope ? " active" : "") + '" type="button" data-scope="' + escapeHtml(scope.key) + '">' + escapeHtml(scope.label) + "</button>";
    }).join("");
    Array.from(elements.detailScopes.querySelectorAll(".scope-btn")).forEach(function (button) {
      button.addEventListener("click", function () {
        state.detailScope = button.dataset.scope;
        renderDetailScopes(state.detail);
        loadRelatedLogs();
      });
    });
  }

  function sameRecord(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function renderRelatedLogs() {
    if (!state.relatedLogs.length) {
      elements.relatedBody.innerHTML = '<tr><td colspan="11" class="empty-cell">没有关联记录</td></tr>';
      return;
    }
    elements.relatedBody.innerHTML = state.relatedLogs.map(function (item) {
      var selected = sameRecord(item, state.detail);
      return "<tr" + (selected ? ' class="related-selected"' : "") + ">" +
        "<td>" + escapeHtml(formatTime(item.time)) + "</td>" +
        "<td>" + escapeHtml(item.event || "—") + "</td>" +
        "<td>" + escapeHtml(item.username || "—") + "</td>" +
        "<td>" + escapeHtml(item.option || "—") + "</td>" +
        '<td><span class="' + (item.success ? "badge badge-success" : "badge badge-danger") + '">' + (item.success ? "成功" : "失败") + "</span></td>" +
        "<td>" + escapeHtml(item.kind || "unknown") + "</td>" +
        '<td><span class="' + (item.analysis ? "badge badge-success" : "badge badge-muted") + '">' + (item.analysis ? "是" : "否") + "</span></td>" +
        "<td>" + escapeHtml(item.analysis_usage == null || item.analysis_usage === "" ? "—" : item.analysis_usage) + "</td>" +
        "<td>" + escapeHtml(item.elapsed_ms != null ? item.elapsed_ms + " ms" : "—") + "</td>" +
        '<td class="related-message">' + escapeHtml(item.message || "—") + "</td>" +
        '<td><button class="detail-btn" type="button" data-related="true"' + (selected ? " disabled" : "") + ">查看</button></td></tr>";
    }).join("");
    Array.from(elements.relatedBody.querySelectorAll(".detail-btn")).forEach(function (button, index) {
      button.addEventListener("click", function () {
        showLogDetail(state.relatedLogs[index]);
      });
    });
  }

  async function loadRelatedLogs() {
    var item = state.detail;
    if (!item) return;
    var scope = detailScopes(item).find(function (candidate) { return candidate.key === state.detailScope; });
    if (!scope) {
      state.relatedLogs = [item];
      elements.relatedStatus.textContent = "当前记录缺少可关联字段";
      renderRelatedLogs();
      return;
    }
    var requestSeq = ++state.relatedRequest;
    elements.relatedStatus.textContent = "更多查询中…";
      elements.relatedBody.innerHTML = '<tr><td colspan="11" class="empty-cell">加载中</td></tr>';
    var query = new URLSearchParams({ keyword: String(scope.value).slice(0, 100), limit: "200", offset: "0" });
    try {
      var payload = await request("query-logs?" + query.toString());
      if (requestSeq !== state.relatedRequest) return;
      var logs = payload.logs || [];
      if (!logs.some(function (record) { return sameRecord(record, item); })) logs.unshift(item);
      state.relatedLogs = logs;
      elements.relatedStatus.textContent = "匹配 " + logs.length + " 条 · 数据源 " + ({ file: "文件日志", redis: "Redis", memory: "进程内存" }[payload.source] || payload.source || "—");
      renderRelatedLogs();
    } catch (error) {
      if (requestSeq !== state.relatedRequest) return;
      state.relatedLogs = [item];
      elements.relatedStatus.textContent = error.message;
      renderRelatedLogs();
    }
  }

  function showLogDetail(item) {
    state.detail = item;
    state.detailScope = "";
    state.relatedLogs = [item];
    elements.logsPanel.classList.add("detail-open");
    elements.detailPanel.hidden = false;
    renderLogDetail();
    loadRelatedLogs().catch(function () {});
    elements.detailPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeLogDetail() {
    state.detail = null;
    state.detailScope = "";
    state.relatedLogs = [];
    state.relatedRequest += 1;
    elements.logsPanel.classList.remove("detail-open");
    elements.detailPanel.hidden = true;
  }

  function renderLogs(payload) {
    state.hasMore = Boolean(payload.pagination && payload.pagination.has_more);
    elements.total.textContent = String(payload.total == null ? "—" : payload.total);
    elements.success.textContent = String((payload.stats && payload.stats.success) || 0);
    elements.failure.textContent = String((payload.stats && payload.stats.failure) || 0);
    elements.source.textContent = { file: "文件日志", redis: "Redis", memory: "进程内存" }[payload.source] || payload.source || "—";

    if (!state.logs.length) {
      elements.body.innerHTML = '<tr><td colspan="11" class="empty-cell">没有匹配的记录</td></tr>';
      updatePagination();
      return;
    }
    var html = "";
    state.logs.forEach(function (item) {
      html += "<tr>" +
        "<td>" + escapeHtml(formatTime(item.time)) + "</td>" +
        "<td>" + escapeHtml(item.username || "—") + "</td>" +
        "<td>" + escapeHtml(item.option || item.event || "—") + "</td>" +
        '<td><span class="' + (item.success ? "badge badge-success" : "badge badge-danger") + '">' + (item.success ? "成功" : "失败") + "</span></td>" +
        "<td>" + escapeHtml(item.kind || "unknown") + "</td>" +
        '<td><span class="' + (item.analysis ? "badge badge-success" : "badge badge-muted") + '">' + (item.analysis ? "是" : "否") + "</span></td>" +
        "<td>" + escapeHtml(item.analysis_usage == null || item.analysis_usage === "" ? "—" : item.analysis_usage) + "</td>" +
        "<td>" + escapeHtml(item.elapsed_ms != null ? item.elapsed_ms + " ms" : "—") + "</td>" +
        '<td><code class="run-id" title="' + escapeHtml(item.run_id || "") + '">' + escapeHtml(item.run_id || "—") + "</code></td>" +
        "<td>" + escapeHtml(item.client_ip || "—") + "</td>" +
        '<td><button class="detail-btn" type="button" aria-label="查看更多日志">更多</button></td></tr>';
    });
    elements.body.innerHTML = html;
    elements.body.closest(".table-scroll").scrollTop = 0;
    Array.from(elements.body.querySelectorAll(".detail-btn")).forEach(function (button, index) {
      button.addEventListener("click", function () { showLogDetail(state.logs[index]); });
    });
    updatePagination();
  }

  function updatePagination() {
    var limit = Number(new FormData(elements.filter).get("limit") || 50);
    elements.pageInfo.textContent = "第 " + (state.offset / limit + 1) + " 页";
    elements.prev.disabled = state.offset <= 0;
    elements.next.disabled = !state.hasMore;
  }

  function exportCsv() {
    if (!state.logs.length) return;
    var columns = ["time", "username", "option", "success", "kind", "elapsed_ms", "run_id", "client_ip"];
    var rows = [columns.join(",")].concat(state.logs.map(function (item) {
      return columns.map(function (column) {
        var value = item[column] == null ? "" : String(item[column]);
        return '"' + value.replace(/"/g, '""') + '"';
      }).join(",");
    }));
    var blob = new Blob(["\ufeff" + rows.join("\n")], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "edu-query-logs.csv";
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function drawCpu(history) {
    var canvas = elements.cpuChart;
    if (!canvas || !canvas.clientWidth) return;
    var width = canvas.clientWidth;
    var height = canvas.clientHeight || 104;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    var context = canvas.getContext("2d");
    context.scale(dpr, dpr);
    context.clearRect(0, 0, canvas.width, canvas.height);
    var values = history.map(function (item) { return item.cpu_percent; })
      .filter(function (value) { return value != null; });
    var styles = getComputedStyle(document.documentElement);
    var primary = styles.getPropertyValue("--eduquery-primary").trim();
    if (values.length < 2) {
      context.fillStyle = styles.getPropertyValue("--eduquery-muted").trim();
      context.font = "600 12px Inter, system-ui, sans-serif";
      context.textAlign = "center";
      context.fillText(values.length ? "正在积累 CPU 采样" : "等待采样数据", width / 2, height / 2 + 4);
      return;
    }
    context.strokeStyle = getComputedStyle(document.documentElement)
      .getPropertyValue("--eduquery-border").trim() || "rgba(0,0,0,.08)";
    context.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach(function (ratio) {
      var y = height - 10 - ratio * (height - 20);
      context.beginPath();
      context.moveTo(4, y);
      context.lineTo(width - 4, y);
      context.stroke();
    });
    var points = values.map(function (value, index) {
      return {
        x: index / (values.length - 1) * (width - 12) + 6,
        y: height - 10 - Number(value) / 100 * (height - 20)
      };
    });
    context.beginPath();
    context.strokeStyle = primary;
    context.lineWidth = 2;
    points.forEach(function (point, index) {
      if (index === 0) context.moveTo(point.x, point.y); else context.lineTo(point.x, point.y);
    });
    context.stroke();
    context.save();
    context.globalAlpha = .13;
    context.fillStyle = primary;
    context.beginPath();
    points.forEach(function (point, index) {
      if (index === 0) context.moveTo(point.x, point.y); else context.lineTo(point.x, point.y);
    });
    context.lineTo(points[points.length - 1].x, height - 10);
    context.lineTo(points[0].x, height - 10);
    context.closePath();
    context.fill();
    context.restore();
    var last = points[points.length - 1];
    context.fillStyle = primary;
    context.beginPath();
    context.arc(last.x, last.y, 3, 0, Math.PI * 2);
    context.fill();
  }

  function latestValue(history, selector) {
    for (var index = history.length - 1; index >= 0; index -= 1) {
      var value = selector(history[index]);
      if (value != null) return value;
    }
    return null;
  }

  function clearMetrics(message) {
    setValue(elements.memoryValue, null);
    setLoadState(elements.memoryValue, null);
    elements.stackMemoryInfo.textContent = message;
    setValue(elements.stackMemoryTotal, null);
    elements.stackServices.innerHTML = "";
    elements.trendMax.textContent = "—";
    elements.trendPeak.textContent = "—";
    elements.trendAverage.textContent = "—";
    elements.trendSamples.textContent = "0";
    elements.rss.textContent = "—";
    elements.threadInfo.textContent = "";
    elements.disk.textContent = "—";
    elements.diskPath.textContent = "";
    elements.network.textContent = "—";
    elements.uptime.textContent = "";
    renderApplication({});
    renderHost({});
    renderStorage({}, {});
    elements.services.innerHTML = "";
    drawCpu([]);
  }

  async function loadMetrics() {
    if (state.metricsLoading) return;
    state.metricsLoading = true;
    elements.metricsRefresh.disabled = true;
    try {
      var payload = await request("metrics");
      var history = payload.history || [];
      var latest = payload.latest;
      state.metricHistory = history;
      var cpuPercent = latestValue(history, function (item) { return item.cpu_percent; });
      var memorySnapshot = latestValue(history, function (item) { return item.memory; });
      var memoryPercent = memorySnapshot && memorySnapshot.percent;
      var values = history.map(function (item) { return item.cpu_percent; })
        .filter(function (value) { return value != null; });
      var peak = values.length ? Math.max.apply(null, values) : null;
      var average = values.length ? values.reduce(function (sum, value) { return sum + Number(value); }, 0) / values.length : null;
      elements.trendMax.textContent = cpuPercent != null ? Number(cpuPercent).toFixed(1) + "%" : "采样中";
      elements.trendPeak.textContent = peak != null ? Number(peak).toFixed(1) + "%" : "—";
      elements.trendAverage.textContent = average != null ? Number(average).toFixed(1) + "%" : "—";
      elements.trendSamples.textContent = String(values.length);
      elements.rss.textContent = formatBytes(latest && latest.process && latest.process.rss_bytes);
      elements.threadInfo.textContent = "PID " + (latest && latest.process && latest.process.pid || "—");
      var diskUsed = latest && latest.disk && latest.disk.used_bytes;
      var diskTotal = latest && latest.disk && latest.disk.total_bytes;
      elements.disk.textContent = diskUsed != null && diskTotal ? formatBytes(diskUsed) + " / " + formatBytes(diskTotal) : "—";
      elements.diskPath.textContent = latest && latest.disk ? latest.disk.path : "";
      elements.network.textContent = formatBytes(latest && latest.network && latest.network.received_bytes) + " ↓ / " +
        formatBytes(latest && latest.network && latest.network.sent_bytes) + " ↑";
      elements.uptime.textContent = latest ? "运行 " + Math.round(latest.uptime_seconds / 60) + " 分钟" : "";
      drawCpu(history);
      var collectedAt = latest && latest.collected_at ? new Date(latest.collected_at) : null;
      elements.metricsUpdated.textContent = collectedAt && !isNaN(collectedAt.getTime())
        ? "更新于 " + collectedAt.toLocaleTimeString("zh-CN", { hour12: false })
        : "等待采样";
      elements.metricsNotice.textContent = "实时采集中";
      elements.metricsNotice.classList.remove("error");
      elements.metricsNotice.classList.add("is-live");
      renderApplication(payload);
      renderOrchestration(latest);
      renderHost(latest);
      renderStorage(latest, payload.services || {});
    } catch (error) {
      renderServices(null);
      clearMetrics("采样失败");
      elements.metricsUpdated.textContent = "更新失败";
      elements.metricsNotice.textContent = error.message;
      elements.metricsNotice.classList.add("error");
      elements.metricsNotice.classList.remove("is-live");
      return;
    } finally {
      state.metricsLoading = false;
      elements.metricsRefresh.disabled = false;
    }

    renderServices(payload);
  }

  function renderServices(payload) {
    var services = payload && payload.services ? payload.services : {};
    var serviceLabels = { redis: "Redis", file_log: "文件日志", global_concurrency: "并发上限", rate_limit_per_minute: "每分钟限流" };
    elements.services.innerHTML = Object.keys(serviceLabels).map(function (key) {
      var value = services[key];
      var statusClass = value === "ok" ? "success" : value === "error" ? "danger" : "muted";
      var display = key === "redis" || key === "file_log"
        ? '<span class="badge badge-' + statusClass + '">' + escapeHtml({ ok: "正常", error: "异常", disabled: "未启用", "not-configured": "未配置" }[value] || value) + "</span>"
        : escapeHtml(value);
      return "<div><dt>" + serviceLabels[key] + "</dt><dd>" + display + "</dd></div>";
    }).join("");
  }

  function bindEvents() {
    function setLoginMessage(message, state) {
      elements.loginMessage.textContent = message;
      elements.loginMessage.classList.toggle("hidden", !message);
      elements.loginMessage.classList.toggle("notice-info", state !== "error");
      elements.loginMessage.classList.toggle("notice-error", state === "error");
    }

    elements.loginForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      setLoginMessage("验证中…", "info");
      try {
        await activate(elements.tokenInput.value.trim());
        elements.tokenInput.value = "";
        setLoginMessage("");
      } catch (error) {
        setToken("");
        setLoginMessage(error.message, "error");
      }
    });
    elements.logout.addEventListener("click", function () {
      setToken(""); stopTimers(); location.reload();
    });
    [["tabMetrics", "metrics"], ["tabLogs", "logs"]].forEach(function (pair) {
      $(pair[0]).addEventListener("click", function () {
        setActiveTab(pair[1]);
      });
    });
    elements.filterToggle.addEventListener("click", function () {
      var expanded = elements.filterToggle.getAttribute("aria-expanded") === "true";
      elements.filterToggle.setAttribute("aria-expanded", String(!expanded));
      elements.filter.classList.toggle("filters-expanded", !expanded);
      document.getElementById("advancedFilters").classList.toggle("expanded", !expanded);
      elements.filterToggle.textContent = expanded ? "展开筛选" : "收起筛选";
    });
    elements.filter.addEventListener("submit", function (event) {
      event.preventDefault(); state.offset = 0; loadLogs().catch(showError);
    });
    elements.filter.addEventListener("reset", function () {
      window.setTimeout(function () { state.offset = 0; loadLogs().catch(showError); }, 0);
    });
    elements.prev.addEventListener("click", function () {
      var limit = Number(new FormData(elements.filter).get("limit") || 50);
      state.offset = Math.max(0, state.offset - limit);
      loadLogs().catch(showError);
    });
    elements.next.addEventListener("click", function () {
      var limit = Number(new FormData(elements.filter).get("limit") || 50);
      state.offset += limit;
      loadLogs().catch(function (error) {
        state.offset = Math.max(0, state.offset - limit);
        showError(error);
      });
    });
    elements.exportBtn.addEventListener("click", exportCsv);
    elements.metricsRefresh.addEventListener("click", function () {
      loadMetrics();
    });
    elements.detailBack.addEventListener("click", closeLogDetail);
    elements.detailCopy.addEventListener("click", function () {
      if (!state.detail) return;
      navigator.clipboard.writeText(JSON.stringify(state.detail, null, 2)).then(function () {
        elements.detailCopy.textContent = "已复制";
        window.setTimeout(function () { elements.detailCopy.textContent = "复制 JSON"; }, 1200);
      }).catch(function () {});
    });
  }

  function showError(error) {
    elements.source.textContent = error.message;
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupTheme(); setupCustomSelects(); bindEvents();
    if (window.ResizeObserver) {
      state.chartObserver = new ResizeObserver(function () {
        window.requestAnimationFrame(function () { drawCpu(state.metricHistory); });
      });
      state.chartObserver.observe(elements.cpuChart);
    }
    var saved = getToken();
    if (saved) {
      activate(saved).catch(function () { setToken(""); });
    }
  });
})();
