# 更新日志

RocketCatShell 各版本的功能变更、兼容性调整、问题修复和迁移说明统一记录在本文件中。`README.md` 只维护当前功能、配置与使用方式。

后续开发先在“未发布”下记录，发布时再归入对应版本。

## 未发布

### Docker / Linux

- 默认 Compose 将 OneBot HTTP / SSE 和 WebSocket 服务端分别发布到较少冲突的宿主机环回端口 `16300`、`16301`，容器内监听端口继续为 `3000`、`3001`；仅使用客户端传输的部署可完全省略两项服务端端口映射。

## v0.2.3（五类 OneBot 传输、双向兼容与性能优化）

### 界面与交互

- v0.2.3 第一轮优化侧栏与主要管理卡片的指针反馈：侧栏项目以轻微横移配合状态高亮，网络配置、基础信息、运行诊断和插件管理的主卡片以轻微上移配合品牌色描边；动效仅在精细指针设备启用，并与键盘操作、减弱动效偏好及现有卡片拖拽排序保持隔离。
- 网络配置新增 `HTTP服务器`、`HTTP客户端`、`HTTP SSE服务器`、`Websocket服务器`、`Websocket客户端` 五种准确类型的新建菜单与筛选栏，主操作文案统一精简为“新建”；Bot 设置、网络卡片、基础信息和运行诊断统一由后端传输目录动态渲染类型字段与状态，类型创建后固定，筛选状态下排序只替换可见 Bot 的全局槽位。

### OneBot 网络传输

- 将原有反向 WebSocket 客户端拆入模块化 `OneBotTransport` 契约和声明式 `TransportSpec` 注册表；五种类型各自拥有独立模块，只组合共享的消息 codec、按需 0～8 Worker / 256 队列 action 调度器、HTTP action 服务与 WebSocket peer 基础组件，新增传输不需要修改 Bot API、BridgeRuntime 或通用 WebUI 渲染器。
- HTTP服务器支持 GET / POST action、Bearer / query Token、CORS 与可选同端口 WebSocket；HTTP SSE服务器在相同 action 能力上增加 `/_events` 有序事件流；Websocket服务器区分 `/api` action-only 与其它 action + event 路径，并为事件连接发送生命周期和心跳。
- HTTP客户端以有界队列 POST 事件，携带 `X-Self-ID` 和可选 HMAC-SHA1 `X-Signature`，并执行响应中的 `reply` 快速回复；未实现的删除、踢人、禁言和审批操作明确按 `1404` 记录。Websocket客户端保留原反向连接行为，同时增加可配置重连、心跳、Array / String CQ 格式与标准 OneBot 请求头。
- 新增 `config/onebot_transports.json` 格式 1，按 Bot ID 保存规范化 `{type, settings}`；v0.2.1 / v0.2.2 平面字段会无损迁移为 Websocket客户端，配置导入 / 导出支持新旧格式，非客户端在旧 `bots.json` 中使用不可连接的安全投影，避免回退旧版后误连默认上游。
- Websocket客户端补全 v0.2.2 双向兼容：回退旧版后修改的 URL、Token、Debug 与自身消息开关会在再次升级时重新吸收，同时保留 v0.2.3 的消息格式、心跳和重连设置；迁移与协调由传输模块声明，不在注册表写死类型分支。
- 启用的 OneBot 服务端会在保存前检查 Bot 间及 WebUI 监听冲突；所有 URL、Host、端口、格式、心跳、重连和字段类型在写盘前校验。监听、HTTP 投递或 OneBot 对端失败均独立报告，不消耗 Rocket.Chat 重连次数，也不会自动停用 Bot。

### 性能与稳定性

- 事件循环热路径指标改为无锁精确计数与固定 `1/16` 延迟采样；OneBot Array 事件复用不可变序列化帧，HTTP、SSE 和多个 WebSocket peer 不再重复深拷贝与编码。HTTP 快速回复异步提交，SSE 改为事件驱动关闭与 15 秒 keepalive，慢 peer 使用独立有界队列隔离。
- OneBot action Worker 与 Rocket.Chat 房间分片 Worker 改为按需创建并在空闲 60 秒后回收到 0；房间路由和队列深度统计移除逐消息队列遍历，同时继续保证同房间严格顺序、满载丢弃最新消息及精确计数。
- Journal 快照阈值提高至 8192 个逻辑批次，写入线程可按顺序合并最多 32 个连续 mutation batch；快照继续兼容格式 1，并分别记录锁等待、锁内复制、序列化与写入耗时。
- 媒体下载改为约 1 MiB 分块写入并同步计算摘要，大文件 E2EE 使用约 512 KiB 批次；文件预览、编辑、上传、原子替换和目录 ZIP 构建移出事件循环，目录下载使用磁盘临时文件流式响应并持续执行动态空间保护。
- 系统终端以 16 KiB / 16 ms 合并输出，使用 200k 字符环形历史和每客户端独立的 64 帧 / 1 MiB 发送队列；PTY 读取线程只保留一个待处理事件循环唤醒，慢客户端会断开并可通过重连恢复近期输出。
- 高频 WebUI 控制面改用纯 ASGI 鉴权与缓存中间件；Bot 卡片使用 compact 响应和 ETag，完整编辑数据按需读取，诊断使用 1 秒 single-flight 缓存，未变化的数据不再重复传输或重建。
- 新增固定种子的五分钟迭代压力模式、四 peer 扇出 / 房间路由 / compact Bot 微基准及空闲回收验证；多轮剖析与测试报告写入忽略目录 `data/perf/`，用于比较吞吐、延迟、CPU、RSS、线程、句柄、Task、队列和精确过载丢弃。

