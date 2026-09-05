"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("setup-form");
const preview = byId("config-preview");
const formError = byId("form-error");
const actionStatus = byId("action-status");
const apiToken = randomToken();
const setupToken = readSetupToken();
let desktopApp = false;

const platformNames = {
  plex: "Plex",
  jellyfin: "Jellyfin",
  emby: "Emby",
  filesystem: "Other players",
};

const secretIds = new Set([
  "server-token",
  "opensubtitles-key",
  "translation-key",
  "transcription-key",
]);

const aiApprovals = { translation: "", transcription: "" };

function aiSelection(prefix) {
  return JSON.stringify([value(`${prefix}-provider`), value(`${prefix}-url`)]);
}

function aiDescription(prefix) {
  try {
    return aiConnections.describe(value(`${prefix}-provider`), value(`${prefix}-url`));
  } catch {
    return null;
  }
}

function approvedAI(prefix) {
  return checked(`${prefix}-enabled`) && checked(`${prefix}-confirm`)
    && aiApprovals[prefix] === aiSelection(prefix) ? aiDescription(prefix) : null;
}

function clearAIApproval(prefix) {
  byId(`${prefix}-key`).value = "";
  byId(`${prefix}-confirm`).checked = false;
  aiApprovals[prefix] = "";
}

function updateAIControls(prefix) {
  const enabled = checked(`${prefix}-enabled`);
  const provider = value(`${prefix}-provider`);
  const description = aiDescription(prefix);
  byId(`${prefix}-provider`).disabled = !enabled;
  byId(`${prefix}-url`).disabled = !enabled || !provider;
  byId(`${prefix}-model`).disabled = !enabled || !provider;
  byId(`${prefix}-confirm`).disabled = !enabled || !description;
  byId(`${prefix}-key`).disabled = !approvedAI(prefix);
  const data = prefix === "translation" ? "Subtitle text" : "Audio segments";
  byId(`${prefix}-destination`).textContent = description
    ? `${data} and any key you enter will be sent to ${description.origin}. Nothing is sent until you start a job.`
    : "Choose a provider and a valid endpoint before adding a key.";
}

function aiConfig(prefix, maskSecrets) {
  const connection = aiDescription(prefix);
  const approved = approvedAI(prefix);
  const setting = `PAIRCUE_${prefix.toUpperCase()}`;
  return [
    configLine(`${setting}_ENABLED`, String(checked(`${prefix}-enabled`))),
    configLine(`${setting}_PROVIDER`, value(`${prefix}-provider`) || "custom"),
    configLine(`${setting}_BASE_URL`, connection?.baseUrl || ""),
    configLine(`${setting}_APPROVED_ORIGIN`, approved?.origin || ""),
    configLine(`${setting}_API_KEY`, approved ? secretValue(`${prefix}-key`, maskSecrets) : ""),
    configLine(`${setting}_MODEL`, value(`${prefix}-model`)),
  ];
}

function selectedPlatform() {
  return form.querySelector('input[name="platform"]:checked').value;
}

function selectedMode() {
  return form.querySelector('input[name="mode"]:checked').value;
}

function selectedPlatformName() {
  return platformNames[selectedPlatform()];
}

function value(id) {
  return byId(id).value.trim();
}

function checked(id) {
  return byId(id).checked;
}

function quote(raw) {
  if (/[\u0000-\u001F]/.test(raw)) {
    throw new Error("Configuration values cannot contain control characters.");
  }
  return JSON.stringify(raw);
}

