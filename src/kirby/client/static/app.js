"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  url: "", samples: {}, agents: [], role: "", generation: 0,
  task: null, events: [], tasks: [], nextPage: "", observation: null,
  reading: false, canceling: false, listing: false, request: null,
  fileOptions: null, attachments: [],
};
const terminal = new Set(["completed", "failed", "canceled", "rejected"]);
const labels = {
  submitted: "접수됨", working: "실행 중", completed: "완료", failed: "실패",
  canceled: "취소됨", rejected: "거절됨", input_required: "입력 필요",
  auth_required: "인증 필요", unknown: "확인 필요", idle: "대기",
};
const encoder = new TextEncoder();
const statusOf = (task) => (task?.status?.state || "idle").replace(/^TASK_STATE_/, "").toLowerCase();
const isTerminal = (task) => terminal.has(statusOf(task));
const endpoint = (role, suffix) => `/api/agents/${encodeURIComponent(role)}${suffix}`;
const taskEndpoint = (role, id) => endpoint(role, `/tasks/${encodeURIComponent(id)}`);
const pretty = (value) => JSON.stringify(value, null, 2);
const current = (role, generation) => role === state.role && generation === state.generation;
const fileTypes = { ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv", ".json": "application/json", ".jsonl": "application/x-ndjson" };
const formatSize = (size) => size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MiB` : size >= 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${size} B`;

function notify(message, kind = "info") {
  $("notice").textContent = message;
  $("notice").dataset.kind = kind;
  $("notice").hidden = !message;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-KIRBY-Client": "1", ...options.headers },
    signal: options.signal || AbortSignal.timeout(30000),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).error || message; } catch { /* Use HTTP status. */ }
    throw new Error(message);
  }
  return response;
}

async function json(path, options) {
  return (await request(path, options)).json();
}

function badge(value) {
  const span = document.createElement("span");
  span.className = "state-badge";
  span.dataset.state = value;
  span.textContent = labels[value] || value;
  return span;
}

function updateControls() {
  const busy = Boolean(state.observation) || state.reading;
  const available = Boolean(state.role);
  const activeTask = Boolean(state.task?.id) && !isTerminal(state.task);
  const uploading = state.attachments.some((file) => ["queued", "uploading", "verifying"].includes(file.status));
  const filesReady = state.attachments.every((file) => file.status === "ready");
  $("role-select").disabled = busy || state.canceling || !state.agents.length;
  $("send-button").disabled = busy || state.canceling || !available || !filesReady;
  $("send-button").firstElementChild.textContent = state.observation ? "요청 처리 중" : "요청 보내기";
  for (const id of ["input-text", "context-id", "message-id", "reset-button"]) $(id).disabled = busy;
  document.querySelectorAll('input[name="mode"]').forEach((input) => { input.disabled = busy; });
  $("sample-button").disabled = busy || !state.samples[state.role];
  $("continue-button").disabled = busy || statusOf(state.task) !== "completed";
  $("get-button").disabled = busy || !state.task?.id;
  $("subscribe-button").disabled = busy || !activeTask;
  $("cancel-button").disabled = !activeTask || state.canceling;
  $("cancel-button").textContent = state.canceling ? "취소 요청 중…" : "작업 취소";
  $("download-button").disabled = !state.task;
  $("refresh-button").disabled = state.listing || !available;
  $("filter-button").disabled = state.listing || !available;
  $("more-button").disabled = state.listing;
  document.querySelectorAll(".task-row").forEach((row) => { row.disabled = busy || state.canceling; });
  $("observation-row").hidden = !state.observation;
  $("file-input").disabled = busy || uploading || !state.fileOptions?.enabled || state.attachments.length >= (state.fileOptions?.max_attachments || 4);
  document.querySelectorAll(".attachment-remove").forEach((button) => { button.disabled = busy; });
}

