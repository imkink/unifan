"use strict";

const POLL_INTERVAL_MS = 2000;
const LANGUAGE_STORAGE_KEY = "unifan.language";

const messages = {
  en: {
    "page.title": "UniFan Dashboard",
    "brand.subtitle": "Rack Fan Control System",
    "language.label": "Language",
    "action.settings": "Settings",
    "action.settingsUnavailable": "Control settings are not available yet",
    "section.temperature": "Temperature",
    "section.fan": "Fan",
    "section.network": "Network",
    "section.system": "System",
    "field.humidity": "Humidity",
    "field.ipAddress": "IP Address",
    "field.gateway": "Gateway",
    "field.interface": "Interface",
    "field.serialNumber": "Serial Number",
    "field.version": "Version",
    "field.dataSource": "Data Source",
    "field.sample": "Sample",
    "status.waiting": "Waiting",
    "status.online": "Online",
    "status.offline": "Offline",
    "status.ready": "Ready",
    "status.stale": "Stale",
    "status.simulation": "Simulation",
    "status.fansOnline": "{count} online",
    "status.fanWarnings": "{count} warning",
    "source.simulated": "Simulated",
    "source.hardware": "Hardware",
    "source.uninitialized": "Uninitialized",
    "phase.waiting_ip": "Waiting for IP",
    "phase.offline": "Offline",
    "phase.error": "Network error",
    "phase.stopped": "Stopped",
    "fan.aria": "Fan {id}: {rpm} RPM",
    "fan.group": "G{group}",
    "connection.connecting": "Connecting to UniFan…",
    "connection.simulation": "Simulation data · Control policy is not active",
    "connection.hardware": "Live hardware data",
    "connection.error": "Dashboard connection unavailable · Retrying",
    "footer.sourceCode": "Source Code",
  },
  "zh-CN": {
    "page.title": "UniFan 管理面板",
    "brand.subtitle": "机架风扇控制系统",
    "language.label": "语言",
    "action.settings": "设置",
    "action.settingsUnavailable": "控制策略尚未启用，暂时无法设置",
    "section.temperature": "温度",
    "section.fan": "风扇",
    "section.network": "网络",
    "section.system": "系统",
    "field.humidity": "湿度",
    "field.ipAddress": "IP 地址",
    "field.gateway": "网关",
    "field.interface": "网络接口",
    "field.serialNumber": "序列号",
    "field.version": "版本",
    "field.dataSource": "数据来源",
    "field.sample": "采样序号",
    "status.waiting": "等待中",
    "status.online": "在线",
    "status.offline": "离线",
    "status.ready": "正常",
    "status.stale": "数据过期",
    "status.simulation": "模拟模式",
    "status.fansOnline": "{count} 路在线",
    "status.fanWarnings": "{count} 路告警",
    "source.simulated": "模拟数据",
    "source.hardware": "真实硬件",
    "source.uninitialized": "未初始化",
    "phase.waiting_ip": "等待 IP",
    "phase.offline": "离线",
    "phase.error": "网络错误",
    "phase.stopped": "已停止",
    "fan.aria": "风扇 {id}：{rpm} RPM",
    "fan.group": "第 {group} 组",
    "connection.connecting": "正在连接 UniFan…",
    "connection.simulation": "当前为模拟数据 · 控制策略尚未启用",
    "connection.hardware": "当前为真实硬件数据",
    "connection.error": "管理面板连接失败 · 正在重试",
    "footer.sourceCode": "源代码",
  },
};

const fanIcon = '<span class="fan-icon" aria-hidden="true"></span>';

const elements = {
  temperature: document.querySelector("#temperature"),
  humidity: document.querySelector("#humidity"),
  fanGrid: document.querySelector("#fan-grid"),
  fanSummary: document.querySelector("#fan-summary"),
  networkState: document.querySelector("#network-state"),
  networkIp: document.querySelector("#network-ip"),
  networkGateway: document.querySelector("#network-gateway"),
  networkDns: document.querySelector("#network-dns"),
  networkInterface: document.querySelector("#network-interface"),
  systemState: document.querySelector("#system-state"),
  deviceId: document.querySelector("#device-id"),
  version: document.querySelector("#version"),
  dataSource: document.querySelector("#data-source"),
  sequence: document.querySelector("#sequence"),
  connectionMessage: document.querySelector("#connection-message"),
  languageSelector: document.querySelector("#language-selector"),
};

let language = preferredLanguage();
let lastPayload = null;
let connectionFailed = false;

