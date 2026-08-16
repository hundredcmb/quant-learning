const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = {
  mode: "daily",
  weight: "float",
  level: 1,
  jobId: null,
  pollTimer: null,
  result: null,
  jobSnapshot: null,
  mainSort: { key: "pct", dir: "desc" },
  subRows: [],
  subSort: { key: "pct_chg", dir: "desc" },
  currentIndustry: null,
  klineData: null,
  klineSubchart: "amount",
  klineChart: null,
};

document.addEventListener("DOMContentLoaded", () => {
  setDefaultDates();
  refreshConfigButton();
  bindEvents();
  setMode();
  updateSortArrows("#main-table", state.mainSort);
});

function refreshConfigButton() {
  fetch("/api/config")
    .then(handleFetchError)
    .then((data) => {
      const btn = $("#config-btn");
      btn.textContent = data.configured ? "数据配置" : "数据配置 · 未设置";
      btn.classList.toggle("unset", !data.configured);
    })
    .catch(() => {
      const btn = $("#config-btn");
      btn.textContent = "数据配置 · 读取失败";
      btn.classList.add("unset");
    });
}

function openConfigPanel() {
  setTokenStatus("正在读取配置...");
  showElement("#config-panel");
  $("#token-input").focus();
  fetch("/api/config")
    .then(handleFetchError)
    .then((data) => {
      if (data.configured) {
        setTokenStatus(`已保存 token（${data.token_mask}）`, "ok");
      } else {
        setTokenStatus("尚未配置 token，请填写后保存", "err");
      }
    })
    .catch((error) => setTokenStatus(`读取配置失败: ${error.message}`, "err"));
}

function closeConfigPanel() {
  hideElement("#config-panel");
}

function saveToken() {
  const token = $("#token-input").value.trim();
  const saveBtn = $("#token-save-btn");
  saveBtn.disabled = true;
  fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  })
    .then(handleFetchError)
    .then(() => {
      $("#token-input").value = "";
      refreshConfigButton();
      setTokenStatus(token ? "已保存，下次查询将使用新 token" : "已清除 token", "ok");
    })
    .catch((error) => setTokenStatus(`保存失败: ${error.message}`, "err"))
    .finally(() => {
      saveBtn.disabled = false;
    });
}

function testToken() {
  const testBtn = $("#token-test-btn");
  testBtn.disabled = true;
  setTokenStatus("正在测试...");
  fetch("/api/config/test", { method: "POST" })
    .then(handleFetchError)
    .then((data) => {
      setTokenStatus(data.message, data.ok ? "ok" : "err");
    })
    .catch((error) => setTokenStatus(`测试失败: ${error.message}`, "err"))
    .finally(() => {
      testBtn.disabled = false;
    });
}

function setTokenStatus(message, cls) {
  const status = $("#token-status");
  status.textContent = message;
  status.className = cls ? `token-status ${cls}` : "token-status";
}

function setDefaultDates() {
  const now = new Date();
  setPreviousMonthDefaults(now);
  $("#daily-date").value = toInputDate(previousWeekday(now));

  fetch("/api/defaults")
    .then(handleFetchError)
    .then((data) => {
      if (data.daily_date) {
        $("#daily-date").value = data.daily_date;
      }
      if (data.range_start) {
        $("#range-start").value = data.range_start;
      }
      if (data.range_end) {
        $("#range-end").value = data.range_end;
      }
    })
    .catch(() => {
      // 后端或网络不可用时保留本地工作日兜底值。
    });
}

function setPreviousMonthDefaults(now) {
  const firstOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  const lastOfPrevMonth = new Date(firstOfMonth);
  lastOfPrevMonth.setDate(0);
  const firstOfPrevMonth = new Date(lastOfPrevMonth);
  firstOfPrevMonth.setDate(1);
  $("#range-start").value = toInputDate(firstOfPrevMonth);
  $("#range-end").value = toInputDate(lastOfPrevMonth);
}

function previousWeekday(now) {
  const candidate = new Date(now);
  candidate.setDate(candidate.getDate() - 1);
  while (candidate.getDay() === 0 || candidate.getDay() === 6) {
    candidate.setDate(candidate.getDate() - 1);
  }
  return candidate;
}

function toInputDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function bindEvents() {
  $$('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      state.mode = document.querySelector('input[name="mode"]:checked').value;
      setMode();
    });
  });

  $("#weight").addEventListener("change", (event) => {
    state.weight = event.target.value;
    if (state.result) {
      renderMainTable();
    }
  });

  $("#level").addEventListener("change", (event) => {
    state.level = Number(event.target.value);
    if (state.result) {
      renderMainTable();
    }
  });

  $("#query-btn").addEventListener("click", submit);
  $("#cancel-btn").addEventListener("click", cancelTask);
  $("#config-btn").addEventListener("click", openConfigPanel);
  $("#config-close").addEventListener("click", closeConfigPanel);
  $("#token-save-btn").addEventListener("click", saveToken);
  $("#token-test-btn").addEventListener("click", testToken);
  $("#sub-close").addEventListener("click", closeSubPanel);
  $("#kline-close").addEventListener("click", closeKlinePanel);
  $("#result-back-btn").addEventListener("click", backToQuery);

  $("#kline-subchart").addEventListener("change", (event) => {
    state.klineSubchart = event.target.value;
    if (state.klineData && state.klineChart) {
      updateKlineSubchart();
    }
  });

  window.addEventListener("resize", () => {
    if (state.klineChart) {
      state.klineChart.resize();
      positionKlineSubchart(state.klineChart);
    }
  });

  bindSortableHeaders("#main-table", state.mainSort, renderMainTable);
  bindSortableHeaders("#sub-table", state.subSort, renderSubTable);
  $("#main-tbody").addEventListener("click", handleMainTableClick);
}

function setMode() {
  const isDaily = state.mode === "daily";
  $("#daily-field").classList.toggle("hidden", !isDaily);
  $("#range-start-field").classList.toggle("hidden", isDaily);
  $("#range-end-field").classList.toggle("hidden", isDaily);
}