function renderAttachments() {
  const container = $("attachment-list");
  container.replaceChildren();
  $("file-count").textContent = `${state.attachments.length} / ${state.fileOptions?.max_attachments || 4}`;
  for (const file of state.attachments) {
    const row = document.createElement("div");
    row.className = "attachment-row";
    row.dataset.state = file.status;
    const details = document.createElement("div");
    details.className = "attachment-details";
    const name = document.createElement("strong");
    name.textContent = file.filename;
    const status = document.createElement("span");
    status.textContent = `${formatSize(file.size)} · ${{ queued: "대기 중", uploading: `업로드 ${file.progress}%`, verifying: "파일 확인 중", ready: "첨부 준비 완료", error: file.error || "업로드 실패" }[file.status]}`;
    details.append(name, status);
    if (["queued", "uploading", "verifying"].includes(file.status)) {
      const progress = document.createElement("progress");
      progress.max = 100;
      progress.value = file.progress;
      progress.setAttribute("aria-label", `${file.filename} 업로드 진행률`);
      details.append(progress);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button attachment-remove";
    remove.textContent = "제거";
    remove.setAttribute("aria-label", `${file.filename} 첨부 제거`);
    remove.addEventListener("click", () => {
      file.controller?.abort();
      file.xhr?.abort();
      state.attachments = state.attachments.filter((item) => item !== file);
      renderAttachments();
    });
    row.append(details, remove);
    container.append(row);
  }
  updateControls();
}

function clearAttachments() {
  for (const file of state.attachments) {
    file.controller?.abort();
    file.xhr?.abort();
  }
  state.attachments = [];
  $("file-input").value = "";
  renderAttachments();
}

function directUpload(upload, selected, entry, isCurrent) {
  // The browser streams its File directly to MinIO. Never read it into a string
  // or send its bytes through the credential bridge or the KIRBY API.
  return new Promise((resolve, reject) => {
    const url = new URL(upload.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || upload.method !== "POST") {
      reject(new Error("지원하지 않는 업로드 주소입니다."));
      return;
    }
    const form = new FormData();
    for (const [key, value] of Object.entries(upload.fields)) form.append(key, value);
    form.append("file", selected, selected.name);
    const xhr = new XMLHttpRequest();
    entry.xhr = xhr;
    xhr.open("POST", url.href);
    xhr.timeout = 300000;
    xhr.upload.onprogress = (event) => {
      if (!isCurrent() || !event.lengthComputable) return;
      entry.progress = Math.min(100, Math.round(event.loaded / event.total * 100));
      renderAttachments();
    };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`저장소 업로드 HTTP ${xhr.status}`));
    xhr.onerror = () => reject(new Error("저장소에 연결하지 못했습니다. 업로드 주소와 CORS 설정을 확인하세요."));
    xhr.ontimeout = () => reject(new Error("파일 업로드 시간이 초과되었습니다."));
    xhr.onabort = () => reject(new DOMException("Upload canceled", "AbortError"));
    xhr.send(form);
  });
}

async function attachFiles(event) {
  const selected = [...event.target.files];
  event.target.value = "";
  if (!state.fileOptions?.enabled || state.observation || state.reading) return;
  const options = state.fileOptions;
  if (selected.length + state.attachments.length > options.max_attachments) {
    notify(`첨부는 대화당 최대 ${options.max_attachments}개까지 사용할 수 있습니다.`, "error");
    return;
  }
  const allowed = options.allowed_extensions;
  for (const file of selected) {
    const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (!allowed.includes(extension) || file.size <= 0 || file.size > options.max_file_bytes) {
      notify(`${file.name}: ${allowed.join(", ")} 형식의 UTF-8 파일을 ${formatSize(options.max_file_bytes)} 이하로 선택하세요.`, "error");
      return;
    }
  }
  const role = state.role;
  const generation = state.generation;
  const entries = selected.map((file) => ({ filename: file.name, size: file.size, status: "queued", progress: 0, controller: new AbortController() }));
  state.attachments.push(...entries);
  notify("");
  renderAttachments();
  // One upload at a time keeps memory and object-store traffic bounded.
  for (const [index, entry] of entries.entries()) {
    const isCurrent = () => current(role, generation) && state.attachments.includes(entry) && !entry.controller.signal.aborted;
    if (!isCurrent()) continue;
    const file = selected[index];
    const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    try {
      entry.status = "uploading";
      renderAttachments();
      const reserved = await json(endpoint(role, "/files"), {
        method: "POST", signal: entry.controller.signal,
        body: JSON.stringify({ filename: file.name, media_type: fileTypes[extension], size: file.size }),
      });
      if (!isCurrent()) continue;
      await directUpload(reserved.upload, file, entry, isCurrent);
      if (!isCurrent()) continue;
      entry.status = "verifying";
      entry.progress = 100;
      renderAttachments();
      const complete = await json(endpoint(role, `/files/${encodeURIComponent(reserved.id)}/complete`), { method: "POST", signal: entry.controller.signal });
      if (!isCurrent()) continue;
      entry.part = complete.part;
      entry.status = "ready";
    } catch (error) {
      if (!isCurrent()) continue;
      entry.status = "error";
      entry.error = error.message;
      notify(`${entry.filename}: ${error.message} 첨부를 제거한 뒤 다시 선택하세요.`, "error");
    } finally {
      entry.xhr = null;
      if (current(role, generation)) renderAttachments();
    }
  }
}

