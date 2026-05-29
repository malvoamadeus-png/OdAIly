import {
  fetchEditorProfile,
  fetchFeedItems,
  fetchFeedState,
  markSeen,
  submitFeedback
} from "./lib/feed.js";
import {
  generateNewsflash,
  searchNewsGeneration
} from "./lib/news-gen.js";
import { playNotificationBeep } from "./lib/sound.js";
import {
  clearAuthSession,
  getActiveTopTab,
  getNewsGenDraft,
  getNewsGenResult,
  getPanelSessionId,
  getSettings,
  saveActiveTopTab,
  saveNewsGenResult,
  saveSettings
} from "./lib/storage.js";
import {
  ensureAuthSession,
  refreshAuthSession,
  signInWithPassword,
  signOut
} from "./lib/supabase.js";

const app = document.getElementById("app");
const FEED_TAB = "feed";
const NEWS_GEN_TAB = "news_gen";

const state = {
  settings: null,
  session: null,
  profile: null,
  activeTopTab: FEED_TAB,
  panelSessionId: null,
  loading: true,
  error: "",
  loginError: "",
  pollTimer: null,
  soundCooldownUntil: 0,
  lastFeedIds: new Set(),
  feedItems: [],
  feedState: new Map(),
  seenFeedKeys: new Set(),
  expandedFeedKeys: new Set(),
  newsGenDraft: null,
  newsGenResult: null,
  newsGenRequestState: {
    mode: null,
    loading: false,
    error: ""
  }
};

function sanitizeTopTab(value) {
  return value === NEWS_GEN_TAB ? NEWS_GEN_TAB : FEED_TAB;
}

