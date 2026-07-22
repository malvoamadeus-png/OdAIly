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
  soundProfiles: {
    newsflash_backstage: {
      enabled: true,
      preset: "beep_short",
      volume: 0.45,
      cooldownMs: 4000
    },
    newsflash_direct: {
      enabled: true,
      preset: "beep_double",
      volume: 0.58,
      cooldownMs: 3500
    },
    auditor_alert: {
      enabled: true,
      preset: "sharp_long_repeat",
      volume: 0.82,
      cooldownMs: 2000
    },
    writer3_context: {
      enabled: true,
      preset: "beep_short",
      volume: 0.45,
      cooldownMs: 4000
    },
    whale: {
      enabled: true,
      preset: "beep_short",
      volume: 0.45,
      cooldownMs: 4000
    }
  },
  feedLaneRatios: { high: 75, ai: 25 }
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
    soundProfiles: sanitizeSoundProfiles(data.soundProfiles),
    supabaseUrl: BUILTIN_SUPABASE_URL,
    supabaseAnonKey: BUILTIN_SUPABASE_PUBLISHABLE_KEY,
    pluginApiBaseUrl: BUILTIN_EDITOR_PLUGIN_API_BASE_URL
  };
}

export async function saveSettings(values) {
  const nextValues = { ...values };
  if ("soundProfiles" in nextValues) {
    nextValues.soundProfiles = sanitizeSoundProfiles(nextValues.soundProfiles);
  }
  await chrome.storage.local.set(nextValues);
}

export async function resetSettings() {
  await chrome.storage.local.set(DEFAULT_SETTINGS);
}

function sanitizeSoundProfile(profile, fallback) {
  const value = profile && typeof profile === "object" ? profile : {};
  const enabled = "enabled" in value ? Boolean(value.enabled) : Boolean(fallback.enabled);
  const preset = typeof value.preset === "string" && value.preset.trim() ? value.preset.trim() : fallback.preset;
  const volume = Number.isFinite(Number(value.volume))
    ? Math.max(0, Math.min(1, Number(value.volume)))
    : fallback.volume;
  const cooldownMs = Number.isFinite(Number(value.cooldownMs))
    ? Math.max(500, Math.min(30000, Math.round(Number(value.cooldownMs))))
    : fallback.cooldownMs;
  return { enabled, preset, volume, cooldownMs };
}

export function sanitizeSoundProfiles(value) {
  const fallback = DEFAULT_SETTINGS.soundProfiles;
  if (!value || typeof value !== "object") {
    return JSON.parse(JSON.stringify(fallback));
  }
  return {
    newsflash_backstage: sanitizeSoundProfile(value.newsflash_backstage, fallback.newsflash_backstage),
    newsflash_direct: sanitizeSoundProfile(value.newsflash_direct, fallback.newsflash_direct),
    auditor_alert: sanitizeSoundProfile(value.auditor_alert, fallback.auditor_alert),
    writer3_context: sanitizeSoundProfile(value.writer3_context, fallback.writer3_context),
    whale: sanitizeSoundProfile(value.whale, fallback.whale)
  };
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
  if (![high, ai].every((item) => Number.isFinite(item) && item >= 10)) {
    return { ...fallback };
  }
  const total = high + ai;
  if (total <= 0) {
    return { ...fallback };
  }
  return {
    high: Math.round((high / total) * 100),
    ai: Math.max(10, 100 - Math.round((high / total) * 100))
  };
}

export async function getFeedLaneRatios() {
  const data = await chrome.storage.local.get(FEED_LANE_RATIOS_KEY);
  return sanitizeFeedLaneRatios(data[FEED_LANE_RATIOS_KEY]);
}

export async function saveFeedLaneRatios(value) {
  await chrome.storage.local.set({ [FEED_LANE_RATIOS_KEY]: sanitizeFeedLaneRatios(value) });
}
