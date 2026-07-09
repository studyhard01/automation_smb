const state = {
  tools: [],
  sessionId: "",
  history: [],
  openaiApiKey: "",
};

const toolList = document.querySelector("#toolList");
const messages = document.querySelector("#messages");
const chatForm = document.querySelector("#chatForm");
const messageInput = document.querySelector("#messageInput");
const providerInput = document.querySelector("#provider");
const baseUrlInput = document.querySelector("#baseUrl");
const modelInput = document.querySelector("#model");
const modelOptions = document.querySelector("#modelOptions");
const checkLlmButton = document.querySelector("#checkLlm");
const settingsButton = document.querySelector("#settingsButton");
const llmStatus = document.querySelector("#llmStatus");
const reloadTools = document.querySelector("#reloadTools");
const toolLabForm = document.querySelector("#toolLabForm");
const toolInstruction = document.querySelector("#toolInstruction");
const toolDraft = document.querySelector("#toolDraft");
const debugTraceInput = document.querySelector("#debugTrace");
const debugRawLlmInput = document.querySelector("#debugRawLlm");
const providerNotice = ensureProviderNotice();
const settingsDialog = ensureSettingsDialog();
const openaiApiKeyInput = settingsDialog.querySelector("#openaiApiKey");
const saveSettingsButton = settingsDialog.querySelector("#saveSettings");
const clearOpenaiKeyButton = settingsDialog.querySelector("#clearOpenaiKey");

function ensureProviderNotice() {
  let notice = document.querySelector("#providerNotice");
  if (!notice) {
    notice = document.createElement("div");
    notice.id = "providerNotice";
    notice.className = "provider-notice";
    llmStatus.insertAdjacentElement("afterend", notice);
  }
  return notice;
}

function ensureSettingsDialog() {
  let dialog = document.querySelector("#settingsDialog");
  if (dialog) return dialog;

  dialog = document.createElement("dialog");
  dialog.id = "settingsDialog";
  dialog.className = "settings-dialog";
  dialog.innerHTML = `
    <form method="dialog" class="settings-form">
      <header>
        <h2>Provider Settings</h2>
        <button class="icon-button" type="submit" aria-label="close">x</button>
      </header>
      <label>
        OpenAI API key
        <input id="openaiApiKey" type="password" autocomplete="off" placeholder="sk-..." />
      </label>
      <p>
        OpenAI provider를 사용하면 대화, 선택한 tool 설명, tool 실행 결과가 OpenAI API로 전송될 수 있습니다.
        API key는 이 브라우저 탭의 메모리에만 유지하고 저장하지 않습니다.
      </p>
      <footer>
        <button id="clearOpenaiKey" class="secondary-button" type="button">Clear</button>
        <button id="saveSettings" class="secondary-button" type="button">Apply</button>
      </footer>
    </form>
  `;
  document.body.appendChild(dialog);
  return dialog;
}

