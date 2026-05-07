# RocketCatShell

[![Platform](https://img.shields.io/badge/Platform-OneBot%20v11%20Reverse%20WS-pink)](#)
[![Runtime](https://img.shields.io/badge/Python-%3E%3D3.11-blue)](#环境要求)

将 [Rocket.Chat](https://rocket.chat) 通过桥接方式接入 OneBot v11 生态的独立客户端。它继承了插件版 [RocketCat](https://github.com/Creeper3222/astrbot_plugin_rocketchat_onebot_bridge) 已经验证过的桥接核心、独立 WebUI 和管理能力，但已经不再依附于 AstrBot 插件宿主，而是作为一个可以独立运行、独立配置、独立扩展的本地控制台存在。

本项目的目标不是继续做一个“宿主里的桥接插件”，而是把 RocketCat 发展成一套真正独立的 `Rocket.Chat <-> OneBot v11` 桥接软件。

这意味着：

- RocketCatShell 自己拥有 `config/`、`data/`、`logs/` 目录边界。
- RocketCatShell 自己提供本地 WebUI、登录认证、Bot 管理和插件管理。
- RocketCatShell 仍然可以作为 OneBot reverse WebSocket 客户端与 AstrBot 协同，但不再依赖 AstrBot 插件宿主才能运行。

---

## Linux / Docker 版说明

这个仓库是 RocketCatShell 的 Linux 打包版，目标是直接用于 Linux 主机和 Docker 部署。

- 新增 `launcher.sh`，用于 Linux 本机一键创建 `.venv`、补齐依赖并启动。
- 新增 `Dockerfile`、`docker-compose.yml` 和 `docker/entrypoint.sh`，用于构建镜像并持久化运行数据。
- 容器首次启动时，如果 `config/shell.json` 不存在，会自动写入一份适合容器环境的默认配置：`webui_host=0.0.0.0`、`auto_open_browser=false`。
- 容器首次启动时，如果挂载出来的 `data/plugins/` 还是空的，会自动补种内置插件 `rocketcat_plugin_adapt_iamthinking`，避免被 volume 覆盖后丢失。

---

## v0.1.2 更新

- Linux / Docker 版 bridge 已同步 RocketCatShell v0.1.2 的线程回复修复：`tmid` 会作为独立线程上下文持久化与传递，文本、图片、文件、语音、视频回复都会优先回到当前线程。
- 修复了线程语义与普通 `reply` 语义混淆，以及 Rocket.Chat 发送回包缺失 `tmid` 时的线程丢失问题；加密房间里的分段文本、图片、文件回复会优先通过自回显消息修正线程，必要时回填已知线程 ID。
- 在开启“子频道会话隔离”时，Docker 版也会按 room 维度维护线程上下文，并通过时间戳阻止旧消息重放覆盖较新的线程绑定。

---

## v0.1.1 更新

- 修复了 AstrBot 唤醒词 / 指令兼容问题：标准 OneBot `message` 和 `raw_message` 重新恢复为“纯当前用户正文”，不再混入房间名、发送者名和引用前缀。
- 补回了上游认知所需的 Rocket.Chat 元数据：发送者、提及、子频道 / 群上下文、引用链和回复摘要改为独立字段写入事件与消息注册表，不再污染标准文本。
- 新增可配置的 message 双向索引窗口：支持按上限自动清理最早映射、在达到重置阈值后自动重排 surrogate ID，并支持 WebUI 手动重建索引。
- `id_map.json` 与 `message_registry.json` 现在会同步整理：`by_source`、`by_surrogate`、缓存的 `message_id` / `reply id` 会跟随 active mappings 一起重建，`latest_by_context_sender` 会按保留消息重新计算。

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
- 支持配置导出 / 导入，统一打包 Bot 设置、WebUI 密码 / 端口、message 双向索引条数上限和本地插件主配置。
- 支持自动重连、最大连续重连次数限制、自动停用失败 Bot。
- 支持动态订阅新房间，机器人被拉入新房间后无需重启。
- 支持兼容 AstrBot 唤醒词 / 指令的入站消息格式，标准 `message` / `raw_message` 保持为纯当前用户正文。
- 支持 OneBot 风格的群聊、私聊、消息查询、群成员查询、登录信息查询。
- 支持文本、`at`、引用回复、图片、文件、语音、视频、Markdown 出站发送。
- 支持 Rocket.Chat 线程上下文感知出站：同一子频道内的线程文本、图片、文件、语音、视频回复会优先跟随当前线程，而不是错误串到最近一次活跃的其他线程。
- 支持引用链提取、回复来源识别、提及用户映射、群聊 / 私聊上下文映射，以及发送者 / 提及 / 回复 / 子频道等独立认知元数据。
- 支持可配置的 message 双向索引窗口、自动清理 / 重排和 WebUI 手动重建。
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
- Rocket.Chat 引用和消息链接会转换为 OneBot `reply` 语义，并补充引用上下文文本；线程消息本身不会再被误折叠成普通 `reply`，而是单独保留 `rocketchat_thread_source_id` / `thread_source_id` 供线程路由使用。
- 发送者、提及、引用链、回复摘要、房间名、房间 slug、上下文群 ID 等 Rocket.Chat 认知信息会以独立字段写入事件和消息注册表。
- 图片、普通文件、音频、视频附件会被识别并转换成对应的 OneBot 媒体段。
- 不支持直接桥接的媒体会降级为可读文本占位，避免整条消息消失。

### 出站能力

- OneBot `text` 直接发送为 Rocket.Chat 文本。
- OneBot `at` 会转换为 Rocket.Chat `@username` 或 `@all`。
- OneBot `reply` 会转换为 Rocket.Chat 消息链接引用格式。
- 当当前会话上下文处于 Rocket.Chat 线程内时，文本和媒体出站会自动带上对应 `tmid`；如果发送回包暂时缺失 `tmid`，桥接器会优先用自回显消息修正，必要时回填已知线程 ID。
- OneBot `image` 支持 HTTP(S) 链接、本地文件和 Base64 数据。
- OneBot `file`、`record`、`video` 支持本地文件；远端媒体会先尝试下载再上传。
- OneBot `markdown` 会按文本内容发往 Rocket.Chat。

### 上下文与映射

- Rocket.Chat 的房间 ID、用户 ID、消息 ID 会被桥接器映射为可持久化的 OneBot surrogate ID。
- `id_map.json` 负责保存 source ID 与 surrogate ID 的双向映射；其中 message 命名空间支持按窗口上限自动清理最早映射，并在达到重置阈值后自动重排 surrogate ID。
- `message_registry.json` 负责保存富消息缓存、`by_source` / `by_surrogate` 索引和 `latest_by_context_sender` 路由提示，并会在 message 索引整理时同步重建。
- 群聊上下文使用上下文房间注册表维持群上下文到真实房间的绑定关系。
- 在开启“子频道会话隔离”时，群聊上下文会按 room 维度保留线程路由信息，并通过时间戳拒绝旧消息重放覆盖较新的线程绑定。
- 私聊上下文使用私聊房间映射存储维护用户与私聊房间的绑定关系。
- 可选开启“子频道会话隔离”，把不同子房间拆成不同会话上下文。

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

RocketCatShell 启动后会在本地启动一个独立 WebUI，默认监听 `127.0.0.1`，默认端口 `5751`。

### 页面能力

- `网络配置`：查看 Bot 状态、创建 / 编辑 / 删除 Bot。
- `基础信息`：查看每个 Bot 的账号信息、OneBot self ID、Rocket.Chat 服务器品牌头像和服务器名称。
- `猫猫日志`：查看 RocketCatShell 运行日志，并支持清空日志。
- `基础设置`：管理 WebUI 登录密码、WebUI 端口、message 双向索引条数上限，以及配置导出 / 导入。
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
- 导出内容包含所有 Bot 设置、WebUI 登录密码、WebUI 端口、message 双向索引条数上限和本地插件主配置。
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

如果你的 Linux 主机已经有 Python 3，直接运行 `launcher.sh` 即可。启动器会自动创建本地 `.venv`，并在缺依赖时自动安装。

```bash
chmod +x launcher.sh
./launcher.sh
```

### 方式二：使用 Docker Compose

如果你更希望直接作为容器部署，在项目根目录执行：

```bash
cp .env.example .env
docker compose up -d --build
```

这个 linux 仓库已经按你当前这台机器的常见链路收敛了默认值：

- RocketCatShell 跑在 Docker 容器里。
- AstrBot 仍跑在宿主机上。
- 容器内默认 OneBot 上游地址是 `ws://host.docker.internal:6200/ws/`，用于连接宿主机 AstrBot 暴露的 OneBot v11 reverse WS。
- WebUI 默认只发布到宿主机回环地址 `127.0.0.1:5751`，避免直接暴露到公网。

默认会持久化以下目录：

- `./config`
- `./data/bots`
- `./data/plugins`
- `./data/plugin_data`
- `./logs`

如果你需要改宿主机端口、OneBot 上游地址或持久化路径，直接编辑项目根目录下的 `.env`。

### 方式三：使用本地虚拟环境

在项目根目录执行：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```



---

## 启动

### Linux 启动器

项目根目录已经提供：

```text
launcher.sh
```

它会优先使用本地 `.venv/bin/python`。

如果本地 `.venv` 不存在，启动器会自动尝试使用系统 `python3` 或 `python` 创建 `.venv`。

如果检测到 `aiohttp`、`cryptography`、`fastapi`、`uvicorn` 或 `PyYAML` 等依赖缺失，启动器还会自动执行：

```bash
pip install -r requirements.txt
```

然后再启动 RocketCatShell。

### Docker 启动

如果使用 Compose：

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f
```

推荐先检查 `.env` 里的这几个字段是否符合你的机器：

- `ROCKETCAT_WEBUI_BIND_IP=127.0.0.1`
- `ROCKETCAT_WEBUI_PORT=5751`
- `ROCKETCAT_DEFAULT_ONEBOT_WS_URL=ws://host.docker.internal:6200/ws/`
- `ROCKETCAT_CONFIG_DIR=./config`
- `ROCKETCAT_BOTS_DIR=./data/bots`
- `ROCKETCAT_PLUGINS_DIR=./data/plugins`
- `ROCKETCAT_PLUGIN_DATA_DIR=./data/plugin_data`
- `ROCKETCAT_LOGS_DIR=./logs`

如果你之前已经在宿主机本地跑过一次 RocketCatShell，项目根目录下可能已经存在 `config/shell.json`，并且其中的 `webui_host` 仍然是本地模式默认值 `127.0.0.1`。这时容器会直接复用这份配置，导致端口映射后仍无法从宿主机访问 WebUI。遇到这种情况，请把 `webui_host` 改成 `0.0.0.0`，或者删除这份 `config/shell.json` 让容器入口自动重建。

第一次通过 compose 启动时，如果 `config/shell.json` 还不存在，容器会直接读取 `.env` 中的 `ROCKETCAT_*` 变量生成这份默认配置，而不是再写入固定死的静态模板。

如果只想单独构建镜像并手动运行：

```bash
docker build -t rocketcatshell:linux .
docker run -d \
	--name rocketcatshell \
	-p 5751:5751 \
	-v $(pwd)/config:/app/config \
	-v $(pwd)/data/bots:/app/data/bots \
	-v $(pwd)/data/plugins:/app/data/plugins \
	-v $(pwd)/data/plugin_data:/app/data/plugin_data \
	-v $(pwd)/logs:/app/logs \
	rocketcatshell:linux
```

### Python 模块入口

也可以直接使用：

```bash
python3 -m rocketcat_shell
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

- WebUI 地址：`127.0.0.1:5751`
- WebUI 初始密码：`123456`
- 最大 message 双向索引储存条数：`1000`
- shell 默认 OneBot reverse WS 地址：`ws://127.0.0.1:6200/ws/`
- `next_onebot_self_id`：`910001`

也就是说，只要依赖安装正确，RocketCatShell 在空配置状态下可以自己创建必需目录和初始配置文件。

对于 Docker 版，`docker/entrypoint.sh` 会在 `config/shell.json` 缺失时先写入一份容器默认模板，因此首次容器启动的 WebUI 监听地址会变成 `0.0.0.0:5751`，同时关闭自动打开浏览器。

对当前这个仓库来说，模板默认值已经进一步收敛为：

- `webui_host=0.0.0.0`
- `webui_port=5751`
- `default_onebot_ws_url=ws://host.docker.internal:6200/ws/`
- `auto_open_browser=false`

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
ws://127.0.0.1:6200/ws/
```

如果 RocketCatShell 跑在 Docker 容器里，而 AstrBot 跑在宿主机上，那么在 RocketCatShell 里更应该填写：

```text
ws://host.docker.internal:6200/ws/
```

如果 AstrBot 也部署在 Docker 容器里，并且与 RocketCatShell 在同一个 Docker 网络中运行，则应该使用 AstrBot 容器服务名或别名，例如：

```text
ws://astrbot:6200/ws/
```

当前 compose 已经把这个地址作为默认新建 Bot 的 OneBot 上游地址写进首份 `config/shell.json`。

### 2. 启动 RocketCatShell

启动后打开：

```text
http://127.0.0.1:5751/
```

如果你运行在 Docker 容器里，仍然通过宿主机访问：

```text
http://127.0.0.1:5751/
```

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
| `webui_host` | WebUI 监听主机；本地运行默认 `127.0.0.1`，Docker 推荐设为 `0.0.0.0`。 |
| `webui_port` | WebUI 监听端口，默认 `5751`。 |
| `webui_access_password` | WebUI 登录密码，默认 `123456`。 |
| `message_index_max_entries` | 最大 message 双向索引储存条数，默认 `1000`；超出后会清理最早映射，并在达到重置阈值后自动重排当前窗口。 |
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
- `data/bots/<bot>/id_map.json` 保存 user / room / message / context 的双向 surrogate ID 映射。
- `data/bots/<bot>/message_registry.json` 保存富消息缓存、`by_source` / `by_surrogate` 索引和最近上下文发言者到房间的路由提示。
- `logs/` 保存 RocketCatShell 自己的运行日志。
- Docker Compose 默认只把 `config/`、`data/bots/`、`data/plugins/`、`data/plugin_data/`、`logs/` 作为宿主机持久化目录挂载出来，不会把代码目录本身映射回容器。
- 为了避免宿主机空目录把镜像里的内置插件盖掉，容器入口会自动把内置插件补种到 `data/plugins/`。

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
