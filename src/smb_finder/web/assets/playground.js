const state = {
  tools: [],
  sessionId: "",
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
const llmStatus = document.querySelector("#llmStatus");
const reloadTools = document.querySelector("#reloadTools");
const toolLabForm = document.querySelector("#toolLabForm");
const toolInstruction = document.querySelector("#toolInstruction");
const toolDraft = document.querySelector("#toolDraft");

function selectedToolIds() {
  return [...document.querySelectorAll("[data-tool-id]:checked")].map((item) => item.value);
}

function addMessage(role, text, options = {}) {
  const el = document.createElement("article");
  el.className = `message ${role}${options.error ? " error" : ""}`;
  el.textContent = text;
  if (options.traces?.length) {
    const traceBox = document.createElement("div");
    traceBox.className = "trace";
    traceBox.innerHTML = options.traces
      .map((trace) => {
        const status = trace.error_code ? `${trace.status} / ${trace.error_code}` : trace.status;
        return `<div><strong>${trace.tool_name}</strong> ${status}, ${trace.elapsed_ms}ms<br>${escapeHtml(
          trace.result_text || ""
        )}</div>`;
      })
      .join("");
    el.appendChild(traceBox);
  }
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
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
  const payload = {
    message,
    selected_tool_ids: selectedToolIds(),
    session_id: state.sessionId,
    provider: providerInput.value,
    local_base_url: baseUrlInput.value.trim(),
    model: modelInput.value.trim(),
  };
  const response = await fetch("/api/playground/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    addMessage("assistant", JSON.stringify(data, null, 2), { error: true });
    return;
  }
  state.sessionId = data.session_id || state.sessionId;
  const warnings = data.warnings?.length ? `\n\n주의: ${data.warnings.join(", ")}` : "";
  addMessage("assistant", `${data.assistant_message}${warnings}`, {
    traces: data.tool_calls,
    error: Boolean(data.error_code),
  });
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message) return;
  messageInput.value = "";
  addMessage("user", message);
  addMessage("assistant", "처리 중...");
  const pending = messages.lastElementChild;
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
  toolDraft.textContent = "초안 생성 중...";
  const response = await fetch("/api/playground/tool-draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  setLlmStatus("local LLM 확인 중...");
  const response = await fetch("/api/playground/llm-status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: providerInput.value,
      local_base_url: baseUrlInput.value.trim(),
      model: modelInput.value.trim(),
    }),
  });
  const data = await response.json();
  modelOptions.innerHTML = "";
  for (const modelName of data.available_models || []) {
    const option = document.createElement("option");
    option.value = modelName;
    modelOptions.appendChild(option);
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
loadTools();
