// content-script.js
console.log("Content script loaded on Twitter page.");

// 使用 MutationObserver 观察页面变化
const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.addedNodes.length) {
            addCustomButtons();
        }
    });
});

observer.observe(document.body, {childList: true, subtree: true});

// 初始调用
addCustomButtons();

function addCustomButtons() {
    // 找到所有推文
    const tweets = document.querySelectorAll("article[data-testid=\"tweet\"]");

    tweets.forEach((tweet) => {
        // 检查是否已添加按钮
        if (tweet.querySelector(".custom-button")) return;

        // 找到操作按钮容器，通常是 div[data-testid="tweetButtonInline"]
        const actions = tweet.querySelector("div[role=\"group\"]");
        if (!actions) return;

        // 创建自定义按钮
        const imageElement = document.createElement("img");
        imageElement.src = "https://oss.odaily.top/image/logo-primary.svg";
        imageElement.alt = "Odaily";
        imageElement.className = "custom-button";
        imageElement.style.width = "18.75px";
        imageElement.style.height = "18.75px";
        imageElement.style.margin = "auto 10px";
        imageElement.style.border = "none";
        imageElement.style.borderRadius = "50%";
        imageElement.style.cursor = "pointer";

        imageElement.addEventListener("click", (e) => {
            e.preventDefault();
            extractTweetInfo(tweet);
        });

        // 添加到操作按钮后面
        actions.appendChild(imageElement);
    });
}

function extractTweetInfo(tweet) {
    // 提取显示名称
    const displayNameElement = tweet.querySelector("div[data-testid=\"User-Name\"] a[role=\"link\"] span");
    const displayName = displayNameElement ? displayNameElement.textContent : "未知";

    // 提取 @handle (用户 ID)
    // const userNameDiv = tweet.querySelector("div[data-testid=\"User-Name\"]");
    // let handle = "未知";
    // if (userNameDiv) {
    //     const spans = userNameDiv.querySelectorAll("span");
    //     for (let span of spans) {
    //         if (span.textContent.startsWith("@")) {
    //             handle = span.textContent;
    //             break;
    //         }
    //     }
    // }

    // 提取推文内容
    const contentElement = tweet.querySelector("div[data-testid=\"tweetText\"]");
    const content = contentElement ? contentElement.textContent : "无内容";

    // 提取时间
    // const timeElement = tweet.querySelector("time");
    // const time = timeElement ? timeElement.dateTime : "未知";

    // 格式化数据
    const formattedContent = `Nickname: ${displayName}\nContent: ${content}`;

    // 发送到 background 以打开 side panel 并填充
    chrome.runtime.sendMessage({
        type: "FILL_TWEET_INFO", data: formattedContent,
    }).then();
}