function randomToken() {
  const bytes = new Uint8Array(36);
  crypto.getRandomValues(bytes);
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function readSetupToken() {
  if (!window.location.protocol.startsWith("http")) {
    return "";
  }
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  if (token) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  return token;
}

function authorizedHeaders(extra = {}) {
  return { ...extra, Authorization: `Bearer ${setupToken}` };
}

function secretValue(id, maskSecrets) {
  const raw = value(id);
  if (!maskSecrets || !raw) {
    return raw;
  }
  return "•••••••• (saved in file)";
}

function configLine(name, raw) {
  return `${name}=${quote(raw)}`;
}

function buildConfig(maskSecrets = false) {
  const mode = selectedMode();
  const platform = selectedPlatform();
  const lines = ["# Generated locally by SubDuet Setup. Keep this file private."];
  if (mode === "library") {
    const hostMediaPath = value("host-media-path").replace(/\/+$/, "") || "/";
    const torrentPath = hostMediaPath === "/" ? "/Torrents" : `${hostMediaPath}/Torrents`;
    lines.push(
      configLine("MEDIA_PATH", hostMediaPath),
      configLine("TORRENT_WATCH_PATH", torrentPath),
      configLine("PAIRCUE_PORT", value("host-port")),
      configLine("PUID", value("puid")),
      configLine("PGID", value("pgid")),
      "",
    );
  }
  lines.push(
    configLine("PAIRCUE_PLATFORM", platform),
    configLine("PAIRCUE_MEDIA_ROOT", mode === "library" ? "/media" : "."),
    configLine("PAIRCUE_STATE_DIR", mode === "library" ? "/state" : ".paircue-state"),
    configLine("PAIRCUE_SCAN_INTERVAL_SECONDS", "1800"),
  );

  if (mode === "library" && platform !== "filesystem") {
    lines.push(
      configLine("PAIRCUE_SERVER_URL", value("server-url")),
      configLine("PAIRCUE_SERVER_TOKEN", secretValue("server-token", maskSecrets)),
      configLine("PAIRCUE_SERVER_PATH_PREFIX", value("server-prefix")),
    );
    if (platform === "jellyfin" || platform === "emby") {
      lines.push(configLine("PAIRCUE_SERVER_USER_ID", value("server-user-id")));
    }
  }

  lines.push(
    "",
    "# The learning pair",
    configLine("PAIRCUE_SOURCE_LANGUAGE", value("source-language")),
    configLine("PAIRCUE_TARGET_LANGUAGE", value("target-language")),
    configLine("PAIRCUE_TARGET_LANGUAGE_STYLE", value("target-style")),
    configLine("PAIRCUE_BILINGUAL_ORDER", value("line-order")),
    configLine("PAIRCUE_SYNC_ENABLED", "true"),
    configLine("PAIRCUE_CLEAN_SOURCE_OUTPUT", "false"),
    "",
    "# Exact-release subtitle search",
    configLine("PAIRCUE_SUBTITLE_DOWNLOAD_ENABLED", String(checked("search-enabled"))),
    configLine(
      "PAIRCUE_OPENSUBTITLES_API_KEY",
      secretValue("opensubtitles-key", maskSecrets),
    ),
    "",
    "# Translation",
    ...aiConfig("translation", maskSecrets),
    configLine("PAIRCUE_TRANSLATION_DISABLE_THINKING", String(value("translation-provider") === "zai")),
    configLine(
      "PAIRCUE_TRANSLATION_FINAL_CHECK_ENABLED",
      String(checked("translation-final-check-enabled")),
    ),
    "",
    "# Speech transcription fallback",
    ...aiConfig("transcription", maskSecrets),
    "",
    "# Local-only service access",
    configLine("PAIRCUE_WEBHOOK_ENABLED", "false"),
    configLine("PAIRCUE_API_HOST", mode === "library" ? "0.0.0.0" : "127.0.0.1"),
    configLine("PAIRCUE_API_PORT", "9292"),
    configLine("PAIRCUE_API_TOKEN", maskSecrets ? "•••••••• (generated in download)" : apiToken),
    configLine("PAIRCUE_TRUSTED_HOSTS", "localhost,127.0.0.1"),
  );
  return `${lines.join("\n")}\n`;
}

function updateMode() {
  const library = selectedMode() === "library";
  byId("library-options").hidden = !library;
  byId("single-note").hidden = library;
  byId("quick-pair-card").hidden = !desktopApp;
  byId("continue-journey").textContent = library
    ? "Set up library automation"
    : "Set up one video";
  byId("details-summary").textContent =
    `${selectedPlatformName()} · ${library ? "Library automation" : "One video"}`;
  byId("language-step-number").textContent = library ? "4" : "3";
  byId("automation-step-number").textContent = library ? "5" : "4";
  byId("download-config").textContent = library
    ? (desktopApp ? "Save and open dashboard" : "Save paircue.env")
    : "Save and choose a video";
  updatePlatform();
  updateNextStep();
}

function setProgress(stage) {
  const stages = ["platform", "start", "details"];
  const activeIndex = stages.indexOf(stage);
  stages.forEach((name, index) => {
    const item = byId(`progress-${name}`);
    item.toggleAttribute("data-complete", index < activeIndex);
    if (index === activeIndex) {
      item.setAttribute("aria-current", "step");
    } else {
      item.removeAttribute("aria-current");
    }
  });
}

function focusStage(headingId) {
  const heading = byId(headingId);
  heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: true });
  heading.scrollIntoView({ block: "start" });
}