### Docker / Linux

- Linux 版三方意图合并 Windows v0.2.3 的平台中立实现，同时保留 Linux PTY、进程组、诊断、受保护路径、PID 1 更新 handoff 与容器可写层恢复链路。
- Compose 默认新增仅绑定宿主机 `127.0.0.1` 的 `3000`（HTTP / SSE）与 `3001`（WebSocket）映射；容器内服务端需监听 `0.0.0.0`，客户端访问宿主机服务使用 `host.docker.internal`。
- `config/onebot_transports.json` 纳入现有持久配置挂载；镜像重建、WebUI 同容器更新、回退和重新升级均保留五类传输配置及 v0.2.2 WebSocket 兼容投影。


## v0.2.2（Windows / Docker Linux 版本管理、UI 与性能稳定性）

### Docker / Linux 发行与容器更新

- Docker/Linux 同步迁入本版的平台中立性能、稳定性、WebUI、卡片排序、配置兼容和 I Am Thinking v0.2.0 四态适配，同时保留 Linux PTY、OS/CPU 诊断、进程组、文件保护、Docker 网络和媒体 URL 行为。
- Linux 更新发现固定使用 `Creeper3222/RocketCatShell-linux` 与 `RocketCatShell-linux-vX.Y.Z.zip`，v0.2.1 及更早版本继续排除；清单新增 `platform: linux` 和 `container_runtime_generation: 1`。
- 容器更新采用无 Docker socket 的可写层事务：PID 1 优雅退出后 `exec` 冻结 helper，精确备份和替换应用层，使用 loopback 健康检查；目标失败或替换中断时由镜像内 helper 恢复。
- 修复容器内版本切换时旧 WebUI 刚释放端口、目标进程却回退到随机端口而导致健康检查误判的问题；更新启动现在只重试配置端口，并由 PID 1 接管 `SIGTERM` 完整执行最长 30 秒的优雅排空后再进入自动回滚。
- Dockerfile、Compose、entrypoint、`.env.example` 与 Linux launcher 随正式 ZIP 提供给镜像部署，但被明确排除在 WebUI 热事务受管路径之外。
- 普通 `docker restart` 保留容器可写层更新；删除或强制重建容器恢复镜像版本，七个持久挂载中的配置、Bot 状态、用户插件、插件数据、身份数据库、媒体与日志不受影响。

### 界面、交互与运行时

