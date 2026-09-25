# memo-role

> 本地离线多模型角色扮演机器人，全局统一记忆，支持自定义人设、NapCat 对接、网页对话，适配安卓与低配 PC。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 项目简介

memo-role 是一个完全本地、离线运行的多模型角色扮演（Roleplay）机器人。它把模型推理、角色人设、对话渠道和记忆存储解耦，让你可以在自己的设备上同时驱动多个本地模型与多个人设，并通过 QQ（NapCat）或网页与它们对话。

所有推理与数据都留在本机，不依赖任何云端 API。

## 核心特性

- **本地离线推理** — 基于 `llama.cpp` 等本地推理后端，断网可用，数据不出设备。
- **多模型并存** — 同时加载/切换多个本地模型，按人设或场景选择不同模型。
- **全局统一记忆** — 跨会话、跨渠道共享记忆，角色记得住长期设定与历史对话。
- **自定义人设** — 用一份人设配置定义角色性格、说话风格、背景故事与开场白。
- **NapCat 对接** — 接入 NapCat，把角色带到 QQ 聊天中。
- **网页对话** — 内置 Web 对话界面，浏览器直接开聊。
- **低配友好** — 面向安卓（Termux）与低配置 PC 优化，尽量在有限内存与算力下稳定运行。

## 适配平台

| 平台 | 说明 |
| --- | --- |
| Android | 通过 Termux 运行，面向移动端内存与算力约束做适配 |
| 低配 PC | 面向小内存 / 无独显的桌面环境做优化 |

## 技术方向

- **推理层**：`llama.cpp` 等本地推理后端，支持量化模型
- **记忆层**：全局统一记忆存储，跨会话与跨渠道共享
- **接入层**：NapCat（QQ）与网页对话双通道
- **人设层**：可插拔的人设配置

## 安装与启动

### 1. 安装依赖

```bash
cd memo-role
pip install -r requirements.txt
```

只需要 Python 与上面这些纯 Python 依赖；**推理模型与 llama-server 单独准备**（见第 3 步）。

### 2. 准备配置

```bash
cp config.example.yaml config.yaml   # 不改也能跑，全部有默认值
python -m memo_role --check          # 启动自检：一次看清缺什么
```

自检会检查依赖、配置、目录权限、数据库、模型目录、推理后端、监听端口、静态资源，
并给出一句结论，例如「可以开始第一轮测试（界面 / 指令 / 人设 / 记忆 / 文件 / 管理后台）；
真实聊天需要先下载模型」。**排障时先跑它，把输出贴出来即可。**

### 3. 准备模型（想真实聊天才需要）

把 GGUF 文件放进配置里的模型目录（默认 `models/`），**文件名必须与模型目录登记的一致**，
例如最小的那个：

```
models/smollm2-135m-instruct-q4_k_m.gguf      # 约 100MB，仅用于验证链路
models/qwen2.5-0.5b-instruct-q4_k_m.gguf      # 约 400MB，中文勉强可用，低配推荐
```

导出完整清单（含每个模型期望的路径）可看管理后台的「模型」页，或：

```bash
python -m memo_role --check     # 「模型」一项会打印出期望路径
```

> ⚠️ **内置下载源未经核实**：`memo_role/inference/catalog.py` 里的 `hf_repo` / `hf_file`
> 是开发环境（无法访问 HuggingFace）留下的推测值，不保证有效。请自行在 HuggingFace
> 搜索对应模型名，或从其它机器拷贝 GGUF 过来；下载后可用 `models/catalog.json`
> 覆盖内置条目（含自定义模型）。

另外还需要 **llama.cpp 的 `llama-server` 可执行文件**（默认后端），装好后把路径填进
`inference.llama_server.bin_path`，或让它出现在 `PATH` 里。找不到时程序会在第一次
对话时明确报出「缺哪个文件 / 缺哪个可执行文件」，而不是静默等待。

### 4. 启动

```bash
python -m memo_role                      # 默认 127.0.0.1:8000
python -m memo_role --host 0.0.0.0       # 手机访问必须监听 0.0.0.0
python -m memo_role --port 9000          # 换端口
python -m memo_role --reload             # 开发用热重载
```

浏览器打开 `http://<设备IP>:8000`（本机则 `http://127.0.0.1:8000`）：
`/` 对话页、`/admin` 管理后台、`/files` 文件管理。

## 安卓 App（可选外壳）

