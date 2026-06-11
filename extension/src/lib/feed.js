import { rpc } from "./supabase.js";

export async function fetchEditorProfile(config, session) {
  const profile = await rpc(config, session, "editor_plugin_profile");
  if (Array.isArray(profile)) {
    return profile[0] ?? null;
  }
  return profile && typeof profile === "object" ? profile : null;
}

export async function fetchFeedItems(config, session, limit = 120) {
  const rows = await rpc(config, session, "editor_plugin_feed", { p_limit: limit });
  const list = Array.isArray(rows) ? rows : [];
  return list.map((item) => ({
    ...item,
    badges: Array.isArray(item.badges) ? item.badges.filter(Boolean) : [],
    action_schema: item.action_schema || { type: "read" },
    meta_json: item.meta_json || {}
  }));
}

export async function fetchFeedState(config, session, feedItemIds) {
  if (!Array.isArray(feedItemIds) || feedItemIds.length === 0) {
    return new Map();
  }
  const rows = await rpc(config, session, "editor_plugin_state", { p_feed_item_ids: feedItemIds });
  const map = new Map();
  for (const row of Array.isArray(rows) ? rows : []) {
    map.set(`${row.feed_kind}:${row.feed_item_id}`, row);
  }
  return map;
}

export async function submitFeedback(config, session, payload) {
  return await rpc(config, session, "editor_plugin_submit_feedback", payload);
}