function updateInput() {
  const bytes = encoder.encode($("input-text").value).length;
  $("input-size").textContent = bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
  $("context-indicator").textContent = $("context-id").value.trim() ? "기존 대화" : "새 대화";
}

function addEvent(kind, data) {
  state.events.push({ time: new Date().toISOString(), kind, data });
  // Bound the local viewer/report; this is a snapshot client, not an event archive.
  if (state.events.length > 300) state.events.shift();
  $("event-count").textContent = state.events.length;
  const container = $("event-log");
  container.replaceChildren();
  for (const entry of state.events) {
    const item = document.createElement("div");
    item.className = "event-item";
    const label = document.createElement("div");
    label.className = "event-label";
    label.textContent = `${new Date(entry.time).toLocaleTimeString("ko-KR", { hour12: false })}  ·  ${entry.kind}`;
    const pre = document.createElement("pre");
    pre.textContent = pretty(entry.data);
    item.append(label, pre);
    container.append(item);
  }
  container.scrollTop = container.scrollHeight;
}

function clearResult() {
  state.task = null;
  state.events = [];
  state.request = null;
  $("event-count").textContent = "0";
  const empty = document.createElement("p");
  empty.className = "log-empty";
  empty.textContent = "아직 수신한 이벤트가 없습니다.";
  $("event-log").replaceChildren(empty);
  $("elapsed").hidden = true;
  renderTask();
}

function renderTask() {
  const task = state.task;
  const status = statusOf(task);
  $("task-empty").hidden = Boolean(task);
  $("task-details").hidden = !task;
  $("task-state").dataset.state = status;
  $("task-state").textContent = labels[status] || status;
  if (task) {
    $("task-id").textContent = task.id || "접수 확인 중";
    $("task-context").textContent = task.contextId || "—";
    document.querySelectorAll(".task-progress [data-step]").forEach((step, index) => {
      step.classList.toggle("active", index === 0 || (index === 1 && status !== "submitted") || (index === 2 && isTerminal(task)));
    });
    const messages = {
      submitted: "작업이 접수되었습니다. Worker 실행을 기다리고 있습니다.",
      working: "Worker가 에이전트를 실행하고 있습니다.",
      completed: "작업이 완료되었습니다. 같은 대화로 후속 요청을 보낼 수 있습니다.",
      failed: "작업이 실패했습니다. 실패한 Context는 새 대화로 다시 시작하세요.",
      canceled: "작업 취소가 확인되었습니다. 다음 요청은 새 대화로 시작하세요.",
    };
    $("task-message").textContent = task.metadata?.error
      ? `${messages[status] || "작업 상태를 확인하세요."} (${task.metadata.error})`
      : messages[status] || "상태 조회로 작업의 최신 상태를 확인하세요.";
    $("task-message").dataset.error = String(["failed", "rejected"].includes(status));
    const artifacts = task.artifacts || [];
    $("artifact-count").textContent = artifacts.length;
    $("artifacts").replaceChildren();
    if (!artifacts.length) {
      const placeholder = document.createElement("div");
      placeholder.className = "result-placeholder";
      placeholder.textContent = isTerminal(task) ? "이 작업에는 결과 Artifact가 없습니다." : "결과를 기다리고 있습니다.";
      $("artifacts").append(placeholder);
    }
    artifacts.forEach((artifact, index) => {
      const box = document.createElement("div");
      box.className = "artifact";
      const label = document.createElement("div");
      label.className = "artifact-label";
      label.textContent = artifact.name || `Artifact ${index + 1} · ${artifact.artifactId || "result"}`;
      box.append(label);
      for (const part of artifact.parts || []) {
        let downloadUrl;
        try {
          const url = new URL(part.url);
          if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) downloadUrl = url.href;
        } catch { /* Non-file parts use the normal JSON/text view. */ }
        if (downloadUrl) {
          const link = document.createElement("a");
          link.className = "artifact-download";
          link.href = downloadUrl;
          link.download = part.filename || "download";
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          const size = Number(part.metadata?.size);
          link.textContent = `${part.filename || "파일 다운로드"}${Number.isFinite(size) && size > 0 ? ` · ${formatSize(size)}` : ""} ↓`;
          const help = document.createElement("p");
          help.className = "field-help";
          help.textContent = "링크가 만료되면 ‘상태 조회’를 눌러 새 다운로드 링크를 받으세요.";
          box.append(link, help);
        } else {
          const pre = document.createElement("pre");
          if (typeof part.text === "string") {
            try { pre.textContent = pretty(JSON.parse(part.text)); } catch { pre.textContent = part.text; }
          } else pre.textContent = pretty(part);
          box.append(pre);
        }
      }
      $("artifacts").append(box);
    });
  }
  renderList();
  updateControls();
}

