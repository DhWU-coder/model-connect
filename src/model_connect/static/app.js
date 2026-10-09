"use strict";

// 连接信息和任务 ID 仅保留在标签页会话中，主题偏好单独长期保存。
const $ = (id) => document.getElementById(id);
const connectionStorageKey = "model-connect-connection";
const defaults = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  google: "https://generativelanguage.googleapis.com/v1beta",
  openai_compatible: "",
};
const providerLabels = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google Gemini",
  openai_compatible: "OpenAI 兼容",
};
const statusLabels = {
  pending: "等待检测",
  running: "检测中",
  success: "调用成功",
  failed: "调用失败",
  skipped: "不适用",
  cancelled: "已取消",
};
const errors = {
  authentication_failed: "鉴权失败",
  permission_denied: "权限不足",
  quota_exceeded: "额度不足",
  rate_limited: "请求限流",
  not_found: "模型或接口不存在",
  server_error: "上游服务错误",
  unsupported_request: "参数或能力不支持",
  timeout: "请求超时",
  network_error: "网络错误",
  empty_response: "未返回文本",
  invalid_response: "响应格式异常",
  refused: "内容被拒绝",
  incomplete_response: "响应未完成",
  internal_error: "任务执行异常",
};
let provider = "openai";
let models = [];
let visibleModels = [];
let selected = new Set();
let currentJob = null;
let running = false;
let fetching = false;
let filterVersion = 0;
let filterTimer = null;
let pollTimer = null;
let latestSnapshot = null;
let resultDialogStatus = null;

// 浏览器禁用存储时继续使用当前页，不把存储异常当成服务故障。
function readSessionValue(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSessionValue(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // 无法保存时不影响表单输入、模型调用或结果显示。
  }
}

function updateProviderControls() {
  for (const item of document.querySelectorAll("[data-provider]")) {
    const active = item.dataset.provider === provider;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  }
  $("protocolRow").hidden = ["anthropic", "google"].includes(provider);
  $("protocol").options[0].text = "默认 · Chat Completions";
}

function saveConnectionSession() {
  writeSessionValue(
    connectionStorageKey,
    JSON.stringify({
      provider,
      baseUrl: $("baseUrl").value,
      apiKey: $("apiKey").value,
    }),
  );
}

function restoreConnectionSession() {
  const saved = readSessionValue(connectionStorageKey);
  if (saved === null) return;
  try {
    const data = JSON.parse(saved);
    // 只恢复已知 provider 和字符串字段，损坏数据整体回退。
    if (
      !data ||
      typeof data.provider !== "string" ||
      !Object.hasOwn(defaults, data.provider) ||
      typeof data.baseUrl !== "string" ||
      typeof data.apiKey !== "string"
    ) {
      writeSessionValue(connectionStorageKey, null);
      return;
    }
    provider = data.provider;
    $("baseUrl").value = data.baseUrl;
    $("apiKey").value = data.apiKey;
    updateProviderControls();
  } catch {
    writeSessionValue(connectionStorageKey, null);
  }
}

for (const id of ["baseUrl", "apiKey"]) {
  $(id).addEventListener("input", saveConnectionSession);
  $(id).addEventListener("change", saveConnectionSession);
}
// 页面离开前同步最后一次输入，兼容浏览器自动填充。
window.addEventListener("pagehide", saveConnectionSession);

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
}

function notice(message, error = false) {
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          cache: "no-store",
        },
  );
  const data = await response.json();
  if (!response.ok) {
    let message = data.detail || "请求失败";
    if (Array.isArray(message))
      message = message
        .map(
          (item) =>
            `${item.loc.slice(1).join(".")}：${item.msg.replace(/^Value error, /, "")}`,
        )
        .join("\n");
    const error = new Error(String(message));
    error.status = response.status;
    throw error;
  }
  return data;
}

