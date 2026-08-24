# RocketCatShell

[![Platform](https://img.shields.io/badge/Platform-OneBot%20v11%20Multi--Transport-pink)](#)
[![Runtime](https://img.shields.io/badge/Python-%3E%3D3.11-blue)](#环境要求)

将 [Rocket.Chat](https://rocket.chat) 通过桥接方式接入 OneBot v11 生态的独立客户端。它继承了插件版 [RocketCat](https://github.com/Creeper3222/astrbot_plugin_rocketchat_onebot_bridge) 已经验证过的桥接核心、独立 WebUI 和管理能力，但已经不再依附于 AstrBot 插件宿主，而是作为一个可以独立运行、独立配置、独立扩展的本地控制台存在。

本项目的目标不是继续做一个“宿主里的桥接插件”，而是把 RocketCat 发展成一套真正独立的 `Rocket.Chat <-> OneBot v11` 桥接软件。

这个 live 目录对应 RocketCatShell 的 Linux / Docker 版：平台中立功能与 Windows 版保持同代，同时保留容器初始化、外部持久挂载、内置插件安全播种、Linux PTY、容器诊断和应用层事务更新等 Linux 专属实现。Rocket.Chat 媒体通过 RocketCatShell WebUI 端口上的令牌 HTTP URL 统一上报，不要求与 AstrBot 共享目录。

当前发布版本为 `v0.2.3`，完整版本变化与迁移记录见 [CHANGELOG.md](CHANGELOG.md)。

这意味着：

- RocketCatShell 自己拥有 `config/`、`data/`、`logs/` 目录边界。
- RocketCatShell 自己提供本地 WebUI、登录认证、Bot 管理、插件管理、项目根目录内文件管理和系统终端。
- RocketCatShell 提供 `HTTP服务器`、`HTTP客户端`、`HTTP SSE服务器`、`Websocket服务器`、`Websocket客户端` 五种独立 OneBot v11 传输；与 AstrBot 协同时可继续使用默认的 `Websocket客户端`。

---

## Linux / Docker 补充说明

- `launcher.sh` 会先调用 `tools/check_requirements.py` 检查依赖，再在缺失或版本不兼容时自动安装并复检；`v0.1.5` 新增的 `psutil` 也包含在这条自动补装链路里。
- `Dockerfile` 会把 `tools/` 一并打进镜像，保证容器内和宿主机目录里的工具链一致。
- `docker/entrypoint.sh` 仍负责容器首次启动时写入 `config/shell.json` 默认值，并对外挂 `data/plugins` 中的内置插件执行缺失补种与版本变更自动刷新；`rocketcat_plugin_built_in_command` 与 `rocketcat_plugin_adapt_iamthinking` 都会随镜像自动同步到外挂插件目录。
- `docker-compose.yml`、`.env` 和 `.env.example` 已改为 Linux 风格的默认持久化路径 `/opt/rocketcatshell/...`，不再使用旧的 Windows `D:/docker/...` 示例。
- 用户身份注册表使用独立挂载 `/app/data/user_identity`；默认宿主目录为 `/opt/rocketcatshell/data/user_identity`，必须与 `config/`、`data/bots/` 一起备份。
- 全局媒体临时目录使用独立挂载 `/app/data/temp`，默认宿主目录为 `/opt/rocketcatshell/data/temp`。该目录只保存可重建缓存，可以随时在停止相关任务后手动清理。
- 从 v0.1.8 升级时，应先停止 RocketCatShell 与对应 OneBot 消费端并完成备份，再用 v0.1.9 镜像执行一次性迁移；下面的 AstrBot 配置挂载与参数可按实际路径调整：

```bash
docker run --rm \
  -v /opt/rocketcatshell/config:/app/config \
  -v /opt/rocketcatshell/data/bots:/app/data/bots \
  -v /opt/rocketcatshell/data/user_identity:/app/data/user_identity \
  -v /path/to/astrbot-config.json:/migration/astrbot-config.json \
  138763327/rocketcatshell-linux:v0.1.9 \
  python /app/tools/migrate_user_identity.py \
    --project-root /app \
    --bot-id bot_xxxxxxxx \
    --astrbot-config /migration/astrbot-config.json
```

  迁移工具会备份 Bot 旧映射文件、建立服务器级 SQLite、重建私聊绑定、清理废弃 self ID 配置字段，并写出 `identity_scope.json` 与 `user_identity_migration.json`。只有迁移清单明确命中的旧 AstrBot 管理员 ID 才会被替换。
- WebUI 文件管理边界是容器内 `/app`，宿主机目录只有通过 compose 挂载到 `/app/config`、`/app/data/...`、`/app/logs` 等路径后才可见。媒体发布接口仅通过不可预测令牌读取 `/app/data/temp` 中的 RocketCatShell 自身缓存，不需要也不会映射 AstrBot 的 `data/temp`。
- 当 OneBot 地址是 `host.docker.internal`、`localhost` 或回环地址时，媒体 URL 自动使用 `http://127.0.0.1:<WebUI端口>`；当 OneBot 指向其它 Docker 服务时，默认使用 `http://rocketcatshell:<WebUI端口>`。自定义服务名可设置 `ROCKETCAT_DOCKER_SERVICE_NAME`，远程代理或特殊网络可直接设置 `ROCKETCAT_UPSTREAM_MEDIA_BASE_URL`。
- 官方镜像自 `v0.1.9` 起同时支持 `linux/amd64` 和 `linux/arm64`；Docker 会根据宿主机架构自动选择对应 manifest。
- Compose 默认仅把 HTTP / SSE 服务端的 `3000` 和 WebSocket 服务端的 `3001` 发布到宿主机 `127.0.0.1`。容器内创建服务端类型时，监听 Host 必须填写 `0.0.0.0` 且端口要与 Compose 映射一致；客户端访问宿主机服务应使用 `host.docker.internal`。
- `config/onebot_transports.json` 位于持久化 `config/` 挂载中。v0.2.2 及更早版本的旧平面 WebSocket 配置会自动迁移为正式 `Websocket客户端`，并继续写入旧版可读取的兼容投影。

---

## 架构说明

```text
Rocket.Chat Server
		^
		|  REST API + DDP/WebSocket
		v
RocketCatShell
		^
		|  OneBot v11 HTTP / SSE / WebSocket
		v
OneBot v11 Peer
		^
		|  plugins / providers / event pipeline
		v
AstrBot or other compatible OneBot-side workflow
```

声明：

- RocketCatShell 围绕 OneBot v11 语义工作，并把五种网络类型拆分为互不耦合的传输模块。
- 当前已适配 [AstrBot](https://github.com/AstrBotDevs/AstrBot)；如果对端是 AstrBot，可继续复用其 aiocqhttp / OneBot v11 链路并使用 `Websocket客户端`。
- RocketCatShell 当前不是一个通用的 Rocket.Chat 官方平台适配器，而是一套 OneBot 语义桥接器。

---

## 功能特性

- 支持 Rocket.Chat 频道、私有群组、私聊消息桥接为 OneBot v11 语义。
- 支持统一 Bot 注册表，不再使用主 bot / 副 bot 的分层持久化模型。
- 内置独立 WebUI，可管理网络配置、基础信息、运行诊断、运行日志、文件管理、系统终端、基础设置和本地插件。
- WebUI 默认启用登录门禁，初始密码为 `123456`。
- 支持自定义 WebUI 端口，并在端口占用时自动回退到可用端口。
- 支持配置导出 / 导入，统一打包 Bot 设置、WebUI 密码 / 端口、消息映射窗口条数上限和本地插件主配置。
- 支持自动重连、最大连续重连次数限制、自动停用失败 Bot。
- 支持动态订阅新房间，机器人被拉入新房间后无需重启。
- 支持兼容 AstrBot 唤醒词 / 指令的入站消息格式，标准 `message` / `raw_message` 保持为纯当前用户正文。
- 支持 OneBot 风格的群聊、私聊、消息查询、群成员查询、登录信息查询。
- 支持以内存热存储 + snapshot / journal 恢复的运行态，降低高频消息场景下的磁盘读写压力。
- 支持文本、`at`、引用回复、图片、文件、语音、视频、Markdown 出站发送。
- 支持引用链提取、回复来源识别、提及用户映射、群聊 / 私聊上下文映射，以及发送者 / 提及 / 回复 / 子频道等独立认知元数据。
- 支持固定大小的 message 索引窗口、超窗自动裁剪和 WebUI 手动窗口重建。
- 支持远端媒体下载、大小限制控制、本地临时文件落地和 Base64 媒体上传。
- 支持 Rocket.Chat 官方 E2EE 私聊 / 私有群组文本与媒体收发。
- 支持本地插件系统，可发现、启停、重载、卸载本地插件，并在运行时接管 OneBot action。
- 支持内置指令系统插件 `rocketcat_plugin_built_in_command`，当前提供精确纯文本 `#rocketcat` 与 `#system` 两条本地命令。
- `#rocketcat` 可在 Rocket.Chat 房间内直接返回当前桥接 Bot 基础信息、连接状态、OneBot self_id、bot 头像和服务器 branding 信息。
- `#system` 可在 Rocket.Chat 房间内直接返回当前 Shell 环境的系统快照，用于快速查看版本、CPU、内存与进程占用状态。
- [I Am Thinking](https://github.com/sssn-tech/astrbot_plugin_iamthinking) 适配能力已从核心桥接层剥离为本地插件 `rocketcat_plugin_adapt_iamthinking`。
- `rocketcat_plugin_adapt_iamthinking` 现已支持把 `set_msg_emoji_like` 独立映射为 Rocket.Chat 贴表情与 typing 指示器，并允许分别开关。
- 支持项目级单实例启动保护，阻止同一目录下重复拉起多份 RocketCatShell runtime。

---

## 当前实现范围

### 已实现的 OneBot 动作

- `send_group_msg`
- `send_private_msg`
- `send_msg`
- `get_msg`
- `get_group_info`
- `get_group_member_info`
- `get_group_member_list`
- `get_stranger_info`
- `get_login_info`
- `set_msg_emoji_like`：由本地插件决定是否处理；当前 `rocketcat_plugin_adapt_iamthinking` 可把该动作映射为 Rocket.Chat reaction 与可选 typing 指示器

### 当前不支持的 OneBot 动作

- `send_group_forward_msg`
- `send_private_forward_msg`

RocketCatShell 当前这一版明确不承诺合并转发消息语义。

---

## 消息与媒体能力

### 入站能力

- Rocket.Chat 文本消息会被转换为 OneBot `message` 事件。
- 私聊会映射为 OneBot `private` 消息。
- 频道和私有群组会映射为 OneBot `group` 消息。
- 标准 OneBot `message` / `raw_message` 会优先保持纯当前用户正文，确保 AstrBot 的唤醒词、命令前缀和 `startswith(...)` 检查仍然成立。
- Rocket.Chat `mentions` 会转换为 OneBot `at` 段。
- Rocket.Chat 引用、消息链接、线程回复会转换为 OneBot `reply` 语义，并补充引用上下文文本。
- 发送者、提及、引用链、回复摘要、房间名、房间 slug、上下文群 ID 等 Rocket.Chat 认知信息会以独立字段写入事件和消息注册表。
- 图片、普通文件、音频、视频附件会被识别并转换成对应的 OneBot 媒体段。
- 不支持直接桥接的媒体会降级为可读文本占位，避免整条消息消失。

### 出站能力

- OneBot `text` 直接发送为 Rocket.Chat 文本。
- OneBot `at` 会转换为 Rocket.Chat `@username` 或 `@all`。
- OneBot `reply` 会转换为 Rocket.Chat 消息链接引用格式。
- OneBot `image` 支持 HTTP(S) 链接、本地文件和 Base64 数据。
- OneBot `file`、`record`、`video` 支持本地文件；远端媒体会先尝试下载再上传。
- OneBot `markdown` 会按文本内容发往 Rocket.Chat。

### 上下文与映射

- Rocket.Chat 的房间 ID、用户 ID、消息 ID 会被桥接器映射为可持久化的 OneBot surrogate ID，但热路径以内存态为准。
- 每个 bot 的桥接运行态会落盘为 `runtime.snapshot.bin` 与 `runtime.journal.bin`，覆盖 ID 映射、消息缓存、私聊房间映射、群上下文绑定和最近消息窗口，用于快速恢复最近状态。
- message 命名空间采用固定窗口，只保留最近 N 条映射；窗口整理时会同步刷新消息缓存、reply 关联以及 `latest_by_context_sender` 路由提示。
- 群聊上下文使用上下文房间注册表维持群上下文到真实房间的绑定关系。
- 私聊上下文使用私聊房间映射存储维护用户与私聊房间的绑定关系。
- 可选开启“子频道会话隔离”，把不同子房间拆成不同会话上下文。

---

## 性能与诊断

- 启动恢复阶段会记录 `snapshot_load_ms`、`journal_replay_ms` 和 `journal_records_replayed`，便于判断热存储恢复成本。
- 入站 tracing 会拆分 `translate` 与 `emit_event` 两个阶段，并把 `room_lookup`、`mapping_alloc`、`room_bindings`、`mention_segments`、`quote_contexts`、`mention_metadata`、`media_segments`、`context_media`、`message_store`、`batch_commit` 等热路径阶段拆开记录。
- `room_info_cache_ttl_seconds` 用于平衡房间元信息实时性与 REST 开销；默认值适合大多数稳定群组场景。
- 开发者源码工具 [tools/benchmark_inbound_translate.py](https://github.com/Creeper3222/RocketCatShell-linux/blob/main/tools/benchmark_inbound_translate.py) 可用于本地构造文本 / 引用 / 线程 / 图片场景，对比 control 与 rebuild 两条入站翻译链路的延迟；该工具不包含在最小运行 ZIP 或 Docker 镜像中。

---

## E2EE 支持

当前实现支持 Rocket.Chat 官方 E2EE 链路，覆盖：

- 加密私聊房间 `d`
- 加密私有群组 `p`
- 加密文本消息
- 加密图片、语音、视频、普通文件上传和下载

实现特征：

- 启用了 `e2ee_password` 后，桥接器会初始化本机密钥对并请求 / 同步房间密钥。
- 接收入站加密消息时，会自动解密再注入 OneBot 事件流。
- 发送到加密房间时，会自动走加密消息体和加密媒体上传确认流程；媒体上传会分块读取原文件并分块写出密文临时文件。
- Rocket.Chat 8.2+ 删除加密附件时产生的 `removed-file` 标记会在解密合并后保留。
- Rocket.Chat 8.3+ E2EE REST 接口启用严格请求校验后，只提交对应端点允许的字段。
- E2EE 多引用会从解密正文开头的系统引用前缀生成多个顶层 OneBot `reply`，普通正文链接不会被误判。
- E2EE 解密媒体会写入所有 Bot 共用的 `data/temp`，按文件签名修正图片扩展名后发布为令牌 HTTP URL，与非加密频道使用同一套上游可见性策略。
- 如果 E2EE 初始化失败，不会影响未加密房间的正常收发。

---

## 独立 WebUI
<p align="center">
  <img src="https://github.com/user-attachments/assets/9cd515ce-92f5-4a63-8d8d-8f42d360b836" width="100%" />
</p>

RocketCatShell 启动后会在本地启动一个独立 WebUI：本机直接运行时默认监听 `127.0.0.1`，默认端口 `5751`；Docker 容器首次启动时会写入 `webui_host=0.0.0.0`，再由 `docker-compose.yml` 负责对宿主机发布端口。

### 页面能力

- `网络配置`：查看 Bot 状态、创建 / 编辑 / 删除 Bot。
- `基础信息`：查看每个 Bot 的账号信息、OneBot self ID、Rocket.Chat 服务器品牌头像和服务器名称。
- `猫猫日志`：查看 RocketCatShell 与 `RocketCatPerf` 运行日志，可按级别和 `Perf` 开关过滤，并支持清空日志。
- `基础设置`：管理 WebUI 登录认证 / 文件管理鉴权密码、WebUI 端口、消息映射窗口条数上限，以及配置导出 / 导入。
- `文件管理`：浏览容器内 `/app` 项目根目录，支持目录进入 / 返回、文本查看与允许范围内的编辑保存、图片预览、上传、重命名、移动、删除和打包下载；敏感持久化数据文件需要再次输入 WebUI 登录认证 / 文件管理鉴权密码。
- `插件管理`：管理 RocketCatShell 本地插件，包括启停、设置、重载和卸载。

### WebUI 认证
<p align="center">
  <img src="https://github.com/user-attachments/assets/d233e9d8-1931-46b0-9309-91957443e8f2" width="100%" />
</p>

- RocketCatShell 默认启用密码访问。
- 初始 WebUI 登录认证 / 文件管理鉴权密码为 `123456`。
- 后端提供登录、登出、Cookie 会话和受保护 API 访问控制。
- 会话失效时，前端会自动跳回登录页。
- WebUI 登录认证 / 文件管理鉴权密码不允许设置为空。

### 配置导出 / 导入

- 导出默认文件名为 `rocketcat_config.json`。
- 顶层判别字段为 `Is rocketcat config`。
- 导出内容包含所有 Bot 设置（包括 `room_info_cache_ttl_seconds` 与 `perf_trace_enabled`）、WebUI 登录认证 / 文件管理鉴权密码、WebUI 端口、消息映射窗口条数上限和本地插件主配置。
- 导入时会先校验判别字段；若不是 RocketCatShell 配置文件，则会返回失败提示。
- Bot 卡片顺序会在网络配置、基础信息和运行诊断间共享，插件卡片使用独立顺序；顺序随配置导入导出。缺少该字段的 v0.2.1 配置仍可直接导入。

### Docker 容器内版本管理

- 版本管理固定读取 `Creeper3222/RocketCatShell-linux` 的官方 Release 和 `RocketCatShell-linux-vX.Y.Z.zip` 资产。
- 更新包必须通过 Linux 清单、SHA-256、路径、大小、文件数量、持久状态兼容和 runtime generation 校验。
- 更新事务会先备份精确应用层，再由容器 PID 1 切换到冻结 helper；目标版本未在 120 秒内健康启动时会在容器重启时自动恢复备份。
- 两个内置插件随 RocketCatShell 整体更新，不提供独立更新器；用户插件和插件数据不会被替换。
- 普通容器重启保留 WebUI 切换后的版本；容器删除或重建恢复镜像版本。如要永久升级镜像，请使用 Docker Hub 正式标签和部署脚本。

---

## 本地插件系统
<p align="center">
  <img src="https://github.com/user-attachments/assets/cd1b4f28-02a7-467a-a6c6-739114a9e5bb" width="100%" />
</p>

RocketCatShell 当前已经拥有自己的本地插件系统，而不再只是依赖外部宿主插件机制。

当前约定如下：

- 插件本体目录：`data/plugins/<plugin>`
- 插件主配置：`config/plugins_config/<plugin>_config.json`
- 插件持久化数据：`data/plugin_data/<plugin>`

当前插件管理能力包括：

- 自动发现本地插件
- 读取 `metadata.yaml` 和可选 `_conf_schema.json`
- 保存插件主配置
- 启用 / 停用插件
- 全局单例运行：一个启用插件只创建一个实例，各 Bot 只建立轻量 runtime binding
- 插件级原子重载；候选实例初始化或任一 runtime 绑定失败时继续保留旧实例
- 自动发现并在现有 WebUI 内打开插件 Dashboard
- 卸载插件本体，并可选删除插件主配置与插件持久化数据

当前内置示例包括：

- `rocketcat_plugin_built_in_command`：RocketCatShell 自有的内置指令系统插件。当前精确拦截 `#rocketcat` 与 `#system`，在本地直接回复，不再把命令正文继续交给上游；插件回复也会在入站侧抑制自回显再次上报。
- `rocketcat_plugin_adapt_iamthinking`：用于接管 `set_msg_emoji_like`。除 reaction shortcode 映射外，现在还支持独立的 typing 指示器开关；bot 进入思考阶段时会触发 Rocket.Chat typing，应答结束时主动清除，长时间思考会自动续期心跳。
- 发布仓库与镜像默认仅包含 `rocketcat_plugin_built_in_command` 与 `rocketcat_plugin_adapt_iamthinking` 两个内置插件；其它位于持久化 `data/plugins/` 的插件属于用户扩展，不会被源码同步或镜像补种流程删除。

### v0.2.1 插件生命周期

| Hook | 作用域 | 用途 |
|---|---|---|
| `on_initialize()` | 每个插件实例一次 | 初始化全局资源、注册 Dashboard API / SSE。 |
| `on_load(runtime)` | 每个已启用 Bot 一次 | 建立当前 Bot 的轻量绑定；消息与 Action 处理仍显式接收该 `runtime`。 |
| `on_unload(runtime)` | 每个 Bot 解绑一次 | 只清理当前 runtime 的状态，不能误删其他 Bot 的状态。 |
| `on_terminate()` | 每个插件实例一次 | 插件禁用、卸载、成功替换或 Shell 关闭时清理全局任务。 |

### 内置 Dashboard 目录与接口

插件页面按以下目录自动发现：

```text
data/plugins/rocketcat_plugin_example/
├─ metadata.yaml
├─ main.py                         # 纯静态 Dashboard 可省略
└─ pages/
   └─ dashboard/
      ├─ index.html
      ├─ app.js
      └─ styles.css
```

`metadata.yaml` 可用 `dashboard_page: dashboard` 指定默认页面；否则优先使用名为 `dashboard` 的页面，再回退到按名称排序后的第一个页面。没有有效 `pages/<name>/index.html` 的插件不会显示 Dashboard 按钮。

需要后端能力的插件在全局初始化阶段注册 RocketCat 原生接口：

```python
from rocketcat_shell.plugin_system.base import RocketCatPlugin


class Plugin(RocketCatPlugin):
    async def on_initialize(self):
        self.context.register_dashboard_api(
            "status",
            self.get_status,
            methods={"GET"},
        )
        self.context.register_dashboard_sse("events", self.stream_events)

    async def get_status(self, request):
        return {"ok": True, "query": request.query}
```

页面侧通过注入的 `window.RocketCatPluginDashboard` 使用 `getContext()`、`apiGet()`、`apiPost()`、`upload()`、`download()`、`subscribeSSE()` 和 `unsubscribeSSE()`。iframe 不持有 WebUI Cookie 或登录令牌；父页面负责认证和转发。静态资源 URL 使用高强度临时令牌，并在插件禁用、重载、卸载或页面关闭时失效。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | `>= 3.11` |
| 运行依赖 | `aiohttp`, `cryptography`, `fastapi`, `orjson`, `psutil`, `python-multipart`, `uvicorn`, `websockets`；其中 `websockets` 为 WebUI 系统终端和实时通道提供 Uvicorn WebSocket 后端 |
| Rocket.Chat | 需要可用的 REST API、DDP/WebSocket 和 E2EE 接口（如使用加密功能） |
| OneBot 对端 | 需要支持所选 HTTP、SSE 或 WebSocket 模式的 OneBot v11 实现；纯 HTTP服务器 action 模式可以不建立事件订阅 |

---

## 安装依赖

### 方式一：直接运行 launcher.sh（推荐）

如果你的 Linux 主机已经有 Python 3，直接运行 `launcher.sh`。启动器会自动创建本地 `.venv`，检查 `requirements.txt` 是否满足，并在缺失或版本不兼容时自动安装依赖。

```bash
chmod +x launcher.sh
./launcher.sh
```

### 方式二：使用本地虚拟环境

在项目根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 方式三：使用 Docker Compose

如果你希望直接作为容器部署，先按需修改 `.env`，再执行：

```bash
docker compose up -d --build
```

默认会把持久化目录挂到 `/opt/rocketcatshell/...`；如果你要改宿主机挂载位置，优先修改 `.env` 里的 `ROCKETCAT_*_DIR`。RocketChat → OneBot 媒体通过 WebUI 端口的令牌 HTTP URL 上报，不需要额外挂载 AstrBot 的媒体目录。



---

## 启动

### Linux 启动器

项目根目录已经提供：

```text
launcher.sh
```

它会优先使用本地 `.venv/bin/python`。

如果本地 `.venv` 不存在，启动器会自动尝试使用系统 `python3` 或 `python` 创建 `.venv`。

如果检测到依赖缺失或版本不满足，启动器会自动执行：

```bash
pip install -r requirements.txt
```

然后再启动 RocketCatShell。

### Python 模块入口

也可以直接使用：

```bash
python -m rocketcat_shell
```

可选参数：

- `--once`：只做初始化和状态构建，不启动 WebUI 服务器。
- `--no-browser`：启动后不自动打开浏览器。
- `--print-status`：把当前 shell 状态输出到标准输出。
- `--verbose`：本次运行强制使用 `DEBUG` 日志级别。

---

## 首次启动与初始化行为

RocketCatShell 在第一次安装、还没有保存过任何配置时，会自动在项目根目录下创建并写入：

- `config/`
- `config/plugins_config/`
- `data/`
- `data/temp/`
- `data/bots/`
- `data/user_identity/`
- `data/plugins/`
- `data/plugin_data/`
- `logs/`
- `config/shell.json`
- `config/bots.json`

其中初始默认值包括：

- 本机直接运行时的 WebUI 地址：`http://127.0.0.1:5751/`
- Docker Compose 默认宿主机访问地址：`http://127.0.0.1:5751/`（容器内 `webui_host=0.0.0.0`，再由 compose 负责端口发布）
- WebUI 登录认证 / 文件管理鉴权密码初始值：`123456`
- 最大消息映射窗口条数：`1000`
- 性能策略：`balanced`
- 入站 Worker：`0`（按 CPU 自动选择 2 或 4）
- OneBot 出站队列：`512`
- 身份缓存：`4096`
- 媒体缓存：`1 GiB`、保留 `168` 小时
- 日志轮转：单文件 `10 MiB`、保留 `3` 份
- WebUI 终端：最多 `6` 个，空闲关闭为 `0`（不限制）
- 本机直接运行时默认 OneBot reverse WS 地址：`ws://127.0.0.1:6199/ws/`
- Docker Compose / `docker/entrypoint.sh` 默认 OneBot reverse WS 地址：`ws://host.docker.internal:6200/ws/`
- Bot 的 OneBot `self_id` 会在登录 Rocket.Chat 后根据不可变 `userId` 自动生成。

也就是说，只要依赖安装正确，RocketCatShell 在空配置状态下可以自己创建必需目录和初始配置文件。

---

## 快速开始

### 1. 准备 OneBot v11 对端

如果你的对端是 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，最直接的方式是在 AstrBot 中创建内置 OneBot v11 平台，并让 RocketCatShell 使用 `Websocket客户端`：

1. 打开 `机器人`
2. 点击 `+ 创建机器人`
3. 选择 `OneBot v11`
4. 填写反向 WebSocket 主机、端口与 Token

本地部署最常见的地址是：

```text
ws://127.0.0.1:6199/ws/
```

如果你使用当前这个 Docker / Linux live，并且 OneBot 上游跑在宿主机上，那么默认 `.env` / `docker/entrypoint.sh` 会使用：

```text
ws://host.docker.internal:6200/ws/
```

RocketCatShell 会把 RocketChat 媒体上报为令牌 HTTP URL。宿主机 AstrBot 会通过发布到 `127.0.0.1:5751` 的 WebUI 端口下载，并按自身标准链路缓存到 `AstrBot/data/temp`；无需配置共享路径。若 AstrBot 与 RocketCatShell 位于同一 Docker 网络，确保 RocketCatShell 的服务名可解析；默认服务名为 `rocketcatshell`。

使用 HTTP / SSE / WebSocket 服务端类型时，容器内监听 Host 必须设为 `0.0.0.0`。默认 Compose 将 HTTP / SSE 的容器端口 `3000` 与 WebSocket 的容器端口 `3001` 映射到宿主机同名端口，并且只绑定 `127.0.0.1`；需要调整时修改 `.env` 中对应的 `ROCKETCAT_ONEBOT_*` 变量。客户端类型访问宿主机服务时不要填写容器内的 `127.0.0.1`，应使用 `host.docker.internal`。

特殊网络、反向代理或远程 AstrBot 可在 `.env` 中显式覆盖：

```text
ROCKETCAT_UPSTREAM_MEDIA_BASE_URL=http://可被AstrBot访问的地址:5751
```

### 2. 启动 RocketCatShell

在默认 `docker-compose.yml` 端口发布配置下，宿主机打开：

```text
http://127.0.0.1:5751/
```

如果你是直接运行 `launcher.sh`，且没有改 `webui_host` / `webui_port`，本机访问地址同样是这个地址。

使用默认密码登录：

```text
123456
```

首次登录后建议立刻在 `基础设置` 页修改 WebUI 登录认证 / 文件管理鉴权密码。

### 3. 创建第一个 Bot
<p align="center">
  <img src="https://github.com/user-attachments/assets/611a6601-0af6-4ebf-ac3c-e301a03631eb" width="100%" />
</p>
在 `网络配置` 页点击 `新建`，选择一种网络类型，然后为该 Bot 填写：

- Rocket.Chat 服务器地址
- Rocket.Chat 用户名
- Rocket.Chat 密码
- 按需填写 E2EE 密钥密码
- 所选传输的 Host / Port 或 URL
- 所选传输的消息格式、Token、心跳、重连及其它可用选项

高级设置中还可以进一步设置：

- Rocket.Chat 重连延迟
- Rocket.Chat 最大连续重连次数
- 子频道会话隔离
- 远端媒体大小上限

“上报自身消息”和“调试日志”属于 OneBot 传输设置，仅在对应类型提供时显示。Rocket.Chat 的两项重连设置只约束聊天服务器侧；OneBot 监听、投递或对端连接失败不会消耗这些次数，也不会自动停用 Bot。

### 五种 OneBot 网络类型

每个 Bot 只绑定一种网络类型，创建后不能直接切换类型。五类传输共享 OneBot action、消息编解码和有界队列基础设施，但各自拥有独立的启动、停止、监听、投递、心跳与诊断生命周期。

| 类型 | RocketCat 角色与端点 | 主要配置 |
|------|----------------------|----------|
| `HTTP服务器` | 监听 OneBot HTTP action API；纯 HTTP 模式不主动推送事件，可选同端口 WebSocket。 | Host、Port、CORS、WebSocket、Array / String、Token。 |
| `HTTP客户端` | 把事件 POST 到目标 URL，携带 `X-Self-ID`；配置 Token 时附加 `X-Signature: sha1=...`。 | URL、自身消息、Array / String、HMAC Token。 |
| `HTTP SSE服务器` | 提供 HTTP action API，并在 `/_events` 提供有序 SSE 事件流；可选同端口 WebSocket。 | Host、Port、CORS、WebSocket、自身消息、格式、Token。 |
| `Websocket服务器` | 监听 WebSocket；`/api` 只处理 action，其它路径同时接收 action 和事件。 | Host、Port、自身消息、格式、心跳、Token。 |
| `Websocket客户端` | 主动连接 OneBot WebSocket 对端，接收 action 并推送事件；断线后按自身间隔持续等待。 | URL、自身消息、格式、重连、心跳、Token。 |

服务端 Token 同时接受 `Authorization: Bearer <token>` 和 `access_token` query。HTTP 客户端响应中的 `reply` 会复用现有 `send_msg` 链路执行；尚未实现的删除、踢人、禁言和审批快速操作只记录明确的 `1404`。`Array` 传输 OneBot segment 数组，`String` 使用正确转义的 CQ 码。

### 4. 如需导入已有配置
<p align="center">
  <img src="https://github.com/user-attachments/assets/ba61315c-9273-4f30-a6a0-ac55a19297f1" width="100%" />
</p>

在 `基础设置` 页点击 `导入配置`，选择已有的 `rocketcat_config.json`。

如果要迁移当前环境，也可以先点击 `导出配置` 生成配置快照，再导入到新环境。

---

## 配置项说明

### Shell 主配置

`config/shell.json` 主要包含：

| 配置项 | 说明 |
|--------|------|
| `webui_host` | WebUI 监听主机；本机直接运行默认 `127.0.0.1`，Docker 容器首次启动生成的 `shell.json` 默认 `0.0.0.0`。 |
| `webui_port` | WebUI 监听端口，默认 `5751`。 |
| `webui_access_password` | WebUI 登录认证 / 文件管理鉴权密码，默认 `123456`。该密码同时用于登录 WebUI 和打开敏感持久化数据文件。 |
| `message_index_max_entries` | 最大消息映射窗口条数，默认 `1000`；超出后会清理最早映射，并在达到重置阈值后自动重排当前窗口。 |
| `log_level` | 日志级别，默认 `INFO`。 |
| `auto_open_browser` | 启动后是否自动打开浏览器。 |
| `default_onebot_ws_url` | 新建 `Websocket客户端` 时使用的默认 URL；保留该字段以兼容 v0.2.1 / v0.2.2。 |
| `default_onebot_access_token` | 新建 `Websocket客户端` 时使用的默认 Token；其它服务端 / HTTP 类型会生成独立的随机 16 位 Token。 |
| `default_reconnect_delay` | 默认 Rocket.Chat 重连延迟；不作用于 OneBot 对端。 |
| `default_max_reconnect_attempts` | 默认 Rocket.Chat 最大连续重连次数；不作用于 OneBot 对端。 |
| `default_enable_subchannel_session_isolation` | 默认是否开启子频道会话隔离。 |
| `default_remote_media_max_size` | 默认远端媒体上传 / 下载大小上限。 |
| `default_skip_own_messages` | 默认是否忽略机器人自己的消息。 |
| `default_debug` | 默认是否开启调试日志。 |
| `performance_profile` | 性能策略，v0.2.0 默认并仅提供 `balanced`。 |
| `inbound_worker_count` | 入站 Worker 数量；`0` 按 CPU 自动选择 2 或 4。 |
| `onebot_outgoing_queue_max_entries` | OneBot 出站队列上限，默认 `512`。 |
| `identity_cache_max_entries` | 用户身份与 Rocket.Chat 用户缓存上限，默认 `4096`。 |
| `media_cache_max_bytes` | `/app/data/temp` 媒体缓存总量上限，默认 `1 GiB`。 |
| `media_cache_max_age_hours` | 媒体缓存最长保留时间，默认 `168` 小时。 |
| `log_file_max_bytes` | 单个日志文件上限，默认 `10 MiB`。 |
| `log_file_backup_count` | 轮转日志备份数量，默认 `3`。 |
| `terminal_max_sessions` | WebUI Linux PTY 终端会话上限，默认 `6`。 |
| `terminal_idle_timeout_seconds` | 无连接终端的空闲关闭时间，默认 `0`；`0` 表示不限制，仅作用于 WebUI 终端会话，不会关闭 RocketCatShell 容器或主进程。 |

#### 性能与资源（高级设置）

WebUI 的“性能与资源（高级设置）”统一管理消息映射、入站并发、队列、缓存、日志和终端资源边界。设置会写入 `config/shell.json`，并随配置导出；导入旧配置时缺失字段自动采用 v0.2.0 默认值。Docker 首次启动还可通过 `.env` 中同名的 `ROCKETCAT_*` 环境变量指定初值。

| 设置项 | 详细行为 |
|--------|----------|
| 性能策略 | `performance_profile` 当前仅支持 `balanced`，作为吞吐、响应速度和容器资源占用的稳定基线。 |
| 入站 Worker | `inbound_worker_count` 允许 `0`～`8`；`0` 表示 CPU 不超过 4 核时使用 2 个 Worker，否则使用 4 个。保存后增量重建受影响的 Bot runtime。 |
| 最大消息映射窗口条数 | `message_index_max_entries` 默认 `1000`。窗口越大，历史 `get_msg` 和引用恢复范围越长，同时占用更多内存和快照空间；修改后立即整理现有窗口。 |
| OneBot 出站队列上限 | `onebot_outgoing_queue_max_entries` 默认 `512`。断线期间通过有界队列施加背压，重连后保持顺序发送，避免无界积压。 |
| 身份缓存上限 | `identity_cache_max_entries` 默认 `4096`。超出后按最近使用顺序淘汰热缓存，不会删除 SQLite 持久映射。 |
| 媒体缓存上限和保留时间 | `media_cache_max_bytes` 默认 `1 GiB`，`media_cache_max_age_hours` 默认 7 天；清理器只删除较旧且未被发布的 `/app/data/temp` 缓存。 |
| 日志轮转 | `log_file_max_bytes` 默认 `10 MiB`，`log_file_backup_count` 默认 `3`；新边界在下次完整启动容器后生效。 |
| Linux PTY 终端边界 | `terminal_max_sessions` 默认 `6`；`terminal_idle_timeout_seconds` 默认 `0`，表示不限制。非零值只关闭没有 WebUI 连接的空闲终端，不会停止容器或 Bot。 |

Compose 和 `.env.example` 暴露全部高级设置环境变量，但它们只用于首次生成 `shell.json`；已有持久化配置优先，不会因镜像升级被覆盖。配置导入、导出和 WebUI 保存使用相同字段与校验范围。

```dotenv
ROCKETCAT_PERFORMANCE_PROFILE=balanced
ROCKETCAT_INBOUND_WORKER_COUNT=0
ROCKETCAT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES=512
ROCKETCAT_IDENTITY_CACHE_MAX_ENTRIES=4096
ROCKETCAT_MEDIA_CACHE_MAX_BYTES=1073741824
ROCKETCAT_MEDIA_CACHE_MAX_AGE_HOURS=168
ROCKETCAT_LOG_FILE_MAX_BYTES=10485760
ROCKETCAT_LOG_FILE_BACKUP_COUNT=3
ROCKETCAT_TERMINAL_MAX_SESSIONS=6
ROCKETCAT_TERMINAL_IDLE_TIMEOUT_SECONDS=0
```

### 单个 Bot 与 OneBot 传输配置

`config/bots.json` 保存 Rocket.Chat Bot 主记录与旧版兼容投影；`config/onebot_transports.json` 以 Bot ID 保存格式版本为 `1` 的规范化 OneBot tagged union：

```json
{
  "format_version": 1,
  "transports": {
    "bot_id": {
      "type": "websocket-client",
      "settings": {}
    }
  }
}
```

v0.2.1 / v0.2.2 的平面 `onebot_ws_url`、Token、`skip_own_messages` 和 Debug 会自动迁移为 `Websocket客户端`，并补齐 Array 格式、5000ms 重连与 30000ms 心跳。回退 v0.2.2 后仍可使用旧界面修改 WebSocket URL、Token、Debug 与自身消息开关；再次升级时这些旧版修改会覆盖对应共享字段，而 v0.2.3 专属的消息格式、重连和心跳设置继续从独立传输配置恢复。非 WebSocket 客户端会在 `bots.json` 写入不可连接的安全占位地址，防止回退旧版时误连默认对端；独立传输配置不会被旧版改写。

Bot 主记录主要包含：

| 配置项 | 说明 |
|--------|------|
| `id` | Bot 唯一 ID。 |
| `name` | Bot 显示名。 |
| `enabled` | 是否启用该 Bot。 |
| `server_url` | Rocket.Chat 服务器地址。 |
| `username` | Rocket.Chat 用户名。 |
| `password` | Rocket.Chat 密码。 |
| `e2ee_password` | E2EE 私钥密码。 |
| `onebot_ws_url` | `Websocket客户端` 的兼容 URL 投影；其它类型写入不可连接的安全占位值。 |
| `onebot_access_token` | `Websocket客户端` 的兼容 Token 投影。 |
| `onebot_transport` | API 与配置导出使用的规范化 `{type, settings}`；类型创建后不可修改。 |
| `OneBot self_id` | 不由用户配置；根据 Rocket.Chat Bot 的不可变 userId 自动建立 `sha256-linear-v1` 映射。 |
| `reconnect_delay` | Rocket.Chat 断线重连等待秒数；不控制任何 OneBot 传输。 |
| `max_reconnect_attempts` | Rocket.Chat 最大连续重连次数；`0` 表示不限次数，不控制任何 OneBot 传输。 |
| `enable_subchannel_session_isolation` | 是否按子频道隔离上下文。 |
| `remote_media_max_size` | 当前 Bot 的远端媒体上传 / 下载大小上限。 |
| `room_info_cache_ttl_seconds` | 房间信息缓存 TTL，单位秒，默认 `300`。 |
| `perf_trace_enabled` | 是否输出入站性能追踪日志；也可被环境变量 `ROCKETCAT_PERF_TRACE` 覆盖。 |
| `skip_own_messages` | 是否忽略自己发出的消息。 |
| `debug` | 是否启用调试模式。 |

---

## 持久化目录

RocketCatShell 当前的正式目录语义如下：

```text
config/
	shell.json
	bots.json
	onebot_transports.json
	plugins_config/

data/
	bots/
	user_identity/
	plugins/
	plugin_data/

logs/
	rocketcat.log
```

说明：

- `config/` 只保存配置和插件主配置。
- `data/` 保存全局媒体临时缓存、本地插件本体、插件持久化数据、用户身份注册表和各 Bot 运行时数据。
- `data/temp/` 保存所有 Bot 共用的解密媒体与临时下载文件；目录内容是可重建缓存，可以由用户手动查看和清理。
- `data/user_identity/*.sqlite3` 保存服务器级 `sha256-linear-v1` 用户映射；同服多个 Bot 会共享该数据库。
- `data/bots/<bot>/runtime.snapshot.bin` 保存最近一次热存储快照，覆盖 ID 映射、消息缓存、私聊房间映射和群上下文绑定。
- `data/bots/<bot>/runtime.journal.bin` 保存快照之后的增量变更，用于启动恢复和窗口整理后的状态回放。
- Bot 运行时仍然会按目录划分，但桥接热路径以内存态为准，不再依赖旧版逐文件在线更新模式。
- `logs/` 保存 RocketCatShell 自己的运行日志。

当前代码中的路径解析都基于项目根目录的相对布局发现，不依赖写死的 Windows 绝对路径。

---

## 已知限制

- 当前提供五类 OneBot v11 网络传输，但仍是语义桥接器，不是官方 Rocket.Chat 平台适配器。
- 合并转发消息当前未实现。
- 系统事件、审计事件、编辑 / 撤回 / 已读等非消息类事件不在这一版的桥接承诺范围内。
- E2EE 仅覆盖 Rocket.Chat 加密私聊和加密私有群组。
- 远端媒体如果下载失败、超出大小限制或源地址不可用，相关媒体发送会失败或降级。
- `set_msg_emoji_like` 的扩展行为依赖本地插件；如果未安装对应插件，核心会返回未处理。

---

## 致谢
- 已适配上游[AstrBot](https://github.com/AstrBotDevs/AstrBot)
- 插件版 RocketCat 桥接器为当前独立版提供了已验证的桥接核心和 WebUI 设计基础
- 基础实现参考：[NET-Homeless/astrbot_plugin_rocket_chat_adapter](https://github.com/NET-Homeless/astrbot_plugin_rocket_chat_adapter) `v0.5.3`
- 与 AstrBot 的 OneBot v11 / aiocqhttp 协同链路为当前桥接路径提供了成熟上游
- [Rocket.Chat](https://rocket.chat) — 开源团队协作平台
- [aiohttp](https://github.com/aio-libs/aiohttp) — Python 异步 HTTP 客户端
- [FastAPI](https://fastapi.tiangolo.com/) — 轻量 WebUI 后端框架
