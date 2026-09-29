# 实现与数据流

Agent Task Notifier 当前由 Codex 安装器、事件桥接程序和通知渠道组成。它在本机运行，不需要常驻服务。

```text
Codex notify / Hook
        │
        ▼
notify_bridge.py ──► 事件过滤与消息格式化 ──► 已配置的通知渠道
        │
        └──► 安装前已有的 Codex notify 命令（仅完成事件）
```

## 事件处理

- `setup.py` 为指定的 Codex 实例配置 `notify` 回调，以及 `UserPromptSubmit` 和 `PermissionRequest` Hook。Cockpit 已有的通知回调会保留。
- `UserPromptSubmit` Hook 为主用户轮次写入本地标记，不保存提示词。标记机制启用后，完成事件须匹配该轮次，才会进入通知渠道。桥接程序还会过滤带子 agent 标识的事件和已识别的桌面内部元数据轮次。
- `agent-turn-complete` 事件生成完成通知：标题取最后一条用户输入，正文取最后一条助手回答，并按 UTF-8 字节数截断。桥接程序不调用模型生成摘要。
- `PermissionRequest` Hook 生成等待授权通知。Hook 需要在对应 Codex 实例中通过 `/hooks` 信任；未启用或未信任时，不会收到该类通知。
- 项目名根据事件工作目录与该实例 `.codex-global-state.json` 中的本地项目路径匹配。没有匹配项目时显示“新会话”。

已配置的渠道会并发发送，单个渠道失败会重试一次。`notifier.log` 记录事件类型、渠道、结果、耗时和错误类型，不写入通知正文或渠道密钥。符合通知条件的完成事件仍以原始 JSON 转发给已有的 `notify` 命令。

## 配置与边界

通知渠道配置保存在项目目录的 `notify.env`；安装器创建该文件时设置权限为 `600`。桥接程序每次运行都会重新读取配置，进程环境中的同名变量优先于文件值。`notify.env`、日志和轮次标记目录已被 Git 忽略。

安装器会修改目标实例的 `config.toml` 和 `hooks.json`，修改前备份已有文件。更改这些回调后应重启对应 Codex 实例；仅修改渠道配置无需重启。

通知内容会发送到所选的第三方服务或自建服务。公共 ntfy 服务器上的 Topic 应使用随机且不易猜测的字符串。Bark 的物理网卡直连功能仅用于 Bark 通道；直连失败后的普通 HTTPS 回退仍受系统路由影响。

其他事件源的状态和接入要求见 [平台支持](platforms.md)。