function connection() {
  let headers = {};
  if ($("extraHeaders").value.trim()) {
    try {
      headers = JSON.parse($("extraHeaders").value);
    } catch {
      throw new Error("附加请求头必须是有效的 JSON 对象");
    }
    if (
      !headers ||
      Array.isArray(headers) ||
      typeof headers !== "object" ||
      Object.values(headers).some((value) => typeof value !== "string")
    ) {
      throw new Error("附加请求头的名称和值都必须是字符串");
    }
  }
  return {
    provider,
    base_url: $("baseUrl").value.trim(),
    api_key: $("apiKey").value.trim(),
    list_path: $("listPath").value.trim(),
    probe_path: $("probePath").value.trim(),
    headers,
  };
}

function setRunning(value) {
  running = value;
  for (const id of ["connectionFields", "probeFields", "catalogFields"])
    $(id).disabled = value;
  $("cancelProbe").hidden = !value;
  $("startProbe").hidden = value;
  updateSelection();
}

function eligible(model) {
  return model.supported !== false || $("force").checked;
}

function updateSelection() {
  const count = selected.size;
  $("selectionCount").textContent = count
    ? `匹配 ${visibleModels.length} 个 · 已选 ${count} 个`
    : `匹配 ${visibleModels.length} 个 · 尚未选择`;
  $("startProbe").disabled = !count || running || fetching;
  $("startProbe").innerHTML =
    `开始检测${count ? ` ${count} 个模型` : ""} <span>↗</span>`;
  const available = visibleModels.filter(eligible);
  const checked = available.filter((model) => selected.has(model.id)).length;
  $("selectAll").checked = available.length > 0 && checked === available.length;
  $("selectAll").indeterminate = checked > 0 && checked < available.length;
  $("selectAll").disabled = available.length === 0 || running;
}

function renderModels() {
  $("catalogCount").textContent = models.length;
  if (!visibleModels.length) {
    $("modelList").innerHTML =
      `<div class="empty catalog-empty"><span class="empty-icon">≋</span><h3>${models.length ? "没有匹配的模型" : "先找到要检测的模型"}</h3><p>${models.length ? "调整筛选规则，再试一次。" : "连接 API 获取列表，或手动添加模型名称。"}</p><span class="empty-providers">OpenAI · Anthropic · Gemini · Compatible</span></div>`;
  } else {
    $("modelList").innerHTML = visibleModels
      .map(
        (model) =>
          `<label class="model-row ${selected.has(model.id) ? "selected" : ""}" title="${escapeHtml(model.reason || model.name)}"><input type="checkbox" data-model="${escapeHtml(model.id)}" ${selected.has(model.id) ? "checked" : ""} ${!eligible(model) || running ? "disabled" : ""}><span class="model-name">${escapeHtml(model.id)}</span><span class="model-status">${model.supported === false ? "文本检测不适用" : "待实际验证"}</span></label>`,
      )
      .join("");
  }
  updateSelection();
}

async function applyFilter(reselect = true) {
  const version = ++filterVersion;
  const data = await api("/api/filter", {
    models,
    filter: {
      mode: $("filterMode").value,
      pattern: $("filterPattern").value,
      case_sensitive: $("caseSensitive").checked,
    },
  });
  if (version !== filterVersion) return;
  visibleModels = data.models;
  if (reselect)
    selected = new Set(visibleModels.filter(eligible).map((model) => model.id));
  renderModels();
}

function filterChanged() {
  clearTimeout(filterTimer);
  $("filterHint").textContent =
    $("filterMode").value === "glob"
      ? "* 任意长度 · ? 单个字符 · 例如 *_flash_*"
      : "多个规则换行输入，满足任意一条即可";
  filterTimer = setTimeout(
    () => applyFilter().catch((error) => notice(error.message, true)),
    180,
  );
}

$("providerChoices").addEventListener("click", (event) => {
  const button = event.target.closest("[data-provider]");
  if (!button || running || fetching || provider === button.dataset.provider)
    return;
  provider = button.dataset.provider;
  updateProviderControls();
  $("baseUrl").value = defaults[provider];
  $("apiKey").value = "";
  $("extraHeaders").value = "";
  $("listPath").value = "models";
  $("probePath").value = "";
  $("protocol").value = "default";
  saveConnectionSession();
  models = [];
  visibleModels = [];
  selected.clear();
  ++filterVersion;
  renderModels();
  notice("");
});