function applyEvent(event) {
  if (event.error) throw new Error(event.error);
  const kind = ["task", "statusUpdate", "artifactUpdate", "message"].find((key) => event[key]) || "event";
  addEvent(kind, event);
  if (event.task && !(state.task?.id === event.task.id && isTerminal(state.task) && !isTerminal(event.task))) state.task = event.task;
  if (event.statusUpdate) {
    const update = event.statusUpdate;
    if (!(state.task?.id === update.taskId && isTerminal(state.task) && !isTerminal({ status: update.status }))) {
      state.task = { ...state.task, id: update.taskId, contextId: update.contextId, status: update.status };
    }
  }
  if (event.artifactUpdate) {
    const update = event.artifactUpdate;
    const artifacts = [...(state.task?.artifacts || [])];
    const index = artifacts.findIndex((artifact) => artifact.artifactId === update.artifact.artifactId);
    if (index < 0) artifacts.push(update.artifact);
    else if (update.append) artifacts[index] = { ...artifacts[index], parts: [...(artifacts[index].parts || []), ...(update.artifact.parts || [])] };
    else artifacts[index] = update.artifact;
    state.task = { ...state.task, id: update.taskId, contextId: update.contextId, artifacts };
  }
  renderTask();
}

async function readStream(path, options, operation) {
  const response = await request(path, { ...options, signal: operation.controller.signal });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (!current(operation.role, operation.generation) || operation.controller.signal.aborted) return;
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) applyEvent(JSON.parse(line));
      if (done) {
        if (buffer.trim()) applyEvent(JSON.parse(buffer));
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (state.task?.id && isTerminal(state.task)) {
    // Get includes terminal metadata and the complete artifact snapshot.
    state.task = await json(taskEndpoint(operation.role, state.task.id), { signal: operation.controller.signal });
    addEvent("get", state.task);
    renderTask();
  } else {
    throw new Error("작업 종료 전에 스트림이 닫혔습니다. 상태 조회 또는 구독으로 확인하세요.");
  }
}

function delay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const aborted = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", aborted); resolve(); }, milliseconds);
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) aborted();
  });
}

