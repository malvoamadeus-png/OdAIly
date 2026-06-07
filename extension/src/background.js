import { DEFAULT_SETTINGS } from "./lib/storage.js";

const ODAILY_NEWSFLASH_URL = "https://odaily.info/content/newsflash/newsflash-list";
const ODAILY_NEWSFLASH_URL_PATTERN = `${ODAILY_NEWSFLASH_URL}*`;
const NEWSFLASH_RESET_DELAY_MS = 500;
const NEWSFLASH_RESET_RETRY_COUNT = 6;
const NEWSFLASH_RESET_RETRY_INTERVAL_MS = 400;

async function seedDefaults() {
  const current = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
  const patch = {};
  for (const [key, value] of Object.entries(DEFAULT_SETTINGS)) {
    if (current[key] === undefined) {
      patch[key] = value;
    }
  }
  if (current.activeTopTab === undefined) {
    patch.activeTopTab = "feed";
  }
  if (Object.keys(patch).length > 0) {
    await chrome.storage.local.set(patch);
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await seedDefaults();
});

chrome.runtime.onStartup.addListener(async () => {
  await seedDefaults();
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findOrOpenNewsflashTab() {
  const [existingTab] = await chrome.tabs.query({ url: [ODAILY_NEWSFLASH_URL_PATTERN] });
  if (existingTab?.id) {
    await chrome.tabs.update(existingTab.id, { active: true });
    if (existingTab.windowId !== undefined) {
      await chrome.windows.update(existingTab.windowId, { focused: true });
    }
    return existingTab.id;
  }
  const createdTab = await chrome.tabs.create({ url: ODAILY_NEWSFLASH_URL, active: true });
  if (!createdTab.id) {
    throw new Error("未能打开 Odaily 快讯页");
  }
  return createdTab.id;
}

async function waitForTabComplete(tabId, timeoutMs = 15000) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === "complete") {
    return;
  }
  await new Promise((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(handleUpdated);
      reject(new Error("Odaily 快讯页加载超时"));
    }, timeoutMs);
    function handleUpdated(updatedTabId, changeInfo) {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") {
        return;
      }
      clearTimeout(timeoutId);
      chrome.tabs.onUpdated.removeListener(handleUpdated);
      resolve();
    }
    chrome.tabs.onUpdated.addListener(handleUpdated);
  });
}

async function tryClickNewsflashResetButton(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const buttons = Array.from(document.querySelectorAll("button"));
      const resetButton = buttons.find((button) => {
        const text = (button.textContent || "").replace(/\s+/g, "");
        return text === "重置";
      });
      if (!resetButton || resetButton.disabled || resetButton.getAttribute("aria-disabled") === "true") {
        return false;
      }
      resetButton.click();
      return true;
    }
  });
  return Boolean(result);
}

async function openNewsflashAndReset() {
  const tabId = await findOrOpenNewsflashTab();
  await waitForTabComplete(tabId);
  await sleep(NEWSFLASH_RESET_DELAY_MS);
  for (let attempt = 0; attempt < NEWSFLASH_RESET_RETRY_COUNT; attempt += 1) {
    if (await tryClickNewsflashResetButton(tabId)) {
      return { ok: true };
    }
    if (attempt < NEWSFLASH_RESET_RETRY_COUNT - 1) {
      await sleep(NEWSFLASH_RESET_RETRY_INTERVAL_MS);
    }
  }
  throw new Error("未找到可点击的“重置”按钮");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "ODAILY_NEWS_GEN_CAPTURED") {
    const payload = message.payload ?? null;
    const windowId = sender.tab?.windowId;
    Promise.all([
      chrome.storage.local.set({
        activeTopTab: "news_gen",
        newsGenDraft: payload,
        newsGenResult: null
      })
    ])
      .then(async () => {
        if (windowId) {
          await chrome.sidePanel.open({ windowId });
        }
      })
      .catch(() => undefined);
    return false;
  }

  if (message?.type === "ODAILY_OPEN_NEWSFLASH_AND_RESET") {
    openNewsflashAndReset()
      .then((result) => sendResponse(result))
      .catch((error) => {
        sendResponse({
          ok: false,
          error: error instanceof Error ? error.message : "Odaily 快讯页操作失败"
        });
      });
    return true;
  }

  return false;
});