$("toggleKey").addEventListener("click", () => {
  const show = $("apiKey").type === "password";
  $("apiKey").type = show ? "text" : "password";
  $("toggleKey").textContent = show ? "隐藏" : "显示";
  $("toggleKey").setAttribute(
    "aria-label",
    `${show ? "隐藏" : "显示"} API Key`,
  );
});

$("fetchModels").addEventListener("click", async () => {
  if (fetching) return;
  try {
    fetching = true;
    $("connectionFields").disabled = true;
    $("fetchModels").textContent = "正在获取完整模型列表…";
    updateSelection();
    notice("");
    const data = await api("/api/models", {
      connection: connection(),
      timeout: Number($("timeout").value),
    });
    models = data.models;
    $("baseUrl").value = data.base_url;
    saveConnectionSession();
    await applyFilter();
    notice(
      `已获取 ${models.length} 个候选模型。列表已完成分页获取，接下来通过真实请求验证。`,
    );
  } catch (error) {
    notice(`${error.message}\n也可以使用手动添加模型继续检测。`, true);
  } finally {
    fetching = false;
    $("connectionFields").disabled = running;
    $("fetchModels").innerHTML = "<span>↓</span> 获取模型列表";
    updateSelection();
  }
});

for (const id of ["filterPattern", "filterMode", "caseSensitive"])
  $(id).addEventListener("input", filterChanged);
$("modelList").addEventListener("change", (event) => {
  const id = event.target.dataset.model;
  if (!id) return;
  if (event.target.checked) selected.add(id);
  else selected.delete(id);
  event.target
    .closest(".model-row")
    .classList.toggle("selected", event.target.checked);
  updateSelection();
});
$("selectAll").addEventListener("change", () => {
  for (const model of visibleModels.filter(eligible)) {
    if ($("selectAll").checked) selected.add(model.id);
    else selected.delete(model.id);
  }
  renderModels();
});
$("force").addEventListener("change", () => {
  if (!$("force").checked)
    for (const model of models.filter((item) => item.supported === false))
      selected.delete(model.id);
  renderModels();
});

async function addModelNames(names) {
  const existing = new Set(models.map((model) => model.id));
  let count = 0;
  for (let name of names) {
    name = String(name).trim();
    if (provider === "google") name = name.replace(/^models\//, "");
    if (name && !existing.has(name)) {
      if (name.length > 512) throw new Error("模型名称不能超过 512 字符");
      models.push({
        id: name,
        name,
        supported: null,
        reason: "手动添加，等待实际验证",
        methods: [],
      });
      existing.add(name);
      count++;
    }
  }
  await applyFilter();
  notice(`已添加 ${count} 个模型，重复名称已自动合并。`);
}

$("addModels").addEventListener("click", async () => {
  try {
    const names = $("manualModels")
      .value.split(/[\n,，]+/)
      .filter((name) => name.trim());
    if (!names.length) throw new Error("请先输入模型名称");
    await addModelNames(names);
    $("manualModels").value = "";
  } catch (error) {
    notice(error.message, true);
  }
});

$("importModels").addEventListener("change", async (event) => {
  try {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 1024 * 1024) throw new Error("模型列表文件不能超过 1 MB");
    const content = await file.text();
    let names;
    if (file.name.toLowerCase().endsWith(".json")) {
      const data = JSON.parse(content);
      const list = Array.isArray(data) ? data : data.models || data.data;
      if (!Array.isArray(list))
        throw new Error("JSON 需要是名称数组，或带 models/data 数组的对象");
      names = list.map((item) =>
        typeof item === "string" ? item : item.id || item.name || "",
      );
    } else names = content.split(/[\n,，]+/);
    await addModelNames(names);
  } catch (error) {
    notice(`导入失败：${error.message}`, true);
  } finally {
    event.target.value = "";
  }
});

