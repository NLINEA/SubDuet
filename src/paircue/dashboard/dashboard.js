"use strict";

const byId = (id) => document.getElementById(id);
let token = "";
let pollTimer = null;

function authorization() {
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...authorization(), ...(options.headers || {}) },
  });
  if (!response.ok) {
    throw new Error(response.status === 401 ? "That dashboard token is not valid." : "SubDuet could not answer.");
  }
  return response.json();
}

function setRunning(state, label) {
  byId("running-state").dataset.state = state;
  byId("running-label").textContent = label;
}

function resultRow(item) {
  const row = document.createElement("li");
  const name = document.createElement("span");
  const status = document.createElement("span");
  const message = document.createElement("span");
  const time = document.createElement("time");
  name.className = "result-name";
  status.className = "result-status";
  message.className = "result-message";
  time.className = "result-time";
  name.textContent = item.media_name;
  status.textContent = item.status;
  status.dataset.status = item.status;
  message.textContent = item.message;
  const timestamp = new Date(item.updated_at);
  time.dateTime = item.updated_at;
  time.textContent = Number.isNaN(timestamp.valueOf()) ? "" : timestamp.toLocaleString();
  row.append(name, status, message, time);
  return row;
}

function renderStatus(payload) {
  byId("working-count").textContent = String(payload.pending);
  byId("completed-count").textContent = String(payload.results.completed || 0);
  byId("failed-count").textContent = String(payload.results.failed || 0);
  const list = byId("recent-list");
  list.replaceChildren(...payload.recent.map(resultRow));
  const hasRecent = payload.recent.length > 0;
  list.hidden = !hasRecent;
  byId("empty-state").hidden = hasRecent;
  byId("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const alert = byId("scan-alert");
  const scanFailed = payload.scan_status === "error";
  alert.hidden = !scanFailed;
  alert.textContent = scanFailed ? payload.scan_message : "";
  if (scanFailed) {
    setRunning("error", "Library needs attention");
  } else {
    setRunning("running", payload.pending ? "SubDuet is working" : "SubDuet is running");
  }
}

async function refresh() {
  try {
    renderStatus(await request("/v1/status"));
  } catch (error) {
    setRunning("error", error.message);
  }
}

async function connect(candidate) {
  token = candidate.trim();
  if (!token) {
    throw new Error("Enter the private dashboard token.");
  }
  const context = await request("/v1/dashboard-context");
  byId("platform-label").textContent = `${context.platform.toUpperCase()} LIBRARY`;
  byId("pair-label").textContent = `${context.source_language} dialogue → ${context.target_language} learning subtitles`;
  byId("desktop-controls").hidden = !context.desktop;
  byId("connect-panel").hidden = true;
  byId("dashboard").hidden = false;
  await refresh();
  window.clearInterval(pollTimer);
  pollTimer = window.setInterval(refresh, 2500);
}

byId("connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = byId("connect-error");
  error.hidden = true;
  try {
    await connect(byId("dashboard-token").value);
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  }
});

byId("scan-now").addEventListener("click", async () => {
  const button = byId("scan-now");
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const payload = await request("/v1/scan", { method: "POST" });
    button.textContent = payload.message;
    await refresh();
  } catch (error) {
    button.textContent = error.message;
    await refresh();
  }
  window.setTimeout(() => {
    button.disabled = false;
    button.textContent = "Scan library now";
  }, 1600);
});

async function desktopAction(action) {
  window.clearInterval(pollTimer);
  const payload = await request(`/v1/desktop/${action}`, { method: "POST" });
  setRunning("waiting", payload.message);
  byId("scan-now").disabled = true;
}

byId("stop-paircue").addEventListener("click", () => desktopAction("stop"));
byId("edit-settings").addEventListener("click", () => desktopAction("edit"));

const fragmentToken = new URLSearchParams(window.location.hash.slice(1)).get("token");
if (fragmentToken) {
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  connect(fragmentToken).catch((error) => {
    byId("connect-error").textContent = error.message;
    byId("connect-error").hidden = false;
  });
}
