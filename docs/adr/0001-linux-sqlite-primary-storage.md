---
status: accepted
---

# Linux SQLite 作为唯一活动数据库

OdAIly 选择 Linux 本机 SQLite 承载生产业务、队列、搜索缓存和运行遥测，并移除 Supabase 的长期运行依赖，以消除远程连接池、长事务和浏览器直连数据库造成的故障面。新版本只允许 `sqlite` 活动后端，不包含可切回 Postgres 的运行分支；Supabase 在切换点冻结为旧版本的回切基线，不做持续双写。灾难回切必须切回切换前 commit 和环境文件，允许舍弃 SQLite 时期数据，但去重账本仍须用于评估外部副作用是否会重放。