// 弹窗读取完整快照，不受主表筛选影响，也不发起新的模型调用。
function renderResultModelsDialog() {
  const status = resultDialogStatus;
  if (!status) return;
  const results = (latestSnapshot?.results || []).filter(
    (result) => result.status === status,
  );
  const label = statusLabels[status];
  const modelCount = new Set(results.map((result) => result.model)).size;
  $("resultModelsTitle").textContent = `${label}的模型`;
  $("resultModelsSummary").textContent =
    `${modelCount} 个模型 · ${results.length} 条调用结果${latestSnapshot?.status === "running" ? " · 检测进行中" : ""}`;
  const list = $("resultModelsList");
  const scrollTop = list.scrollTop;
  const protocolLabels = {
    chat: "Chat Completions",
    responses: "Responses",
    messages: "Messages",
    generateContent: "generateContent",
  };
  list.innerHTML = results.length
    ? results
        .map((result) => {
          const latency =
            result.latency_ms == null
              ? "—"
              : result.latency_ms < 1000
                ? `${result.latency_ms} ms`
                : `${(result.latency_ms / 1000).toFixed(2)} s`;
          const content =
            status === "failed"
              ? result.error || "未提供错误详情"
              : result.text || "未提供回复文本";
          return `<li class="dialog-model-row"><div class="dialog-model-heading"><code class="dialog-model-name">${escapeHtml(result.model)}</code><span class="status ${status}">${escapeHtml(protocolLabels[result.protocol] || result.protocol)}</span></div><p class="dialog-result-meta">HTTP ${escapeHtml(result.http_status ?? "未返回")} · ${escapeHtml(latency)}</p><pre class="dialog-result-text ${status === "failed" ? "result-error" : ""}">${escapeHtml(content)}</pre></li>`;
        })
        .join("")
    : `<li class="empty dialog-empty">暂无${label}的模型${latestSnapshot?.status === "running" ? "，等待检测结果更新。" : "。"}</li>`;
  // 检测结果刷新时保留当前阅读位置，关闭按钮的焦点也保持不变。
  list.scrollTop = scrollTop;
}

const resultModelsDialog = $("resultModelsDialog");
for (const [id, status] of [
  ["successModelsButton", "success"],
  ["failedModelsButton", "failed"],
]) {
  $(id).addEventListener("click", () => {
    resultDialogStatus = status;
    renderResultModelsDialog();
    $("resultModelsList").scrollTop = 0;
    resultModelsDialog.showModal();
    document.body.classList.add("dialog-open");
  });
}
$("closeResultModels").addEventListener("click", () =>
  resultModelsDialog.close(),
);
resultModelsDialog.addEventListener("close", () => {
  resultDialogStatus = null;
  document.body.classList.remove("dialog-open");
});
resultModelsDialog.addEventListener("click", (event) => {
  if (event.target !== resultModelsDialog) return;
  const bounds = resultModelsDialog.getBoundingClientRect();
  // 仅点击弹窗外的遮罩时关闭，弹窗内留白仍可正常点击。
  if (
    event.clientX < bounds.left ||
    event.clientX > bounds.right ||
    event.clientY < bounds.top ||
    event.clientY > bounds.bottom
  ) {
    resultModelsDialog.close();
  }
});