function showPlatformStage() {
  byId("quick-feedback").hidden = true;
  byId("next-feedback").hidden = true;
  byId("platform-step").hidden = false;
  byId("platform-picker").hidden = false;
  byId("journey-stage").hidden = true;
  byId("details-stage").hidden = true;
  byId("next-step").hidden = true;
  byId("platform-step-number").textContent = "1";
  setProgress("platform");
  focusStage("platform-heading");
}

function showJourneyStage() {
  byId("platform-step").hidden = false;
  byId("platform-picker").hidden = true;
  byId("journey-stage").hidden = false;
  byId("details-stage").hidden = true;
  byId("next-step").hidden = true;
  byId("platform-step-number").textContent = "2";
  byId("platform-summary").textContent = `${selectedPlatformName()} selected`;
  updateMode();
  setProgress("start");
  focusStage("journey-heading");
}

function showDetailsStage() {
  byId("platform-step").hidden = true;
  byId("details-stage").hidden = false;
  byId("next-step").hidden = false;
  updateMode();
  setProgress("details");
  focusStage(selectedMode() === "library" ? "library-heading" : "language-heading");
}

function updateSubtitlePreset() {
  const preset = form.querySelector('input[name="subtitle-preset"]:checked').value;
  const choices = {
    both: { search: false, translation: false, transcription: false },
    one: { search: false, translation: true, transcription: false },
    automatic: { search: true, translation: true, transcription: true },
  };
  byId("search-enabled").checked = choices[preset].search;
  byId("translation-enabled").checked = choices[preset].translation;
  byId("transcription-enabled").checked = choices[preset].transcription;
  setPanelEnabled("search-enabled", "search-panel");
  setPanelEnabled("translation-enabled", "translation-panel");
  setPanelEnabled("transcription-enabled", "transcription-panel");
}

function setPanelEnabled(toggleId, panelId) {
  const enabled = checked(toggleId);
  const panel = byId(panelId);
  panel.hidden = !enabled;
  panel.querySelectorAll("input, select").forEach((input) => {
    input.disabled = !enabled;
  });
  if (toggleId === "translation-enabled" || toggleId === "transcription-enabled") {
    const prefix = toggleId.replace("-enabled", "");
    if (!enabled) clearAIApproval(prefix);
    updateAIControls(prefix);
  }
}

function updatePlatform() {
  const platform = selectedPlatform();
  const isFolder = selectedMode() === "single" || platform === "filesystem";
  const needsUser = platform === "jellyfin" || platform === "emby";
  byId("server-fields").hidden = isFolder;
  byId("user-id-field").hidden = !needsUser;
  byId("server-url").disabled = isFolder;
  byId("server-token").disabled = isFolder;
  byId("server-prefix").disabled = isFolder;
  byId("server-user-id").disabled = !needsUser;
  byId("continue-platform").textContent = `Continue with ${selectedPlatformName()}`;
  byId("platform-summary").textContent = `${selectedPlatformName()} selected`;
  ["plex", "jellyfin", "emby"].forEach((name) => {
    byId(`${name}-help`).hidden = platform !== name;
  });
  if (!isFolder) {
    const defaults = {
      plex: "http://127.0.0.1:32400",
      jellyfin: "http://127.0.0.1:8096",
      emby: "http://127.0.0.1:8096",
    };
    const knownDefaults = new Set([
      "http://plex:32400",
      "http://jellyfin:8096",
      "http://emby:8096",
      ...Object.values(defaults),
    ]);
    if (!value("server-url") || knownDefaults.has(value("server-url"))) {
      byId("server-url").value = defaults[platform];
    }
  }
}