`android/` 目录里是一个可安装的 APK：**把网页界面装进一个应用图标里**，
省掉「打开浏览器输地址」，并补齐了网页在 WebView 下默认会坏的两件事 ——
文件管理页的**上传**与**下载**（不实现的话点了没反应）。

**它不是「自带 Python 的独立 App」**：Python 服务与模型需要**单独运行**，APK 只是客户端。
之所以不做成独立 App：Web 层依赖 fastapi + pydantic v2，后者含 Rust 编写的
`pydantic-core`，要为安卓交叉编译原生扩展，成本远高于收益。

### 使用：服务跑在哪台机器上？

**方式一 · 服务跑在电脑上（最省事，也最容易排查）**

1. 电脑上启动服务，加 `--host 0.0.0.0` 让手机能连进来：
   ```bash
   pip install -r requirements.txt
   python -m memo_role --host 0.0.0.0
   ```
2. 查电脑的局域网 IP，在 App 的「设置」里填 `192.168.1.5:8000` 这样的地址。
3. 电脑防火墙需允许 Python 通过专用网络，否则手机会一直连不上。

**方式二 · 服务与 App 在同一台手机上**

⚠️ 这里有个坑：**直接在 Termux 里 `pip install -r requirements.txt` 会失败**。
`pydantic-core` 只发布 glibc（manylinux / musllinux）版本的安装包，而 Termux 用的是
Bionic libc，pip 找不到匹配的轮子，会转去从源码编译 Rust 扩展 —— 慢，且经常编不过。

所以要在 **proot 里的 Ubuntu**（真正的 glibc 环境）中运行：

```bash
# 在 Termux 里
pkg install proot-distro
proot-distro install ubuntu
proot-distro login ubuntu

# 以下在 Ubuntu 里执行（Termux 的家目录被绑定在同一路径）
cd /data/data/com.termux/files/home/memo-role
apt update; apt install -y python3-pip
pip install -r requirements.txt
python -m memo_role          # 不需要 --host 0.0.0.0
```

App 默认就连 `http://127.0.0.1:8000`。不加 `--host` 也就不会把文件管理页暴露到局域网。

> 真机上的内存与首字延迟还没有实测数据；跑真实模型时 `llama-server` 需要
> llama.cpp 的安卓构建版本，或在 proot 里自行编译。

### 安装 APK

拷到手机点击安装（需允许「安装未知来源应用」），或 `adb install -r android/dist/memo-role-0.1.1.apk`。
连不上服务时会显示引导页，上面直接写着上面这些命令。

### 重新构建

```bash
cd android
SDK=/path/to/android-sdk ./build.sh     # 需要 SDK 的 build-tools + platforms，见脚本头部
```

产物在 `android/dist/`。构建走的是 `aapt2 → javac → d8 → zipalign → apksigner`，
不用 Gradle：这个 App 只用 Android 框架自带类（无 AndroidX、无第三方库），
跳开 Gradle 能少下载几百 MB 依赖，也避开 AGP 与 JDK 版本匹配的坑。

> 注意：`build.sh` 会主动挑一个 JDK 11~17 —— build-tools 34 自带的 d8 在
> JDK 21+ 上会直接抛空指针。首次构建还会生成一把自签名调试密钥（不纳入版本管理）。

## 人工测试流程

分两轮，第一轮不需要模型 —— 环境没配好也能先把界面与业务逻辑测完。

### 第一轮：不装模型（约 10 分钟）

| # | 步骤 | 预期 |
| --- | --- | --- |
| 1 | `python -m memo_role --check` | 报告能看懂；「模型」「推理后端」可能失败，其余应为 OK |
| 2 | 手机浏览器打开 `http://127.0.0.1:8000`（装了 APK 就直接打开 App） | 三个页面都能打开；App 会自动连 127.0.0.1:8000 |
| 3 | 点「＋ 新建会话」 | 侧栏出现会话，输入框可用，顶栏显示人设 / 模型徽标 |
| 4 | 点下方指令按钮 `/help` | 气泡列出全部指令，且**没有**卡顿（指令不进推理） |
| 5 | 发 `/persona list`、`/model list`、`/status` | 分别列出人设、模型、当前状态 |
| 6 | 发 `/persona catgirl`，再发 `/status` | 人设切换为新会话绑定，状态随之变化 |
| 7 | 点「＋ 新建会话」，再发 `/status` | 新会话仍是默认人设（验证会话级绑定互不影响） |
| 8 | 侧栏 ✎ 重命名会话；✕ 删除会话 | 标题即时更新；删除有二次确认 |
| 9 | 点「清空上下文」→ 确定 | 消息清空，长期记忆保留（`/memory` 仍能查到） |
| 10 | 打开 `/admin`，逐个标签页查看 | 概览有真实数值；人设可增删改；记忆可检索/编辑；模型显示「未下载」 |
| 11 | 在 `/admin` 人设页新建一个人设并保存 | 卡片出现；回到 `/` 的下拉里能选到它 |
| 12 | 打开 `/files`，进入目录、编辑并保存一个文本文件 | 保存成功、内容回显；受保护文件（数据库）不可编辑 |
| 13 | 在 `/files` 新建一个文件，写入内容保存 | 列表出现该文件；再删除它 |
| 14 | 装了 APK 的话：在 App 里点「上传文件」挑一张图片 | 能弹出系统文件选择器并上传成功（WebView 需额外实现，容易坏） |