function selectedToolIds() {
  return [...document.querySelectorAll("[data-tool-id]:checked")].map((item) => item.value);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function limitText(value, limit = 1200) {
  const text = String(value || "");
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...[truncated ${text.length - limit} chars]`;
}

function compactHistory() {
  return state.history.slice(-8).map((item) => ({
    role: item.role,
    content: limitText(item.content, 1000),
  }));
}

function apiHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (providerInput.value === "openai" && state.openaiApiKey) {
    headers["X-Playground-OpenAI-Key"] = state.openaiApiKey;
  }
  return headers;
}

function openSettings() {
  openaiApiKeyInput.value = state.openaiApiKey;
  settingsDialog.showModal();
}

function saveSettings() {
  state.openaiApiKey = openaiApiKeyInput.value.trim();
  settingsDialog.close();
  applyProviderMode();
  if (providerInput.value === "openai") {
    setLlmStatus(state.openaiApiKey ? "OpenAI API key가 이 탭에 설정되었습니다." : "OpenAI API key가 필요합니다.", state.openaiApiKey ? "ok" : "error");
  }
}

function clearOpenaiKey() {
  state.openaiApiKey = "";
  openaiApiKeyInput.value = "";
  applyProviderMode();
  setLlmStatus("OpenAI API key를 지웠습니다.", "error");
}

function setModelOptions(values) {
  modelOptions.innerHTML = "";
  for (const modelName of values || []) {
    const option = document.createElement("option");
    option.value = modelName;
    modelOptions.appendChild(option);
  }
}

function applyProviderMode() {
  if (providerInput.value === "openai") {
    baseUrlInput.value = "https://api.openai.com/v1";
    baseUrlInput.disabled = true;
    baseUrlInput.title = "OpenAI provider는 서버 설정의 OpenAI base URL을 사용합니다.";
    if (!modelInput.value.trim() || modelInput.value.includes(":")) {
      modelInput.value = "gpt-4.1-mini";
    }
    setModelOptions(["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"]);
    providerNotice.textContent =
      "OpenAI provider는 대화와 선택한 tool 결과를 외부 OpenAI API로 보낼 수 있습니다.";
    providerNotice.className = "provider-notice warning";
    return;
  }

  baseUrlInput.disabled = false;
  baseUrlInput.title = "";
  if (baseUrlInput.value === "https://api.openai.com/v1") {
    baseUrlInput.value = "http://127.0.0.1:11434/v1";
  }
  if (modelInput.value.startsWith("gpt-")) {
    modelInput.value = "";
  }
  providerNotice.textContent = "";
  providerNotice.className = "provider-notice";
}

function addMessage(role, text, options = {}) {
  const el = document.createElement("article");
  el.className = `message ${role}${options.error ? " error" : ""}`;
  el.textContent = text;

  if (options.traces?.length) {
    el.appendChild(renderToolTrace(options.traces));
  }
  if (options.agentSteps?.length) {
    el.appendChild(renderAgentTrace(options.agentSteps));
  }
  if (options.debug?.llm_calls?.length) {
    el.appendChild(renderRawDebug(options.debug.llm_calls));
  }

  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function renderToolTrace(traces) {
  const traceBox = document.createElement("div");
  traceBox.className = "trace";
  traceBox.innerHTML = traces
    .map((trace) => {
      const status = trace.error_code ? `${trace.status} / ${trace.error_code}` : trace.status;
      return `<div><strong>${escapeHtml(trace.tool_name)}</strong> ${escapeHtml(status)}, ${escapeHtml(
        trace.elapsed_ms
      )}ms<br>${escapeHtml(trace.result_text || "")}</div>`;
    })
    .join("");
  return traceBox;
}

function renderAgentTrace(steps) {
  const box = document.createElement("div");
  box.className = "agent-trace";
  const title = document.createElement("div");
  title.className = "trace-title";
  title.textContent = "Agent trace";
  box.appendChild(title);

  for (const step of steps) {
    const item = document.createElement("div");
    item.className = `agent-step ${step.kind || ""}`;
    const heading = document.createElement("div");
    heading.className = "agent-step-heading";
    heading.textContent = `#${step.step} ${step.title || step.kind || "step"}${
      step.elapsed_ms ? ` · ${step.elapsed_ms}ms` : ""
    }`;
    const detail = document.createElement("pre");
    detail.textContent = limitText(
      [
        step.action ? `action: ${step.action}` : "",
        step.tool_name || step.tool_id ? `tool: ${step.tool_name || step.tool_id}` : "",
        step.status ? `status: ${step.status}` : "",
        step.error_code ? `error: ${step.error_code}` : "",
        step.detail || "",
      ]
        .filter(Boolean)
        .join("\n"),
      2000
    );
    item.appendChild(heading);
    item.appendChild(detail);
    box.appendChild(item);
  }
  return box;
}

function renderRawDebug(calls) {
  const details = document.createElement("details");
  details.className = "raw-debug";
  const summary = document.createElement("summary");
  summary.textContent = `Raw LLM debug (${calls.length})`;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(calls, null, 2);
  details.appendChild(summary);
  details.appendChild(pre);
  return details;
}

async function loadTools() {
  toolList.textContent = "tool 목록을 불러오는 중...";
  const response = await fetch("/api/playground/tools");
  if (!response.ok) {
    toolList.textContent = `tool 목록 로딩 실패: HTTP ${response.status}`;
    return;
  }
  state.tools = await response.json();
  toolList.innerHTML = "";
  for (const tool of state.tools) {
    const item = document.createElement("section");
    item.className = `tool-item${tool.enabled ? "" : " disabled"}`;
    item.innerHTML = `
      <div class="tool-title">
        <label>
          <input data-tool-id="${escapeHtml(tool.id)}" type="checkbox" value="${escapeHtml(tool.id)}"
            ${tool.default_selected ? "checked" : ""} ${tool.enabled ? "" : "disabled"} />
          <span>${escapeHtml(tool.display_name)}</span>
        </label>
        <span class="badge ${tool.permission === "admin" ? "admin" : ""}">${escapeHtml(tool.permission)}</span>
      </div>
      <p>${escapeHtml(tool.description)}</p>
      <p>${tool.enabled ? `${tool.timeout_ms}ms` : "비활성화"}</p>
    `;
    toolList.appendChild(item);
  }
}

async function sendChat(message) {
  if (providerInput.value === "openai" && !state.openaiApiKey) {
    addMessage("assistant", "OpenAI API key를 Settings에서 입력하세요.", { error: true });
    openSettings();
    return;
  }
  const payload = {
    message,
    selected_tool_ids: selectedToolIds(),
    session_id: state.sessionId,
    history: compactHistory(),
    provider: providerInput.value,
    local_base_url: baseUrlInput.value.trim(),
    model: modelInput.value.trim(),
    debug_trace: debugTraceInput.checked,
    debug_raw_llm: debugRawLlmInput.checked,
  };
  const response = await fetch("/api/playground/chat", {
    method: "POST",
    headers: apiHeaders(),
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    addMessage("assistant", JSON.stringify(data, null, 2), { error: true });
    return;
  }
  state.sessionId = data.session_id || state.sessionId;
  const warnings = data.warnings?.length ? `\n\n주의: ${data.warnings.join(", ")}` : "";
  const budget = data.over_budget ? "\n\n주의: agent 시간 예산을 초과했습니다." : "";
  addMessage("assistant", `${data.assistant_message}${warnings}${budget}`, {
    traces: data.tool_calls,
    agentSteps: data.agent_steps,
    debug: data.debug,
    error: Boolean(data.error_code),
  });
  state.history.push({ role: "user", content: message });
  state.history.push({ role: "assistant", content: data.assistant_message || "" });
  state.history = state.history.slice(-12);
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message) return;
  messageInput.value = "";
  addMessage("user", message);
  const pending = addMessage("assistant", "처리 중...");
  try {
    await sendChat(message);
  } finally {
    pending.remove();
  }
});

toolLabForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const instruction = toolInstruction.value.trim();
  if (!instruction) return;
  if (providerInput.value === "openai" && !state.openaiApiKey) {
    toolDraft.textContent = "OpenAI API key를 Settings에서 입력하세요.";
    openSettings();
    return;
  }
  toolDraft.textContent = "초안 생성 중...";
  const response = await fetch("/api/playground/tool-draft", {
    method: "POST",
    headers: apiHeaders(),
    body: JSON.stringify({
      instruction,
      provider: providerInput.value,
      local_base_url: baseUrlInput.value.trim(),
      model: modelInput.value.trim(),
    }),
  });
  const data = await response.json();
  toolDraft.textContent = JSON.stringify(data, null, 2);
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    const target = tab.dataset.tab === "toolLab" ? "#toolLabView" : "#chatView";
    document.querySelector(target).classList.add("active");
  });
});

function setLlmStatus(text, status = "") {
  llmStatus.textContent = text;
  llmStatus.className = `status-line ${status}`.trim();
}

async function checkLlm() {
  if (providerInput.value === "openai" && !state.openaiApiKey) {
    setLlmStatus("OpenAI API key를 Settings에서 입력하세요.", "error");
    openSettings();
    return;
  }
  setLlmStatus("local LLM 확인 중...");
  const response = await fetch("/api/playground/llm-status", {
    method: "POST",
    headers: apiHeaders(),
    body: JSON.stringify({
      provider: providerInput.value,
      local_base_url: baseUrlInput.value.trim(),
      model: modelInput.value.trim(),
    }),
  });
  const data = await response.json();
  if (providerInput.value === "openai") {
    setModelOptions(["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"]);
  } else {
    setModelOptions(data.available_models || []);
  }
  if (!modelInput.value.trim() && data.available_models?.length) {
    modelInput.value = data.available_models[0];
  }
  if (data.chat_ok) {
    setLlmStatus(`${data.model_used} 연결 정상 (${data.elapsed_ms}ms)`, "ok");
    return;
  }
  const message = data.message || data.error_code || `HTTP ${response.status}`;
  setLlmStatus(message, "error");
}

checkLlmButton.addEventListener("click", checkLlm);
reloadTools.addEventListener("click", loadTools);
providerInput.addEventListener("change", applyProviderMode);
settingsButton?.addEventListener("click", openSettings);
saveSettingsButton.addEventListener("click", saveSettings);
clearOpenaiKeyButton.addEventListener("click", clearOpenaiKey);
applyProviderMode();
loadTools();
