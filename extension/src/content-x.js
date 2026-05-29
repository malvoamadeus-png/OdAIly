const BUTTON_SELECTOR = "[data-odaily-news-gen-button='true']";
const MESSAGE_TYPE = "ODAILY_NEWS_GEN_CAPTURED";
const LOGO_URL = "https://oss.odaily.top/image/logo-primary.svg";

function safeText(element) {
  return element?.textContent?.trim() || "";
}

function findHandle(userNameRoot) {
  if (!userNameRoot) {
    return "";
  }
  for (const span of userNameRoot.querySelectorAll("span")) {
    const text = safeText(span);
    if (text.startsWith("@")) {
      return text;
    }
  }
  return "";
}

function extractTweetPayload(tweet) {
  const text = safeText(tweet.querySelector("div[data-testid='tweetText']"));
  if (!text) {
    return null;
  }

  const userNameRoot = tweet.querySelector("div[data-testid='User-Name']");
  const authorDisplayName = safeText(userNameRoot?.querySelector("a[role='link'] span"));
  const authorHandle = findHandle(userNameRoot);
  const timeLink = tweet.querySelector("time")?.closest("a");
  const postUrl = timeLink?.href || "";
  const postIdMatch = postUrl.match(/status\/(\d+)/);
  const postedAt = tweet.querySelector("time")?.getAttribute("datetime") || "";

  return {
    source_type: "x_post",
    platform: "x",
    post_text: text,
    post_url: postUrl || null,
    post_id: postIdMatch?.[1] || null,
    author_display_name: authorDisplayName || null,
    author_handle: authorHandle || null,
    posted_at: postedAt || null
  };
}

function addButtonToTweet(tweet) {
  if (tweet.querySelector(BUTTON_SELECTOR)) {
    return;
  }
  const actions = tweet.querySelector("div[role='group']");
  if (!actions) {
    return;
  }

  const button = document.createElement("img");
  button.src = LOGO_URL;
  button.alt = "Odaily";
  button.title = "抽取到 OdAIly 快讯生成";
  button.setAttribute("data-odaily-news-gen-button", "true");
  button.style.width = "18.75px";
  button.style.height = "18.75px";
  button.style.margin = "auto 10px";
  button.style.border = "none";
  button.style.borderRadius = "50%";
  button.style.cursor = "pointer";

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const payload = extractTweetPayload(tweet);
    if (!payload) {
      return;
    }
    chrome.runtime.sendMessage({
      type: MESSAGE_TYPE,
      payload
    }).catch(() => undefined);
  });

  actions.appendChild(button);
}

function scanTweets() {
  for (const tweet of document.querySelectorAll("article[data-testid='tweet']")) {
    addButtonToTweet(tweet);
  }
}

const observer = new MutationObserver(() => {
  scanTweets();
});

observer.observe(document.body, { childList: true, subtree: true });
scanTweets();