async function observe(label, work) {
  if (state.observation) return;
  const operation = { role: state.role, generation: state.generation, controller: new AbortController(), stopped: false, started: Date.now() };
  state.observation = operation;
  notify("");
  $("observation-text").textContent = label;
  $("elapsed").hidden = false;
  const tick = () => { $("elapsed").textContent = `${((Date.now() - operation.started) / 1000).toFixed(1)}s`; };
  tick();
  const clock = setInterval(tick, 100);
  const timeout = setTimeout(() => operation.controller.abort(new Error("최대 수신 시간 6분을 초과했습니다.")), 360000);
  updateControls();
  try {
    await work(operation);
  } catch (error) {
    if (current(operation.role, operation.generation) && !operation.stopped) {
      const message = operation.controller.signal.aborted ? operation.controller.signal.reason?.message || "연결이 중단되었습니다." : error.message;
      addEvent("observation-error", { error: message });
      notify(`수신이 중단되었습니다. ${message} 작업은 서버에서 계속될 수 있으므로 최근 작업에서 확인하세요.`, "error");
    }
  } finally {
    clearTimeout(timeout);
    clearInterval(clock);
    if (state.observation === operation) state.observation = null;
    updateControls();
    if (current(operation.role, operation.generation)) await refreshList();
  }
}

function stopObservation() {
  const operation = state.observation;
  if (!operation) return;
  operation.stopped = true;
  operation.controller.abort();
  notify("화면의 수신만 중지했습니다. 작업은 서버에서 계속됩니다. 작업을 끝내려면 ‘작업 취소’를 누르세요.");
}

async function send(event) {
  event.preventDefault();
  if (!state.role || state.observation || state.reading || state.canceling) return;
  if (state.attachments.some((file) => file.status !== "ready")) return;
  const text = $("input-text").value;
  if (!text.trim()) { $("input-text").focus(); return; }
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const body = {
    text, context_id: $("context-id").value.trim(),
    message_id: $("message-id").value.trim() || crypto.randomUUID(), immediate: mode === "poll",
    attachments: state.attachments.map((file) => file.part),
  };
  clearResult();
  state.request = { role: state.role, mode, ...body };
  const label = mode === "stream" ? "스트림 수신 중" : mode === "poll" ? "2초마다 상태 확인 중" : "완료 응답 대기 중";
  await observe(label, async (operation) => {
    addEvent("send", { role: operation.role, mode, contextId: body.context_id, messageId: body.message_id });
    const options = { method: "POST", body: JSON.stringify(body), signal: operation.controller.signal };
    if (mode === "stream") {
      await readStream(endpoint(operation.role, "/stream"), options, operation);
      return;
    }
    const response = await json(endpoint(operation.role, "/send"), options);
    applyEvent(response);
    while (mode === "poll" && state.task?.id && !isTerminal(state.task)) {
      await delay(2000, operation.controller.signal);
      state.task = await json(taskEndpoint(operation.role, state.task.id), { signal: operation.controller.signal });
      addEvent("get", state.task);
      renderTask();
    }
  });
}

function renderList() {
  $("history-count").textContent = state.tasks.length;
  $("more-button").hidden = !state.nextPage;
  const container = $("task-list");
  container.replaceChildren();
  if (!state.tasks.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = state.listing ? "작업을 불러오는 중…" : "이 조건에 해당하는 작업이 없습니다.";
    container.append(empty);
  }
  for (const task of state.tasks) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `task-row${task.id === state.task?.id ? " selected" : ""}`;
    row.disabled = Boolean(state.observation) || state.reading || state.canceling;
    row.setAttribute("aria-label", `${labels[statusOf(task)] || statusOf(task)} 작업 ${task.id} 조회`);
    const main = document.createElement("span");
    main.className = "task-row-main";
    const id = document.createElement("span");
    id.className = "task-row-id";
    id.textContent = task.id;
    const context = document.createElement("span");
    context.className = "task-row-context";
    context.textContent = `Context · ${task.contextId || "—"}`;
    main.append(id, context);
    const time = document.createElement("span");
    time.className = "task-row-time";
    time.textContent = task.status?.timestamp ? new Date(task.status.timestamp).toLocaleTimeString("ko-KR", { hour12: false }) : "";
    row.append(main, time, badge(statusOf(task)));
    row.addEventListener("click", () => getTask(task.id, true));
    container.append(row);
  }
}