function shanghaiTime(value) {
  if (!value) {
    return "--";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function relativeTime(value) {
  if (!value) {
    return "--";
  }
  const deltaMs = Date.now() - new Date(value).getTime();
  const deltaMinutes = Math.round(deltaMs / 60000);
  if (deltaMinutes < 1) {
    return "刚刚";
  }
  if (deltaMinutes < 60) {
    return `${deltaMinutes} 分钟前`;
  }
  const deltaHours = Math.round(deltaMinutes / 60);
  if (deltaHours < 24) {
    return `${deltaHours} 小时前`;
  }
  return shanghaiTime(value);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function linkifyText(value) {
  const text = String(value ?? "");
  const regex = /https?:\/\/[^\s]+/g;
  let lastIndex = 0;
  let html = "";
  for (const match of text.matchAll(regex)) {
    const url = match[0];
    const index = match.index ?? 0;
    html += escapeHtml(text.slice(lastIndex, index));
    html += `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>`;
    lastIndex = index + url.length;
  }
  html += escapeHtml(text.slice(lastIndex));
  return html.replaceAll("\n", "<br />");
}

function sanitizeNewsGenDraft(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const postText = String(value.post_text || "").trim();
  if (!postText) {
    return null;
  }
  return {
    source_type: "x_post",
    platform: "x",
    post_text: postText,
    post_url: value.post_url ? String(value.post_url).trim() : null,
    post_id: value.post_id ? String(value.post_id).trim() : null,
    author_display_name: value.author_display_name ? String(value.author_display_name).trim() : null,
    author_handle: value.author_handle ? String(value.author_handle).trim() : null,
    posted_at: value.posted_at ? String(value.posted_at).trim() : null
  };
}

function renderBadges(item) {
  return (Array.isArray(item.badges) ? item.badges : [])
    .filter((badge) => badge && badge.value)
    .map(
      (badge) =>
        `<span class="badge badge--${escapeHtml(badge.tone || "neutral")}" title="${escapeHtml(
          badge.label || ""
        )}">${escapeHtml(badge.value)}</span>`
    )
    .join("");
}

function splitFeedItems() {
  const high = [];
  const low = [];
  for (const item of state.feedItems) {
    if (item.lane === "low") {
      low.push(item);
    } else {
      high.push(item);
    }
  }
  return { high, low };
}

function feedKeyOf(feedKind, feedItemId) {
  return `${feedKind}:${feedItemId}`;
}

function feedKey(item) {
  return feedKeyOf(item.feed_kind, item.feed_item_id);
}

function feedbackKindLabel(item) {
  if (item.feed_kind === "auditor_alert") {
    return "审核者";
  }
  if (item.feed_kind === "writer3_context") {
    return "此前消息";
  }
  return item.status_label || "待处理";
}

function cardFeedbackStatus(item) {
  const row = state.feedState.get(feedKey(item));
  if (!row?.latest_feedback) {
    return null;
  }
  return {
    label: row.latest_feedback === "accept" ? "已接受" : "已拒绝",
    tone: row.latest_feedback === "accept" ? "success" : "danger"
  };
}

function renderFeedCard(item) {
  const itemKey = feedKey(item);
  const feedbackStatus = cardFeedbackStatus(item);
  const isFeedback = item.action_schema?.type === "feedback";
  const isWriter3 = item.feed_kind === "writer3_context";
  const isHighLane = item.lane !== "low";
  const isExpanded = state.expandedFeedKeys.has(itemKey);
  const safeTitle = String(item.title || item.summary || `${item.feed_kind} 消息`);
  const safeSummary = String(item.summary || item.title || "暂无摘要");
  const statusChipLabel = isFeedback
    ? feedbackStatus?.label || feedbackKindLabel(item)
    : item.status_label;
  const statusChipTone = isFeedback && feedbackStatus
    ? feedbackStatus.tone
    : item.status_tone || "neutral";
  const detailUrl = item.detail_url || item.source_url || "";
  const badgeHtml = renderBadges(item);
  const feedbackActionsHtml =
    isFeedback && !feedbackStatus
      ? `
        <button class="textButton actionButton" data-action="accept" data-feed-id="${escapeHtml(
          item.feed_item_id
        )}" data-feed-kind="${escapeHtml(item.feed_kind)}">接受</button>
        <button class="textButton actionButton rejectButton" data-action="reject" data-feed-id="${escapeHtml(
          item.feed_item_id
        )}" data-feed-kind="${escapeHtml(item.feed_kind)}">拒绝</button>
      `
      : "";
  const footerActionsHtml = isWriter3
    ? `
        <button class="textButton cardToolButton" data-action="toggle-expand" data-feed-id="${escapeHtml(
          item.feed_item_id
        )}" data-feed-kind="${escapeHtml(item.feed_kind)}">${isExpanded ? "收起" : "展开全文"}</button>
        <button class="textButton cardToolButton" data-action="copy-summary" data-feed-id="${escapeHtml(
          item.feed_item_id
        )}" data-feed-kind="${escapeHtml(item.feed_kind)}">复制全文</button>
      `
    : "";

  return `
    <article class="feedCard feedCard--${escapeHtml(item.feed_kind)}${
      isExpanded ? " feedCard--expanded" : ""
    }" data-feed-id="${escapeHtml(item.feed_item_id)}" data-feed-kind="${escapeHtml(
      item.feed_kind
    )}">
      <div class="feedCard__rail feedCard__rail--${escapeHtml(item.feed_kind)}"></div>
      <div class="feedCard__body">
        <div class="feedCard__top">
          <div class="feedCard__meta">
            <span class="statusChip statusChip--${escapeHtml(statusChipTone)}">${escapeHtml(
              statusChipLabel
            )}</span>
            ${badgeHtml}
          </div>
          <div class="feedCard__quickActions">
            <span class="timeText" title="${escapeHtml(shanghaiTime(item.occurred_at))}">${escapeHtml(
              relativeTime(item.occurred_at)
            )}</span>
            ${feedbackActionsHtml}
          </div>
        </div>
        <h3 class="feedCard__title${isHighLane ? " feedCard__title--toggle" : ""}">
          ${
            detailUrl && !isHighLane
              ? `<a href="${escapeHtml(detailUrl)}" target="_blank" rel="noreferrer">${escapeHtml(safeTitle)}</a>`
              : escapeHtml(safeTitle)
          }
        </h3>
        <p class="feedCard__summary${isHighLane ? " feedCard__summary--toggle" : ""}${
          isExpanded ? " feedCard__summary--expanded" : ""
        }">${linkifyText(safeSummary)}</p>
        ${
          footerActionsHtml
            ? `
              <div class="feedCard__footer">
                <div class="feedCard__actions">${footerActionsHtml}</div>
              </div>
            `
            : ""
        }
      </div>
    </article>
  `;
}

function renderFeedSection() {
  const { high, low } = splitFeedItems();
  const ratio = Math.max(0.45, Math.min(0.8, Number(state.settings?.splitterRatio || 0.65)));

  return `
    ${
      state.error
        ? `<div class="inlineError">${escapeHtml(state.error)}</div>`
        : ""
    }
    <div class="splitLayout" id="splitLayout" style="--top-ratio:${ratio}">
      <section class="lanePanel lanePanel--high">
        <div class="laneHeader">
          <div>
            <h2>高频区</h2>
            <p>新快讯 · 审核者</p>
          </div>
          <span class="laneCount">${high.length}</span>
        </div>
        <div class="laneList" id="highLane">
          ${high.length ? high.map(renderFeedCard).join("") : `<div class="emptyState">暂无高频消息</div>`}
        </div>
      </section>

      <div class="splitter" id="splitter" title="拖动调整上下分区高度"></div>

      <section class="lanePanel lanePanel--low">
        <div class="laneHeader">
          <div>
            <h2>低频区</h2>
            <p>此前消息 · 巨鲸</p>
          </div>
          <span class="laneCount">${low.length}</span>
        </div>
        <div class="laneList" id="lowLane">
          ${low.length ? low.map(renderFeedCard).join("") : `<div class="emptyState">暂无低频消息</div>`}
        </div>
      </section>
    </div>
  `;
}

function renderSearchCandidates(candidates) {
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return `<div class="emptyState">暂无候选历史项</div>`;
  }
  return candidates
    .map(
      (item) => `
        <article class="newsGenCandidate">
          <div class="newsGenCandidate__top">
            <span class="statusChip statusChip--neutral">${escapeHtml(
              item.target_type === "inflight_candidate" ? "运行中候选" : "Odaily 历史"
            )}</span>
            <span class="timeText">${escapeHtml(
              item.published_at ? shanghaiTime(item.published_at) : `相似度 ${Number(item.similarity || 0).toFixed(4)}`
            )}</span>
          </div>
          <h4>${escapeHtml(item.title || "无标题")}</h4>
          <p class="newsGenCandidate__meta">相似度 ${Number(item.similarity || 0).toFixed(4)}</p>
          ${
            item.source_url
              ? `<a class="newsGenLink" href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">打开链接</a>`
              : ""
          }
        </article>
      `
    )
    .join("");
}

function renderNewsGenResult() {
  const requestState = state.newsGenRequestState;
  if (requestState.loading) {
    return `<div class="emptyState">正在${escapeHtml(requestState.mode === "generate" ? "生成快讯" : "执行 AI 查重")}...</div>`;
  }
  if (requestState.error) {
    return `<div class="inlineError">${escapeHtml(requestState.error)}</div>`;
  }
  if (!state.newsGenResult) {
    return `<div class="emptyState">点击上方按钮开始 AI 查重或生成快讯</div>`;
  }

  if (state.newsGenResult.kind === "search") {
    return `
      <div class="newsGenResultStack">
        <section class="panel newsGenResultPanel">
          <div class="newsGenResultPanel__head">
            <h3>AI查重结果</h3>
            <span class="statusChip statusChip--${
              state.newsGenResult.is_duplicate ? "warning" : "success"
            }">${escapeHtml(state.newsGenResult.is_duplicate ? "疑似重复" : "未发现明显重复")}</span>
          </div>
          <p class="helperText">${escapeHtml(state.newsGenResult.summary || "")}</p>
        </section>
        <section class="panel newsGenResultPanel">
          <div class="newsGenResultPanel__head">
            <h3>候选历史项</h3>
          </div>
          <div class="newsGenCandidates">
            ${renderSearchCandidates(state.newsGenResult.top_candidates)}
          </div>
        </section>
      </div>
    `;
  }

  return `
    <section class="panel newsGenResultPanel">
      <div class="newsGenResultPanel__head">
        <h3>生成快讯</h3>
        <span class="statusChip statusChip--info">${escapeHtml(
          state.newsGenResult.route === "onchain"
            ? "链上"
            : state.newsGenResult.route === "funding"
              ? "融资"
              : "常规"
        )}</span>
      </div>
      <div class="newsGenGenerated">
        <h4>${escapeHtml(state.newsGenResult.title || "未命名快讯")}</h4>
        <p class="newsGenGenerated__content">${linkifyText(state.newsGenResult.content || "")}</p>
      </div>
      <div class="formActions">
        <button id="copyGeneratedButton" class="primaryButton">复制</button>
      </div>
    </section>
  `;
}

function renderNewsGenSection() {
  const draft = state.newsGenDraft;
  const hasDraft = Boolean(draft?.post_text);
  return `
    ${
      state.error
        ? `<div class="inlineError">${escapeHtml(state.error)}</div>`
        : ""
    }
    <div class="newsGenLayout">
      <section class="panel newsGenSourcePanel">
        <div class="newsGenSection__head">
          <div>
            <h2>原文</h2>
            <p class="helperText">来自 X 平台按钮抽取</p>
          </div>
          <div class="formActions">
            <button id="newsGenSearchButton" class="secondaryButton" ${hasDraft ? "" : "disabled"}>AI查重</button>
            <button id="newsGenGenerateButton" class="primaryButton" ${hasDraft ? "" : "disabled"}>生成快讯</button>
          </div>
        </div>
        ${
          hasDraft
            ? `
              <div class="newsGenMeta">
                <span>${escapeHtml(draft.author_display_name || "未知作者")}</span>
                ${
                  draft.author_handle
                    ? `<span class="subtleText">${escapeHtml(draft.author_handle)}</span>`
                    : ""
                }
                ${
                  draft.posted_at
                    ? `<span class="subtleText">${escapeHtml(shanghaiTime(draft.posted_at))}</span>`
                    : ""
                }
                ${
                  draft.post_url
                    ? `<a class="newsGenLink" href="${escapeHtml(draft.post_url)}" target="_blank" rel="noreferrer">打开 X 原文</a>`
                    : ""
                }
              </div>
              <div class="newsGenSourceText">${linkifyText(draft.post_text)}</div>
            `
            : `<div class="emptyState">请先在 X 帖文操作区点击 Odaily 按钮抽取正文</div>`
        }
      </section>
      <div class="newsGenResultArea">
        ${renderNewsGenResult()}
      </div>
    </div>
  `;
}

function renderAuthedShell() {
  const currentTitle = state.activeTopTab === NEWS_GEN_TAB ? "快讯生成" : "信息流";
  app.innerHTML = `
    <div class="appShell appShell--tabs">
      <header class="toolbar">
        <div>
          <p class="eyebrow">OdAIly</p>
          <h1>${escapeHtml(currentTitle)}</h1>
        </div>
        <div class="toolbar__actions">
          ${
            state.activeTopTab === FEED_TAB
              ? `<button id="refreshButton" class="iconButton" title="刷新">刷新</button>`
              : ""
          }
          <button id="openOptionsButton" class="iconButton" title="设置">设置</button>
          <button id="logoutButton" class="iconButton" title="退出">退出</button>
        </div>
      </header>

      <section class="tabBar">
        <button class="tabButton ${state.activeTopTab === FEED_TAB ? "tabButton--active" : ""}" data-top-tab="${FEED_TAB}">信息流</button>
        <button class="tabButton ${state.activeTopTab === NEWS_GEN_TAB ? "tabButton--active" : ""}" data-top-tab="${NEWS_GEN_TAB}">快讯生成</button>
      </section>

      <section class="userStrip">
        <span>${escapeHtml(state.profile?.display_name || state.profile?.email || "")}</span>
        <span class="subtleText">${escapeHtml(state.profile?.email || "")}</span>
      </section>

      <main class="mainView">
        ${state.activeTopTab === NEWS_GEN_TAB ? renderNewsGenSection() : renderFeedSection()}
      </main>
    </div>
  `;

  wireToolbar();
  wireTopTabs();
  if (state.activeTopTab === FEED_TAB) {
    wireFeedInteractions();
  } else {
    wireNewsGenInteractions();
  }
}

function renderConfigGate() {
  app.innerHTML = `
    <div class="centerPage">
      <section class="panel gatePanel">
        <p class="eyebrow">OdAIly</p>
        <h1>插件内置配置缺失</h1>
        <p class="helperText">当前包内没有可用的 Supabase 或快讯生成连接参数，请联系管理员重新打包插件。</p>
        ${state.error ? `<div class="inlineError">${escapeHtml(state.error)}</div>` : ""}
        <button id="openOptionsButton" class="primaryButton">打开设置</button>
      </section>
    </div>
  `;
  document.getElementById("openOptionsButton").addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });
}

function renderLogin() {
  app.innerHTML = `
    <div class="centerPage">
      <section class="panel loginPanel">
        <p class="eyebrow">OdAIly</p>
        <h1>登录 OdAIly 插件</h1>
        <p class="helperText">使用插件白名单中的邮箱密码登录。</p>
        <form id="loginForm" class="loginForm">
          <label class="field">
            <span>邮箱</span>
            <input id="email" type="email" autocomplete="username" required />
          </label>
          <label class="field">
            <span>密码</span>
            <input id="password" type="password" autocomplete="current-password" required />
          </label>
          ${
            state.loginError || state.error
              ? `<div class="inlineError">${escapeHtml(state.loginError || state.error)}</div>`
              : ""
          }
          <div class="formActions">
            <button type="submit" class="primaryButton">登录</button>
            <button type="button" id="openOptionsButton" class="secondaryButton">设置</button>
          </div>
        </form>
      </section>
    </div>
  `;

  document.getElementById("openOptionsButton").addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });
  document.getElementById("loginForm").addEventListener("submit", handleLogin);
}

function renderLoading() {
  app.innerHTML = `
    <div class="centerPage">
      <section class="panel gatePanel">
        <p class="eyebrow">OdAIly</p>
        <h1>正在加载</h1>
        <p class="helperText">正在同步登录状态和侧边栏数据。</p>
      </section>
    </div>
  `;
}

function wireToolbar() {
  document.getElementById("openOptionsButton")?.addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });
  document.getElementById("refreshButton")?.addEventListener("click", async () => {
    await refreshFeed(true);
  });
  document.getElementById("logoutButton")?.addEventListener("click", async () => {
    await signOut(state.settings);
    await resetAuthState("");
  });
}

function wireTopTabs() {
  for (const button of app.querySelectorAll(".tabButton")) {
    button.addEventListener("click", async () => {
      const nextTab = sanitizeTopTab(button.dataset.topTab);
      if (nextTab === state.activeTopTab) {
        return;
      }
      state.activeTopTab = nextTab;
      await saveActiveTopTab(nextTab);
      renderAuthedShell();
    });
  }
}

function findFeedItem(feedKind, feedItemId) {
  return state.feedItems.find(
    (item) => item.feed_kind === feedKind && item.feed_item_id === feedItemId
  );
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "true");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

function wireCardToolButtons() {
  for (const button of app.querySelectorAll(".cardToolButton")) {
    button.addEventListener("click", async () => {
      const feedItemId = button.dataset.feedId;
      const feedKind = button.dataset.feedKind;
      const action = button.dataset.action;
      if (!feedItemId || !feedKind || !action) {
        return;
      }
      const itemKey = feedKeyOf(feedKind, feedItemId);
      if (action === "toggle-expand") {
        const summary = button.closest(".feedCard")?.querySelector(".feedCard__summary");
        if (!summary) {
          return;
        }
        if (state.expandedFeedKeys.has(itemKey)) {
          state.expandedFeedKeys.delete(itemKey);
          summary.classList.remove("feedCard__summary--expanded");
          button.textContent = "展开全文";
        } else {
          state.expandedFeedKeys.add(itemKey);
          summary.classList.add("feedCard__summary--expanded");
          button.textContent = "收起";
        }
        return;
      }

      if (action === "copy-summary") {
        const item = findFeedItem(feedKind, feedItemId);
        if (!item) {
          return;
        }
        const originalLabel = button.textContent;
        button.disabled = true;
        try {
          await copyText(item.summary || item.title || "");
          button.textContent = "已复制";
          window.setTimeout(() => {
            button.textContent = originalLabel;
            button.disabled = false;
          }, 1200);
        } catch (error) {
          state.error = error instanceof Error ? error.message : "复制失败";
          button.textContent = originalLabel;
          button.disabled = false;
          render();
        }
      }
    });
  }
}

function wireHighFrequencyToggles() {
  const highLane = document.getElementById("highLane");
  if (!highLane) {
    return;
  }
  for (const card of highLane.querySelectorAll(".feedCard")) {
    const toggleTargets = card.querySelectorAll(".feedCard__title--toggle, .feedCard__summary--toggle");
    for (const target of toggleTargets) {
      target.addEventListener("click", () => {
        const feedItemId = card.getAttribute("data-feed-id");
        const feedKind = card.getAttribute("data-feed-kind");
        if (!feedItemId || !feedKind) {
          return;
        }
        const itemKey = feedKeyOf(feedKind, feedItemId);
        const summary = card.querySelector(".feedCard__summary");
        if (state.expandedFeedKeys.has(itemKey)) {
          state.expandedFeedKeys.delete(itemKey);
          card.classList.remove("feedCard--expanded");
          summary?.classList.remove("feedCard__summary--expanded");
        } else {
          state.expandedFeedKeys.add(itemKey);
          card.classList.add("feedCard--expanded");
          summary?.classList.add("feedCard__summary--expanded");
        }
      });
    }
  }
}

function isSessionExpiredError(error) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /jwt expired|invalid jwt|token has expired|refresh token|登录状态已失效/i.test(message);
}

async function resetAuthState(message = "") {
  await clearAuthSession();
  state.session = null;
  state.profile = null;
  state.feedItems = [];
  state.feedState = new Map();
  state.lastFeedIds = new Set();
  state.seenFeedKeys.clear();
  state.expandedFeedKeys.clear();
  state.error = "";
  state.loginError = message;
  state.newsGenRequestState = { mode: null, loading: false, error: "" };
  stopPolling();
  render();
}

async function ensureLiveSession(forceRefresh = false) {
  state.session = forceRefresh
    ? await refreshAuthSession(state.settings, state.session)
    : await ensureAuthSession(state.settings);
  if (!state.session) {
    await resetAuthState("登录状态已失效，请重新登录");
    throw new Error("登录状态已失效，请重新登录");
  }
  return state.session;
}

async function withSessionRetry(task) {
  await ensureLiveSession(false);
  try {
    return await task();
  } catch (error) {
    if (!isSessionExpiredError(error)) {
      throw error;
    }
  }
  await ensureLiveSession(true);
  return await task();
}

function wireFeedbackButtons() {
  for (const button of app.querySelectorAll(".actionButton")) {
    button.addEventListener("click", async () => {
      const feedItemId = button.dataset.feedId;
      const feedKind = button.dataset.feedKind;
      const action = button.dataset.action;
      if (!feedItemId || !feedKind || !action) {
        return;
      }
      button.disabled = true;
      try {
        await withSessionRetry(async () => {
          await submitFeedback(state.settings, state.session, {
            p_feed_item_id: feedItemId,
            p_feed_kind: feedKind,
            p_feedback: action,
            p_session_id: state.panelSessionId,
            p_extra_json: {}
          });
        });
        await refreshFeed(false);
      } catch (error) {
        state.error = error instanceof Error ? error.message : "提交反馈失败";
        renderAuthedShell();
      } finally {
        button.disabled = false;
      }
    });
  }
}

function wireSplitter() {
  const splitter = document.getElementById("splitter");
  const layout = document.getElementById("splitLayout");
  if (!splitter || !layout) {
    return;
  }
  let dragging = false;

  const onMove = async (event) => {
    if (!dragging) {
      return;
    }
    const rect = layout.getBoundingClientRect();
    const ratio = Math.max(0.45, Math.min(0.8, (event.clientY - rect.top) / rect.height));
    layout.style.setProperty("--top-ratio", String(ratio));
    state.settings.splitterRatio = ratio;
    await saveSettings({ splitterRatio: ratio });
  };

  splitter.addEventListener("pointerdown", (event) => {
    dragging = true;
    splitter.setPointerCapture(event.pointerId);
  });
  splitter.addEventListener("pointerup", () => {
    dragging = false;
  });
  splitter.addEventListener("pointercancel", () => {
    dragging = false;
  });
  splitter.addEventListener("pointermove", onMove);
}

function wireSeenTracking() {
  const observer = new IntersectionObserver(
    async (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting || entry.intersectionRatio < 0.45) {
          continue;
        }
        const feedItemId = entry.target.getAttribute("data-feed-id");
        const feedKind = entry.target.getAttribute("data-feed-kind");
        if (!feedItemId || !feedKind) {
          continue;
        }
        const key = feedKeyOf(feedKind, feedItemId);
        if (state.seenFeedKeys.has(key)) {
          continue;
        }
        state.seenFeedKeys.add(key);
        observer.unobserve(entry.target);
        try {
          await withSessionRetry(async () => {
            await markSeen(state.settings, state.session, {
              p_feed_item_id: feedItemId,
              p_feed_kind: feedKind,
              p_session_id: state.panelSessionId,
              p_extra_json: {}
            });
          });
        } catch {
          state.seenFeedKeys.delete(key);
        }
      }
    },
    { threshold: [0.45] }
  );

  for (const card of app.querySelectorAll(".feedCard")) {
    observer.observe(card);
  }
}

function wireFeedInteractions() {
  wireFeedbackButtons();
  wireCardToolButtons();
  wireHighFrequencyToggles();
  wireSplitter();
  wireSeenTracking();
}

async function runNewsGenAction(mode) {
  const draft = state.newsGenDraft;
  if (!draft?.post_text || state.newsGenRequestState.loading) {
    return;
  }
  state.newsGenRequestState = { mode, loading: true, error: "" };
  state.error = "";
  renderAuthedShell();
  try {
    const payload = sanitizeNewsGenDraft(draft);
    const result = await withSessionRetry(async () => {
      if (mode === "generate") {
        return await generateNewsflash(state.settings, state.session, payload);
      }
      return await searchNewsGeneration(state.settings, state.session, payload);
    });
    state.newsGenResult = result;
    await saveNewsGenResult(result);
    state.newsGenRequestState = { mode, loading: false, error: "" };
    renderAuthedShell();
  } catch (error) {
    state.newsGenRequestState = {
      mode,
      loading: false,
      error: error instanceof Error ? error.message : "请求失败"
    };
    renderAuthedShell();
  }
}

function wireNewsGenInteractions() {
  document.getElementById("newsGenSearchButton")?.addEventListener("click", async () => {
    await runNewsGenAction("search");
  });
  document.getElementById("newsGenGenerateButton")?.addEventListener("click", async () => {
    await runNewsGenAction("generate");
  });
  document.getElementById("copyGeneratedButton")?.addEventListener("click", async () => {
    if (!state.newsGenResult?.content) {
      return;
    }
    try {
      await copyText(state.newsGenResult.content);
      state.activeTopTab = FEED_TAB;
      state.newsGenRequestState = { mode: null, loading: false, error: "" };
      await saveActiveTopTab(FEED_TAB);
      renderAuthedShell();
    } catch (error) {
      state.newsGenRequestState = {
        mode: "generate",
        loading: false,
        error: error instanceof Error ? error.message : "复制失败"
      };
      renderAuthedShell();
    }
  });
}

async function handleLogin(event) {
  event.preventDefault();
  const email = document.getElementById("email").value;
  const password = document.getElementById("password").value;
  state.loginError = "";
  state.error = "";
  render();
  try {
    state.session = await signInWithPassword(state.settings, email, password);
    await loadProfileAndFeed();
  } catch (error) {
    state.loginError = error instanceof Error ? error.message : "登录失败";
    render();
  }
}

function maybePlaySound(nextItems) {
  if (!state.settings?.soundEnabled) {
    return;
  }
  if (document.visibilityState !== "visible") {
    return;
  }
  const scope = state.settings.soundScope || "high";
  const previousIds = state.lastFeedIds;
  const newItems = nextItems.filter((item) => !previousIds.has(item.feed_item_id));
  const hit = newItems.some((item) => (scope === "all" ? true : item.lane === "high"));
  if (!hit) {
    return;
  }
  if (Date.now() < state.soundCooldownUntil) {
    return;
  }
  state.soundCooldownUntil = Date.now() + 4000;
  playNotificationBeep(state.settings.soundVolume || "medium");
}

async function refreshFeed(shouldSound) {
  try {
    const { items, feedState } = await withSessionRetry(async () => {
      const nextItems = await fetchFeedItems(state.settings, state.session, 120);
      const nextFeedState = await fetchFeedState(
        state.settings,
        state.session,
        nextItems.map((item) => item.feed_item_id)
      );
      return { items: nextItems, feedState: nextFeedState };
    });
    const nextIds = new Set(items.map((item) => item.feed_item_id));
    const validKeys = new Set(items.map(feedKey));
    if (shouldSound) {
      maybePlaySound(items);
    }
    state.feedItems = items;
    state.lastFeedIds = nextIds;
    state.feedState = feedState;
    state.expandedFeedKeys = new Set(
      [...state.expandedFeedKeys].filter((itemKey) => validKeys.has(itemKey))
    );
    state.error = "";
    if (state.activeTopTab === FEED_TAB) {
      renderAuthedShell();
    }
  } catch (error) {
    state.error = error instanceof Error ? error.message : "信息流刷新失败";
    if (state.session) {
      renderAuthedShell();
    } else {
      render();
    }
  }
}

async function loadProfileAndFeed() {
  state.loading = true;
  render();
  state.profile = await withSessionRetry(async () =>
    await fetchEditorProfile(state.settings, state.session)
  );
  if (!state.profile?.enabled) {
    await resetAuthState("当前账号未加入插件白名单或已被停用");
    return;
  }
  state.lastFeedIds = new Set();
  state.seenFeedKeys.clear();
  state.expandedFeedKeys.clear();
  await refreshFeed(false);
  state.loading = false;
  startPolling();
  renderAuthedShell();
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startPolling() {
  stopPolling();
  const intervalMs = Math.max(10, Number(state.settings?.pollIntervalSeconds || 15)) * 1000;
  state.pollTimer = window.setInterval(async () => {
    await refreshFeed(true);
  }, intervalMs);
}

function render() {
  if (!state.settings?.supabaseUrl || !state.settings?.supabaseAnonKey || !state.settings?.pluginApiBaseUrl) {
    renderConfigGate();
    return;
  }
  if (!state.session) {
    renderLogin();
    return;
  }
  if (state.loading && !state.profile) {
    renderLoading();
    return;
  }
  renderAuthedShell();
}

async function boot() {
  state.settings = await getSettings();
  state.panelSessionId = await getPanelSessionId();
  state.activeTopTab = sanitizeTopTab(await getActiveTopTab());
  state.newsGenDraft = sanitizeNewsGenDraft(await getNewsGenDraft());
  state.newsGenResult = await getNewsGenResult();
  if (!state.settings.supabaseUrl || !state.settings.supabaseAnonKey || !state.settings.pluginApiBaseUrl) {
    render();
    return;
  }
  state.session = await ensureAuthSession(state.settings);
  state.loading = Boolean(state.session);
  render();
  if (state.session) {
    await loadProfileAndFeed();
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.session) {
    refreshFeed(false).catch(() => undefined);
  }
});

chrome.storage.onChanged.addListener(async (changes, areaName) => {
  if (areaName !== "local") {
    return;
  }
  if (changes.activeTopTab) {
    state.activeTopTab = sanitizeTopTab(changes.activeTopTab.newValue);
  }
  if (changes.newsGenDraft) {
    state.newsGenDraft = sanitizeNewsGenDraft(changes.newsGenDraft.newValue);
    state.newsGenRequestState = { mode: null, loading: false, error: "" };
  }
  if (changes.newsGenResult) {
    state.newsGenResult = changes.newsGenResult.newValue ?? null;
  }
  const authConfigChanged = Boolean(changes.supabaseUrl || changes.supabaseAnonKey || changes.pluginApiBaseUrl);
  if (
    authConfigChanged ||
    changes.pollIntervalSeconds ||
    changes.splitterRatio ||
    changes.soundEnabled ||
    changes.soundScope ||
    changes.soundVolume
  ) {
    state.settings = await getSettings();
    state.error = "";
    state.loginError = "";
    if (authConfigChanged) {
      stopPolling();
      state.session = await ensureAuthSession(state.settings);
      state.profile = null;
      state.feedItems = [];
      state.feedState = new Map();
      state.lastFeedIds = new Set();
      state.seenFeedKeys.clear();
      state.expandedFeedKeys.clear();
      if (state.session) {
        await loadProfileAndFeed();
        return;
      }
    }
  }
  if (state.session) {
    if (changes.pollIntervalSeconds) {
      startPolling();
    }
    renderAuthedShell();
  } else {
    render();
  }
});

boot().catch((error) => {
  state.error = error instanceof Error ? error.message : "插件初始化失败";
  state.loading = false;
  render();
});