function preferredLanguage() {
  try {
    const saved = localStorage.getItem(LANGUAGE_STORAGE_KEY);
    if (saved && messages[saved]) return saved;
  } catch (error) {
    // Local storage can be unavailable in privacy modes; language still works per page load.
  }
  return "en";
}

function t(key, values = {}) {
  const template = messages[language][key] || messages.en[key] || key;
  return Object.keys(values).reduce((text, name) =>
    text.split(`{${name}}`).join(String(values[name])), template);
}

function translatedValue(prefix, value) {
  if (value === null || value === undefined || value === "") return "--";
  const key = `${prefix}.${String(value).toLowerCase()}`;
  return messages[language][key] || messages.en[key] || String(value);
}

function applyTranslations() {
  document.documentElement.lang = language;
  document.title = t("page.title");
  elements.languageSelector.value = language;
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.title = t(element.dataset.i18nTitle);
  });
}

function show(value, fallback = "--") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function number(value, digits = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
}

function setStatus(element, text, state) {
  element.textContent = text;
  element.classList.toggle("is-ready", state === "ready");
  element.classList.toggle("is-warning", state === "warning");
}

function renderFans(fans) {
  const list = Array.isArray(fans) ? fans.slice(0, 6) : [];
  elements.fanGrid.replaceChildren();
  let warnings = 0;
  for (let index = 0; index < 6; index += 1) {
    const fan = list[index] || { id: index + 1, rpm: null, pwm_percent: null };
    const warning = fan.rpm === 0 || fan.rpm === null || fan.rpm === undefined;
    if (warning) warnings += 1;
    const item = document.createElement("article");
    item.className = warning ? "fan is-warning" : "fan";
    item.setAttribute("aria-label", t("fan.aria", { id: index + 1, rpm: number(fan.rpm) }));
    item.innerHTML = `${fanIcon}
      <strong class="fan-name">#${show(fan.id, index + 1)}</strong>
      <span class="fan-rpm">${number(fan.rpm)} rpm</span>
      <span class="fan-pwm">${number(fan.pwm_percent)}% · ${t("fan.group", { group: show(fan.group) })}</span>`;
    elements.fanGrid.append(item);
  }
  setStatus(elements.fanSummary,
            t(warnings ? "status.fanWarnings" : "status.fansOnline", { count: warnings || 6 }),
            warnings ? "warning" : "ready");
}

function render(payload) {
  const environment = payload.environment || {};
  const network = payload.network || {};
  const system = payload.system || {};

  elements.temperature.textContent = number(environment.temperature_c, 1);
  elements.humidity.textContent = number(environment.humidity_percent, 0);
  renderFans(payload.fans);

  elements.networkIp.textContent = show(network.ip);
  elements.networkGateway.textContent = show(network.gateway);
  elements.networkDns.textContent = show(network.dns);
  elements.networkInterface.textContent = show(network.interface, "W5500").toUpperCase();
  setStatus(elements.networkState,
            network.ready ? t("status.online") : translatedValue("phase", network.phase || "offline"),
            network.ready ? "ready" : "warning");

  elements.deviceId.textContent = show(system.device_id);
  elements.version.textContent = show(system.version);
  elements.dataSource.textContent = translatedValue("source", system.source);
  elements.sequence.textContent = show(system.sequence);
  const warning = system.stale || !system.hardware_ready;
  setStatus(elements.systemState,
            t(system.stale ? "status.stale" : (system.simulated ? "status.simulation" : "status.ready")),
            warning ? "warning" : "ready");

  connectionFailed = false;
  elements.connectionMessage.classList.remove("is-error");
  elements.connectionMessage.textContent = t(system.simulated
    ? "connection.simulation" : "connection.hardware");
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    lastPayload = await response.json();
    render(lastPayload);
  } catch (error) {
    connectionFailed = true;
    elements.connectionMessage.classList.add("is-error");
    elements.connectionMessage.textContent = t("connection.error");
  }
}

elements.languageSelector.addEventListener("change", () => {
  language = messages[elements.languageSelector.value] ? elements.languageSelector.value : "en";
  try {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
  } catch (error) {
    // Keep the selected language for the current page even when persistence is unavailable.
  }
  applyTranslations();
  if (lastPayload) render(lastPayload);
  else if (connectionFailed) elements.connectionMessage.textContent = t("connection.error");
});

applyTranslations();
refresh();
setInterval(refresh, POLL_INTERVAL_MS);