async function refreshList(append = false) {
  if (!state.role || state.listing) return;
  const role = state.role;
  const generation = state.generation;
  state.listing = true;
  updateControls();
  const params = new URLSearchParams({ context_id: $("history-context").value.trim() });
  if (append && state.nextPage) params.set("page_token", state.nextPage);
  try {
    const result = await json(endpoint(role, `/tasks?${params}`));
    if (!current(role, generation)) return;
    state.tasks = append ? [...state.tasks, ...(result.tasks || [])] : result.tasks || [];
    state.nextPage = result.nextPageToken || "";
  } catch (error) {
    if (current(role, generation)) notify(`작업 목록을 가져오지 못했습니다. ${error.message}`, "error");
  } finally {
    if (current(role, generation)) { state.listing = false; renderList(); updateControls(); }
  }
}

async function getTask(id, select = false) {
  if (!id || state.observation || state.reading) return;
  const role = state.role;
  const generation = state.generation;
  state.reading = true;
  updateControls();
  try {
    const task = await json(taskEndpoint(role, id));
    if (!current(role, generation)) return;
    if (select) clearResult();
    if (!(isTerminal(state.task) && !isTerminal(task))) state.task = task;
    addEvent("get", task);
    notify("");
    renderTask();
  } catch (error) {
    if (current(role, generation)) notify(`작업을 조회하지 못했습니다. ${error.message}`, "error");
  } finally {
    state.reading = false;
    updateControls();
  }
}

async function cancelTask() {
  if (!state.task?.id || state.canceling || isTerminal(state.task)) return;
  const role = state.role;
  const generation = state.generation;
  const id = state.task.id;
  state.canceling = true;
  updateControls();
  try {
    const task = await json(`${taskEndpoint(role, id)}/cancel`, { method: "POST" });
    if (!current(role, generation) || state.task?.id !== id) return;
    if (!(isTerminal(state.task) && !isTerminal(task))) state.task = task;
    addEvent("cancel", task);
    renderTask();
    notify(statusOf(task) === "canceled" ? "작업 취소가 확인되었습니다." : isTerminal(task) ? "작업이 종료되었습니다. 최종 상태를 확인하세요." : "취소 요청을 보냈습니다. 아직 취소가 확정되지 않았습니다. 상태 조회 또는 구독으로 확인하세요.");
  } catch (error) {
    if (current(role, generation)) notify(`취소 요청을 확인하지 못했습니다. ${error.message} 상태 조회로 확인하세요.`, "error");
  } finally {
    state.canceling = false;
    updateControls();
  }
}

async function selectRole() {
  if (state.observation) stopObservation();
  state.generation += 1;
  state.role = $("role-select").value;
  state.fileOptions = null;
  clearAttachments();
  $("file-help").textContent = "파일 전송 설정을 확인합니다.";
  state.tasks = [];
  state.nextPage = "";
  state.listing = false;
  $("context-id").value = "";
  $("message-id").value = "";
  $("history-context").value = "";
  $("input-text").value = "";
  $("agent-card-json").textContent = "불러오는 중…";
  $("role-description").textContent = "역할 설명을 불러오는 중입니다.";
  clearResult();
  updateInput();
  notify("");
  const role = state.role;
  const generation = state.generation;
  await Promise.all([
    (async () => {
      try {
        const card = await json(endpoint(role, "/card"));
        if (!current(role, generation)) return;
        $("agent-card-json").textContent = pretty(card);
        $("role-description").textContent = card.description || card.name || role;
      } catch (error) {
        if (!current(role, generation)) return;
        $("agent-card-json").textContent = error.message;
        $("role-description").textContent = "역할 설명을 가져오지 못했습니다.";
        notify(`Agent Card를 가져오지 못했습니다. ${error.message}`, "error");
      }
    })(),
    (async () => {
      try {
        const options = await json(endpoint(role, "/files"));
        if (!current(role, generation)) return;
        state.fileOptions = options;
        $("file-help").textContent = options.enabled
          ? `UTF-8 ${options.allowed_extensions.join(", ")} · 파일당 ${formatSize(options.max_file_bytes)} · 대화당 최대 ${options.max_attachments}개. 후속 대화에서는 이전 첨부가 유지됩니다.`
          : "이 서버에는 파일 전송이 설정되지 않았습니다.";
        if (options.enabled) $("file-input").accept = options.allowed_extensions.join(",");
        renderAttachments();
      } catch {
        if (!current(role, generation)) return;
        $("file-help").textContent = "파일 전송 설정을 가져오지 못했습니다. 텍스트 요청은 계속 사용할 수 있습니다.";
      }
    })(),
    refreshList(),
  ]);
}