- 文档结构：新增根目录 `CHANGELOG.md`，版本功能、兼容性变化、修复与迁移说明不再累积在 README；该文件同时纳入 Windows 整包更新事务。
- Windows Live WebUI 建立统一语义视觉系统，保留浅粉品牌色，同时收敛画布、表面、边框、文字、状态色、间距、圆角、阴影、焦点与 `120ms / 180ms / 220ms` 动效 token；玻璃模糊仅保留在应用外壳和 Dialog。
- 八个核心页面继续独立保留，侧栏改为“连接与状态 / 管理工具 / 系统”分组，并在底部显示版本、运行状态和退出登录入口；所有页面新增 hash 路由与浏览器前进 / 后退恢复。
- `1120px` 以下改为粘性顶栏与默认关闭的侧滑抽屉，`720px` 以下采用单列布局、移动 Dialog 以及文件列表 / User 映射卡片行；桌面侧栏折叠偏好与移动抽屉状态相互独立。
- 所有浮层改为原生 `<dialog>`，补齐初始焦点、触发器焦点恢复、顶层 `Escape`、安全 backdrop 关闭和未保存内容确认；清空日志、删除 Bot、删除映射和整理消息窗口统一使用可复用确认 Dialog。
- 新增最多三条的可访问通知队列、表单内联错误与统一 busy 状态；错误通知延长停留并可手动关闭，hover / focus 时暂停计时，异步保存、刷新、切换、上传、下载和删除会阻止重复提交。
- 网络配置移除无实际行为的伪标签；运行诊断只在首次加载绘制 `180ms` 仪表过渡；猫猫日志改为增量追加、未读计数和“回到底部”，不再把整个日志流持续广播给屏幕阅读器。
- 插件管理改为自适应网格并移除开关绝对定位；Dashboard host 固定声明实际浅色主题，只在 Bridge `ready` 后结束加载，20 秒未握手时提供可聚焦的重试错误。
- 文件管理工具条支持换行，移动端不再依赖横向表格；文件移动树补齐 `treeitem / group`、roving tabindex 和完整方向键操作。系统终端拆分标签与关闭按钮，支持 roving tabindex、键盘重排和 Pointer Events 触控拖放，指针排序使用 `180ms` FLIP 过渡。
- 登录页、Bot 表单、动态开关、日志筛选器和更新重启遮罩补齐原生提交、可访问名称、`aria-current / aria-pressed / aria-busy` 与专用 live region；更新失败后会把焦点移动到可执行操作。
- 动效限制为 transform / opacity 和诊断环进度，只在精细指针设备启用 hover；移除 `transition: all`、普通交互长动效和无用途循环，并完整支持 `prefers-reduced-motion`。
- 新增 `pointer / keyboard / programmatic` 输入方式识别；键盘触发的按压、Dialog、移动导航、诊断首帧和终端操作保持即时，指针与低频程序化状态使用克制的连续反馈。
- 增加无依赖、可取消并继承当前速度的弹簧运行时。移动抽屉支持左缘开合与右缘拖动，安全移动 Sheet 支持下滑关闭，Toast 支持双向横滑、顺序退场、FLIP 重排和页面隐藏暂停计时。
- 系统终端改用独立拖拽柄、实时占位排序、边缘自动滚动和松手归位；`pointercancel / lostpointercapture` 会恢复原顺序且不写后端，顺序保存失败也会回退到拖动前状态。
- Dialog 栈增加连续深度、单层遮罩与焦点安全恢复；Dashboard Bridge 使用 `loading / ready / error` 连续状态，更新事务只在阶段实际改变时反馈，并以勾/叉图形明确完成或失败。
- 减弱动效不再清空所有反馈，而是移除位移和弹簧并保留最长 `120ms` 的透明度/颜色变化；同时新增降低透明度和增强对比度媒体偏好支持。
- I Am Thinking 适配器的四组上游表情 ID 不再要求手写 JSON 数组；设置页改为逐项添加 / 删除的数字 ID 列表，并把思考、工具调用、超时 / 失败、完成各自的上游 ID 与 Rocket.Chat shortcode 组合在同一状态模块中。
- 猫猫日志改为无行分隔线的紧凑连续日志流，缩短单行留白与行高以提升同屏信息密度；性能监控 `Perf` 分类改为默认关闭，仍可按需手动开启。
- 插件卡片开关改用与 `58px` 品牌头像等高的对齐盒，滑块中心轴与头像中心轴保持一致，同时扩大并稳定开关点击区域。
- 插件卡片的“设置 / 重载 / 卸载”操作文字由 `13px` 调整为 `14px / 20px` 固定行高，继续使用系统原生 Microsoft YaHei UI Bold，改善 Windows 低 DPI 下小字号粗体的锯齿感和基线稳定性。
- 基础设置的“消息映射窗口”不再复用父级高级设置的同款边框与阴影，改为带轻微内缩、柔和实色底面和强调轨的嵌入分组，让父子配置层级可以一眼区分。
- 新增 RocketCatShell 品牌头像，并以圆角方框统一用于桌面侧栏、移动顶栏、登录页和浏览器标签；运行时静态副本纳入 Windows 整包更新，源码原图保留在 `assets/logo.png`。
- 网络配置、基础信息与运行诊断新增共享 Bot 卡片顺序，插件管理使用独立顺序；四类卡片均支持从非控件、非文字的卡片表面直接拖动、二维实时占位、边缘自动滚动、Pointer Cancel 恢复和完整键盘排序，文字选择与卡片内控件不会误触拖动，顺序只影响 WebUI 并随配置导入导出。
- 配置导入兼容缺少顺序字段的 v0.2.1 文件；同时把 I Am Thinking 四组整数 ID 的默认值补齐、单组去重和跨态冲突校验前移到导入事务预检，避免无效插件配置产生部分提交。
- 运行时改用后台 Rocket.Chat 连接监督器，Shell 与健康接口不再等待首次登录；入站 Worker 跨重连常驻，控制帧不受消息队列背压，满载时按房间分片队列精确统计并限频告警。
- OneBot action 改为 `8` 个固定 Worker、`256` 条有界队列、目标级串行锁与 `60` 秒执行上限；队列满载时立即返回 `1503`，停止时统一限时排空和取消所属任务。
- Journal 改为 `1024` 个 mutation batch 的异步有界提交，积压反压到入站链路；快照按 `2048` 条记录和低积压时机延迟生成，Journal 达到 `64 MiB` 时强制整理，并补齐写入线程健康和失败唤醒。
- 日志文件、控制台和 WebUI 缓冲统一移到专用监听线程，普通队列为 `4096` 条并为 WARN / ERROR 预留 `256` 条；过载优先舍弃 DEBUG、再舍弃 INFO，并暴露精确降级统计。
- 相同 Rocket.Chat 服务器的 Bot 共享身份 Registry 连接、单线程执行器、缓存和 single-flight；媒体缓存改为 Shell 级唯一清理与内容寻址协调器，重复下载 / 解密合并，插件目录扫描增加 `2` 秒短缓存和显式失效。
- PBKDF2、RSA 和大文件 E2EE 加解密统一移入最多 `2` 个 Worker 的专用线程池；房间、用户、引用与媒体查询增加 single-flight，引用失败只保留 `2` 秒负缓存。
- 入站翻译把媒体解析与房间、身份预取并行推进，避免混合消息按步骤串行等待；五轮基准工具改用唯一消息 / 引用 / 时间戳并输出 p50 / p95 / p99、吞吐、CPU、RSS、线程和句柄 JSON。
- `/api/status?compact=true` 为 WebUI 提供最小运行摘要，`/api/diagnostics` 增加事件循环、日志、入站、OneBot action、持久化和缓存性能指标；页面隐藏时暂停网络、诊断和日志轮询，静态资源按版本标记启用长期缓存。
- 新增隔离全链路压力工具与只读真实链路冒烟工具，覆盖 3 Bot / 12 房间 / 200 用户、过载恢复、Rocket.Chat / OneBot 断线、REST 429 / 500、慢持久化和插件超时，报告写入忽略的 `data/perf/`。

### 修复

