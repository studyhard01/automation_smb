const state = {
  tools: [],
  sessionId: "",
  history: [],
  openaiApiKey: "",
};

const toolCategories = [
  {
    id: "smb",
    name: "SMB 직접 접근",
    description: "공유 폴더와 파일 내용을 직접 탐색합니다.",
  },
  {
    id: "database",
    name: "DB 접근",
    description: "로컬 PostgreSQL에서 vector 기반 근거 chunk를 찾습니다.",
  },
  {
    id: "report",
    name: "보고서 관련",
    description: "핵형 분석과 보고서 작성용 도구를 모아 봅니다.",
  },
];

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
const emptyState = document.querySelector("#emptyState");
const settingsDialog = ensureSettingsDialog();
const openaiApiKeyInput = settingsDialog.querySelector("#openaiApiKey");
const saveSettingsButton = settingsDialog.querySelector("#saveSettings");
const clearOpenaiKeyButton = settingsDialog.querySelector("#clearOpenaiKey");

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
        API key는 현재 브라우저 탭의 메모리에만 유지되며 새로고침하면 사라집니다.
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
  return [...document.querySelectorAll("[data-tool-id]:checked:not(:disabled)")].map((item) => item.value);
}

function executionTypeOf(tool) {
  return tool.execution_type === "code" || tool.execution_type === "llm" ? tool.execution_type : null;
}

function applyToolAvailability() {
  for (const tool of state.tools) {
    const input = toolList.querySelector(`[data-tool-id="${CSS.escape(tool.id)}"]`);
    if (!input) continue;

    const item = input.closest(".tool-item");
    const executionTypeKnown = executionTypeOf(tool) !== null;
    const selectable = tool.enabled === true && executionTypeKnown;

    input.disabled = !selectable;
    if (!selectable) input.checked = false;
    item.classList.toggle("disabled", !selectable);
  }
  updateCategorySummaries();
}

function updateCategorySummaries() {
  for (const category of toolCategories) {
    const categoryElement = toolList.querySelector(`[data-tool-category="${category.id}"]`);
    if (!categoryElement) continue;
    const allTools = [...categoryElement.querySelectorAll("[data-tool-id]")];
    const selected = allTools.filter((input) => input.checked && !input.disabled).length;
    const count = categoryElement.querySelector("[data-category-count]");
    if (count) count.textContent = selected ? `${selected}/${allTools.length} 선택` : `${allTools.length}개 도구`;
    categoryElement.classList.toggle("has-selection", selected > 0);
  }
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
    applyToolAvailability();
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
  applyToolAvailability();
}

