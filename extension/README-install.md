# OdAIly 信息流插件安装说明

## 1. 初始化数据库

先确保现有主库已经完成：

- `x-init-db`
- `x-process-init-db`
- `auditor-init-db`
- `writer3-init-db`
- `whale-watch-init-db`

然后执行：

```bash
./tools/dev python backend/src/main.py editor-plugin-init-db
```

如果要启用 `快讯生成` 一级模块，还需要启动轻接口服务：

```bash
./tools/dev python backend/src/main.py editor-plugin-api-server --host 0.0.0.0 --port 8765
```

生产环境建议直接安装并启用：

- `deploy/odaily-editor-plugin-api.service`

## 2. 加入值班编辑白名单

```bash
./tools/dev python backend/src/main.py editor-plugin-grant-user --email editor@example.com --display-name "夜班编辑"
./tools/dev python backend/src/main.py editor-plugin-list-users
```

移除用户：

```bash
./tools/dev python backend/src/main.py editor-plugin-revoke-user --email editor@example.com
```

## 3. 加载插件

1. 打开 Chrome。
2. 进入 `chrome://extensions/`。
3. 打开“开发者模式”。
4. 选择“加载已解压的扩展程序”。
5. 选择当前仓库中的 `extension/` 文件夹。

## 4. 配置内置连接

这一版插件已经内置：

- `Supabase URL`
- 公开可分发的 `publishable key`
- `快讯生成` 服务地址

当前内置 `快讯生成` 服务地址为：

- `http://47.76.243.147:8765`

值班编辑不需要手填连接参数。

扩展程序选项页只保留：

- 轮询间隔
- 声音提醒

信息流三分区高度比例在侧边栏内拖拽分隔条调整，并保存到 Chrome 本地 storage。

## 5. 登录

值班编辑使用各自的 Supabase Auth 邮箱密码登录。

只有加入 `editor_plugin_users` 白名单且 `enabled=true` 的用户可以读取信息流并提交反馈。

注意：

- `console_admins` 和 `editor_plugin_users` 不是同一张白名单。
- 能登录控制台，不代表自动有插件权限。
- 需要额外执行 `editor-plugin-grant-user` 把对应邮箱加入插件白名单。
