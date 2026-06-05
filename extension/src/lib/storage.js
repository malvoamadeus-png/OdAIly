import {
  BUILTIN_EDITOR_PLUGIN_API_BASE_URL,
  BUILTIN_SUPABASE_PUBLISHABLE_KEY,
  BUILTIN_SUPABASE_URL
} from "./runtime-config.js";

export const DEFAULT_SETTINGS = {
  supabaseUrl: BUILTIN_SUPABASE_URL,
  supabaseAnonKey: BUILTIN_SUPABASE_PUBLISHABLE_KEY,
  pluginApiBaseUrl: BUILTIN_EDITOR_PLUGIN_API_BASE_URL,
  pollIntervalSeconds: 15,
  soundEnabled: true,
  soundScope: "high",
  soundVolume: "medium",
  feedLaneRatios: { high: 60, ai: 20, low: 20 }
};

const SETTINGS_KEYS = Object.keys(DEFAULT_SETTINGS);
const SESSION_KEY = "authSession";
const PANEL_SESSION_KEY = "panelSessionId";
const ACTIVE_TOP_TAB_KEY = "activeTopTab";
const NEWS_GEN_DRAFT_KEY = "newsGenDraft";
const NEWS_GEN_RESULT_KEY = "newsGenResult";
const FEED_LANE_RATIOS_KEY = "feedLaneRatios";

export async function getSettings() {
  const data = await chrome.storage.local.get(SETTINGS_KEYS);
  return {
    ...DEFAULT_SETTINGS,
    ...data,
    supabaseUrl: BUILTIN_SUPABASE_URL,
    supabaseAnonKey: BUILTIN_SUPABASE_PUBLISHABLE_KEY,
    pluginApiBaseUrl: BUILTIN_EDITOR_PLUGIN_API_BASE_URL
  };
}

export async function saveSettings(values) {
  await chrome.storage.local.set(values);
}

export async function resetSettings() {
  await chrome.storage.local.set(DEFAULT_SETTINGS);
}

export async function getAuthSession() {
  const data = await chrome.storage.local.get(SESSION_KEY);
  return data[SESSION_KEY] ?? null;
}

export async function saveAuthSession(session) {
  await chrome.storage.local.set({ [SESSION_KEY]: session });
}

export async function clearAuthSession() {
  await chrome.storage.local.remove(SESSION_KEY);
}

export async function getPanelSessionId() {
  const data = await chrome.storage.local.get(PANEL_SESSION_KEY);
  let panelSessionId = data[PANEL_SESSION_KEY];
  if (!panelSessionId) {
    panelSessionId = crypto.randomUUID();
    await chrome.storage.local.set({ [PANEL_SESSION_KEY]: panelSessionId });
  }
  return panelSessionId;
}

export async function getActiveTopTab() {
  const data = await chrome.storage.local.get(ACTIVE_TOP_TAB_KEY);
  return data[ACTIVE_TOP_TAB_KEY] ?? "feed";
}

export async function saveActiveTopTab(value) {
  await chrome.storage.local.set({ [ACTIVE_TOP_TAB_KEY]: value });
}

export async function getNewsGenDraft() {
  const data = await chrome.storage.local.get(NEWS_GEN_DRAFT_KEY);
  return data[NEWS_GEN_DRAFT_KEY] ?? null;
}

export async function saveNewsGenDraft(value) {
  await chrome.storage.local.set({ [NEWS_GEN_DRAFT_KEY]: value });
}

export async function clearNewsGenDraft() {
  await chrome.storage.local.remove(NEWS_GEN_DRAFT_KEY);
}

export async function getNewsGenResult() {
  const data = await chrome.storage.local.get(NEWS_GEN_RESULT_KEY);
  return data[NEWS_GEN_RESULT_KEY] ?? null;
}

export async function saveNewsGenResult(value) {
  await chrome.storage.local.set({ [NEWS_GEN_RESULT_KEY]: value });
}

export async function clearNewsGenResult() {
  await chrome.storage.local.remove(NEWS_GEN_RESULT_KEY);
}

export function sanitizeFeedLaneRatios(value) {
  const fallback = DEFAULT_SETTINGS.feedLaneRatios;
  if (!value || typeof value !== "object") {
    return { ...fallback };
  }
  const high = Number(value.high);
  const ai = Number(value.ai);
  const low = Number(value.low);
  if (![high, ai, low].every((item) => Number.isFinite(item) && item >= 10)) {
    return { ...fallback };
  }
  const total = high + ai + low;
  if (total <= 0) {
    return { ...fallback };
  }
  return {
    high: Math.round((high / total) * 100),
    ai: Math.round((ai / total) * 100),
    low: Math.max(10, 100 - Math.round((high / total) * 100) - Math.round((ai / total) * 100))
  };
}

export async function getFeedLaneRatios() {
  const data = await chrome.storage.local.get(FEED_LANE_RATIOS_KEY);
  return sanitizeFeedLaneRatios(data[FEED_LANE_RATIOS_KEY]);
}

export async function saveFeedLaneRatios(value) {
  await chrome.storage.local.set({ [FEED_LANE_RATIOS_KEY]: sanitizeFeedLaneRatios(value) });
}