function clearValidity() {
  form.querySelectorAll("input, select").forEach((input) => input.setCustomValidity(""));
  formError.hidden = true;
  formError.textContent = "";
}

function requireField(id, message) {
  const input = byId(id);
  if (!input.disabled && !value(id)) {
    input.setCustomValidity(message);
    return false;
  }
  return true;
}

function validateAI(prefix) {
  if (!checked(`${prefix}-enabled`)) return true;
  let valid = true;
  if (!value(`${prefix}-provider`)) {
    byId(`${prefix}-provider`).setCustomValidity("Choose your provider before entering a key.");
    valid = false;
  }
  valid = requireField(`${prefix}-url`, "Enter your provider's endpoint.") && valid;
  valid = requireField(`${prefix}-model`, "Enter the model name from your provider.") && valid;
  let description;
  try {
    description = aiConnections.describe(value(`${prefix}-provider`), value(`${prefix}-url`));
  } catch (error) {
    byId(`${prefix}-url`).setCustomValidity(error.message);
    return false;
  }
  if (!approvedAI(prefix)) {
    byId(`${prefix}-confirm`).setCustomValidity("Confirm the displayed destination first.");
    valid = false;
  }
  if (!description.local) {
    valid = requireField(`${prefix}-key`, "Add the key for this provider, or use local AI.") && valid;
  }
  return valid;
}

function validate() {
  clearValidity();
  let valid = true;
  const mode = selectedMode();
  const platform = selectedPlatform();
  if (mode === "library") {
    valid = requireField("host-media-path", "Enter the media folder on this machine or NAS.") && valid;
    valid = requireField("host-port", "Enter the local status page port.") && valid;
    valid = requireField("puid", "Enter the container user ID.") && valid;
    valid = requireField("pgid", "Enter the container group ID.") && valid;
    const port = Number(value("host-port"));
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      byId("host-port").setCustomValidity("Use a port between 1 and 65535.");
      valid = false;
    }
  }
  if (mode === "library" && platform !== "filesystem") {
    valid = requireField("server-url", "Enter the server address.") && valid;
    valid = requireField("server-token", "Enter the server token or API key.") && valid;
    valid = requireField("server-prefix", "Enter the library path seen by the server.") && valid;
  }
  if (mode === "library" && (platform === "jellyfin" || platform === "emby")) {
    valid = requireField("server-user-id", "Enter the user ID.") && valid;
  }
  valid = requireField("source-language", "Enter the spoken language.") && valid;
  valid = requireField("target-language", "Enter the learning language.") && valid;
  valid = requireField("target-style", "Describe the subtitle writing style.") && valid;
  const languageTag = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$/;
  if (!languageTag.test(value("source-language"))) {
    byId("source-language").setCustomValidity("Use a language tag such as en, ja, or zh-HK.");
    valid = false;
  }
  if (!languageTag.test(value("target-language"))) {
    byId("target-language").setCustomValidity("Use a language tag such as en, ja, or zh-HK.");
    valid = false;
  }
  if (value("source-language").toLowerCase() === value("target-language").toLowerCase()) {
    byId("target-language").setCustomValidity("Choose two different languages.");
    valid = false;
  }
  if (checked("search-enabled")) {
    valid = requireField("opensubtitles-key", "Add your OpenSubtitles API key or disable search.") && valid;
  }
  valid = validateAI("translation") && valid;
  valid = validateAI("transcription") && valid;
  if (!valid || !form.checkValidity()) {
    formError.textContent = "Check the highlighted fields, then try again.";
    formError.hidden = false;
    form.reportValidity();
    return false;
  }
  return true;
}

function updatePreview() {
  try {
    preview.textContent = buildConfig(true);
  } catch (error) {
    preview.textContent = `Configuration preview unavailable: ${error.message}`;
  }
}

