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

在管理后台的「模型」页可以直接下载：每条内置模型都有「下载」按钮，下载源默认是
国内可直连的 `https://hf-mirror.com`（换成 `https://huggingface.co` 或自建反代也行）。
已经拿到链接的，用同一页的「从链接导入」填进去，服务自己下到 `models/`。

手工放置也可以：把 GGUF 放进配置里的模型目录（默认 `models/`），**文件名必须与模型目录
登记的一致**，例如最小的那个：

```
models/smollm2-135m-instruct-q4_k_m.gguf      # 约 100MB，仅用于验证链路
models/qwen2.5-0.5b-instruct-q4_k_m.gguf      # 约 469MB，中文勉强可用，低配推荐
```

导出完整清单（含每个模型期望的路径）可看管理后台的「模型」页，或：

```bash
python -m memo_role --check     # 「模型」一项会打印出期望路径
```

> 内置下载源（`memo_role/inference/catalog.py` 里的 `hf_repo` / `hf_file`）已逐条实测
> 可下载（2026-09 经 hf-mirror），但上游随时可能改名/下架；下不到时会明确报出 HTTP
> 状态，两分钟内可以自己修：改 `models/catalog.json` 覆盖条目，或直接用「从链接导入」。

另外还需要 **llama.cpp 的 `llama-server` 可执行文件**（默认后端），装好后把路径填进
`inference.llama_server.bin_path`，或让它出现在 `PATH` 里。找不到时程序会在第一次
对话时明确报出「缺哪个文件 / 缺哪个可执行文件」，而不是静默等待。

> **安卓 App 不用管这一条**：`llama-server`（aarch64 预编译版）随 APK 分发，
> 一键初始化时装进容器的 `/usr/local/bin`，开箱即用。见下方「安卓 App」一节。

### 3.5 不想装本地模型？接第三方 API

管理后台「模型」页最上面可以切推理后端：选 `openai_api`，填 `base_url`、模型名、
API 密钥（一行一个，可填多个自动轮询），点「测试连接」确认通，再「保存并切换」。
切换是**立即生效**并写入 `data/runtime.json` 的，重启仍然有效；密钥只存本机、
接口只回显条数，不会出现在页面或日志里。

常见地址：DeepSeek `https://api.deepseek.com/v1`、硅基流动 `https://api.siliconflow.cn/v1`、
本地 Ollama `http://127.0.0.1:11434/v1`（免密钥）。这条路不需要 `llama-server`，
手机上是最快能真聊起来的办法。

### 4. 启动

```bash
python -m memo_role                      # 默认 127.0.0.1:8000
python -m memo_role --host 0.0.0.0       # 手机访问必须监听 0.0.0.0
python -m memo_role --port 9000          # 换端口
python -m memo_role --reload             # 开发用热重载
```

浏览器打开 `http://<设备IP>:8000`（本机则 `http://127.0.0.1:8000`）：
`/` 对话页、`/admin` 管理后台、`/files` 文件管理。

## 安卓 App（原生界面 + 内置 Linux 容器）

`android/` 里是一个可安装的 APK，**自带一个 Linux 容器**：APK 内置 proot 运行时，
首次启动时下载 Ubuntu 官方 arm64 base rootfs 并解压到应用私有目录，再在容器里
安装 Python 依赖、从 GitHub 拉取项目代码，最后启动服务。

**装一个 APK 就能用** —— 不需要 Termux，也不需要另一台电脑。

### 界面

| 页面 | 内容 |
| --- | --- |
| 首页 | 环境状态、一键初始化（带下载/解压进度与执行日志）、启动 / 停止服务 |
| 终端 | 在容器内执行命令，用于排障（不是完整终端，不支持 `vi` 这类交互式程序） |
| 界面 | 项目自带的网页界面：对话页 / 管理后台 / 文件管理 |

### 首次启动会发生什么

点「一键初始化」后依次进行，全程需要联网：

