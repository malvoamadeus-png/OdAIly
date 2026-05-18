# 控制台-Prompt

## 职责

控制台-Prompt 用于管理 Prompt 内容和版本，并发布某个版本为当前生效版本。

Prompt 控制台不管理模型选择、reasoning effort、超时、重试或输出 schema。

## 管理范围

控制台支持：

- 查看 Prompt 模板。
- 新建 Prompt 版本。
- 编辑版本内容。
- 发布某个版本为当前生效版本。
- 保留历史版本，便于回滚和追溯。

当前写作模板除了 X 的常规、链上、融资模板外，还需要支持非主流媒体专用模板 `non_mainstream_media_writer`。

相关表：

- `prompt_templates`
- `prompt_template_versions`

## 发布生效

保存草稿或创建新版本时，不通知 worker。

发布版本时，控制台更新 `prompt_templates.active_version_id`，数据库触发 `prompt_config_changed` 通知。处理 worker 收到通知后刷新本地 Prompt 缓存。

worker 启动时也必须主动加载当前生效 Prompt，不能只依赖通知。

## 版本追溯

编写者1每次生成快讯时，需要记录实际使用的 Prompt template key 和 version id。

这用于排查：

- 哪个 Prompt 版本生成了某条快讯。
- Prompt 修改后质量是否变化。
- 是否需要回滚到旧版本。

## 初始模板

初始 Prompt 模板从 `docs/*.txt` seed 到数据库。

当前种子文件应至少包括：

- `docs/常规快讯模板.txt`
- `docs/链上快讯模板.txt`
- `docs/融资快讯模板.txt`
- `docs/外媒模板.txt` -> `non_mainstream_media_writer`

相关命令：

```powershell
python backend\src\main.py x-process-init-db
```

## 相关文档

- `编写者1.md`：Prompt 选择和版本追溯。
- `判断者.md`：判断者 Prompt 结构化输出要求。
