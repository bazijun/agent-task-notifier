# Agent Task Notifier

Agent Task Notifier 将 Codex 的任务完成和等待授权事件发送到手机或即时通讯。支持本机 Codex 以及 Cockpit 中的 Codex 实例；多个实例共用一份通知渠道配置。完成通知使用事件中的用户问题和最后一条回答，并按固定字节上限截断，同时附上项目名和实例标签。不调用模型生成摘要。

事件源的支持状态见 [平台支持说明](docs/platforms.md)，现有事件处理流程见 [架构说明](docs/architecture.md)。

## 首次配置（macOS）

需要 Codex、Python 3.11+，以及至少一个通知渠道的接收端或凭据。项目只使用 Python 标准库，无需安装 pip/npm 依赖。

1. 克隆项目并进入项目目录，运行 `python3 setup.py`；macOS 也可以双击 `setup.command`。向导根据接收设备推荐通知渠道，并收集相应凭据。
2. 默认接入 `CODEX_HOME` 环境变量指定的实例；未设置时是 `~/.codex`。向导把凭据写入本项目的 `notify.env`（权限 `600`）。确认安装后，会备份并更新该实例的 `config.toml`、`hooks.json`，随后发送一条测试通知。
3. 确认终端显示所选渠道的 `...: ok`。重启对应的 Codex 实例；首次使用新增 Hook 时，在该实例输入 `/hooks` 并信任。
4. 运行一个普通任务，检查手机或所选渠道是否收到完成通知。需要多实例时，先用 `python3 setup.py --list-instances` 查看，再运行下方的安装命令。

### 接收设备

| 接收设备 | 推荐渠道 | 配置方式 |
|---|---|---|
| iPhone/iPad | Bark | 安装 Bark App，复制推送地址或设备 Key；向导选择 `ios` 和 `bark`。 |
| Android | ntfy | 安装 ntfy App，订阅随机且不易猜测的 Topic；向导选择 `android` 和 `ntfy`，填写相同的服务器地址与 Topic。 |

飞书、企业微信和 WxPusher 等渠道也可在向导中选择。iMessage 通道需要发送端 Mac 已登录“信息”并允许脚本控制。Bark 仅用于 iPhone/iPad；Android 请使用 ntfy 或其他已支持的渠道。

使用公共 `ntfy.sh` 时，Topic 应使用随机且不易猜测的字符串，通知内容会经过 ntfy 服务器。向导支持填写自建 ntfy 服务器。重新运行向导不会自动删除已配置的其他渠道；停用渠道时应清空 `notify.env` 中对应的凭据。

出现 `tomllib` 导入错误时，改用 Python 3.11+ 运行向导。测试失败时查看本地 `notifier.log` 中的渠道名称和错误类型。更换凭据可重新运行向导，或直接编辑 `notify.env`。渠道配置在下一次通知时重新读取；修改 Codex 回调或 Hook 后需要重启对应实例。

### 数据与隐私

完成通知包含用户问题和回答的截断文本，并发送到已配置的通知渠道。安装器会保留已有的 Codex `notify` 命令；符合通知条件的完成事件仍以原始 JSON 转发给它。`notify.env`、`notifier.log` 和 `.state/` 已被 Git 忽略；提交前仍应检查暂存区，避免加入个人配置或任务内容。

## 常用维护命令

查看自动发现的实例：

```bash
python3 setup.py --list-instances
```

批量安装或更新默认实例与自动发现的 Cockpit 实例（需先完成渠道配置）：

```bash
python3 setup.py --install-only --all-instances
```

只维护一个实例；不写标签时自动生成 `codex-实例目录名前6位`：

```bash
python3 setup.py --install-only --instance /path/to/CODEX_HOME
python3 setup.py --install-only --instance my-label=/path/to/CODEX_HOME
```

一次维护多个指定实例时重复 `--instance`：

```bash
python3 setup.py --install-only \
  --instance codex-a=/path/to/instance-a \
  --instance codex-b=/path/to/instance-b
```

安装器会：

1. 备份每个实例的 `config.toml` 和 `hooks.json`。
2. 保留 Cockpit 已配置的 `SkyComputerUseClient` 回调，通过 `--previous-notify` 接入通知器。
3. 合并 `PermissionRequest` Hook，不覆盖其他插件 Hook。
4. 合并 `UserPromptSubmit` Hook，为主用户轮次建立 `thread-id + turn-id` 标记，用于过滤内部轮次和子 agent 的完成事件。
5. 把实例标签写进各自的回调命令。默认 `~/.codex` 的标签为 `codex-default`；其他实例可用 `--instance 标签=路径` 自定义。

修改后重启对应 Codex 实例。新增或变更 Hook 后，在该实例输入 `/hooks` 并信任；信任记录按实例隔离。

## Bark 维护

Bark 专属的载荷格式与网络请求位于 `channels/bark.py`：

- `endpoint()`：Bark 服务器和设备 Key 解析。
- `build_payload()`：标题、正文、图标、声音、分组和历史消息链接。
- `send()`：优先绑定 macOS 物理网卡直连 Bark，失败后再走无环境代理的普通直连，并检查 Bark 返回值。

日常配置只修改私密文件 `notify.env`，可参考 `notify.env.example`：