$("request-form").addEventListener("submit", send);
$("input-text").addEventListener("input", updateInput);
$("context-id").addEventListener("input", updateInput);
$("role-select").addEventListener("change", selectRole);
$("file-input").addEventListener("change", attachFiles);
$("sample-button").addEventListener("click", () => {
  $("input-text").value = state.samples[state.role] || "";
  updateInput();
  $("input-text").focus();
});
$("reset-button").addEventListener("click", () => {
  clearAttachments();
  $("context-id").value = "";
  $("message-id").value = "";
  updateInput();
  notify("새 대화로 요청을 보냅니다. 첨부를 비웠으며 메시지 내용은 그대로 유지합니다.");
});
$("continue-button").addEventListener("click", () => {
  if (statusOf(state.task) !== "completed") return;
  clearAttachments();
  $("context-id").value = state.task.contextId;
  $("message-id").value = "";
  $("input-text").value = "이전 분석 결과를 다시 검토해 주세요. 같은 근거 ID와 결과 스키마를 유지해 주세요.";
  document.querySelector(".context-options").open = true;
  updateInput();
  $("input-text").focus();
  notify("현재 작업의 Context ID를 입력했습니다. 후속 메시지를 작성해 보내세요.");
});
document.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener("change", () => {
  $("mode-help").textContent = {
    stream: "연결을 유지하며 작업 상태와 결과를 받습니다.",
    poll: "작업을 접수한 뒤 2초마다 상태를 조회합니다. 최대 6분간 확인합니다.",
    blocking: "완료 응답을 기다립니다. 수신을 멈춘 뒤 최근 작업에서 취소할 수 있습니다.",
  }[input.value];
}));
$("stop-button").addEventListener("click", stopObservation);
$("get-button").addEventListener("click", () => getTask(state.task?.id));
$("cancel-button").addEventListener("click", cancelTask);
$("subscribe-button").addEventListener("click", () => {
  if (!state.task?.id || isTerminal(state.task) || state.observation) return;
  const id = state.task.id;
  observe("작업 상태 구독 중", (operation) => readStream(`${taskEndpoint(operation.role, id)}/subscribe`, {}, operation));
});
$("refresh-button").addEventListener("click", () => refreshList());
$("filter-button").addEventListener("click", () => refreshList());
$("history-context").addEventListener("keydown", (event) => { if (event.key === "Enter") refreshList(); });
$("more-button").addEventListener("click", () => refreshList(true));
$("download-button").addEventListener("click", () => {
  if (!state.task) return;
  const report = { exportedAt: new Date().toISOString(), apiUrl: state.url, role: state.role, request: state.request, task: state.task, events: state.events };
  const url = URL.createObjectURL(new Blob([pretty(report) + "\n"], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `kirby-${state.task.id || "task"}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function start() {
  try {
    const [config, catalog] = await Promise.all([json("/api/config"), json("/api/agents")]);
    state.url = config.url;
    state.samples = config.samples || {};
    state.agents = catalog.agents || [];
    $("target-url").textContent = state.url;
    $("role-select").replaceChildren();
    for (const agent of state.agents) {
      const option = document.createElement("option");
      option.value = agent.id;
      option.textContent = `${agent.name || agent.id} · ${agent.id}`;
      $("role-select").append(option);
    }
    $("connection").dataset.state = "connected";
    $("connection-text").textContent = "API 연결됨";
    if (!state.agents.length) {
      notify("이 인증 정보로 접근할 수 있는 에이전트가 없습니다.", "error");
      $("role-description").textContent = "서버 인증 정보의 역할 설정을 확인하세요.";
      updateControls();
      return;
    }
    await selectRole();
  } catch (error) {
    $("connection").dataset.state = "error";
    $("connection-text").textContent = "연결 확인 필요";
    $("role-description").textContent = "API 연결과 클라이언트 서버 설정을 확인하세요.";
    notify(`API에 연결하지 못했습니다. ${error.message} 서버 설정을 확인한 뒤 페이지를 새로고침하세요.`, "error");
  }
}

start();
