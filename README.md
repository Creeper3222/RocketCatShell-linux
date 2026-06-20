# RocketCatShell

[![Platform](https://img.shields.io/badge/Platform-OneBot%20v11%20Reverse%20WS-pink)](#)
[![Runtime](https://img.shields.io/badge/Python-%3E%3D3.11-blue)](#环境要求)

将 [Rocket.Chat](https://rocket.chat) 通过桥接方式接入 OneBot v11 生态的独立客户端。它继承了插件版 [RocketCat](https://github.com/Creeper3222/astrbot_plugin_rocketchat_onebot_bridge) 已经验证过的桥接核心、独立 WebUI 和管理能力，但已经不再依附于 AstrBot 插件宿主，而是作为一个可以独立运行、独立配置、独立扩展的本地控制台存在。

本项目的目标不是继续做一个“宿主里的桥接插件”，而是把 RocketCat 发展成一套真正独立的 `Rocket.Chat <-> OneBot v11` 桥接软件。

这个 live 目录对应的是 RocketCatShell 的 Linux / Docker 迁移版：核心功能现已与 Windows `v0.1.9` 对齐，同时保留容器初始化、外部挂载目录、内置插件自动补种、Linux PTY，以及 Docker 专属共享媒体路径翻译 / Base64 兼容策略等 docker 化包装层。

> 当前 README 对应版本为 `v0.1.9`。`v0.1.3` 是破坏性架构重构基线，`v0.1.4` 统一收口性能优化，`v0.1.5` 补齐了内置指令与运维增强，`v0.1.6` 推进运行诊断可观测性和入站热路径性能收口，`v0.1.7` 开始补齐面向 Docker 迁移的内置文件管理能力，`v0.1.8` 引入 NapCat 风格 WebUI 系统终端，而 `v0.1.9` 完善多引用、用户身份映射及 Rocket.Chat 7.10–8.5 兼容，并从本版本开始发布 `linux/amd64` 与 `linux/arm64` 镜像。

这意味着：

- RocketCatShell 自己拥有 `config/`、`data/`、`logs/` 目录边界。
- RocketCatShell 自己提供本地 WebUI、登录认证、Bot 管理、插件管理、项目根目录内文件管理和系统终端。
- RocketCatShell 仍然可以作为 OneBot reverse WebSocket 客户端与 AstrBot 协同，但不再依赖 AstrBot 插件宿主才能运行。

---

## v0.1.9（多引用、用户身份与多架构更新）

`v0.1.9` 将 Windows 版已经实地验证的多引用和用户身份重构迁移到 Linux / Docker，同时保留容器环境的媒体共享、Linux PTY 和持久化边界。

- 非加密频道仅当当前消息包含两个及以上顶层 `attachments[*].message_link` 时进入多引用模式；正文中普通粘贴的多个消息链接不会被误判。
- E2EE 频道会额外检查解密正文开头连续的空标签 Markdown 消息链接。仅在 `e2e=done` 且连续命中两条及以上引用时，才按原顺序归一化为多个顶层 OneBot `reply`，并剥离系统引用前缀。
- 多个 `reply` 会被 AstrBot 分别解析为独立 `Reply.chain`，引用顺序、重复引用、文本、图片以及 `get_msg` 恢复行为均会保留。
- 用户身份映射改为服务器级 `sha256-linear-v1` 确定性 11 位 ID。映射保存于 `/app/data/user_identity/*.sqlite3`，同服多个 RocketCat bot 共享映射和人工 override。
- Bot 的 OneBot `self_id` 由登录账号不可变的 Rocket.Chat `userId` 自动生成；Bot 配置不再保存手工填写的 `onebot_self_id`，Shell 配置也不再使用 `next_onebot_self_id`。
- WebUI 增加醒目的 User 映射入口以及映射搜索、刷新、保存、冲突高亮和删除重建操作。覆盖和删除使用 revision 校验，防止用旧页面覆盖新数据。
- 哈希冲突会持久化到每个相关 Bot 的 `re_waring.json`，并在对应 Bot 每次加载时重新输出详细 warning。
- Rocket.Chat 支持范围明确为 `7.10.x–8.5.x`：DDP 继续承担 resume 和订阅，业务 method 优先走 REST；8.x 固定使用 `rooms.media` + `rooms.mediaConfirm`，7.10 仅在现代端点明确不存在时回退旧上传接口。
- E2EE 媒体、历史消息媒体和生图参考图继续使用 Docker 共享媒体目录；关闭 Base64 模式时翻译为宿主机可见路径，开启时使用 `base64://`，不会向上游暴露容器内回环 HTTP 地址。
- 新增独立持久化挂载 `/app/data/user_identity`。升级前必须备份 `config/`、`data/bots/` 和 AstrBot 管理员配置，再运行 `tools/migrate_user_identity.py` 迁移旧 ID。
- Docker Hub 从本版本开始同时发布 `linux/amd64` 与 `linux/arm64`，固定版本标签为 `v0.1.9`，稳定滚动标签为 `latest`。

升级后，旧版实验性 forward 缓存会自然淘汰；message、room、thread 和 context 等非用户映射继续从原 runtime snapshot / journal 恢复。用户映射必须完成一次 v0.1.8 → v0.1.9 迁移，避免 AstrBot 管理员 ID 和私聊绑定突然变化。

---

## v0.1.8（系统终端与媒体上传策略更新）

`v0.1.8` 将 Windows live 已验证的 NapCat 风格 WebUI 终端迁移到 Docker / Linux 版，并针对容器环境使用 Linux PTY 实现，避免引入 Windows 专用的 `pywinpty` 依赖。
- 新增 `系统终端` WebUI 页面，支持新建多个终端、切换终端、关闭终端、拖动终端标签排序，以及在 xterm 区域内直接输入命令。
- Docker / Linux 版终端后端使用容器内 PTY，会话默认工作目录为 `/app`；shell 优先使用有效的 `$SHELL`，否则回退 `/bin/sh`，支持交互式程序、方向键历史、选择复制和窗口尺寸同步。
- WebUI 左上角新增侧边栏展开 / 收起按钮；`基础信息` 与 `运行诊断` 页会直接展示当前 RocketCatShell 版本，运行诊断页 CPU / 内存图标也与 NapCat 风格对齐。
- Bot 设置里的 `远程媒体大小上限(字节)` 现在同时约束远端媒体下载和媒体上传，默认仍为 `20971520`；上传或下载超过限制时会写入 error 日志，日志会包含 Bot、房间、文件名、实际大小和限制值等信息。
- 普通房间媒体上传优先使用 Rocket.Chat 8.0.0+ 的 `rooms.media/:rid` + `rooms.mediaConfirm/:rid/:fileId`，失败时回退旧版 `rooms.upload/:rid`；每个 Bot 会独立记忆当前最适上传端点，并在服务器升级或接口变化后支持反向 fallback。
- Docker 版共享媒体路径策略保持不变：本次只迁移上传端点选择、大小限制和日志行为，不改变 `ROCKETCAT_SHARED_MEDIA_DIR`、`ROCKETCAT_SHARED_MEDIA_HOST_DIR`、compose 挂载语义或 Base64 兼容模式。
- `requirements.txt` 新增 `websockets`，用于 WebUI 终端 WebSocket；Linux / Docker 版不包含 Windows 专用 `pywinpty`。

升级到 `v0.1.8` 不需要迁移现有配置或运行时数据；更新源码后重新构建镜像即可。如果浏览器已经打开旧版 WebUI，刷新页面以获取最新静态资源。

---
## v0.1.7（内置文件管理更新）

`v0.1.7` 的首要目标是把类似 NapCat 的内置文件管理能力迁移到 Docker / Linux 版，方便容器环境下直接在 WebUI 内浏览和管理 RocketCatShell 文件。文件管理根目录固定为容器内项目根 `/app`，只能访问 `/app` 内的相对路径；宿主机文件只有通过 `docker-compose.yml` 挂载到 `/app/...` 后才会出现在文件管理中。

- 新增 `文件管理` WebUI 页面，支持目录进入 / 返回、刷新、文件预览、图片全屏查看、文本编辑保存、新建文件或目录、上传、重命名、移动、删除和打包下载。
- 新增文件 API：`GET /api/files`、`POST /api/files/read`、`POST /api/files/write`、`POST /api/files/create`、`POST /api/files/upload`、`POST /api/files/delete`、`POST /api/files/move`、`POST /api/files/rename`、`GET /api/files/preview`、`GET /api/files/download` 和 `POST /api/files/download`。
- 文件管理 API 只接受 `/app` 内相对路径，拒绝 `..`、系统绝对路径、Windows 盘符路径和符号链接解析后的越界目标；这只约束 WebUI 文件管理边界，不改变 Docker 版共享媒体目录翻译或 Base64 兼容策略。
- Docker 包装层和核心源码默认只读保护，包括 `rocketcat_shell/`、`tools/`、`docker/`、两个内置插件源码目录，以及 `requirements.txt`、`Dockerfile`、`docker-compose.yml`、`.dockerignore`、`.env.example`、`launcher.sh` 等发布与启动文件。
- 用户挂载或创建在非保护目录中的文件仍可按规则管理。例如外部挂载到 `/app/data/plugins` 的非内置插件、`/app/data/plugin_data`、`/app/data/bots` 与 `/app/data/resource` 中的普通文件可浏览、上传、移动、删除或编辑。
- 对明确的敏感持久化数据文件增加二次鉴权，包括 `config/shell.json`、`config/bots.json`、`config/plugins_config/*.json` 和 `data/bots/**/runtime_state.json`。鉴权密码复用 WebUI 登录认证 / 文件管理鉴权密码，密码只通过请求体提交；鉴权文件保存前会再次提示修改风险。
- 文件列表图标按常见类型细分：目录、普通文件、`.txt`、`.json/.py/.md`、`.pdf`、`.doc/.docx` 使用不同图标；PDF 类型栏单独显示为 `PDF文件`。
- `requirements.txt` 新增 `python-multipart`，用于 WebUI 上传文件接口。

升级到 `v0.1.7` 不需要迁移 `v0.1.6` 的配置目录或运行时数据；Docker / Linux 版在更新源码后重新构建镜像即可。如果浏览器已经打开旧版 WebUI，刷新页面以获取最新静态资源。

---

## v0.1.6（诊断可观测性与性能收口更新）

`v0.1.6` 建立在 `v0.1.5` 的内置指令、Shell 插件系统和独立 WebUI 管理面之上。这一版不再继续增加新的大块宿主能力，而是把这套独立 Shell 更像“可发布软件”地收口：一方面补上更直观、更低延迟的运行诊断与状态可观测性，另一方面把入站翻译热路径和 benchmark 工具一起推进到更贴近真实负载的状态，方便后续继续做针对性优化。

- `/api/diagnostics` 的主机快照采集现在改为短 TTL 缓存，避免每次请求都重新执行一次固定 CPU 采样等待；内置 `#system` 指令也复用同一套缓存逻辑，页面刷新、轮询和房间内诊断命令不再重复触发整段采样开销。
- 运行诊断已从“网络配置”页拆分为独立导航页，并新增主机快照缓存状态、快照年龄、TTL 等元数据展示。WebUI 现在可以直接区分当前是缓存命中、实时采样还是采样失败，而不是只看到一份静态诊断结果。
- 运行诊断页的主机 CPU / 内存摘要已升级为更直观的环形指示器视图，并补充系统总占用与 Shell 进程占用的双层视觉表达，配合在线 Bot / Snapshot / Journal 汇总，更适合长时间运行时做快速巡检。
- OneBot reverse WebSocket 客户端现在会显式放宽 `aiohttp` 的入站消息大小上限，并在断链日志里补充 `close_code`。这修复了 AstrBot 侧较大的 `base64://` 图片动作在进入 RocketCatShell 前就触发 reverse WS 断开的问题；Docker / Linux 版在启用 Base64 兼容传输或接收较大的本地图片动作时，也不再被默认 `4 MiB` 帧限制提前截断。
- Docker 版这里展示和 `#system` 返回的仍然是当前容器运行环境；容器外宿主机路径可见性、共享媒体目录翻译和 Base64 兼容策略没有被这次更新改写。
- 入站翻译热路径继续做了针对性优化：引用上下文任务只在确实可能存在引用时才构建；回复来源解析结果复用，避免重复扫描正文；纯文本引用不再误走 quoted media 提取；消息注册表 entry 复制路径改为面向 JSON-like 结构的轻量 clone；媒体描述提取改为单次遍历并为扁平 attachment 场景增加快速路径，继续压低高频图片 / 引用 / 混合消息场景下的固定开销。
- `tools/benchmark_inbound_translate.py` 现在支持 `--profile realistic`，可直接带入更接近真实环境的 room info / quote fetch / media delay，并新增 `quote_image`、`media_mix` 场景，避免只靠零延迟微基准得到过于理想化的结论。

升级到 `v0.1.6` 不需要迁移 `v0.1.5` 的配置目录、热存储 snapshot / journal 或本地插件数据；Docker / Linux 版在更新源码后重新构建镜像即可。如果浏览器已经打开旧版 WebUI，刷新页面以获取最新静态资源。

---

## v0.1.5（内置指令与运维增强更新）

`v0.1.5` 建立在 `v0.1.4` 的独立 Shell、热存储 runtime 和本地插件系统之上，重点不再是继续压热路径性能，而是补齐一层更适合日常使用与正式发布的本地控制能力，并补上 Rocket.Chat 新旧上传接口并存时期的兼容缺口。

- 新增本地内置指令插件 `rocketcat_plugin_built_in_command`。它通过 Shell 插件系统直接拦截 Rocket.Chat 入站精确纯文本指令，目前实现 `#rocketcat` 与 `#system` 两条命令，不再要求上游 AstrBot 侧参与处理。
- `#rocketcat` 用于返回当前桥接 Bot 的基础信息：包括客户端显示名、登录账号、显示昵称、OneBot self_id、连接状态和 Rocket.Chat 服务器地址，并追加发送 bot 头像与服务器 branding 头像，方便在房间内快速确认“当前是谁、连的是哪台、状态是否正常”。
- 媒体上传链路新增 plain upload 端点自适应：继续兼容旧版 Rocket.Chat 的 `rooms.upload/:rid`，并可在 `8.0.0+` 环境下自动回退到 `rooms.media/:rid`，修复 `#rocketcat` 中 bot 头像与 server branding 在 Rocket.Chat 8.0.1 等新服上退化为纯文本的问题。
- Rocket.Chat 8.x 的 `rooms.media/:rid` 不再像旧 `rooms.upload/:rid` 那样直接完成发图消息创建，因此 plain 媒体上传现在会在上传成功后继续调用 `rooms.mediaConfirm/:rid/:fileId`，真正把图片或文件发进房间，并恢复后续消息映射链路；同时对同服远端媒体会自动补 `rc_uid/rc_token` 登录态并按响应 `Content-Type` 选择正确后缀，修复 `#rocketcat` 与 WebUI 基础信息页拿不到 bot 头像、或把默认 SVG 头像误当作 PNG 上传后显示损坏的问题。
- `#rocketcat` 的插件直发媒体链路现在会在不需要 OneBot 映射时跳过 5 秒自回显等待，并把内置指令自回显抑制从一次性 `source_id` 扩展为短 TTL 的 `source_id + 房间/正文签名` 匹配，修复新旧服混跑时加密房间第二段回复超时和旧服非加密房间重复回显的问题。
- `#system` 用于返回当前 RocketCatShell 进程所在环境的系统快照：包括版本号、Python 版本、主机名、系统信息、CPU 型号 / 核心数 / 主频 / 系统占用 / Shell 进程占用，以及内存总量 / 已用 / 可用 / 当前进程占用。Docker 版这里反映的是当前容器运行环境；该命令依赖新增运行依赖 `psutil`。
- `rocketcat_plugin_adapt_iamthinking` 不再只做 reaction 映射。现在它可以在继续兼容 `set_msg_emoji_like` 的同时，把“思考中 / 已完成”阶段独立映射为 Rocket.Chat typing 指示器；reaction 与 typing 在插件设置页可分别开关，长时间思考还会自动续期 typing 心跳。
- Shell 启动层新增单实例锁：同一项目目录下的第二个 RocketCatShell 会在 runtime 初始化之前直接退出，不再像旧行为那样因为 WebUI 端口回退而悄悄拉起第二份 runtime，从根源上避免重复订阅和重复上报。

升级到 `v0.1.5` 不需要迁移 `v0.1.4` 的配置目录或 runtime 数据；Docker / Linux 版在更新源码后重新构建镜像，或直接运行 `launcher.sh` 时，会按 `requirements.txt` 自动检查并补装包括 `psutil` 在内的新增依赖。

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

- `launcher.sh` 会先调用 `tools/check_requirements.py` 检查依赖，再在缺失或版本不兼容时自动安装并复检；`v0.1.5` 新增的 `psutil` 也包含在这条自动补装链路里。
- `Dockerfile` 会把 `tools/` 一并打进镜像，保证容器内和宿主机目录里的工具链一致。
- `docker/entrypoint.sh` 仍负责容器首次启动时写入 `config/shell.json` 默认值，并对外挂 `data/plugins` 中的内置插件执行缺失补种与版本变更自动刷新；`rocketcat_plugin_built_in_command` 与 `rocketcat_plugin_adapt_iamthinking` 都会随镜像自动同步到外挂插件目录。
- `docker-compose.yml`、`.env` 和 `.env.example` 已改为 Linux 风格的默认持久化路径 `/opt/rocketcatshell/...`，不再使用旧的 Windows `D:/docker/...` 示例。
- 用户身份注册表使用独立挂载 `/app/data/user_identity`；默认宿主目录为 `/opt/rocketcatshell/data/user_identity`，必须与 `config/`、`data/bots/` 一起备份。
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
- WebUI 文件管理边界是容器内 `/app`，宿主机目录只有通过 compose 挂载到 `/app/config`、`/app/data/...`、`/app/logs` 等路径后才可见；这个边界只用于文件管理，不会改变 `ROCKETCAT_SHARED_MEDIA_DIR` / `ROCKETCAT_SHARED_MEDIA_HOST_DIR` 的共享媒体路径翻译语义。
- 共享媒体导出仍保留在 Docker 版里：默认继续使用 `ROCKETCAT_SHARED_MEDIA_HOST_DIR` 路径传输，把容器内共享媒体目录翻译为宿主机可见路径；只有在路径对上游不可见，或 AstrBot 与 RocketCatShell 同为 Docker、更适合直接传 Base64 时，才建议启用 `enable_base64_media_transport`，或在首次启动前把 `.env` 里的 `ROCKETCAT_DEFAULT_ENABLE_BASE64_MEDIA_TRANSPORT=true`。Base64 是兼容兜底，不是 Docker 版默认快路径。
- 官方镜像自 `v0.1.9` 起同时支持 `linux/amd64` 和 `linux/arm64`；Docker 会根据宿主机架构自动选择对应 manifest。

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
- 发送到加密房间时，会自动走加密消息体和加密媒体上传确认流程；媒体上传会分块读取原文件并分块写出密文临时文件。
- Rocket.Chat 8.2+ 删除加密附件时产生的 `removed-file` 标记会在解密合并后保留。
- Rocket.Chat 8.3+ E2EE REST 接口启用严格请求校验后，只提交对应端点允许的字段。
- E2EE 多引用会从解密正文开头的系统引用前缀生成多个顶层 OneBot `reply`，普通正文链接不会被误判。
- E2EE 解密媒体会写入 Docker 共享媒体目录，再按设置转换为宿主机路径或 `base64://`，与非加密频道使用同一套上游可见性策略。
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

当前内置示例包括：

- `rocketcat_plugin_built_in_command`：RocketCatShell 自有的内置指令系统插件。当前精确拦截 `#rocketcat` 与 `#system`，在本地直接回复，不再把命令正文继续交给上游；插件回复也会在入站侧抑制自回显再次上报。
- `rocketcat_plugin_adapt_iamthinking`：用于接管 `set_msg_emoji_like`。除 reaction shortcode 映射外，现在还支持独立的 typing 指示器开关；bot 进入思考阶段时会触发 Rocket.Chat typing，应答结束时主动清除，长时间思考会自动续期心跳。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | `>= 3.11` |
| 运行依赖 | `aiohttp`, `cryptography`, `fastapi`, `orjson`, `psutil`, `uvicorn`, `PyYAML` |
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

默认会把持久化目录挂到 `/opt/rocketcatshell/...`；如果你要改宿主机挂载位置，优先修改 `.env` 里的 `ROCKETCAT_*_DIR` 和 `ROCKETCAT_SHARED_MEDIA_HOST_DIR`。

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
- Base64 媒体传输开关：`false`
- 本机直接运行时默认 OneBot reverse WS 地址：`ws://127.0.0.1:6199/ws/`
- Docker Compose / `docker/entrypoint.sh` 默认 OneBot reverse WS 地址：`ws://host.docker.internal:6200/ws/`
- Bot 的 OneBot `self_id` 会在登录 Rocket.Chat 后根据不可变 `userId` 自动生成。

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

首次登录后建议立刻在 `基础设置` 页修改 WebUI 登录认证 / 文件管理鉴权密码。

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
| `webui_access_password` | WebUI 登录认证 / 文件管理鉴权密码，默认 `123456`。该密码同时用于登录 WebUI 和打开敏感持久化数据文件。 |
| `message_index_max_entries` | 最大消息映射窗口条数，默认 `1000`；超出后会清理最早映射，并在达到重置阈值后自动重排当前窗口。 |
| `enable_base64_media_transport` | 是否把可本地读取的图片/语音/视频优先转成 `base64://` 发送，默认 `false`；关闭时继续沿用共享目录路径模式。 |
| `log_level` | 日志级别，默认 `INFO`。 |
| `auto_open_browser` | 启动后是否自动打开浏览器。 |
| `default_onebot_ws_url` | 新建 Bot 时使用的默认 OneBot reverse WS 地址。 |
| `default_onebot_access_token` | 新建 Bot 时使用的默认 OneBot Access Token。 |
| `default_reconnect_delay` | 默认重连延迟。 |
| `default_max_reconnect_attempts` | 默认最大连续重连次数。 |
| `default_enable_subchannel_session_isolation` | 默认是否开启子频道会话隔离。 |
| `default_remote_media_max_size` | 默认远端媒体上传 / 下载大小上限。 |
| `default_skip_own_messages` | 默认是否忽略机器人自己的消息。 |
| `default_debug` | 默认是否开启调试日志。 |

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
| `reconnect_delay` | 断线重连等待秒数。 |
| `max_reconnect_attempts` | 最大重连次数；`0` 表示不限次数。 |
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
- `data/` 保存本地插件本体、插件持久化数据、用户身份注册表和各 Bot 运行时数据。
- `data/user_identity/*.sqlite3` 保存服务器级 `sha256-linear-v1` 用户映射；同服多个 Bot 会共享该数据库。
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
