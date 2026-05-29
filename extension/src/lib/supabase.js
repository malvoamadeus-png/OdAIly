import {
  clearAuthSession,
  getAuthSession,
  saveAuthSession
} from "./storage.js";

function ensureConfig(config) {
  if (!config.supabaseUrl || !config.supabaseAnonKey) {
    throw new Error("插件内置连接参数缺失，请联系管理员重新打包插件");
  }
}

function buildUrl(config, path) {
  return `${config.supabaseUrl.replace(/\/+$/, "")}${path}`;
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

async function requestJson(url, init) {
  const response = await fetch(url, init);
  if (!response.ok) {
    await parseError(response);
  }
  if (response.status === 204) {
    return null;
  }
  return await response.json();
}

function sessionFromPayload(payload) {
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    expiresAt: Number(payload.expires_at || Math.floor(Date.now() / 1000) + Number(payload.expires_in || 3600)),
    user: {
      id: payload.user?.id || null,
      email: payload.user?.email || null
    }
  };
}

export async function signInWithPassword(config, email, password) {
  ensureConfig(config);
  const payload = await requestJson(buildUrl(config, "/auth/v1/token?grant_type=password"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: config.supabaseAnonKey
    },
    body: JSON.stringify({
      email: email.trim().toLowerCase(),
      password
    })
  });
  const session = sessionFromPayload(payload);
  await saveAuthSession(session);
  return session;
}

export async function signOut(config) {
  const session = await getAuthSession();
  if (session?.accessToken) {
    try {
      await requestJson(buildUrl(config, "/auth/v1/logout"), {
        method: "POST",
        headers: {
          apikey: config.supabaseAnonKey,
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
  if (!session?.refreshToken) {
    await clearAuthSession();
    return null;
  }
  try {
    const payload = await requestJson(buildUrl(config, "/auth/v1/token?grant_type=refresh_token"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        apikey: config.supabaseAnonKey
      },
      body: JSON.stringify({
        refresh_token: session.refreshToken
      })
    });
    const nextSession = sessionFromPayload(payload);
    await saveAuthSession(nextSession);
    return nextSession;
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
  return await requestJson(buildUrl(config, `/rest/v1/rpc/${name}`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: config.supabaseAnonKey,
      Authorization: `Bearer ${session.accessToken}`
    },
    body: JSON.stringify(body)
  });
}