- 修复首次启动器在系统默认 Python 为 3.13 / 3.14 时安装 `pywinpty 2.x` 没有可用 wheel、转而要求本机 Rust 与 Visual Studio C++ 工具链并最终失败的问题；Windows PTY 依赖升级为带 CPython 3.10–3.14 预编译 wheel 的 `pywinpty 3.0.5+`，自动安装同时禁止该依赖静默回退到源码构建。
- 修复 OneBot 上游未连接也会消耗 Rocket.Chat 重连次数、连续失败后错误停用整个 Bot 的问题；两侧重连策略现已完全拆分，OneBot 以独立 5 秒间隔持续后台等待，重复失败仅写入调试日志，离线事件不积压或恢复后补发。
- 修复启用页面 hash 路由后，系统终端 WebSocket 地址误携带 URL fragment、导致终端无法连接的问题。
- 修复手机端日志自动滚动开关被旧定位规则裁出面板，以及 User 映射长 Bot 名和整块冲突色造成的可读性问题。
- 修复桌面端 User 映射弹窗宽度不足、必须滚动到底部再横向拖动才能操作右侧按钮的问题；弹窗现在按视口展开，七列会在可用宽度内完整排布且不再显示横向滚动条。
- 修复基础设置高级区域中“消息映射窗口”卡片与上方性能字段边框紧贴的问题，为两个配置层级恢复统一的垂直间距。

### 版本管理与 I Am Thinking 四态适配

- `基础设置` 新增版本管理面板，可从官方 GitHub Releases 检查并选择升级、回滚或同版本重装；稳定版与预发布都会显示并明确标记，任何安装动作都必须由管理员确认。
- 更新包必须携带 RocketCatShell `update-manifest.json`，并通过产品、版本、Windows 平台、受管路径、文件大小和 SHA-256 校验；配置、日志、数据库、运行数据、用户插件和 `.venv` 不属于代码替换范围。
- Windows 更新助手会在独立进程中等待 Shell 优雅退出、备份精确受管路径、启动目标版本并检查健康状态；目标版本启动失败时自动恢复原版本，进程中断时由 `launcher.bat` 在下次启动前恢复事务。
- 更新系统从 `v0.2.2` 才开始提供。`v0.2.1` 及更早版本没有发布清单，必须手动升级到 `v0.2.2`，也不会出现在更新或回滚列表中。
- 两个内置插件继续随 RocketCatShell 整包更新；本版不提供独立插件更新器，也不改变用户自行安装的 `data/plugins/` 扩展。
- `rocketcat_plugin_adapt_iamthinking` 升级为 `v0.2.0`，对齐上游超时/失败、工具调用和超时后恢复行为。上游 QQ 数字表情 ID 仅用于识别四态，Rocket.Chat 侧仍使用独立可配置的 reaction shortcode。
- 思考态默认映射 `[66] -> :heart:`，工具态 `[270] -> :tools:`，错误态 `[264] -> :octagonal_sign:`，完成态 `[74] -> :sunny:`。思考与工具态保持 typing；错误与完成态立即结束 typing。
- 如果在 AstrBot 的 I Am Thinking 插件中修改四组数字表情 ID，需要在 RocketCatShell 适配器设置中同步对应数组；AstrBot 本体和上游插件无需修改。

### 更新事务修复

- 修复内置插件更新后目录时间戳与 Python 缓存可能沿用旧加载结果、导致健康检查误判的问题。
- 修复 `launcher.bat` 向独立更新助手传递安装根目录时，尾部反斜杠可能破坏参数解析的问题。
- 将已安装的 `update-manifest.json` 纳入备份、替换和恢复事务，确保升级、同版本重装与回滚后清单和实际代码一致。

## v0.2.1（全局插件实例与内置 Dashboard）

- 每个启用插件现在只创建一个全局实例；多个 Bot 仅保留轻量 runtime binding，降低多 Bot 部署中的插件内存、后台任务与初始化开销。
- 插件可在 `pages/<page_name>/index.html` 提供管理页面。WebUI 仅为拥有页面的插件显示 Dashboard 图标，并在 RocketCatShell 内部全屏打开。
- Dashboard 页面运行在无 `allow-same-origin` 的受限 iframe 中，通过 `window.RocketCatPluginDashboard` Bridge 使用父 WebUI 的认证链路访问插件 API、上传下载与 SSE。
- 插件可在全局 `on_initialize()` 中通过 `PluginContext.register_dashboard_api()` 和 `register_dashboard_sse()` 注册控制面接口；关闭、禁用、卸载或重载时会撤销页面令牌并终止 SSE。
- `on_load(runtime)` 与 `on_unload(runtime)` 继续表示单个 Bot runtime 的绑定和解绑；新增 `on_terminate()` 负责全局插件最终清理。
- 内置指令与 I Am Thinking 适配器的可变状态均按 runtime 隔离，避免不同 Bot 使用相同房间或消息 ID 时互相影响。
- 纯 Dashboard 插件只需提供 `pages/<page_name>/index.html` 等静态页面即可，不必实现 `main.py` 或处理任何 Bot 消息。

## v0.2.0（性能、效率与资源治理）

- 消息窗口分配不再在每条消息上复制完整映射；只有窗口变化、手动整理或 checkpoint 时才生成完整快照。
- 用户身份缓存命中直接在事件循环内完成；同一服务器的多个 Bot 共享 SQLite 连接、LRU 缓存与批量 `ensure_mappings`。
- E2EE 媒体采用流式下载、AES-CTR 解密、SHA-256 计算和原子落盘；`data/temp` 内已缓存文件直接发布 URL。
- OneBot 出站队列、用户/身份/媒体缓存、文件日志、WebUI 日志和终端会话均具有明确上限。
- Bot 配置采用增量 reconciliation；修改一个 Bot 不再重启所有 Bot，插件配置更新只重建插件 binding。
- WebUI 新增“性能与资源”高级设置与队列、缓存、媒体和重载诊断数据。
- 默认策略为 `balanced`，普通用户无需调参；旧配置缺少新增字段时自动采用安全默认值。

