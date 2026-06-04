// 监听扩展图标点击事件
chrome.action.onClicked.addListener((tab) => {
    // 打开侧边栏
    chrome.sidePanel.open({windowId: tab.windowId}).then();
});

// 监听消息以填充 tweet info
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "FILL_TWEET_INFO") {
        chrome.storage.local.set({tweetData: message.data}, () => {
            chrome.sidePanel.open({windowId: sender.tab.windowId}).then();
        });
    }
});

// 安装或更新时的初始化
chrome.runtime.onInstalled.addListener(() => {
    console.log("Google AI 助手已安装");
});