function updateNextStep() {
  const library = selectedMode() === "library";
  byId("next-step").removeAttribute("data-phase");
  byId("next-number").textContent = "NEXT";
  byId("next-link").hidden = true;
  byId("next-feedback").hidden = true;
  if (library) {
    if (desktopApp) {
      byId("next-heading").textContent = "Your dashboard opens next";
      byId("next-copy").textContent =
        "SubDuet stays running on this device, scans the library, and shows every result visually.";
      byId("next-command").textContent = "No Docker or terminal command required.";
    } else {
      byId("next-heading").textContent = "Start the library service";
      byId("next-copy").textContent = "Put paircue.env beside docker-compose.yml, then run:";
      byId("next-command").textContent = [
        "docker compose --env-file paircue.env build core",
        "docker compose --env-file paircue.env run --rm core subduet doctor",
        "docker compose --env-file paircue.env up -d core",
      ].join("\n");
    }
    return;
  }
  byId("next-heading").textContent = "Try one video";
  byId("next-copy").textContent =
    "After saving, SubDuet opens your system file chooser. Pick one video and it starts for you.";
  byId("next-command").textContent = "Later: subduet learn --config paircue.env";
}

function configForAction() {
  if (!validate()) {
    return null;
  }
  try {
    return buildConfig(false);
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
    return null;
  }
}

