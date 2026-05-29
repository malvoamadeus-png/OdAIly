const NEWS_GEN_REQUEST_TIMEOUT_MS = 30000;

function ensureApiConfig(config) {
  if (!config.pluginApiBaseUrl) {
    throw new Error("插件内置快讯生成服务地址缺失，请联系管理员重新打包插件");
  }
}

function buildUrl(config, path) {
  return `${config.pluginApiBaseUrl.replace(/\/+$/, "")}${path}`;
}

async function parseError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const message =
    payload?.message ||
    payload?.msg ||
    payload?.error ||
    `${response.status} ${response.statusText}`;
  throw new Error(message);
}

async function requestJson(url, init) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), NEWS_GEN_REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(url, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
  if (!response.ok) {
    await parseError(response);
  }
  if (response.status === 204) {
    return null;
  }
  return await response.json();
}

async function postWithSession(config, session, path, payload) {
  ensureApiConfig(config);
  if (!session?.accessToken) {
    throw new Error("登录状态已失效，请重新登录");
  }
  const response = await requestJson(buildUrl(config, path), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${session.accessToken}`
    },
    body: JSON.stringify(payload)
  });
  if (!response?.ok) {
    throw new Error(response?.message || "请求失败");
  }
  return response.data ?? null;
}

export async function searchNewsGeneration(config, session, payload) {
  return await postWithSession(config, session, "/plugin/news-gen/search", payload);
}

export async function generateNewsflash(config, session, payload) {
  return await postWithSession(config, session, "/plugin/news-gen/generate", payload);
}
