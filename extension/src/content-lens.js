const HOST_ID = "odaily-text-lens-host";
const STYLE = `
  :host {
    position: fixed;
    inset: 0;
    z-index: 2147483647;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    pointer-events: none;
  }

  :host([hidden]) {
    display: none;
  }

  .card {
    width: min(620px, calc(100vw - 36px));
    max-height: calc(100vh - 36px);
    overflow-y: auto;
    overscroll-behavior: contain;
    pointer-events: auto;
    padding: 24px 24px 18px;
    border: 1px solid rgba(100, 116, 139, 0.24);
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.98);
    color: #0f172a;
    font-family: Inter, "Segoe UI", system-ui, sans-serif;
    box-shadow: 0 24px 70px rgba(15, 23, 42, 0.3), 0 5px 18px rgba(15, 23, 42, 0.12);
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin-bottom: 15px;
  }

  .badge,
  .status {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    padding: 3px 7px;
    border-radius: 999px;
    background: #f1f5f9;
    color: #334155;
    font-size: 11px;
    line-height: 1.2;
  }

  .status[data-tone="success"] { background: #ecfdf5; color: #047857; }
  .status[data-tone="warning"] { background: #fff7ed; color: #b45309; }
  .status[data-tone="danger"] { background: #fef2f2; color: #b91c1c; }
  .status[data-tone="info"] { background: #eff6ff; color: #1d4ed8; }

  h2 {
    margin: 0;
    font-size: clamp(24px, 3vw, 34px);
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  p {
    margin: 17px 0 0;
    color: #334155;
    font-size: clamp(18px, 2.2vw, 24px);
    line-height: 1.65;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .time {
    margin-top: 17px;
    color: #94a3b8;
    font-size: 12px;
  }
`;

let host = null;
let shadow = null;
let latestMessageId = 0;

function ensureHost() {
  if (host?.isConnected) {
    return host;
  }
  host = document.createElement("div");
  host.id = HOST_ID;
  host.hidden = true;
  shadow = host.attachShadow({ mode: "closed" });
  document.documentElement.appendChild(host);
  return host;
}

function text(value) {
  return String(value ?? "");
}

function sendKeyState(state, event) {
  chrome.runtime.sendMessage({
    type: "ODAILY_TEXT_LENS_KEY_STATE",
    state,
    key: event.key,
    code: event.code,
    ctrlKey: event.ctrlKey,
    altKey: event.altKey,
    shiftKey: event.shiftKey,
    metaKey: event.metaKey
  }).catch(() => undefined);
}

function render(message) {
  const nextHost = ensureHost();
  if (!message.visible || !message.item) {
    nextHost.hidden = true;
    shadow.replaceChildren();
    return;
  }

  const item = message.item;
  const style = document.createElement("style");
  style.textContent = STYLE;
  const card = document.createElement("article");
  card.className = "card";

  const meta = document.createElement("div");
  meta.className = "meta";
  const status = document.createElement("span");
  status.className = "status";
  status.dataset.tone = text(item.status_tone || "neutral");
  status.textContent = text(item.status_label || item.feed_kind || "信息");
  meta.append(status);
  for (const badge of Array.isArray(item.badges) ? item.badges : []) {
    if (!badge?.value) continue;
    const badgeNode = document.createElement("span");
    badgeNode.className = "badge";
    badgeNode.textContent = text(badge.value);
    meta.append(badgeNode);
  }

  const title = document.createElement("h2");
  title.textContent = text(item.title || item.summary || "信息");
  const summary = document.createElement("p");
  summary.textContent = text(item.summary || item.title || "暂无摘要");
  const time = document.createElement("div");
  time.className = "time";
  time.textContent = text(item.occurred_at || "");

  card.append(meta, title, summary, time);
  shadow.replaceChildren(style, card);
  nextHost.hidden = false;
}

if (!globalThis.__odailyTextLensInstalled) {
  globalThis.__odailyTextLensInstalled = true;
  document.addEventListener("keydown", (event) => {
    if (!event.repeat) {
      sendKeyState("down", event);
    }
  }, true);
  document.addEventListener("keyup", (event) => {
    sendKeyState("up", event);
  }, true);
  window.addEventListener("blur", () => {
    chrome.runtime.sendMessage({
      type: "ODAILY_TEXT_LENS_KEY_STATE",
      state: "blur"
    }).catch(() => undefined);
  });
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type !== "ODAILY_TEXT_LENS_UPDATE") {
      return;
    }
    const messageId = Number(message.message_id || 0);
    if (messageId < latestMessageId) {
      return;
    }
    latestMessageId = messageId;
    render(message);
  });
}