1. 安装内置的 proot 运行时（约 290 KB，瞬间完成）
2. 下载 Ubuntu base rootfs（约 30 MB）
3. 解压（约 3400 个文件，含 194 个符号链接）
4. 写入 DNS 与软件源配置
5. 安装内置的本地推理引擎 llama-server（约 27 MB，从 APK 里拷进容器，几秒）
6. 从 GitHub 拉取项目代码（走 tarball，省掉在容器里装 git）
7. 容器内 `apt install python3-pip` + 补 `libgomp1` 等系统库 +
   `pip install -r requirements.txt`（约 150 MB），最后试跑一次 `llama-server --version`

第 5 步与第 7 步末尾的试跑都是为了把「漏文件 / 缺库」当场写在日志里，
而不是等到第一次聊天才报错。

第 7 步里「补齐系统库」是**尽力而为**：某一样装不上只警告、不中断初始化
（缺的库只影响本地模型，第三方 API 那条路不依赖本地库）。结束时会把
`llama-server --version` 的试跑结果落盘，首页状态据此显示「已就绪」还是
「已装入但起不来」—— 文件齐不等于跑得起来，缺一个 `libgomp.so.1` 就是这样。

建议留出 2 GB 以上存储空间（容器 + 依赖 + 后续模型）。

> 从 0.2.x 升上来时容器和代码都还在，点一次「一键初始化」即可：已做过的步骤会自动
> 跳过，只补本地推理引擎与新增的系统库（第 5、7 步），**模型与聊天数据不受影响**。

### 更新代码

网页界面与后端逻辑都在容器里的项目目录中，仓库一更新就需要重新拉一次。
「设置 → 更新代码」只重新下载项目代码并覆盖代码文件，**不动容器、不动模型、
配置与聊天数据**，几秒到几十秒完成；服务在运行中的话会自动重启。

> **重启不是可选项**：进程只在启动时读一次代码。代码换了而进程没换，就会出现
> 「页面是新的、点什么都报 Not Found（接口不存在）」这种拧巴状态 —— 前端是静态
> 文件、每次现取，接口却是启动时注册好的。所以更新代码、一键初始化都会强制重启，
> 界面上也会把「端口上有人应答但不是本 App 启动的」这种残留状态明说出来。
>
> 页面与静态资源在服务端强制 `Cache-Control: no-store`，浏览器（尤其是安卓
> WebView）不会拿旧 JS 出来顶替。于是**界面新而接口 404 = 服务还是旧进程**，
> 这个判断是可靠的 —— 前端也会在 404 时直接把这句话提示出来。

只有在容器本身出问题、或想彻底重来时，才需要「设置 → 重置」再走一遍
「一键初始化」（那会重新下载容器与依赖，约 180 MB）。

### 为什么 targetSdk 停在 28

Android 10（API 29）起禁止「从可写应用主目录执行文件」（W^X）：以 API 29+
为目标的不可信应用无法对应用主目录中的文件调用 `execve()`。而容器里的 `bash`、
`python3` 恰好都在应用数据目录下，targetSdk 一旦 ≥ 29 就全部执行不了。
Termux 与 UserLAnd 至今保持 28 也是这个原因。

代价是**无法上架 Google Play**（那里要求更高的 targetSdk），只能侧载安装；
Android 本身只拒绝 targetSdk < 23 的应用，28 在 Android 14/15 上可正常安装。

### 已知限制

- **服务随 App 进程存活**：App 被系统回收后服务会随之停止（前台服务保活尚未做）。
- **推理引擎只面向 arm64**：随 APK 分发的是 llama.cpp 官方 ubuntu-arm64 预编译包
  （需要 glibc ≥ 2.38，容器是 Ubuntu 24.04，满足）。
- **未在真机上验证过**：见下方「测试状态」。

### 安装

拷到手机点击安装（需允许「安装未知来源应用」），或
`adb install -r android/dist/memo-role-0.3.3.apk`。

### 重新构建

```bash
cd android
SDK=/path/to/android-sdk ./build.sh     # 需要 SDK 的 build-tools + platforms，见脚本头部
```

产物在 `android/dist/`。构建走的是 `aapt2 → javac → d8 → zipalign → apksigner`，
不用 Gradle：这个 App 只用 Android 框架自带类（无 AndroidX、无第三方库），
跳开 Gradle 能少下载几百 MB 依赖，也避开 AGP 与 JDK 版本匹配的坑。