| 变量 | 用途 | 常用值 |
|---|---|---|
| `BARK_SERVER` | Bark 服务端 | `https://api.day.app` |
| `BARK_DEVICE_KEY` | iPhone 设备推送 Key | Bark App 提供的 Key |
| `BARK_DIRECT` | 优先绑定物理网卡直连 Bark | `1`（默认） |
| `BARK_DIRECT_INTERFACE` | 可选固定物理网卡；留空自动识别 | 留空或 `en0` |
| `BARK_DIRECT_DNS` | 可选直连 DNS，逗号分隔；留空使用内置多路 DNS | 留空 |
| `BARK_DIRECT_IPS` | 可选 Bark 真实 IPv4 兜底，逗号分隔 | 留空 |
| `BARK_LEVEL` | 通知级别 | `active`、`timeSensitive`、`passive`、`critical` |
| `BARK_GROUP` | Bark 历史消息分组 | `Codex` |
| `BARK_ICON` | 通知图标公网 URL | 向导默认填入 Codex 图标 URL |
| `BARK_OPEN_URL` | 点击系统通知后立即跳转；两步跳转模式必须留空 | 留空 |
| `BARK_HISTORY_URL` | Bark 历史消息中的可选手动跳转链接 | 留空 |
| `BARK_HISTORY_LINK_TEXT` | 历史消息中的链接文字 | 留空时为“打开链接” |
| `BARK_SOUND` | Bark 声音名称 | 留空使用默认声音 |

改 `notify.env` 或 `channels/bark.py` 不需要重新安装实例回调，也通常不需要重启 Codex；下一条通知会直接读取新配置。

### 独立于代理的稳定直连

`BARK_DIRECT=1` 默认启用。通知器优先在 macOS 上识别物理网卡，查询真实 IPv4 地址，并将 Bark 请求绑定到该网卡；TLS 仍校验 Bark 域名。物理网卡直连失败时，会禁用环境代理后重试普通 HTTPS 请求。回退请求仍受系统路由影响，不能保证绕过 TUN。

这项能力只改变当前通知器进程的套接字行为，不读取或修改任何代理软件、系统代理、DNS 或路由配置。

### Bark 两步跳转（可选）

如果希望点击系统通知先进入 Bark 历史消息，再通过消息底部链接打开自己的远程工具，可以配置：

```env
BARK_OPEN_URL=
BARK_HISTORY_URL=你的应用链接
BARK_HISTORY_LINK_TEXT=打开远程工具
```

`BARK_OPEN_URL` 必须留空。只要它有值，Bark 就会把它作为通知的顶层 `url`，点击系统通知时会立即跳转。`BARK_HISTORY_URL` 则被写入 Markdown 正文，只在 Bark 历史消息中显示为可点击链接。

修改并保存 `notify.env` 后无需重启 Bark、Codex 或额外服务，也无需重新运行 `setup.py`；Hook 每次执行都会重新读取配置和当前脚本。直接发送测试通知验证：

```bash
python3 notify_bridge.py --test --instance-label codex-default
```

只有修改了某个实例的 `config.toml` 或 `hooks.json` 时，才需要重启对应 Codex 实例，并在实例中执行 `/hooks` 完成信任。

## 消息格式维护

消息格式位于 `notify_bridge.py`：

- `latest_user_message()`：从事件中取最后一条用户输入，并移除已识别的桌面环境和图片包装。
- `is_internal_title_generation()`：识别并忽略桌面端隐藏的任务标题生成轮次，包括返回 `suggestions` 的格式。
- `internal_metadata_generation_kind()`：即时过滤标题、活动摘要、线程描述和标题重判等桌面内部元数据轮次。
- `is_subagent_event()`：在完成事件使用主轮次标记前排除带子 agent 标识的事件。
- `mark_user_turn()` / `consume_user_turn_marker()`：通过 `UserPromptSubmit` 建立主用户轮次标记。标记机制启用后，完成通知只放行匹配的轮次；标记不保存提示词内容。
- `project_name()`：读取对应实例 `.codex-global-state.json` 中的 `local-projects`，按侧边栏项目根目录识别项目。
- `format_event()`：组织标题、原始回答、项目名和实例标签。
- `TITLE_BYTES` / `ANSWER_BYTES`：控制通知标题和回答的 UTF-8 字节截断长度。

当前格式：

```text
标题：用户问题（超出长度时截断）

正文：Codex 最后一条回答（超出长度时截断）

项目：Codex 侧边栏中的项目名（不属于项目则为“新会话”）
实例：codex-default
```

## 文件结构

- `notify_bridge.py`：Codex 事件解析、消息格式和通道路由。
- `channels/bark.py`：Bark 独立通道，Bark 定制主要改这里。
- `setup.py`：多实例发现、批量接入、标签和 Hook 合并。
- `notify.env`：真实私密配置，权限 `600`，已被 Git 忽略。
- `notify.env.example`：可提交的变量模板。
- `notifier.log`：发送结果和耗时，不记录密钥，已被 Git 忽略。
- `.state/user-turns/`：本地用户轮次短期标记，已被 Git 忽略，不保存问题正文。

## 测试

离线测试：

```bash
python3 -m unittest -v
```

真实 Bark 测试：

```bash
python3 notify_bridge.py --test --instance-label codex-default
```

## 许可证

本项目使用 [MIT 许可证](LICENSE)。