function addMessage(role, text, options = {}) {
  emptyState?.remove();
  const el = document.createElement("article");
  el.className = `message ${role}${options.error ? " error" : ""}`;
  if (role === "assistant" && options.structured) {
    el.classList.add("structured");
    const answerSection = renderMessageSection("LLM 답변", text);
    if (Number.isFinite(Number(options.elapsedMs))) {
      answerSection.appendChild(renderRunMetrics(options.elapsedMs, options.overBudget));
    }
    if (options.tokenUsage) {
      answerSection.appendChild(renderTokenUsage(options.tokenUsage));
    }
    el.appendChild(answerSection);
    if (options.traces?.length) {
      el.appendChild(renderMessageSection("도구 실행 결과", renderToolTrace(options.traces)));
    }
    if (options.agentSteps?.length) {
      el.appendChild(renderMessageSection("Agent 요청 흐름", renderAgentTrace(options.agentSteps)));
    }
  } else {
    el.textContent = text;
    if (options.traces?.length) {
      el.appendChild(renderToolTrace(options.traces));
    }
    if (options.tokenUsage) {
      el.appendChild(renderTokenUsage(options.tokenUsage));
    }
    if (options.agentSteps?.length) {
      el.appendChild(renderAgentTrace(options.agentSteps));
    }
  }
  if (options.debug?.llm_calls?.length) {
    el.appendChild(renderRawDebug(options.debug.llm_calls));
  }
  if (role === "assistant" && options.requestId) {
    const footer = document.createElement("footer");
    footer.className = "message-request-id";
    footer.textContent = `요청 ID: ${options.requestId}`;
    el.appendChild(footer);
  }

  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function renderMessageSection(title, content) {
  const section = document.createElement("section");
  section.className = "message-section";
  const heading = document.createElement("h3");
  heading.className = "message-section-title";
  heading.textContent = title;
  section.appendChild(heading);

  if (content instanceof Node) {
    section.appendChild(content);
  } else {
    const body = document.createElement("div");
    body.className = "message-section-body";
    body.textContent = content || "";
    section.appendChild(body);
  }
  return section;
}

function formatTokenUsage(usage) {
  if (!usage) return "";
  const total = Number(usage.total_tokens || 0);
  const prompt = Number(usage.prompt_tokens || 0);
  const completion = Number(usage.completion_tokens || 0);
  const calls = Number(usage.calls || 0);
  if (!total && !prompt && !completion) return "";
  return `${usage.provider || "llm"} / ${usage.model || "model"} · input ${prompt} · output ${completion} · total ${total} · calls ${calls}`;
}

function renderTokenUsage(usage) {
  const box = document.createElement("div");
  box.className = "token-usage";
  box.textContent = `Token usage: ${formatTokenUsage(usage)}`;
  return box;
}

function renderRunMetrics(elapsedMs, overBudget) {
  const box = document.createElement("div");
  box.className = "run-metrics";

  const elapsed = document.createElement("span");
  elapsed.textContent = `응답 ${Number(elapsedMs).toFixed(1)}ms`;
  box.appendChild(elapsed);

  const budget = document.createElement("span");
  budget.className = `budget-status ${overBudget ? "over" : "within"}`;
  budget.textContent = overBudget ? "시간 예산 초과" : "시간 예산 이내";
  box.appendChild(budget);
  return box;
}

function renderToolTrace(traces) {
  const traceBox = document.createElement("div");
  traceBox.className = "trace";
  for (const trace of traces) {
    const item = document.createElement("div");
    const status = trace.error_code ? `${trace.status} / ${trace.error_code}` : trace.status;
    item.innerHTML = `<strong>${escapeHtml(trace.tool_name)}</strong> ${escapeHtml(status)}, ${escapeHtml(
      trace.elapsed_ms
    )}ms`;
    const pre = document.createElement("pre");
    pre.textContent = trace.result_text || (trace.result_payload ? JSON.stringify(trace.result_payload, null, 2) : "");
    item.appendChild(pre);
    traceBox.appendChild(item);
  }
  return traceBox;
}

function renderAgentTrace(steps) {
  const box = document.createElement("div");
  box.className = "agent-trace";

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
  toolList.innerHTML = '<div class="tool-list-status">도구 목록을 불러오는 중...</div>';
  try {
    const response = await fetch("/api/playground/tools");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    state.tools = await response.json();
    toolList.innerHTML = "";
    for (const category of toolCategories) {
      const categoryTools = state.tools.filter((tool) => tool.category === category.id);
      if (!categoryTools.length) continue;
      const panelId = `tool-category-${category.id}`;
      const categoryElement = document.createElement("section");
      categoryElement.className = "tool-category";
      categoryElement.dataset.toolCategory = category.id;
      categoryElement.innerHTML = `
        <button class="tool-category-toggle" type="button" aria-expanded="false" aria-controls="${panelId}">
          <span class="tool-category-copy">
            <strong>${escapeHtml(category.name)}</strong>
            <small>${escapeHtml(category.description)}</small>
          </span>
          <span class="tool-category-side">
            <span class="tool-category-count" data-category-count>${categoryTools.length}개 도구</span>
            <span class="tool-category-chevron" aria-hidden="true">⌄</span>
          </span>
        </button>
        <div id="${panelId}" class="tool-category-content" hidden></div>
      `;
      const categoryContent = categoryElement.querySelector(".tool-category-content");
      for (const tool of categoryTools) {
        const executionType = executionTypeOf(tool);
        const executionClass = executionType || "unknown";
        const executionLabel = executionType === "code" ? "코드" : executionType === "llm" ? "LLM" : "미확인";
        const selectable = tool.enabled === true && executionType !== null;
        const item = document.createElement("section");
        item.className = `tool-item${selectable ? "" : " disabled"}`;
        item.innerHTML = `
          <div class="tool-title">
            <label>
              <input data-tool-id="${escapeHtml(tool.id)}" type="checkbox" value="${escapeHtml(tool.id)}"
                ${tool.default_selected && selectable ? "checked" : ""} ${selectable ? "" : "disabled"} />
              <span>${escapeHtml(tool.display_name)}</span>
            </label>
            <div class="tool-badges">
              <span class="badge execution-${executionClass}" title="도구 실행 방식: ${executionLabel}"
                aria-label="도구 실행 방식: ${executionLabel}">${executionLabel}</span>
            </div>
          </div>
          <p>${escapeHtml(tool.description)}</p>
          <p class="tool-meta">${selectable ? `제한 시간 ${escapeHtml(tool.timeout_ms)}ms` : "현재 실행할 수 없는 도구"}</p>
        `;
        categoryContent.appendChild(item);
      }
      const toggle = categoryElement.querySelector(".tool-category-toggle");
      toggle.addEventListener("click", () => {
        const expanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!expanded));
        categoryContent.hidden = expanded;
        categoryElement.classList.toggle("expanded", !expanded);
      });
      toolList.appendChild(categoryElement);
    }
    if (!state.tools.length) {
      toolList.innerHTML = '<div class="tool-list-status">등록된 도구가 없습니다.</div>';
    }
    toolList.querySelectorAll("[data-tool-id]").forEach((input) => {
      input.addEventListener("change", updateCategorySummaries);
    });
    applyToolAvailability();
  } catch (error) {
    toolList.innerHTML = `<div class="tool-list-status error">도구 목록을 불러오지 못했습니다. ${escapeHtml(
      error.message
    )}</div>`;
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
    const errorMessage = data.message || data.detail || data.error_code || `HTTP ${response.status}`;
    addMessage("assistant", `요청 실패: ${errorMessage}`, { error: true, requestId: data.request_id });
    return;
  }
  state.sessionId = data.session_id || state.sessionId;
  addMessage("assistant", data.assistant_message, {
    structured: true,
    traces: data.tool_calls,
    tokenUsage: data.token_usage,
    agentSteps: data.agent_steps,
    debug: data.debug,
    error: Boolean(data.error_code),
    requestId: data.request_id,
    elapsedMs: data.elapsed_ms,
    overBudget: data.over_budget,
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
  } catch (error) {
    addMessage("assistant", `요청 중 오류가 발생했습니다: ${error.message}`, { error: true });
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
  try {
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
    toolDraft.textContent = response.ok
      ? JSON.stringify(data, null, 2)
      : `초안 생성 실패: ${data.message || data.detail || data.error_code || `HTTP ${response.status}`}`;
  } catch (error) {
    toolDraft.textContent = `초안 생성 중 오류가 발생했습니다: ${error.message}`;
  }
});

document.querySelectorAll("[data-example-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    messageInput.value = button.dataset.examplePrompt || "";
    messageInput.focus();
  });
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
  const providerLabel = providerInput.value === "openai" ? "OpenAI" : "local LLM";
  setLlmStatus(`${providerLabel} 연결 확인 중...`);
  try {
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
      const usage = formatTokenUsage(data.token_usage);
      setLlmStatus(`${data.model_used} 연결 정상 (${data.elapsed_ms}ms)${usage ? ` · ${usage}` : ""}`, "ok");
      return;
    }
    const message = data.message || data.error_code || `HTTP ${response.status}`;
    setLlmStatus(message, "error");
  } catch (error) {
    setLlmStatus(`연결 확인 실패: ${error.message}`, "error");
  }
}

checkLlmButton.addEventListener("click", checkLlm);
reloadTools.addEventListener("click", loadTools);
providerInput.addEventListener("change", applyProviderMode);
settingsButton?.addEventListener("click", openSettings);
saveSettingsButton.addEventListener("click", saveSettings);
clearOpenaiKeyButton.addEventListener("click", clearOpenaiKey);
applyProviderMode();
loadTools();