> 注意：`build.sh` 会主动挑一个 JDK 11~17 —— build-tools 34 自带的 d8 在
> JDK 21+ 上会直接抛空指针。

### 内置的第三方二进制

**proot 运行时**：`android/assets/proot/` 里的 5 个文件（`proot`、`loader`、`loader32`、
两个 `.so`）取自 Termux 官方仓库，许可以及上游源码地址见该目录下的 `NOTICE.md`。
它们不是本项目的代码，随 APK 分发是为了让 App 能自己拉起容器。
**每个文件都是必需的**，尤其 `loader` —— 缺了它 proot 无法在容器里执行任何程序。

**本地推理引擎**：`android/vendor/llama-<版本>-ubuntu-arm64.tar.gz` 是从
[llama.cpp](https://github.com/ggml-org/llama.cpp) 官方预编译发布包裁剪出来的
`llama-server` 及其依赖库（MIT 许可，版本、下载地址与压缩包 sha256 见包内 `NOTICE.md`）。
官方包约 32 MB，装了 `llama-cli`、`llama-bench`、`llama-quantize` 等手机用不到的程序；
这里只保留「从 `llama-server` 出发按 `DT_NEEDED` 展开、再加上 ggml 运行时 dlopen 的
CPU 变体库」的最小闭包（约 27 MB 原始 / 约 12 MB 压缩）。

`android/assets/llama/` 是构建时从该压缩包展开的产物（**不入库**，见 `.gitignore`），
`build.sh` 每一步都会重新展开并校验。要换 llama.cpp 版本：

```bash
cd android
python3 tools/vendor_llama.py --tag b11191        # 从官方 GitHub 下载并裁剪
# 或指定已下好的包 / 已解压的目录
python3 tools/vendor_llama.py --archive /path/to/llama-b11191-bin-ubuntu-arm64.tar.gz
./tests/check-llama.sh                            # 离线校验（见下）
```

### 离线自测（不需要手机）

```bash
android/tests/check-runtime.sh   # 校验 proot 运行时是否齐全、形态是否能在安卓上跑
android/tests/check-llama.sh     # 校验 llama-server 与它的 .so 是否齐全、能不能在容器里加载
android/tests/check-service-supervision.sh   # 校验「清理残留服务」的 pattern 既不漏杀也不误杀
android/tests/check-bootstrap.sh # 校验初始化的「补齐系统库」：判断得准、失败不带崩整体
android/tests/run.sh             # 校验自写的 tar 解压器（需要 JDK 11+ / python3 / GNU tar）
```

`check-runtime.sh` 逐项确认随包分发的 proot 运行时：文件是否齐全（**proot 官方包装的不止
一个可执行文件**，漏掉 `libexec/proot/loader` 会导致容器里什么都执行不了）、架构是否正确、
两个 loader 是否静态链接、proot 的解释器是否为系统 linker、动态依赖是否都在包里。
离线运行，不需要网络与 SDK。

`check-llama.sh` 逐项确认随包分发的本地推理引擎：目录内容与仓库里的 vendor 包是否逐个
对齐（防手工改过或解压不全）、关键文件是否齐全、是否都是 aarch64、是否都是实体文件
（**APK 的 assets 不保留符号链接**，官方包里的 `libllama.so -> libllama.so.0` 这类链条
必须先摊平）、动态依赖闭包是否完整（除容器负责提供的系统库外都必须随包）、
二进制要求的 glibc 版本是否不超过容器自带的 2.39。
带上 rootfs 路径（`./check-llama.sh /path/to/rootfs`）还能顺带确认那 11 个系统库
确实存在于容器里。

`check-service-supervision.sh` 从 `Container.java` 里**取出实际使用的那条 pkill
pattern**（而不是另抄一份），再拿它去比对真实的服务命令行、proot 命令行与
「执行这条命令的 shell 自己」：必须命中前者、必须放过后者。
写死裸 pattern（`memo-role`）会连自己一起杀掉 —— 这个坑实测踩过，
而这种「清理失败 / 自杀」在手机上只会表现为「服务起不来」，很难查，所以用脚本钉住。

`check-bootstrap.sh` 从 `bootstrap.sh` 里**取出真实的 `have_lib` / `ensure_lib` 与自检块**
（不另抄一份），配上假的 `ldconfig` / `apt-get` 跑：链接器认账时不该去装；真装不上时只警告、
不能把脚本带走；自检的三种结果（ok / fail / 二进制不在）落盘要各自正确。
真机踩过的地方：proot 下按路径 `ls` 判断「库在不在」会误判成「装不上」，
而那个返回值又被 `set -e` 放大成整个初始化失败 —— 一个可选库把容器装崩了。

`run.sh` 用合成 tar.gz 分别喂给 `TarGz` 与系统 `tar`，逐项比对条目、类型、符号链接目标、
内容摘要、权限位（含 setuid）与硬链接。覆盖 GNU 与 POSIX(pax) 两种打包格式，
以及超长路径、二进制内容、空文件等边界 —— 解压是初始化流程的第 3 步，
错了后面全卡住，所以在真机之外必须有办法验证。

### 签名约定（重要）

安卓要求「覆盖安装的新版本必须与已装版本同一签名」，否则只能卸载重装 ——
而这个 App 卸载会连容器、依赖和模型一起删掉（几个 GB）。因此：

- **签名密钥**：`android/debug.keystore`（自签名调试密钥，**不纳入版本管理**，
  换了机器要自己保管好；丢了就只能卸载重装）。
- **指纹记录**：`android/signing-cert.sha256`（纳入版本管理）。
  `build.sh` 在签名**之前**核对密钥指纹，不一致直接中止且不产出 APK；
  签名后还会再核验一次成品，防止签错文件。
- 确实要换密钥：删掉 `signing-cert.sha256` 重新构建，或用
  `ALLOW_KEY_CHANGE=1 ./build.sh` 临时放行。

当前指纹：`0730afc2e31455e6d94e0950f2b931c93fa491b750fe3b92266c7b5db392d544`
（v0.1.0 / v0.1.1 / v0.2.0 / v0.2.1 / v0.2.2 / v0.2.3 / v0.2.4 / v0.3.0 / v0.3.1 / v0.3.2 /
v0.3.3 全部一致，可逐版覆盖安装）

## 人工测试流程

分两轮，第一轮不需要模型 —— 环境没配好也能先把界面与业务逻辑测完。

### 第一轮 · 安卓 App（0.2.0 的重点，只在这一台设备上完成）

| # | 步骤 | 预期 |
| --- | --- | --- |
| 1 | 安装 APK 并打开 | 顶栏显示「未初始化」；首页三项状态均为未就绪 |
| 2 | 点「一键初始化」，中途盯着日志 | 日志逐行滚动；下载与解压阶段进度条前进；界面不假死 |
| 3 | 等待结束（首次几分钟） | 日志出现「初始化完成」，随后自动跳到「界面」页并显示对话页 |
| 4 | 看顶栏 | 显示「运行中 · 端口 8000」 |
| 5 | 切到「终端」，执行 `python3 -V` | 打印 Python 版本 |
| 6 | 终端执行 `ls /root/memo-role` | 列出项目文件（说明代码已从 GitHub 拉进容器） |
| 7 | 首页点「停止服务」再「启动服务」 | 状态来回切换；重启后「界面」页仍能打开对话页 |
| 8 | 界面页测「对话页 / 管理后台 / 文件管理」 | 与浏览器里一致；文件管理页可用（上传/下载已适配 WebView） |

失败时的反馈方式：**首页那一整块日志可以长按复制**，连同卡住的那一步一起发出来即可。

### 第一轮 · 浏览器 / 电脑（不装模型）

| # | 步骤 | 预期 |
| --- | --- | --- |
| 1 | `python -m memo_role --check` | 报告能看懂；「模型」「推理后端」可能失败，其余应为 OK |
| 2 | 浏览器打开 `http://127.0.0.1:8000` | 三个页面都能打开 |
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
| 14 | 在 `/files` 点「上传文件」挑一张图片 | 弹出系统文件选择器并上传成功（WebView 下需额外实现，容易坏） |

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
| M12 | 安卓容器客户端 | 自带 proot 运行时：首次启动下载并解压 Ubuntu rootfs、容器内装依赖、从 GitHub 拉代码、启动服务；原生界面（首页 / 终端 / 界面）+ 日志与进度 | `android/src/`、`android/assets/` |
| M13 | 安卓与第三方 API 可用 | 管理后台可切 `openai_api` 后端（填地址/模型/多密钥、测连接后立即生效）、可下载模型与从链接导入、文件页窄屏布局修复；修 SmolLM2 下载源 404 | `web/api/models.py`、`inference/downloader.py`、`web/static/admin.html` |
| M14 | 安卓本地推理引擎 | aarch64 的 `llama-server` 随 APK 分发：裁剪出最小闭包、装进容器 `/usr/local/bin`、bootstrap 补系统库并试跑、状态页显示版本；离线校验脚本 | `android/vendor/`、`android/tools/vendor_llama.py`、`android/tests/check-llama.sh`、`Container.java` |

测试：`python -m pytest`（536 项，全离线运行，不触碰真实推理与网络）。

安卓侧的离线校验（真机之外的尽调，逐条有据）：

| 校验项 | 方法 | 结论 |
| --- | --- | --- |
| APK 结构与签名 | `apksigner verify` + `aapt2 dump badging` | v2+v3 签名通过；targetSdk 为 28 |
| 包内 proot 未被损坏 | 解出 assets 里的 proot 与源 deb 比对 MD5 | 一致（`e8b9fd8b…`） |
| proot 能否脱离 Termux 运行 | `readelf` 查 ELF 解释器与依赖 | 解释器是系统 `/system/bin/linker64` |
| proot 运行时是否完整可行 | `android/tests/check-runtime.sh` | 5 个文件齐全；两个 loader 静态链接；架构正确；依赖都在包里 |
| proot 是否支持安卓必需选项 | 二进制字符串 + 包元数据 | 支持 `--link2symlink` / `--kill-on-exit` |
| 依赖能否免编译安装 | `pip download --platform manylinux2014_aarch64` | 全部 30 个包均有 aarch64 现成轮子（含 `pydantic-core`） |
| rootfs 结构是否被解压器覆盖 | 用 `tarfile` 解析真实 Ubuntu 包 | 3413 条目，typeflag 仅 0/1/2/5，无 pax 头；usrmerge（`bin` 是指向 `usr/bin` 的符号链接）已适配 |
| 解压器产出是否与系统 tar 一致 | `android/tests/run.sh`：同一 tar.gz 分别给 `TarGz` 与 GNU tar，逐项比对 | 真实 rootfs 3413 条目零差异；合成包（GNU / pax 两种格式）也全对 |
| 签名是否可覆盖安装 | 逐版比对 `apksigner verify --print-certs` | v0.1.0 / v0.1.1 / v0.2.0 / v0.2.1 指纹一致 |
| 代码拉取路径是否正确 | 解析 GitHub tarball 顶层 | 单一顶层目录 `memo-role-main`，与 `fetchProject` 的假设一致 |
| llama-server 是否齐全可用 | `android/tests/check-llama.sh`（带 rootfs 路径） | 18 个文件与 vendor 包逐个对齐；全是实体文件；依赖闭包完整；最高要求 GLIBC_2.38 ≤ 容器 2.39；11 个系统库在 rootfs 内逐个确认 |
| llama-server 能否在容器里跑 | 取与 App 同一个 Ubuntu 24.04 rootfs，用 qemu-aarch64 实跑 | `--version` 正常；加载 SmolLM2-135M 后 `/health` 返回 200、`/v1/chat/completions` 生成出真实文本 |
| 容器里怎么放这套二进制 | 在同一个 rootfs 上分别试「软链」「复制到别的目录」 | 两种都不行：库在同目录靠 `$ORIGIN` 找，而 ggml 是**扫可执行文件所在目录**找那 8 个 CPU 变体（`GGML_BACKEND_PATH` 只能指定单个文件），挪走就报 `no backends are loaded`。所以 /usr/local/bin 放的是一个 `exec` 真身的入口脚本 |
| proot 能否执行入口脚本 | 读 proot 源码 `src/execve/shebang.c` | `expand_shebang()` 会解析脚本的 shebang 并换成容器内的解释器，脚本可直接执行 |
| 项目后端能否拉起本地引擎 | 让 `LlamaServerBackend` 自己启动（经 qemu 包装）并连发两次对话 | 启动参数被接受、模型加载完成、`/health` 就绪；随后 400 是测试配置的 `n_ctx` 太小、300s 超时是 qemu 模拟太慢，均与链路无关 |

**APK 仍未在真机或模拟器上运行过** —— 本环境没有 KVM，跑不了模拟器；
上面这些只能证明「材料齐备且自洽」，不能证明在具体设备上能跑通。

### 后续任务

按「先能真跑、再好用」排序。每项都写明落点，避免改错模块。

**P0 — 真实链路尚未跑通（当前全部验证都在假后端上完成）**

1. **真实模型端到端**：环境已具备自检与明确报错（M10），但**尚未在真机上跑过一次真实推理**。下载 GGUF + 准备 llama-server 后按 README「人工测试流程 · 第二轮」走一遍。落点：`inference/`（后端是否有 bug）+ `web/static/app.js`（SSE 展示）。
2. **安卓容器客户端首次真机验证**：M12 的所有材料都验过、代码也编译通过，但**从未在任何设备上运行**。按「人工测试流程 · 第一轮 · 安卓 App」逐条走。落点：`android/src/`（proot 调用、解压、网络）。
3. **安卓上的本地模型端到端**：`llama-server` 已随 APK 分发并在 qemu + 同款 rootfs 上
   跑通推理（M14），但**尚未在真机上跑过一次**。手机上按「人工测试流程 · 第二轮」走。
   落点：`android/assets/bootstrap.sh`（系统库是否齐全）+ `Container.java`（安装与状态）。
4. **服务保活**：目前服务随 App 进程存活，切后台被回收就断。改用前台服务 + 常驻通知。落点：`android/src/`（新增 Service 组件 + 通知渠道，注意 Android 13+ 要 `POST_NOTIFICATIONS`）。
5. **NapCat 联调**：`napcat.enabled: true` 后真实收发 QQ 消息，验证 @ 识别、群里 `/persona` 切换绑定、群聊标签前缀。落点：`adapters/napcat.py`。
6. **手机上的推理速度**：手机上只有 CPU、线程数默认 2，135M 之外的模型可能很慢。
   需要真机实测后再决定默认 `n_ctx` / `n_threads` / 量化档位，必要时在管理后台暴露。落点：`inference/` + `web/static/admin.html`。

**P1 — 功能补全**

7. **对话页**：消息分页（现在一次最多取 100 条）、会话导出为 Markdown / JSON。落点：`web/api/dialogue.py` + `static/app.js`。
8. **人设**：头像支持上传图片到沙箱（现在只能是 emoji 或手填 URL）。落点：`web/api/files.py` 复用上传 + 人设表单。
9. **记忆**：管理后台支持手动新增记忆（现在只能改 / 删）。落点：`web/api/memories.py` 需补 `POST`。
10. **模型下载的校验与对齐**：现在只校验 HTTP 状态与大小，没有校验哈希；下载中断只能重下（无断点续传）。落点：`inference/downloader.py` + `catalog.py` 补 sha256。
11. **文件页**：图片与文本预览、批量选择删除；表单从 `window.prompt` 换成弹窗，与其它页面风格统一。落点：`static/files.html`。
12. **安卓文件页**：目前「文件」只能通过网页界面看，App 内还没有原生文件浏览器。落点：`android/src/`（新增面板，读沙箱根目录）。

**P2 — 工程化与安全**

13. **部署脚本**：README 的安装 / 启动章节已补（M10），Termux 与 Windows 的一键启动脚本仍缺（`scripts/` 目录不存在）。
14. **CI**：仓库暂无 `.github/`，补 GitHub Actions 跑 pytest + ruff。
15. **访问控制**：Web 层**没有任何鉴权**。服务监听 127.0.0.1 时只有本机可访问（安卓客户端正是这么做的）；但用 `--host 0.0.0.0` 时同网段任何人都能访问文件管理页（等于远程文件读写），长期暴露必须先加访问口令。
16. **可观测性**：后台日志目前只读文件尾部，考虑请求级日志与错误聚合。

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