## v0.1.9（多引用与 Rocket.Chat 8.x 兼容更新）

`v0.1.9` 将 Rocket.Chat 多引用消息对齐为 AstrBot 原生可识别的多个顶层 `Reply` 组件，不再把多引用伪装成 OneBot 合并转发容器。该结构已经通过 RocketCatShell、AstrBot aiocqhttp 适配器以及两个本地生图插件的联合验证。

- 非加密频道仅当 Rocket.Chat 当前消息包含两个及以上顶层 `attachments[*].message_link` 时进入多引用模式；正文中普通粘贴的多个消息链接不会被误判为多引用。
- E2EE 加密频道会额外检查解密后正文开头连续的空标签 Markdown 消息链接前缀。Rocket.Chat 8.5 在加密房间里会把多引用编码到这段前缀中，而不是放在普通房间使用的顶层 `attachments[*].message_link`；RocketCatShell 仅在 `e2e=done` 且此前缀连续命中两条及以上引用时才将其归一化为并列 Reply，并把这段前缀从当前正文中剥离，避免把引用链接正文误上报给 OneBot 上游。
- 每条顶层引用按 Rocket.Chat 附件数组顺序转换为一个普通 OneBot `reply` 段。引用顺序和重复引用均会保留，当前消息正文及自身媒体仍留在当前消息链中。
- AstrBot aiocqhttp 会逐个调用 `get_msg` 解析这些 `reply` 段，最终得到多个带独立 `chain` 的顶层 `Reply` 组件。被引用消息的文本、图片及其他受支持媒体因此可以按引用分别进入上游。
- 已验证 `astrbot_plugin_image_generation` 的 `/生图` 与 `astrbot_plugin_grok_suite` 的 `/grok生图` 均可从多个 `Reply.chain` 中原生提取全部参考图，不再依赖 LLM 对图片内容的纯文本转述。
- 引用消息优先通过 Rocket.Chat 消息接口获取完整内容；消息已删除、不可访问或获取失败时，使用当前引用附件快照回退。
- 消息窗口重建、运行状态重启恢复和引用 ID 淘汰均支持多个并列 Reply；多引用日志使用独立的“并列原生 Reply”格式，不再复用单链引用日志。
- 已移除实验阶段加入的 6xxx forward ID 命名空间、合并转发容器缓存、`get_forward_msg` 实现及关联热存储清理逻辑。旧 snapshot / journal 中的实验性 forward 命名空间会在加载后被忽略，并在后续快照中自然消失。
- RocketCatShell 仍未定义真正的 Rocket.Chat 合并转发语义；`get_forward_msg`、`send_group_forward_msg` 和 `send_private_forward_msg` 当前统一返回不支持。

用户身份映射同步改为安全的确定性 11 位 ID：

- Rocket.Chat 不可变 `userId` 使用 `sha256-linear-v1` 映射到 `10000000000–99999999999`。正常用户即使清空单个 bot 的 runtime 数据、改变首次发言顺序，也会得到相同 OneBot ID。
- 用户只在 bot 登录或首次出现在消息、成员、mention 等链路时建立映射；启动过程不会调用 `users.list` 或枚举服务器全部账号。
- 主槽冲突使用线性开放地址法向后探测，并在服务器级 SQLite 注册表中持久化实际映射。同一 Rocket.Chat 服务器的多个 RocketCat bot 共用映射与人工 override。
- bot 自身的 OneBot `self_id` 同样由登录账号的 Rocket.Chat `userId` 生成；Bot 设置不再提供可独立配置的 `onebot_self_id`。
- WebUI 的 Bot 编辑页提供“审查 User 映射列表”，可分页搜索 userId、用户名、昵称和 OneBot ID。只有 OneBot ID 可编辑；已被实际占用的 ID 会拒绝保存。
- 发生哈希冲突时，“猫猫日志”会记录先入用户、后入用户、主槽、最终 ID 和偏移量，并写入每个相关 bot 的 `re_waring.json`。未解决提醒会在该 bot 每次启动时重复输出。
- 冲突列表中先入槽位者使用蓝底白字，后入偏移者使用红底白字。人工修改后通过正向哈希和唯一索引重新验证；SHA-256 不存在也不伪造所谓“反向哈希”。

Rocket.Chat 兼容层同步完成以下调整：

