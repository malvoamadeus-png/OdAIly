import {
  clearAuthSession,
  getAuthSession,
  saveAuthSession
} from "./storage.js";

function ensureConfig(config) {
  if (!config.pluginApiBaseUrl) {
    throw new Error("插件内置连接参数缺失，请联系管理员重新打包插件");
  }
}

function buildUrl(config, path) {
  return `${config.pluginApiBaseUrl.replace(/\/+$/, "")}${path}`;
}

function ensureSecureLoginUrl(config) {
  const url = new URL(config.pluginApiBaseUrl);
  if (url.protocol === "https:" || ["localhost", "127.0.0.1"].includes(url.hostname)) {
    return;
  }
  throw new Error("插件登录服务必须使用 HTTPS，请联系管理员更新插件服务地址");
}

async function parseError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const message =
    payload?.msg ||
    payload?.message ||
    payload?.error_description ||
    payload?.error ||
    `${response.status} ${response.statusText}`;
  throw new Error(message);
}

async function requestJson(url, init, timeoutMs = 30000) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!response.ok) {
      await parseError(response);
    }
    if (response.status === 204) {
      return null;
    }
    return await response.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function sessionFromPayload(payload) {
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token || null,
    expiresAt: Number(payload.expires_at || Math.floor(Date.now() / 1000) + Number(payload.expires_in || 3600)),
    user: {
      id: payload.user?.id || null,
      email: payload.user?.email || null
    }
  };
}

export async function signInWithPassword(config, email, password) {
  ensureConfig(config);
  ensureSecureLoginUrl(config);
  const response = await requestJson(buildUrl(config, "/plugin/auth/login"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      email: email.trim().toLowerCase(),
      password
    })
  }, 20000);
  if (!response?.ok) {
    throw new Error(response?.message || "登录失败");
  }
  const payload = response.data;
  const session = sessionFromPayload(payload);
  await saveAuthSession(session);
  return session;
}

export async function signOut(config) {
  const session = await getAuthSession();
  if (session?.accessToken) {
    try {
      await requestJson(buildUrl(config, "/plugin/auth/logout"), {
        method: "POST",
        headers: {
          Authorization: `Bearer ${session.accessToken}`
        }
      });
    } catch {
      // Ignore logout failures and clear local state anyway.
    }
  }
  await clearAuthSession();
}

export async function refreshAuthSession(config, session) {
  ensureConfig(config);
  if (!session?.accessToken) {
    await clearAuthSession();
    return null;
  }
  try {
    const response = await requestJson(buildUrl(config, "/plugin/auth/profile"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.accessToken}`
      }
    });
    if (!response?.ok) {
      throw new Error(response?.message || "登录状态已失效，请重新登录");
    }
    return session;
  } catch {
    await clearAuthSession();
    return null;
  }
}

export async function ensureAuthSession(config) {
  const session = await getAuthSession();
  if (!session) {
    return null;
  }
  if (!session.expiresAt || session.expiresAt - 60 <= Math.floor(Date.now() / 1000)) {
    return await refreshAuthSession(config, session);
  }
  return session;
}

export async function rpc(config, session, name, body = {}) {
  ensureConfig(config);
  if (!session?.accessToken) {
    throw new Error("登录状态已失效，请重新登录");
  }
  const pathByName = {
    editor_plugin_profile: "/plugin/auth/profile",
    editor_plugin_feed: "/plugin/feed/items",
    editor_plugin_state: "/plugin/feed/state",
    editor_plugin_mark_seen: "/plugin/feed/mark-seen",
    editor_plugin_submit_feedback: "/plugin/feed/feedback"
  };
  const path = pathByName[name];
  if (!path) {
    throw new Error(`Unsupported plugin RPC: ${name}`);
  }
  const payloadByName = {
    editor_plugin_feed: { limit: body.p_limit },
    editor_plugin_state: { feed_item_ids: body.p_feed_item_ids },
    editor_plugin_mark_seen: body,
    editor_plugin_submit_feedback: body
  };
  const response = await requestJson(buildUrl(config, path), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${session.accessToken}`
    },
    body: JSON.stringify(payloadByName[name] || {})
  });
  if (!response?.ok) {
    throw new Error(response?.message || "请求失败");
  }
  return response.data ?? null;
}