function submit() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.result = null;
  state.jobSnapshot = null;
  hideElement("#result-panel");
  hideElement("#error-panel");
  hideElement("#cancel-btn");

  const payload = state.mode === "daily" ? buildDailyPayload() : buildRangePayload();
  if (!payload) {
    return;
  }

  showElement("#progress-panel");
  updateProgress(0, "提交任务", "正在提交任务", "");
  $("#query-btn").disabled = true;

  fetch(`/api/rankings/${state.mode}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(handleFetchError)
    .then((data) => {
      state.jobId = data.job_id;
      $("#cancel-btn").disabled = false;
      showElement("#cancel-btn");
      startPolling();
    })
    .catch((error) => {
      $("#query-btn").disabled = false;
      showError(error.message);
    });
}

function buildDailyPayload() {
  const date = $("#daily-date").value;
  if (!date) {
    showError("请选择日期");
    return null;
  }
  return { date };
}

function buildRangePayload() {
  const startDate = $("#range-start").value;
  const endDate = $("#range-end").value;
  if (!startDate || !endDate) {
    showError("请选择区间开始日期和结束日期");
    return null;
  }
  if (startDate > endDate) {
    showError("开始日期不能晚于结束日期");
    return null;
  }
  return { start_date: startDate, end_date: endDate };
}

function startPolling() {
  state.pollTimer = setTimeout(pollJob, 600);
}

function pollJob() {
  fetch(`/api/jobs/${state.jobId}`)
    .then(handleFetchError)
    .then((data) => {
      state.jobSnapshot = data;
      if (data.status === "queued" || data.status === "running") {
        const message = data.queue_position > 0 ? `排队中，前方还有 ${data.queue_position} 个任务` : data.message;
        updateProgress(data.progress, data.phase, message, data.message);
        startPolling();
        return;
      }

      if (data.status === "success") {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
        hideElement("#progress-panel");
        hideElement("#cancel-btn");
        $("#query-btn").disabled = false;
        state.result = data.result;
        renderResult();
        return;
      }

      if (data.status === "cancelled") {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
        hideElement("#progress-panel");
        hideElement("#cancel-btn");
        $("#query-btn").disabled = false;
        showError("任务已取消");
        return;
      }

      if (data.status === "error") {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
        $("#query-btn").disabled = false;
        hideElement("#progress-panel");
        hideElement("#cancel-btn");
        showError(data.error || "任务执行失败");
      }
    })
    .catch((error) => {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      $("#query-btn").disabled = false;
      hideElement("#cancel-btn");
      showError(error.message);
    });
}

function cancelTask() {
  if (!state.jobId) {
    return;
  }
  $("#cancel-btn").disabled = true;
  fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" })
    .then(handleFetchError)
    .catch((error) => {
      $("#cancel-btn").disabled = false;
      showError(error.message);
    });
}

function renderResult() {
  if (!state.result) {
    return;
  }

  const baseText = state.result.mode === "daily"
    ? `单日排行 · ${formatDateText(state.result.date)}`
    : `区间排行 · ${formatDateText(state.result.start_date)} ~ ${formatDateText(state.result.end_date)}`;
  const elapsed = elapsedText(state.jobSnapshot);
  $("#summary-text").textContent = elapsed ? `${baseText} · ${elapsed}` : baseText;
  $("#result-back-btn").classList.remove("hidden");
  showElement("#result-panel");
  renderMainTable();
}

function renderMainTable() {
  if (!state.result) {
    return;
  }

  const level = String(state.level);
  const sourceRows = state.result.levels[level] || [];
  const rows = sourceRows.map((row, index) => ({
    ...row,
    rank: index + 1,
    pct: state.weight === "float" ? row.float_weighted_pct : row.equal_weighted_pct,
    count: state.weight === "float" ? row.float_constituent_count : row.equal_constituent_count,
  }));
  sortRows(rows, state.mainSort);
  rows.forEach((row, index) => {
    row.rank = index + 1;
  });

  $("#table-title").textContent = `申万 L${state.level} 级行业排行`;
  $("#table-count").textContent = `共 ${rows.length} 个行业`;
  updateSortArrows("#main-table", state.mainSort);

  const tbody = $("#main-tbody");
  tbody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const indexCodeHtml = state.level === 1
      ? `<a class="index-link" data-kline-code="${escapeHtml(row.index_code)}">${escapeHtml(row.index_code)}</a>`
      : escapeHtml(row.index_code);
    const industryNameHtml = state.level === 1
      ? `<a class="index-link" data-kline-code="${escapeHtml(row.index_code)}">${escapeHtml(row.industry_name)}</a>`
      : escapeHtml(row.industry_name);
    tr.innerHTML = `
      <td>${row.rank}</td>
      <td>${indexCodeHtml}</td>
      <td>${industryNameHtml}</td>
      <td class="${pctClass(row.pct)}">${formatPct(row.pct)}</td>
      <td>${row.count}</td>
      <td>
        <button
          class="link-btn"
          data-index-code="${escapeHtml(row.index_code)}"
          data-industry-name="${escapeHtml(row.industry_name)}"
        >查看成分股</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function handleMainTableClick(event) {
  const klineLink = event.target.closest("[data-kline-code]");
  if (klineLink) {
    openKlinePanel(klineLink.dataset.klineCode, klineLink.textContent.trim());
    return;
  }

  const button = event.target.closest("[data-index-code]");
  if (!button) {
    return;
  }
  openSubPanel(button.dataset.indexCode, button.dataset.industryName);
}

function openSubPanel(indexCode, industryName) {
  state.currentIndustry = { indexCode, industryName };
  state.subRows = [];
  // 原地修改排序状态(不重新赋值), 保持 bindSortableHeaders 闭包引用的对象一致
  state.subSort.key = "pct_chg";
  state.subSort.dir = "desc";
  $("#sub-title").textContent = `${industryName} · 成分股`;
  $("#sub-subtitle").textContent = `${indexCode} · ${state.weight === "float" ? "流通市值加权" : "等权"}`;
  $("#sub-tbody").innerHTML = "";
  hideElement("#sub-error");
  showElement("#sub-loading");
  showElement("#sub-panel");
  updateSortArrows("#sub-table", state.subSort);

  fetch(`/api/jobs/${state.jobId}/constituents/${state.level}/${encodeURIComponent(indexCode)}?weight=${state.weight}`)
    .then(handleFetchError)
    .then((data) => {
      state.subRows = data.rows || [];
      hideElement("#sub-loading");
      renderSubTable();
    })
    .catch((error) => {
      hideElement("#sub-loading");
      $("#sub-error").textContent = error.message;
      showElement("#sub-error");
    });
}

function renderSubTable() {
  const rows = state.subRows.map((row) => ({ ...row }));
  sortRows(rows, state.subSort);
  updateSortArrows("#sub-table", state.subSort);

  const tbody = $("#sub-tbody");
  tbody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.ts_code)}</td>
      <td>${escapeHtml(row.name)}</td>
      <td class="${pctClass(row.pct_chg)}">${formatPct(row.pct_chg)}</td>
      <td>${formatPrice(row.close)}</td>
    `;
    tbody.appendChild(tr);
  });
}

function closeSubPanel() {
  hideElement("#sub-panel");
  state.currentIndustry = null;
  state.subRows = [];
}

function openKlinePanel(indexCode, industryName) {
  state.klineData = null;
  state.klineSubchart = "amount";
  const subchartSelect = $("#kline-subchart");
  if (subchartSelect) {
    subchartSelect.value = "amount";
  }
  $("#kline-title").textContent = industryName;
  $("#kline-subtitle").textContent = indexCode;
  if (state.klineChart) {
    state.klineChart.dispose();
    state.klineChart = null;
  }
  hideElement("#kline-error");
  showElement("#kline-loading");
  showElement("#kline-panel");

  fetch(`/api/index/${encodeURIComponent(indexCode)}/kline`)
    .then(handleFetchError)
    .then((data) => {
      state.klineData = data;
      hideElement("#kline-loading");
      renderKlineChart();
    })
    .catch((error) => {
      hideElement("#kline-loading");
      $("#kline-error").textContent = error.message;
      showElement("#kline-error");
    });
}

function closeKlinePanel() {
  hideElement("#kline-panel");
  state.klineData = null;
  if (state.klineChart) {
    state.klineChart.dispose();
    state.klineChart = null;
  }
}

function renderKlineChart() {
  if (!state.klineData || !window.echarts) {
    return;
  }

  const bars = state.klineData.bars || [];
  if (bars.length === 0) {
    $("#kline-error").textContent = "暂无 K 线数据";
    showElement("#kline-error");
    return;
  }

  if (state.klineChart) {
    state.klineChart.dispose();
  }
  const container = document.getElementById("kline-chart");
  const chart = window.echarts.init(container);
  state.klineChart = chart;
  // 弹窗刚显示时布局可能未完成，等下一帧按真实容器尺寸重排，避免画布与窗口大小不一致
  requestAnimationFrame(() => {
    chart.resize();
    positionKlineSubchart(chart);
  });

  const dates = bars.map((bar) => formatDateText(bar.date));
  const candleData = bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]);
  const isAmount = state.klineSubchart === "amount";
  const subValues = bars.map((bar) => (isAmount ? bar.amount : bar.vol));
  const subColors = bars.map((bar) => {
    if (bar.close >= bar.open) {
      return "#d92d20";
    }
    return "#12995b";
  });
  const validSubValues = subValues.filter((value) => value != null && Number.isFinite(Number(value)));
  const subMax = validSubValues.length ? Math.max(...validSubValues) : null;

  chart.setOption({
    animation: false,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: buildKlineTooltipFormatter(bars, isAmount),
    },
    axisPointer: {
      link: [{ xAxisIndex: "all" }],
    },
    grid: [
      { left: 70, right: 24, top: 30, height: "50%" },
      { left: 70, right: 24, top: "72%", height: "14%" },
    ],
    xAxis: [
      {
        type: "category",
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "#cbd5e1" } },
        axisLabel: { show: false },
        min: "dataMin",
        max: "dataMax",
      },
      {
        type: "category",
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "#cbd5e1" } },
        axisLabel: { show: true },
        min: "dataMin",
        max: "dataMax",
      },
    ],
    yAxis: [
      {
        scale: true,
        splitLine: { lineStyle: { color: "#edf0f4" } },
        axisLabel: { color: "#64748b" },
      },
      {
        gridIndex: 1,
        scale: true,
        show: false,
        splitLine: { show: false },
      },
    ],
    graphic: [
      {
        id: "sub-max",
        type: "text",
        left: 76,
        top: "70%",
        style: {
          text: formatAxisMax(subMax),
          fill: "#64748b",
          font: "12px sans-serif",
        },
      },
    ],
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: [0, 1],
        start: Math.max(0, 100 - (250 / bars.length) * 100),
        end: 100,
      },
      {
        type: "slider",
        xAxisIndex: [0, 1],
        bottom: 4,
        height: 24,
        start: Math.max(0, 100 - (250 / bars.length) * 100),
        end: 100,
      },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: candleData,
        itemStyle: {
          color: "#d92d20",
          color0: "#12995b",
          borderColor: "#d92d20",
          borderColor0: "#12995b",
        },
      },
      {
        name: isAmount ? "成交额" : "成交量",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: subValues,
        itemStyle: { color: (params) => subColors[params.dataIndex] },
      },
    ],
  });
}

function positionKlineSubchart(chart) {
  const select = $("#kline-subchart");
  if (!select) {
    return;
  }
  try {
    const gridComp = chart.getModel().getComponent("grid");
    const grid = Array.isArray(gridComp) ? gridComp[0] : gridComp;
    const chartEl = document.getElementById("kline-chart");
    const bodyEl = document.querySelector(".kline-body");
    if (grid && grid.coordinateSystem && chartEl && bodyEl) {
      const rect = grid.coordinateSystem.getRect();
      const chartRect = chartEl.getBoundingClientRect();
      const bodyRect = bodyEl.getBoundingClientRect();
      const chartH = chart.getHeight();
      const subTop = chartH * 0.72;
      const centerY = (rect.y + rect.height + subTop) / 2;
      select.style.top = `${chartRect.top - bodyRect.top + centerY - select.offsetHeight / 2}px`;
      select.style.left = `${chartRect.left - bodyRect.left + rect.x + 4}px`;
    }
  } catch (err) {
    // 布局未就绪时保留 CSS 默认位置（左上角），不阻塞渲染
  }
}

function buildKlineTooltipFormatter(bars, isAmount) {  return (params) => {
    const candle = params.find((item) => item.seriesType === "candlestick");
    if (!candle) {
      return "";
    }
    const index = candle.dataIndex;
    const bar = bars[index];
    const subLabel = isAmount ? "成交额" : "成交量";
    const subText = isAmount ? formatAmount(bar.amount) : formatVolume(bar.vol);
    return [
      `<strong>${formatDateText(bar.date)}</strong>`,
      `开盘：${formatNumber(bar.open)}`,
      `最高：${formatNumber(bar.high)}`,
      `最低：${formatNumber(bar.low)}`,
      `收盘：${formatNumber(bar.close)}`,
      `${subLabel}：${subText}`,
    ].join("<br>");
  };
}

function updateKlineSubchart() {
  if (!state.klineData || !state.klineChart) {
    return;
  }
  const bars = state.klineData.bars || [];
  const isAmount = state.klineSubchart === "amount";
  const subValues = bars.map((bar) => (isAmount ? bar.amount : bar.vol));
  const subColors = bars.map((bar) => {
    if (bar.close >= bar.open) {
      return "#d92d20";
    }
    return "#12995b";
  });
  const validSubValues = subValues.filter((value) => value != null && Number.isFinite(Number(value)));
  const subMax = validSubValues.length ? Math.max(...validSubValues) : null;

  state.klineChart.setOption({
    tooltip: {
      formatter: buildKlineTooltipFormatter(bars, isAmount),
    },
    series: [
      {},
      {
        name: isAmount ? "成交额" : "成交量",
        data: subValues,
        itemStyle: { color: (params) => subColors[params.dataIndex] },
      },
    ],
    graphic: {
      id: "sub-max",
      style: { text: formatAxisMax(subMax) },
    },
  });
}

function backToQuery() {
  hideElement("#result-panel");
  $("#result-back-btn").classList.add("hidden");
  state.result = null;
  state.jobSnapshot = null;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function bindSortableHeaders(selector, sortState, renderFunction) {
  $$(`${selector} th[data-sort]`).forEach((th) => {
    th.classList.add("sortable");
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortState.key === key) {
        sortState.dir = sortState.dir === "desc" ? "asc" : "desc";
      } else {
        sortState.key = key;
        sortState.dir = "desc";
      }
      renderFunction();
    });
  });
}

function updateSortArrows(selector, sortState) {
  $$(`${selector} th[data-sort]`).forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) {
      arrow.remove();
    }
  });
  const current = document.querySelector(`${selector} th[data-sort="${sortState.key}"]`);
  if (current) {
    const arrow = document.createElement("span");
    arrow.className = "sort-arrow";
    arrow.textContent = sortState.dir === "desc" ? "↓" : "↑";
    current.appendChild(arrow);
  }
}

function sortRows(rows, sortState) {
  const key = sortState.key;
  const direction = sortState.dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const aValue = a[key];
    const bValue = b[key];
    if (typeof aValue === "string" && typeof bValue === "string") {
      return aValue.localeCompare(bValue, "zh-CN") * direction;
    }
    if (aValue == null && bValue == null) {
      return 0;
    }
    if (aValue == null) {
      return 1;
    }
    if (bValue == null) {
      return -1;
    }
    return (Number(aValue) - Number(bValue)) * direction;
  });
}

function updateProgress(percent, phase, message, detailMessage) {
  $("#progress-bar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("#progress-percent").textContent = `${Math.round(percent)}%`;
  $("#progress-phase").textContent = phase || "";
  $("#progress-message").textContent = detailMessage || message || "";
}

function showError(message) {
  const panel = $("#error-panel");
  panel.textContent = message;
  showElement(panel);
}

function showElement(element) {
  resolveElement(element).classList.remove("hidden");
}

function hideElement(element) {
  resolveElement(element).classList.add("hidden");
}

function resolveElement(element) {
  return typeof element === "string" ? $(element) : element;
}

function handleFetchError(response) {
  if (!response.ok) {
    return response.json().then((data) => {
      throw new Error(data.detail || `请求失败: ${response.status}`);
    }).catch((error) => {
      if (error instanceof Error) {
        throw error;
      }
      throw new Error(`请求失败: ${response.status}`);
    });
  }
  return response.json();
}

function formatPct(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatPrice(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  return Number(value).toFixed(2);
}

function formatNumber(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  return Number(value).toFixed(2);
}

function formatAmount(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  return `${(Number(value) / 10000).toFixed(2)} 亿元`;
}

function formatAxisMax(value) {
  // 输入为 sw_daily 的 amount(万元) / vol(万股)：≥1万 显示万，≥1亿 显示亿，不足 1万 显示原数字
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  const number = Number(value);
  if (number >= 1e4) {
    return `${(number / 1e4).toFixed(2)}亿`;
  }
  if (number >= 1) {
    return `${number.toFixed(2)}万`;
  }
  return String(Math.round(number * 1e4));
}

function formatVolume(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  return `${Number(value).toFixed(2)} 万股`;
}

function pctClass(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "zero";
  }
  if (Number(value) > 0) {
    return "up";
  }
  if (Number(value) < 0) {
    return "down";
  }
  return "zero";
}

function formatDateText(value) {
  if (!value) {
    return "";
  }
  const text = String(value);
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
}

function elapsedText(job) {
  if (!job || !job.started_at || !job.finished_at) {
    return "";
  }
  const seconds = (new Date(job.finished_at) - new Date(job.started_at)) / 1000;
  return seconds >= 1 ? `耗时 ${seconds.toFixed(1)}s` : "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