### 第二轮：真实模型（聊天链路）

| # | 步骤 | 预期 |
| --- | --- | --- |
| 1 | `--check` 中「模型」「推理后端」变为 OK | 模型文件与 llama-server 都已就位 |
| 2 | `/admin` 模型页把已下载的模型「设为默认」 | 提示切换成功；首次会加载模型，需要等待 |
| 3 | 在 `/` 发一句普通聊天 | **逐字流式**出现回复；等待期间可点「停止」 |
| 4 | 再问一句与上句相关的话 | 模型记得上下文（工作记忆生效） |
| 5 | 告诉它一件事实（如「我住在杭州」），然后 `/reset`，再问 | 上下文清空但长期记忆生效，仍答得出杭州 |
| 6 | `/memory` | 能看到从对话里提取的记忆条目 |
| 7 | 切换人设后再聊 | 语气/角色变化明显 |
| 8 | `/admin` 记忆页删掉那条记忆，再问 | 该信息不再被召回 |

> 遇到任何一步失败：记下步骤号 + 页面提示原文（或 `--check` 输出）即可反馈，
> 不需要自己排查。

**测试前请先知道这两条（不是 bug）：**

- **指令的回复不会写进会话历史**：`/help`、`/status` 这类指令在调用模型之前就被拦截，
  结果是直接返回的，因此刷新页面后这些气泡会消失（它们在界面上带「指令」标记）。
  这是有意的 —— 让指令不进模型的上下文，省算力也不污染角色扮演。
- **失败的那条消息会留在历史里**：用户消息先入库再推理，所以模型报错后，
  刷新页面仍会看到自己发的那句，而助手侧没有回复（因为确实没生成出来）。

## 项目状态

进度与待办**只维护在本节**，避免散落在各处导致重复开发或遗漏。

### 已完成里程碑

| # | 里程碑 | 内容 | 代码位置 |
| --- | --- | --- | --- |
| M1 | 基础设施 | 配置加载（YAML + 环境变量覆盖）、分级日志、SQLite 建表 | `memo_role/config.py`、`db.py`、`logging_setup.py` |
| M2 | 推理层 | `llama_server` / `llama_cpp` / `openai_api` 三后端统一抽象、模型注册表、生成参数打包 | `memo_role/inference/` |
| M3 | 记忆层 | 工作记忆 + 情节记忆 + 核心记忆、向量化（hashing / llama_server / openai）、规则或 LLM 提取 | `memo_role/memory/` |
| M4 | 人设层 | 人设卡读写与导入导出、内置人设、系统提示词渲染 | `memo_role/persona/` |
| M5 | 对话编排 | 会话级人设 / 模型绑定、内置指令拦截、群聊回复策略、生成串行化 | `memo_role/dialogue/` |
| M6 | NapCat 接入 | OneBot v11 反向 WebSocket、消息与指令解析、与记忆层打通 | `memo_role/adapters/` |
| M7 | Web 后端 | FastAPI 装配、对话 / 人设 / 记忆 / 模型 / 系统 / 文件六组 API、SSE 流式、文件沙箱 | `memo_role/web/` |
| M8 | Web 前端 | 气泡聊天页、管理后台（概览 / 人设 / 记忆 / 模型 / 对话策略）、文件管理页；原生 HTML/JS/CSS，无构建步骤 | `memo_role/web/static/` |
| M9 | 命令行入口 | `python -m memo_role` 启动服务，支持 `--host` / `--port` / `--config` / `--root` / `--reload` / `--check` | `memo_role/__main__.py` |
| M10 | 自检与就绪提示 | 启动自检报告（依赖 / 目录 / 模型 / 后端 / 端口）、对话页缺少模型时的前置提示、后端自动拉起修复 | `memo_role/selfcheck.py`、`inference/llama_server.py` |
| M11 | 安卓外壳 APK | 可安装的 WebView 客户端：地址可配、连不上给引导页、补齐文件上传与下载、图标与构建脚本（不依赖 Gradle） | `android/` |

