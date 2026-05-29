import { DEFAULT_SETTINGS } from "./lib/storage.js";

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

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "ODAILY_NEWS_GEN_CAPTURED") {
    return false;
  }
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
});