async function copyConfig() {
  const config = configForAction();
  if (config === null) {
    return;
  }
  try {
    await navigator.clipboard.writeText(config);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = config;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.append(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
  actionStatus.textContent = "Configuration copied. Keep it somewhere private.";
}

function downloadConfigFile(config) {
  const url = URL.createObjectURL(new Blob([config], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "paircue.env";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  actionStatus.textContent = "paircue.env downloaded. Do not commit this file to GitHub.";
}

async function saveConfig() {
  const config = configForAction();
  if (config === null) {
    return;
  }
  if (!window.location.protocol.startsWith("http") || !setupToken) {
    downloadConfigFile(config);
    return;
  }
  const button = byId("download-config");
  button.disabled = true;
  try {
    if (desktopApp && selectedMode() === "library") {
      button.textContent = "Checking platform…";
      actionStatus.textContent = `Connecting to ${selectedPlatform()}…`;
      const testResponse = await fetch("/test-platform", {
        method: "POST",
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ config, mode: selectedMode() }),
      });
      const testPayload = await testResponse.json();
      if (!testResponse.ok || !testPayload.ok) {
        throw new Error(testPayload.message || "SubDuet could not verify this platform.");
      }
      actionStatus.textContent = testPayload.message;
    }
    button.textContent = "Saving…";
    const response = await fetch("/config", {
      method: "POST",
      headers: authorizedHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ config, mode: selectedMode() }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.saved) {
      throw new Error(payload.message || "SubDuet could not save the setup.");
    }
    button.textContent = "Saved";
    actionStatus.textContent = payload.backup
      ? `Saved in ${payload.location}. Your previous file was backed up as ${payload.backup}.`
      : `Saved ${payload.filename} in ${payload.location}.`;
    if (selectedMode() === "single") {
      actionStatus.textContent = `Saved ${payload.filename}. Look for the video file window.`;
      pollProgress();
    } else if (desktopApp) {
      actionStatus.textContent = "Saved. SubDuet is opening your private dashboard…";
      pollProgress();
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = selectedMode() === "library"
      ? (desktopApp ? "Save and open dashboard" : "Save paircue.env")
      : "Save and choose a video";
    formError.textContent = `${error.message} You can still use “Copy config”.`;
    formError.hidden = false;
  }
}

function renderProgress(payload) {
  const panel = byId("next-step");
  const number = byId("next-number");
  const heading = byId("next-heading");
  const copy = byId("next-copy");
  const output = byId("next-command");
  const link = byId("next-link");
  const feedback = byId("next-feedback");
  panel.dataset.phase = payload.phase;
  output.textContent = Array.isArray(payload.outputs) ? payload.outputs.join("\n") : "";
  link.hidden = true;
  feedback.hidden = true;

  if (payload.phase === "choosing") {
    number.textContent = "1";
    heading.textContent = "Choose one video";
    copy.textContent = payload.message;
    return;
  }
  if (payload.phase === "processing" || payload.phase === "starting" || payload.phase === "saved") {
    number.textContent = "•••";
    heading.textContent = "SubDuet is working";
    copy.textContent = payload.message;
    return;
  }
  if (payload.phase === "completed") {
    number.textContent = "DONE";
    if (payload.action_url) {
      heading.textContent = "Your SubDuet dashboard is ready";
      copy.textContent = payload.message;
      link.href = payload.action_url;
      link.hidden = false;
      window.setTimeout(() => window.location.assign(payload.action_url), 900);
    } else {
      heading.textContent = "Your bilingual subtitle is ready";
      copy.textContent = `${payload.message} The finished file is highlighted in your file manager.`;
      feedback.hidden = false;
    }
    return;
  }
  if (payload.phase === "cancelled") {
    number.textContent = "SAVED";
    heading.textContent = "Your setup is ready for later";
    copy.textContent = payload.message;
    return;
  }
  if (payload.phase === "failed") {
    number.textContent = "CHECK";
    heading.textContent = "SubDuet needs one more thing";
    copy.textContent = payload.message;
  }
}

async function pollProgress() {
  while (true) {
    try {
      const response = await fetch("/progress", {
        headers: authorizedHeaders({ Accept: "application/json" }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error("progress check failed");
      }
      renderProgress(payload);
      if (payload.terminal) {
        return;
      }
    } catch {
      byId("next-step").dataset.phase = "failed";
      byId("next-number").textContent = "CHECK";
      byId("next-heading").textContent = "SubDuet stopped reporting progress";
      byId("next-copy").textContent =
        "Your setup is saved. Reopen SubDuet to check the video or try again.";
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 650));
  }
}

async function updateSystemReadiness() {
  const status = byId("system-check");
  if (!window.location.protocol.startsWith("http")) {
    status.textContent = "SubDuet checks the video tools when this page is opened from the app.";
    return;
  }
  try {
    const response = await fetch("/readiness", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error("readiness check failed");
    }
    if (payload.ready) {
      status.dataset.status = "ready";
      status.textContent = "✓ This device is ready to read and align video.";
      return;
    }
    status.dataset.status = "needs-attention";
    status.textContent =
      "Optional video tools are missing. Search, translation, and two SRT tracks still work; embedded subtitles, timing alignment, and speech generation need FFmpeg.";
  } catch {
    status.textContent = "SubDuet could not check the video tools. Setup can still continue.";
  }
}

async function updateAppContext() {
  if (!window.location.protocol.startsWith("http")) {
    return;
  }
  try {
    const response = await fetch("/context", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    desktopApp = response.ok && payload.desktop === true;
    byId("choose-media-folder").hidden = !desktopApp;
    byId("cli-pair-note").hidden = desktopApp;
    byId("local-port-field").hidden = desktopApp;
    byId("nas-permissions").hidden = desktopApp;
    updateMode();
    updatePreview();
  } catch {
    desktopApp = false;
  }
}

async function chooseMediaFolder() {
  if (!desktopApp || !setupToken) {
    return;
  }
  const button = byId("choose-media-folder");
  button.disabled = true;
  button.textContent = "Choosing…";
  try {
    const response = await fetch("/choose-folder", {
      method: "POST",
      headers: authorizedHeaders(),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error("SubDuet could not open the folder chooser.");
    }
    if (payload.selected && payload.path) {
      byId("host-media-path").value = payload.path;
      byId("host-media-path").dispatchEvent(new Event("input", { bubbles: true }));
    }
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Choose folder";
  }
}

async function quickPairSubtitles() {
  if (!desktopApp || !setupToken) {
    return;
  }
  const button = byId("quick-pair");
  const status = byId("quick-pair-status");
  const feedback = byId("quick-feedback");
  let completed = false;
  feedback.hidden = true;
  button.disabled = true;
  button.textContent = "Choose two subtitles…";
  status.textContent = "First choose the spoken subtitle, then the learning subtitle.";
  try {
    const order = value("quick-pair-order");
    const response = await fetch(
      `/quick-pair?order=${encodeURIComponent(order)}`,
      { method: "POST", headers: authorizedHeaders() },
    );
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || "SubDuet could not pair those subtitles.");
    }
    if (!payload.completed) {
      status.textContent = payload.message;
      return;
    }
    completed = true;
    status.textContent = `${payload.message} ${payload.filename} is highlighted in your file manager. Keep it beside the video; if ${selectedPlatformName()} does not see it, match the video's base name while keeping .mul.srt. Reopen SubDuet to pair another.`;
    feedback.hidden = false;
    button.textContent = "Pairing complete";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = completed;
    if (button.textContent === "Choose two subtitles…") {
      button.textContent = "Choose two SRTs";
    }
  }
}

async function quickPairDemo() {
  if (!desktopApp || !setupToken) {
    return;
  }
  const button = byId("quick-demo");
  const status = byId("quick-pair-status");
  const feedback = byId("quick-feedback");
  feedback.hidden = true;
  button.disabled = true;
  button.textContent = "Creating demo…";
  status.textContent = "Creating a tiny project-owned subtitle in your Downloads folder.";
  try {
    const order = value("quick-pair-order");
    const response = await fetch(`/demo-pair?order=${encodeURIComponent(order)}`, {
      method: "POST",
      headers: authorizedHeaders(),
    });
    const payload = await response.json();
    if (!response.ok || !payload.completed) {
      throw new Error(payload.message || "SubDuet could not create the safe demo.");
    }
    button.textContent = "Demo complete";
    status.textContent = `${payload.message} ${payload.filename} is highlighted in your file manager. It uses only short dialogue written for SubDuet.`;
    feedback.hidden = false;
  } catch (error) {
    button.disabled = false;
    button.textContent = "Try safe demo";
    status.textContent = error.message;
  }
}

form.addEventListener("input", () => {
  clearValidity();
  updatePreview();
  updateNextStep();
});

["translation", "transcription"].forEach((prefix) => {
  byId(`${prefix}-provider`).addEventListener("change", () => {
    clearAIApproval(prefix);
    byId(`${prefix}-url`).value = aiConnections.presets[value(`${prefix}-provider`)] || "";
    byId(`${prefix}-model`).value = "";
    updateAIControls(prefix);
    updatePreview();
  });
  byId(`${prefix}-url`).addEventListener("input", () => {
    clearAIApproval(prefix);
    updateAIControls(prefix);
  });
  byId(`${prefix}-confirm`).addEventListener("change", () => {
    if (checked(`${prefix}-confirm`) && aiDescription(prefix)) {
      aiApprovals[prefix] = aiSelection(prefix);
    } else {
      clearAIApproval(prefix);
    }
    updateAIControls(prefix);
    updatePreview();
  });
});

form.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener("change", () => {
    updateMode();
    updatePreview();
  });
});

form.querySelectorAll('input[name="platform"]').forEach((input) => {
  input.addEventListener("change", () => {
    updatePlatform();
    updatePreview();
  });
});

form.querySelectorAll('input[name="subtitle-preset"]').forEach((input) => {
  input.addEventListener("change", () => {
    updateSubtitlePreset();
    updatePreview();
  });
});

[
  ["search-enabled", "search-panel"],
  ["translation-enabled", "translation-panel"],
  ["transcription-enabled", "transcription-panel"],
].forEach(([toggleId, panelId]) => {
  byId(toggleId).addEventListener("change", () => {
    setPanelEnabled(toggleId, panelId);
    updatePreview();
  });
  setPanelEnabled(toggleId, panelId);
});

byId("swap-languages").addEventListener("click", () => {
  const source = byId("source-language");
  const target = byId("target-language");
  [source.value, target.value] = [target.value, source.value];
  updatePreview();
});

byId("copy-config").addEventListener("click", copyConfig);
byId("download-config").addEventListener("click", saveConfig);
byId("choose-media-folder").addEventListener("click", chooseMediaFolder);
byId("quick-pair").addEventListener("click", quickPairSubtitles);
byId("quick-demo").addEventListener("click", quickPairDemo);
byId("continue-platform").addEventListener("click", showJourneyStage);
byId("continue-journey").addEventListener("click", showDetailsStage);
byId("change-platform").addEventListener("click", showPlatformStage);
byId("change-journey").addEventListener("click", showJourneyStage);

secretIds.forEach((id) => {
  byId(id).addEventListener("paste", () => {
    actionStatus.textContent = "Secret added locally. It will be hidden from the preview.";
  });
});

updateSubtitlePreset();
updateMode();
updatePreview();
updateSystemReadiness();
updateAppContext();