- 支持范围明确为 Rocket.Chat `7.10.x–8.5.x`。启动时通过公开的 `/api/info` 读取版本；低于 `7.10.0` 会明确拒绝启动，无法读取版本时使用能力探测模式。
- DDP/WebSocket 继续负责 resume 登录和消息订阅；typing 与 `e2e.requestSubscriptionKeys` 优先通过 `/api/v1/method.call/:method` 调用。只有 7.10 或未知版本在该端点明确不存在时才回退原始 DDP method。
- Rocket.Chat 8.x 上传固定使用 `rooms.media/:rid` + `rooms.mediaConfirm/:rid/:fileId`，不会再尝试 8.0 已移除的 `rooms.upload/:rid`；7.10 仅在新版端点返回 404、405、410 或 501 时回退旧端点。
- 普通上传确认会发送 `msg`、`description`、`fileName` 和可选 `tmid`。上传前会清理危险文件名，并优先依据文件签名判断 MIME；扩展名与实际内容冲突时使用匹配内容的安全文件名。
- E2EE REST 请求按 Rocket.Chat 8.3+ 的严格 schema 构造，不发送未知字段；E2EE 媒体确认对齐 8.5 的 `content` + `fileContent` 两层密文结构，不在确认请求中暴露明文文件名和描述。
- 对齐 Rocket.Chat 8.2+ 的加密消息合并行为：服务端已经标记为 `removed-file` 的附件不会被解密出来的旧附件内容重新复活。
- Rocket.Chat 8.5 在 E2EE 房间会把多引用编码为解密正文开头连续的空标签 Markdown 消息链接，而不是普通房间的 `attachments[*].message_link`。RocketCatShell 会仅对 `e2e=done` 的这种系统前缀格式做等价归一化，并按原顺序生成多个顶层 OneBot `reply`；普通粘贴链接、正文中间链接和单引用不会被误判为多引用。
- E2EE 图片解密后会进入所有 Bot 共用的 `data/temp` 受控媒体缓存，并通过仅监听 `127.0.0.1`、使用随机能力令牌的 WebUI HTTP 地址交给 OneBot 上游；旧消息快照里的系统临时路径或 `base64://` 引用也会在 `get_msg` 时重新发布。这样 `/生图` 与 `/grok生图` 接收到的参考图来源和非加密频道一致，不再依赖 AstrBot 接受 RocketCatShell 的本地 `%TEMP%` 路径。
- v0.1.9 热修复把上述媒体代理统一并入 WebUI 端口，固定使用 `/_rocketcat/media/{bot_id}/{token}/{filename}` 路由，不再为每个 Bot 创建随机监听端口。图片、音频和视频都以令牌 HTTP URL 上报，AstrBot 会通过标准下载链路自动缓存到自身 `data/temp`。
- RocketCatShell 自身的解密媒体与下载临时文件统一保存到项目级 `data/temp`，不再为每个 Bot 创建 `media_cache`；旧的 `data/bots/_shared_media` 也已退役。
- “启用 Base64 传输媒体”设置已退役；旧 `shell.json` 或导入配置中的字段会被兼容忽略并在重新保存后清理。AstrBot → RocketChat 的协议级 `base64://` 上传以及历史 Base64 缓存转 HTTP URL 的能力仍然保留。
- 运行诊断与 `#system` 输出增加 Rocket.Chat 服务端版本、兼容状态、当前上传端点和 method 传输方式。

升级到 `v0.1.9` 不需要迁移 Bot 配置。旧版实验性合并转发缓存不会继续使用；普通 message、user、room、thread 和 context 映射会照常恢复。

## v0.1.8（系统终端更新）

`v0.1.8` 新增 `系统终端` WebUI 页面，第一阶段对齐 NapCat 的终端管理体验，让管理员可以直接在 WebUI 内创建和切换多个本地终端会话。

- 左侧导航新增 `系统终端`，图标与页面结构对齐 NapCat 的终端入口。
- 页面右上角新增终端创建按钮；没有终端时显示空状态提示，点击按钮即可创建终端。
- 每个终端使用独立 UUID 作为默认标签名，上方 tab 条可点击切换当前终端，也可关闭指定终端。
- 终端 tab 支持横向拖拽排序，排序结果会同步给后端会话列表。
- 后端新增 `/api/terminal/list`、`/api/terminal/create`、`/api/terminal/{id}/close`、`/api/terminal/order` 与 `/api/ws/terminal/{id}`，终端 WebSocket 复用当前 WebUI 登录 cookie 鉴权。

`v0.1.8` 首版终端提供轻量子进程后端；当前 Windows 运行依赖同时声明 `pywinpty`，可用时使用 WinPTY 交互后端，不可用时仍会回退子进程实现。

## v0.1.7（内置文件管理更新）

`v0.1.7` 的首要目标是为 RocketCatShell 增加类似 NapCat 的内置文件管理入口，并为后续 Docker 版迁移做准备。用户可以在 WebUI 内浏览 RocketCatShell 项目根目录、进入子目录、返回上级目录、刷新文件列表、打开 UTF-8 文本文件进行查看或编辑、预览图片文件，并在根目录边界内新建文件、创建目录、上传文件、重命名、批量删除、移动和下载文件。