测试：`python -m pytest`（499 项，全离线运行，不触碰真实推理与网络）。
安卓侧另做两项离线校验：APK 结构/签名（`apksigner verify` + `aapt2 dump badging`）
与地址解析函数（桌面 JVM 跑 `MainActivity.normalize`）。**APK 未在真机或模拟器上运行过** ——
本环境没有 KVM，跑不了模拟器。

### 后续任务

按「先能真跑、再好用」排序。每项都写明落点，避免改错模块。

**P0 — 真实链路尚未跑通（当前全部验证都在假后端上完成）**

1. **真实模型端到端**：环境已具备自检与明确报错（M10），但**尚未在真机上跑过一次真实推理**。下载 GGUF + 准备 llama-server 后按 README「人工测试流程 · 第二轮」走一遍。落点：`inference/`（后端是否有 bug）+ `web/static/app.js`（SSE 展示）。
2. **安卓 / 低配实测**：Termux（PRoot）里装依赖跑起来，记录内存峰值与首字延迟，据此回调 `inference.n_threads`、`n_ctx`。落点：`config.example.yaml` 默认值 + README。
3. **NapCat 联调**：`napcat.enabled: true` 后真实收发 QQ 消息，验证 @ 识别、群里 `/persona` 切换绑定、群聊标签前缀。落点：`adapters/napcat.py`。

**P1 — 功能补全**

4. **对话页**：消息分页（现在一次最多取 100 条）、会话导出为 Markdown / JSON。落点：`web/api/dialogue.py` + `static/app.js`。
5. **人设**：头像支持上传图片到沙箱（现在只能是 emoji 或手填 URL）。落点：`web/api/files.py` 复用上传 + 人设表单。
6. **记忆**：管理后台支持手动新增记忆（现在只能改 / 删）。落点：`web/api/memories.py` 需补 `POST`。
7. **模型**：Web 端触发 GGUF 下载与校验（现在只显示「未下载」；内置下载源未核实，见「安装与启动」第 3 步）。落点：`inference/registry.py` + `web/api/models.py`。
8. **文件页**：图片与文本预览、批量选择删除；表单从 `window.prompt` 换成弹窗，与其它页面风格统一。落点：`static/files.html`。

**P2 — 工程化与安全**

9. **部署脚本**：README 的安装 / 启动章节已补（M10），Termux 与 Windows 的一键启动脚本仍缺（`scripts/` 目录不存在）。
10. **CI**：仓库暂无 `.github/`，补 GitHub Actions 跑 pytest + ruff。
11. **访问控制**：Web 层**没有任何鉴权**。手机测试时若用 `--host 0.0.0.0`，同网段的任何人都能访问文件管理页（等于远程文件读写）；测完请改回 `127.0.0.1`，长期暴露必须先加访问口令。
12. **可观测性**：后台日志目前只读文件尾部，考虑请求级日志与错误聚合。

### 已知取舍（有意为之，不要「顺手优化」）

- **前端不引入框架与构建步骤**：低配设备上 `node_modules` 与打包产物是纯负担，三页全部原生实现。
- **同一时刻只装载一个模型**：`BackendPool` 切模型时卸载旧模型，这是低配设备的内存硬约束。
- **热更设置写 `data/runtime.json`，不回写 `config.yaml`**：避免程序改写用户配置文件、丢注释、难以 review。
- **改指令前缀需要重启**：前缀被 `CommandHandler` 快照在实例上（见 `web/state.py` 的 `DIALOGUE_OVERRIDABLE`），热更会造成前后不一致。
- **`/reset` 只清工作记忆**：长期记忆是全局资产，不应被一次「清空上下文」带走。
- **指令回复不入库**：指令在进推理前被拦截，其回复只回给渠道、不写 `message` 表，
  因此刷新后不显示。代价是「刷新就没了」，收益是角色扮演的上下文不被指令污染。
- **发送失败时把原文还原回输入框**：宁可让用户看到重复的一句，也不要把刚打的字弄丢。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。