// 刷新结果时保留展开项，避免实时更新打断查看错误详情。
function renderJob(job) {
  latestSnapshot = job;
  const counts = job.counts;
  $("statProgress").innerHTML =
    `${job.completed} <small>/ ${job.total}</small>`;
  $("statSuccess").textContent = counts.success || 0;
  $("statFailed").textContent = counts.failed || 0;
  $("statOther").textContent = (counts.skipped || 0) + (counts.cancelled || 0);
  $("progress").max = job.total || 1;
  $("progress").value = job.completed;
  $("jobBadge").textContent =
    {
      running: "检测进行中",
      completed: "检测完成",
      cancelled: "已取消",
      failed: "任务异常",
    }[job.status] || job.status;
  $("jobBadge").classList.toggle("running", job.status === "running");
  const time = new Date(job.created_at).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
  $("jobMeta").textContent =
    `${providerLabels[job.provider]} · ${time} · ${job.total} 次协议检测`;
  $("exportJson").disabled = $("exportCsv").disabled = false;
  $("resultsEmpty").hidden = true;
  $("resultsTableWrap").hidden = false;
  const expanded = new Set(
    [...document.querySelectorAll("details.result-detail[open]")].map(
      (item) => item.dataset.index,
    ),
  );
  const filter = $("resultFilter").value;
  let shown = 0;
  $("resultsBody").innerHTML = job.results
    .map((result, index) => {
      if (filter !== "all" && result.status !== filter) return "";
      shown++;
      const detail = {
        "HTTP 状态": result.http_status,
        返回模型: result.returned_model || "未提供",
        请求次数: result.attempts,
        用量: result.usage,
      };
      const content =
        result.text ||
        result.error ||
        (result.status === "running"
          ? "正在请求模型…"
          : result.status === "cancelled"
            ? "检测已取消"
            : "等待发送请求");
      const latency =
        result.latency_ms === null
          ? "—"
          : result.latency_ms < 1000
            ? `${result.latency_ms} ms`
            : `${(result.latency_ms / 1000).toFixed(2)} s`;
      return `<tr><td><span class="result-model">${escapeHtml(result.model)}</span><span class="result-protocol">${escapeHtml(result.protocol)}</span></td><td><span class="status ${escapeHtml(result.status)}">${statusLabels[result.status] || escapeHtml(result.status)}</span>${result.error_code ? `<span class="error-label">${escapeHtml(errors[result.error_code] || result.error_code)}</span>` : ""}</td><td>${latency}</td><td><pre class="result-text ${result.error ? "result-error" : ""}">${escapeHtml(content)}</pre>${result.attempts ? `<details class="result-detail" data-index="${index}" ${expanded.has(String(index)) ? "open" : ""}><summary>请求详情</summary><pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre></details>` : ""}</td></tr>`;
    })
    .join("");
  $("noResults").hidden = shown > 0;
  setRunning(job.status === "running");
  if (resultModelsDialog.open) renderResultModelsDialog();
}

async function pollJob(jobId) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (currentJob !== jobId) return;
    renderJob(job);
    if (job.status === "running")
      pollTimer = setTimeout(() => pollJob(jobId), 800);
  } catch (error) {
    if (currentJob !== jobId) return;
    notice(error.message, true);
    if (error.status === 404) {
      setRunning(false);
      writeSessionValue("model-connect-job", null);
      currentJob = null;
    } else pollTimer = setTimeout(() => pollJob(jobId), 2000);
  }
}

$("startProbe").addEventListener("click", async () => {
  try {
    if (selected.size > 1000)
      throw new Error("单次最多检测 1000 个模型，请缩小筛选范围或减少勾选数量");
    $("startProbe").disabled = true;
    notice("");
    const job = await api("/api/jobs", {
      connection: connection(),
      models: [...selected],
      model_details: models.filter((model) => selected.has(model.id)),
      protocol: $("protocol").value,
      prompt: $("prompt").value,
      concurrency: Number($("concurrency").value),
      timeout: Number($("timeout").value),
      max_tokens: Number($("maxTokens").value),
      retries: Number($("retries").value),
      force: $("force").checked,
    });
    currentJob = job.id;
    writeSessionValue("model-connect-job", job.id);
    renderJob(job);
    pollJob(job.id);
  } catch (error) {
    notice(error.message, true);
    updateSelection();
  }
});

$("cancelProbe").addEventListener("click", async () => {
  if (!currentJob) return;
  $("cancelProbe").disabled = true;
  try {
    renderJob(await api(`/api/jobs/${currentJob}/cancel`, {}));
    notice("已取消当前检测，排队和进行中的请求均已停止。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    $("cancelProbe").disabled = false;
  }
});
$("resultFilter").addEventListener("change", () => {
  if (latestSnapshot) renderJob(latestSnapshot);
});
for (const format of ["Json", "Csv"])
  $("export" + format).addEventListener("click", () => {
    if (currentJob)
      window.location.assign(
        `/api/jobs/${currentJob}/export?format=${format.toLowerCase()}`,
      );
  });

async function initialize() {
  restoreConnectionSession();
  try {
    const data = await api("/api/health");
    $("healthLabel").textContent = "本地服务已连接";
    $("healthDot").classList.add("success-dot");
    $("version").textContent = `v${data.version}`;
    currentJob = readSessionValue("model-connect-job");
    if (currentJob) await pollJob(currentJob);
  } catch {
    $("healthLabel").textContent = "本地服务连接失败";
    $("healthDot").classList.add("failure-dot");
  }
}
initialize();