- 新增 `文件管理` WebUI 页面。文件管理边界固定为 RocketCatShell 项目根目录，前后端 API 都只接受根目录内的相对路径，拒绝访问 `..`、系统绝对路径、盘符路径或符号链接越界目标。
- 新增文件 API：`GET /api/files` 用于列目录，`POST /api/files/read` 用于文本预览，`POST /api/files/write` 用于保存允许编辑的文本文件，`POST /api/files/create` 用于新建文件或目录，`POST /api/files/upload` 用于上传文件，`POST /api/files/rename` 用于重命名单个项目，`POST /api/files/delete` 用于删除选中项目，`POST /api/files/move` 用于移动选中项目，`GET /api/files/download` 用于单项下载，`POST /api/files/download` 用于将选中项目打包为 `files.zip` 下载。
- 文件表格新增左侧复选框，多选后才会展开批量删除、移动和下载按钮；每一行也新增 `操作` 列，可单独重命名、移动、复制相对路径、下载或删除该项。删除前会弹出二次确认，移动会弹出目标目录树，只允许选择 RocketCatShell 根目录边界内的目录，批量下载会把选中的文件和目录一起封装为 `files.zip`。
- 新建功能支持在 WebUI 弹窗中选择 `文件` 或 `目录`；同名目标会被拒绝，避免覆盖现有项目。
- 上传功能支持拖拽文件到上传框，也支持点击按钮打开系统文件选择器。单次最多上传 20 个文件，单文件上限 100 MiB；上传文件夹结构时会在项目根目录边界内自动创建所需父目录，同名上传文件会自动追加随机后缀，避免覆盖。
- 文本预览最多读取前 `1 MiB` 内容，超出时会在页面提示内容已截断；允许编辑的普通文本文件可在 WebUI 内修改并保存，保存前会弹出二次确认；二进制文件和非 UTF-8 文本不会返回文件内容。
- RocketCatShell 核心源码、WebUI 静态资源、工具脚本和两个内置插件源码只能查看，不能通过文件管理修改、移动或删除；用户在 `data/resource` 等非保护目录中创建的 `.py` 或其他文本文件仍可自由编辑。
- 图片文件会在文件列表中显示缩略图，点击后可在 WebUI 内放大预览；图片预览同样只允许读取 RocketCatShell 根目录边界内的文件。
- 对明确的敏感持久化数据文件增加二次鉴权，包括 `config/shell.json`、`config/bots.json`、`config/plugins_config/*.json` 和 `data/bots/**/runtime_state.json`。鉴权密码复用 WebUI 登录认证 / 文件管理鉴权密码，密码只通过请求体提交，不放入 URL；鉴权文件保存前会再次提示修改风险。
- `基础设置` 中的 WebUI 密码文案已更新为 `WebUI 登录认证 / 文件管理鉴权密码`，强调同一个密码同时用于登录 WebUI 和打开敏感持久化数据文件。

升级到 `v0.1.7` 不需要迁移现有配置或运行态数据。如果浏览器已经打开旧版 WebUI，刷新页面以获取最新静态资源即可。

## v0.1.6（诊断可观测性与性能收口更新）

`v0.1.6` 建立在 `v0.1.5` 的内置指令、Shell 插件系统和独立 WebUI 管理面之上。这一版不再继续增加新的大块宿主能力，而是把这套独立 Shell 更像“可发布软件”地收口：一方面补上更直观、更低延迟的运行诊断与状态可观测性，另一方面把入站翻译热路径和 benchmark 工具一起推进到更贴近真实负载的状态，方便后续继续做针对性优化。

