# 老Mi-X 导出助手

> 本插件由 JakeyCha 自研，安装包为 `老Mi-X导出助手1.0.zip`。用于统计 X 发帖数量，避免手工计数。

## 职责

- 统计值班人员在 X 上的发帖数量。
- 配合 `保安号` Hermes 技能使用，记录控评工作量。

## 安装前提

- 使用 Chrome 浏览器。
- 已从 JakeyCha 获取 `老Mi-X导出助手1.0.zip` 及配套安装说明。

## 安装步骤

### 1. 放置安装包

将 zip 放到仓库 `third-party/老Mi-X导出助手/` 目录：

```text
third-party/老Mi-X导出助手/老Mi-X导出助手1.0.zip
```

### 2. 解压

```bash
./tools/install-mi-x-export-helper.sh
```

或手动执行：

```bash
mkdir -p third-party/老Mi-X导出助手/unpacked
unzip -o "third-party/老Mi-X导出助手/老Mi-X导出助手1.0.zip" -d third-party/老Mi-X导出助手/unpacked
```

### 3. 加载 Chrome 扩展

1. 打开 `chrome://extensions/`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择解压后的扩展目录（通常是 `third-party/老Mi-X导出助手/unpacked/` 或其下一级含 `manifest.json` 的目录）

### 4. 按 JakeyCha 说明完成配置

zip 内应附带作者编写的安装说明。若解压后找不到说明文档，或加载扩展时报错，联系 JakeyCha 获取最新包和安装指引。

## 与 OdAIly 插件的关系

- `老Mi-X导出助手` 是独立的 X 统计工具，不属于 OdAIly 仓库内的 `extension/` 信息流插件。
- OdAIly `extension/` 负责快讯生成和信息流；本插件只负责发帖数量统计。
- 两者可同时加载，互不冲突。

## 相关文档

- 保安号技能：`docs/保安号-Hermes技能.md`
