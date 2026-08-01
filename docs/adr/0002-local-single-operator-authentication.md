---
status: accepted
---

# 网页与插件统一使用本地单操作者认证

OdAIly 新版本只保留一个本地操作者身份，网页和 Chrome 插件通过同一个 Linux 后端登录并使用不透明 Bearer session；bcrypt 密码哈希、账号状态和会话全部保存在本地 SQLite，不再验证或回退到 Supabase Auth。部署内置账号为 `odaily2026@gmail.com`，密码只以 bcrypt 哈希进入代码和数据库；浏览器所有业务数据均经后端 API 访问 SQLite。Supabase 身份只随旧版本保留，用于灾难回切。