- `/api/diagnostics` 的主机快照采集现在改为短 TTL 缓存，避免每次请求都重新执行一次固定 CPU 采样等待；内置 `#system` 指令也复用同一套缓存逻辑，页面刷新、轮询和房间内诊断命令不再重复触发整段采样开销。
- 运行诊断已从“网络配置”页拆分为独立导航页，并新增主机快照缓存状态、快照年龄、TTL 等元数据展示。WebUI 现在可以直接区分当前是缓存命中、实时采样还是采样失败，而不是只看到一份静态诊断结果。
- 运行诊断页的主机 CPU / 内存摘要已升级为更直观的环形指示器视图，并补充系统总占用与 Shell 进程占用的双层视觉表达，配合在线 Bot / Snapshot / Journal 汇总，更适合长时间运行时做快速巡检。
- OneBot reverse WebSocket 客户端现在会显式放宽 `aiohttp` 的入站消息大小上限，并在断链日志里补充 `close_code`。这修复了 AstrBot 侧较大的 `base64://` 图片动作在进入 RocketCatShell 前就触发 reverse WS 断开的问题，像 `astrbot_plugin_grok_suite` 这类 2K 大图不再因为默认 `4 MiB` 帧限制在上传到 Rocket.Chat 之前就被截断。
- 入站翻译热路径继续做了针对性优化：引用上下文任务只在确实可能存在引用时才构建；回复来源解析结果复用，避免重复扫描正文；纯文本引用不再误走 quoted media 提取；消息注册表 entry 复制路径改为面向 JSON-like 结构的轻量 clone；媒体描述提取改为单次遍历并为扁平 attachment 场景增加快速路径，继续压低高频图片 / 引用 / 混合消息场景下的固定开销。
- 开发者源码工具 [tools/benchmark_inbound_translate.py](https://github.com/Creeper3222/RocketCat/blob/main/tools/benchmark_inbound_translate.py) 现在支持 `--profile realistic`，可直接带入更接近真实环境的 room info / quote fetch / media delay，并新增 `quote_image`、`media_mix` 场景，避免只靠零延迟微基准得到过于理想化的结论。该工具不包含在最小运行 ZIP 中。

升级到 `v0.1.6` 不需要迁移 `v0.1.5` 的配置目录、热存储 snapshot / journal 或本地插件数据；如果浏览器已经打开旧版 WebUI，刷新页面以获取最新静态资源即可。

## v0.1.5（内置指令与运维增强更新）

`v0.1.5` 建立在 `v0.1.4` 的独立 Shell、热存储 runtime 和本地插件系统之上，重点不再是继续压热路径性能，而是补齐一层更适合日常使用与正式发布的本地控制能力，并补上 Rocket.Chat 新旧上传接口并存时期的兼容缺口。

- 新增本地内置指令插件 `rocketcat_plugin_built_in_command`。它通过 Shell 插件系统直接拦截 Rocket.Chat 入站精确纯文本指令，目前实现 `#rocketcat` 与 `#system` 两条命令，不再要求上游 AstrBot 侧参与处理。
- `#rocketcat` 用于返回当前桥接 Bot 的基础信息：包括客户端显示名、登录账号、显示昵称、OneBot self_id、连接状态和 Rocket.Chat 服务器地址，并追加发送 bot 头像与服务器 branding 头像，方便在房间内快速确认“当前是谁、连的是哪台、状态是否正常”。
- 媒体上传链路优化 plain upload 端点自适应：默认优先使用 Rocket.Chat 8.0.0+ 的 `rooms.media/:rid` + `rooms.mediaConfirm/:rid/:fileId`，失败时回退旧版 `rooms.upload/:rid`；每个 Bot 独立记忆可用端点，后续上传直接走自身最适链路。
- Rocket.Chat 8.x 的 `rooms.media/:rid` 不再像旧 `rooms.upload/:rid` 那样直接完成发图消息创建，因此 plain 媒体上传会在上传成功后继续调用 `rooms.mediaConfirm/:rid/:fileId`，真正把图片或文件发进房间；如果缓存的旧版端点在服务器升级后失效，也会反向回退到新版链路。同时对同服远端媒体（尤其是 bot 自己的 `/avatar/{username}`）会自动补 `rc_uid/rc_token` 登录态并按响应 `Content-Type` 选择正确后缀，修复本地新服里 `#rocketcat` 与 WebUI 基础信息页拿不到 bot 头像、或把默认 SVG 头像误当作 PNG 上传后显示损坏的问题。
- `#rocketcat` 的插件直发媒体链路现在会在不需要 OneBot 映射时跳过 5 秒自回显等待，并把内置指令自回显抑制从一次性 `source_id` 扩展为短 TTL 的 `source_id + 房间/正文签名` 匹配，修复新旧服混跑时加密房间第二段回复超时和旧服非加密房间重复回显的问题。
- `#system` 用于返回当前 RocketCatShell 进程所在主机的系统快照：包括版本号、Python 版本、主机名、系统信息、CPU 商品名 / 核心数 / 主频 / 系统占用 / Shell 进程占用，以及内存总量 / 已用 / 可用 / 当前进程占用。该命令依赖新增运行依赖 `psutil`。
- `rocketcat_plugin_adapt_iamthinking` 不再只做 reaction 映射。现在它可以在继续兼容 `set_msg_emoji_like` 的同时，把“思考中 / 已完成”阶段独立映射为 Rocket.Chat typing 指示器；reaction 与 typing 在插件设置页可分别开关，长时间思考还会自动续期 typing 心跳。
- Shell 启动层新增单实例锁：同一项目目录下的第二个 RocketCatShell 会在 runtime 初始化之前直接退出，不再像旧行为那样因为 WebUI 端口回退而悄悄拉起第二份 runtime，从根源上避免重复订阅和重复上报。

升级到 `v0.1.5` 不需要迁移 `v0.1.4` 的配置目录或 runtime 数据。如果你通过 `launcher.bat` 启动，启动器会按 `requirements.txt` 自动检查并补装包括 `psutil` 在内的新增依赖；只有你手动直接运行 `python -m rocketcat_shell` 时，才需要先自行执行 `pip install -r requirements.txt`。

## v0.1.4（性能优化更新）

`v0.1.4` 建立在 `v0.1.3` 的 memory-authoritative runtime 之上，目标不是改变桥接语义或目录布局，而是继续压低热路径延迟、内存峰值和 WebUI 空闲开销。

- P0 热路径优化：热存储减少重复深拷贝，source / surrogate message 索引共享同一 entry；入站消息注册表改为紧凑字段存储，需要 hydrate 时再重建 OneBot 事件；Rocket.Chat 入站 DDP 消息改为按房间分片队列处理，同房间保持 FIFO，不同房间可并行。
- P0 去重优化：入站重复消息签名改为轻量字段签名，并对附件、文件、URL、mentions 等大结构使用稳定哈希，降低重复 update 判断成本。
- P1 JSON / 连接优化：新增统一 JSON codec，优先使用 `orjson`；HTTP session 使用连接池、DNS TTL 和 keepalive；WebSocket 发送统一走预序列化字符串，减少 aiohttp 默认 JSON 路径开销。
- P1 媒体优化：普通远端媒体下载改为边下载边写临时文件；E2EE 媒体上传改为原文件分块读取、CTR 分块加密到临时密文文件，再以文件流上传；Base64 媒体增加大小预判和严格解码；Bot 的远端媒体大小上限同时约束媒体下载与上传。
- P1 插件 action 优化：插件可声明 `handled_actions`，运行时按 action 精确分发，未声明的旧插件继续作为 fallback，减少 OneBot action 广播式试探。
- P2 WebUI / 插件控制面优化：插件列表和详情增加目录签名缓存，未变化时不再反复扫目录和解析配置；基础信息页 Rocket.Chat server branding 增加 TTL 缓存；猫猫日志从 1 秒短轮询改为长轮询，空闲时显著减少 WebUI 请求和 JSON 响应。

升级到 `v0.1.4` 不需要迁移 `v0.1.3` 的 runtime 数据；需要重新安装依赖以获得 `orjson` 快路径。

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
- 新增开发者源码工具 [tools/benchmark_inbound_translate.py](https://github.com/Creeper3222/RocketCat/blob/main/tools/benchmark_inbound_translate.py)，可在本地对比 control / rebuild 两条入站翻译路径的延迟差异；该工具不包含在最小运行 ZIP 中。
- message 索引策略改为固定窗口：只保留最近 N 条 message 映射，超出窗口时裁剪最旧映射，WebUI 的“重建索引”只做窗口整理与关联消息缓存重建，不再保留旧版 reset / compact 语义。
