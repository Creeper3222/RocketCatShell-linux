# RocketCatShell

[![Platform](https://img.shields.io/badge/Platform-OneBot%20v11%20Reverse%20WS-pink)](#)
[![Runtime](https://img.shields.io/badge/Python-%3E%3D3.11-blue)](#环境要求)

将 [Rocket.Chat](https://rocket.chat) 通过桥接方式接入 OneBot v11 生态的独立客户端。它继承了插件版 [RocketCat](https://github.com/Creeper3222/astrbot_plugin_rocketchat_onebot_bridge) 已经验证过的桥接核心、独立 WebUI 和管理能力，但已经不再依附于 AstrBot 插件宿主，而是作为一个可以独立运行、独立配置、独立扩展的本地控制台存在。

本项目的目标不是继续做一个“宿主里的桥接插件”，而是把 RocketCat 发展成一套真正独立的 `Rocket.Chat <-> OneBot v11` 桥接软件。

这个 live 目录对应的是 RocketCatShell 的 Linux / Docker 迁移版：核心功能现已与 Windows `v0.1.4` rebuild 对齐，同时保留容器初始化、外部挂载目录和内置插件自动补种等 docker 化包装层。

> 当前 README 对应版本为 `v0.1.4`。`v0.1.3` 是破坏性架构重构基线；本次 P0 / P1 / P2 三轮“最佳性能”优化统一归入 `v0.1.4` 更新。

这意味着：

- RocketCatShell 自己拥有 `config/`、`data/`、`logs/` 目录边界。
- RocketCatShell 自己提供本地 WebUI、登录认证、Bot 管理和插件管理。
- RocketCatShell 仍然可以作为 OneBot reverse WebSocket 客户端与 AstrBot 协同，但不再依赖 AstrBot 插件宿主才能运行。

---

## v0.1.4（性能优化更新）

`v0.1.4` 建立在 `v0.1.3` 的 memory-authoritative runtime 之上，目标不是改变桥接语义、Docker 挂载模型或目录布局，而是继续压低热路径延迟、内存峰值和 WebUI 空闲开销。

- P0 热路径优化：热存储减少重复深拷贝，source / surrogate message 索引共享同一 entry；入站消息注册表改为紧凑字段存储，需要 hydrate 时再重建 OneBot 事件；Rocket.Chat 入站 DDP 消息改为按房间分片队列处理，同房间保持 FIFO，不同房间可并行。
- P0 去重优化：入站重复消息签名改为轻量字段签名，并对附件、文件、URL、mentions 等大结构使用稳定哈希，降低重复 update 判断成本。
- P1 JSON / 连接优化：新增统一 JSON codec，优先使用 `orjson`；HTTP session 使用连接池、DNS TTL 和 keepalive；WebSocket 发送统一走预序列化字符串，减少 aiohttp 默认 JSON 路径开销。
- P1 媒体优化：普通远端媒体下载改为边下载边写临时文件；E2EE 媒体上传改为原文件分块读取、CTR 分块加密到临时密文文件，再以文件流上传；Base64 媒体增加大小预判和严格解码。
- P1 插件 action 优化：插件可声明 `handled_actions`，运行时按 action 精确分发，未声明的旧插件继续作为 fallback，减少 OneBot action 广播式试探。
- P2 WebUI / 插件控制面优化：插件列表和详情增加目录签名缓存，未变化时不再反复扫目录和解析配置；基础信息页 Rocket.Chat server branding 增加 TTL 缓存；猫猫日志从 1 秒短轮询改为长轮询，空闲时显著减少 WebUI 请求和 JSON 响应。

升级到 `v0.1.4` 不需要迁移 `v0.1.3` 的 runtime 数据；Docker / Linux 版需要在更新源码后重新构建镜像或重新安装依赖，才能吃满 `orjson` 快路径收益。

---

## v0.1.3（破坏性更新）

`v0.1.3` 对 RocketCatShell 的运行态、持久化模型、WebUI 管理边界和插件承载方式做了重构级调整。

- 本次更新不承诺兼容 `v0.1.2` 及更早版本的旧配置文件、旧运行态持久化数据、旧目录结构，以及“依附 AstrBot 插件宿主”的部署方式。
- 升级到 `v0.1.3` 前，请先自行备份旧版本目录，再按当前 README 描述的独立 Shell 目录重新部署或迁移。

- 桥接运行态已切换为以内存为权威的热存储：ID 映射、消息注册表、私聊房间映射和群上下文绑定都会在热路径常驻内存，不再依赖旧版 JSON 逐条读写。
- 持久化改为单写入后台 worker：运行态会以 `runtime.snapshot.bin` + `runtime.journal.bin` 的组合落盘，启动时先载入快照再回放 journal，用于恢复最近状态而不是拖慢收发热路径。
- 入站翻译链路新增批量提交与更细粒度的热路径优化：房间信息查询、引用构建、提及提取、媒体描述提取都会尽量复用结果，降低图片 / 引用 / 提及混合消息的处理成本。
- 新增房间信息缓存 TTL 配置 `room_info_cache_ttl_seconds`，默认 300 秒，避免同一房间元信息被高频重复拉取。
- 支持可选性能追踪：可通过环境变量 `ROCKETCAT_PERF_TRACE` 或 bot 原始配置 `perf_trace_enabled` 打开，记录 `translate` / `emit_event` 以及入站 `room_lookup`、`mapping_alloc`、`quote_contexts`、`message_store`、`batch_commit` 等阶段耗时。
- 猫猫日志现在也会捕获 `RocketCatPerf` 性能追踪日志，并提供左上角 `Perf` 开关用于独立过滤这类日志。
- 新增 `tools/benchmark_inbound_translate.py`，可在本地对比 control / rebuild 两条入站翻译路径的延迟差异。
- message 索引策略改为固定窗口：只保留最近 N 条 message 映射，超出窗口时裁剪最旧映射，WebUI 的“重建索引”只做窗口整理与关联消息缓存重建，不再保留旧版 reset / compact 语义。

---

## Linux / Docker 补充说明

- `launcher.sh` 会先调用 `tools/check_requirements.py` 检查依赖，再在缺失或版本不兼容时自动安装并复检。
- `Dockerfile` 会把 `tools/` 一并打进镜像，保证容器内和宿主机目录里的工具链一致。
- `docker/entrypoint.sh` 仍负责容器首次启动时写入 `config/shell.json` 默认值，并在外部 volume 为空时补种内置插件。
- `docker-compose.yml`、`.env` 和 `.env.example` 已改为 Linux 风格的默认持久化路径 `/opt/rocketcatshell/...`，不再使用旧的 Windows `D:/docker/...` 示例。
- 共享媒体导出仍保留在 Docker 版里：默认继续使用 `ROCKETCAT_SHARED_MEDIA_HOST_DIR` 路径传输，减少容器内临时文件与 Base64 膨胀；如果 AstrBot 和 RocketCatShell 同为 Docker 且更适合直接传 Base64，可在基础设置页启用 `enable_base64_media_transport`，或在首次启动前把 `.env` 里的 `ROCKETCAT_DEFAULT_ENABLE_BASE64_MEDIA_TRANSPORT=true`。

---

## 架构说明

```text
Rocket.Chat Server
		^
		|  REST API + DDP/WebSocket
		v
RocketCatShell
		^
		|  OneBot v11 Reverse WebSocket Client
		v
OneBot v11 Consumer
		^
		|  plugins / providers / event pipeline
		v
AstrBot or other compatible OneBot-side workflow
```

声明：

- RocketCatShell 当前仍然围绕 OneBot v11 reverse WebSocket 语义工作。
- 目前已经适配 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，其它onebot v11语义后续再考虑实现
- 如果你的上游是 AstrBot，那么可以继续直接复用 AstrBot 自带的 aiocqhttp / OneBot v11 接入链路。
- RocketCatShell 当前不是一个通用的 Rocket.Chat 官方平台适配器，而是一套 OneBot 语义桥接器。

---

## 功能特性

- 支持 Rocket.Chat 频道、私有群组、私聊消息桥接为 OneBot v11 语义。
- 支持统一 Bot 注册表，不再使用主 bot / 副 bot 的分层持久化模型。
- 内置独立 WebUI，可管理网络配置、基础信息、运行日志、基础设置和本地插件。
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
- [I Am Thinking](https://github.com/sssn-tech/astrbot_plugin_iamthinking) 适配能力已从核心桥接层剥离为本地插件 `rocketcat_plugin_adapt_iamthinking`。

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
- `set_msg_emoji_like`：由本地插件决定是否处理；核心本身不再硬编码 I Am Thinking 逻辑

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
- `tools/benchmark_inbound_translate.py` 可用于本地构造文本 / 引用 / 线程 / 图片场景，对比 control 与 rebuild 两条入站翻译链路的延迟。

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
- 发送到加密房间时，会自动走加密消息体和加密媒体上传确认流程。
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
- `基础设置`：管理 WebUI 登录密码、WebUI 端口、消息映射窗口条数上限，以及配置导出 / 导入。
- `插件管理`：管理 RocketCatShell 本地插件，包括启停、设置、重载和卸载。

### WebUI 认证
<p align="center">
  <img src="https://github.com/user-attachments/assets/d233e9d8-1931-46b0-9309-91957443e8f2" width="100%" />
</p>

- RocketCatShell 默认启用密码访问。
- 初始登录密码为 `123456`。
- 后端提供登录、登出、Cookie 会话和受保护 API 访问控制。
- 会话失效时，前端会自动跳回登录页。
- WebUI 登录密码不允许设置为空。

### 配置导出 / 导入

- 导出默认文件名为 `rocketcat_config.json`。
- 顶层判别字段为 `Is rocketcat config`。
- 导出内容包含所有 Bot 设置（包括 `room_info_cache_ttl_seconds` 与 `perf_trace_enabled`）、WebUI 登录密码、WebUI 端口、消息映射窗口条数上限和本地插件主配置。
- 导入时会先校验判别字段；若不是 RocketCatShell 配置文件，则会返回失败提示。

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
- 运行时重载插件
- 卸载插件本体，并可选删除插件主配置与插件持久化数据

当前 `rocketcat_plugin_adapt_iamthinking` 已作为本地插件存在，用于接管 `set_msg_emoji_like` 并把思考中 / 已完成态映射为 Rocket.Chat reaction shortcode。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | `>= 3.11` |
| 运行依赖 | `aiohttp`, `cryptography`, `fastapi`, `uvicorn`, `PyYAML` |
| Rocket.Chat | 需要可用的 REST API、DDP/WebSocket 和 E2EE 接口（如使用加密功能） |
| OneBot 上游 | 需要可用的 OneBot v11 reverse WebSocket 服务 |

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

默认会把持久化目录挂到项目目录下的 `./config`、`./data/...` 和 `./logs`，共享图片缓存目录会显式挂到 `./data/bots/_shared_media`；如果你要改宿主机挂载位置，优先修改 `.env` 里的 `ROCKETCAT_*_DIR` 和 `ROCKETCAT_SHARED_MEDIA_HOST_DIR`。

Docker 首次启动时，“基础设置”里的“启用 Base64 传输媒体”默认关闭；如果你希望新安装首次生成的 `shell.json` 直接开启它，可以在 `.env` 中把 `ROCKETCAT_DEFAULT_ENABLE_BASE64_MEDIA_TRANSPORT=true`。



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
- `data/bots/`
- `data/plugins/`
- `data/plugin_data/`
- `logs/`
- `config/shell.json`
- `config/bots.json`

其中初始默认值包括：

- 本机直接运行时的 WebUI 地址：`http://127.0.0.1:5751/`
- Docker Compose 默认宿主机访问地址：`http://127.0.0.1:5751/`（容器内 `webui_host=0.0.0.0`，再由 compose 负责端口发布）
- WebUI 初始密码：`123456`
- 最大消息映射窗口条数：`1000`
- Base64 媒体传输开关：`false`
- 本机直接运行时默认 OneBot reverse WS 地址：`ws://127.0.0.1:6199/ws/`
- Docker Compose / `docker/entrypoint.sh` 默认 OneBot reverse WS 地址：`ws://host.docker.internal:6200/ws/`
- `next_onebot_self_id`：`910001`

也就是说，只要依赖安装正确，RocketCatShell 在空配置状态下可以自己创建必需目录和初始配置文件。

---

## 快速开始

### 1. 准备 OneBot v11 reverse WebSocket 上游

如果你的上游是 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，可以先在 AstrBot 中创建内置 OneBot v11 平台：

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

如果 AstrBot 也跑在 Docker，并且它不能直接访问 `ROCKETCAT_SHARED_MEDIA_HOST_DIR` 指向的共享目录，那么可以在“基础设置”页启用“启用 Base64 传输媒体”，或者在首次部署前把 `.env` 里的 `ROCKETCAT_DEFAULT_ENABLE_BASE64_MEDIA_TRANSPORT=true`，让新生成的 `shell.json` 默认启用该模式。

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

首次登录后建议立刻在 `基础设置` 页修改密码。

### 3. 创建第一个 Bot
<p align="center">
  <img src="https://github.com/user-attachments/assets/611a6601-0af6-4ebf-ac3c-e301a03631eb" width="100%" />
</p>
在 `网络配置` 页点击 `新建 Bot`，为该 Bot 填写：

- Rocket.Chat 服务器地址
- Rocket.Chat 用户名
- Rocket.Chat 密码
- 按需填写 E2EE 密钥密码
- OneBot reverse WS 地址
- OneBot Access Token
- OneBot self_id

高级设置中还可以进一步设置：

- 重连延迟
- 最大连续重连次数
- 子频道会话隔离
- 远端媒体大小上限
- 忽略机器人自己的消息
- 调试日志

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
| `webui_access_password` | WebUI 登录密码，默认 `123456`。 |
| `message_index_max_entries` | 最大消息映射窗口条数，默认 `1000`；超出后会清理最早映射，并在达到重置阈值后自动重排当前窗口。 |
| `enable_base64_media_transport` | 是否把可本地读取的图片/语音/视频优先转成 `base64://` 发送，默认 `false`；关闭时继续沿用共享目录路径模式。 |
| `log_level` | 日志级别，默认 `INFO`。 |
| `auto_open_browser` | 启动后是否自动打开浏览器。 |
| `default_onebot_ws_url` | 新建 Bot 时使用的默认 OneBot reverse WS 地址。 |
| `default_onebot_access_token` | 新建 Bot 时使用的默认 OneBot Access Token。 |
| `default_reconnect_delay` | 默认重连延迟。 |
| `default_max_reconnect_attempts` | 默认最大连续重连次数。 |
| `default_enable_subchannel_session_isolation` | 默认是否开启子频道会话隔离。 |
| `default_remote_media_max_size` | 默认远端媒体大小上限。 |
| `default_skip_own_messages` | 默认是否忽略机器人自己的消息。 |
| `default_debug` | 默认是否开启调试日志。 |
| `next_onebot_self_id` | 下一个建议的 OneBot self_id。 |

### 单个 Bot 配置

`config/bots.json` 中每个 Bot 主要包含：

| 配置项 | 说明 |
|--------|------|
| `id` | Bot 唯一 ID。 |
| `name` | Bot 显示名。 |
| `enabled` | 是否启用该 Bot。 |
| `server_url` | Rocket.Chat 服务器地址。 |
| `username` | Rocket.Chat 用户名。 |
| `password` | Rocket.Chat 密码。 |
| `e2ee_password` | E2EE 私钥密码。 |
| `onebot_ws_url` | OneBot reverse WebSocket 地址。 |
| `onebot_access_token` | OneBot reverse WebSocket Token。 |
| `onebot_self_id` | OneBot 机器人 ID，必须唯一。 |
| `reconnect_delay` | 断线重连等待秒数。 |
| `max_reconnect_attempts` | 最大重连次数；`0` 表示不限次数。 |
| `enable_subchannel_session_isolation` | 是否按子频道隔离上下文。 |
| `remote_media_max_size` | 远端媒体大小上限。 |
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
	plugins_config/

data/
	bots/
	plugins/
	plugin_data/

logs/
	rocketcat.log
```

说明：

- `config/` 只保存配置和插件主配置。
- `data/` 保存本地插件本体、插件持久化数据和各 Bot 运行时数据。
- `data/bots/<bot>/runtime.snapshot.bin` 保存最近一次热存储快照，覆盖 ID 映射、消息缓存、私聊房间映射和群上下文绑定。
- `data/bots/<bot>/runtime.journal.bin` 保存快照之后的增量变更，用于启动恢复和窗口整理后的状态回放。
- Bot 运行时仍然会按目录划分，但桥接热路径以内存态为准，不再依赖旧版逐文件在线更新模式。
- `logs/` 保存 RocketCatShell 自己的运行日志。

当前代码中的路径解析都基于项目根目录的相对布局发现，不依赖写死的 Windows 绝对路径。

---

## 已知限制

- 当前仍然围绕 OneBot v11 reverse WebSocket 工作，不是官方 Rocket.Chat 平台适配器。
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
