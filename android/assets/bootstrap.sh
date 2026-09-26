#!/bin/bash
#
# 在容器（rootfs 内的 Ubuntu）里执行：装齐 Python 依赖与本地推理引擎要的系统库。
#
# 谁调用它
# --------
# Android 侧（Container.java）用 proot 进入 rootfs 后执行本脚本，
# 项目代码由宿主侧下载并解压到 /root/memo-role，所以这里不需要 git / curl。
#
# 为什么不用 git
# --------------
# 只是为了取一次代码，为此在 rootfs 里装 git 会多几十 MB；改为宿主侧直接
# 下载 GitHub 的 tarball 再解压，既省空间也少一层网络失败点。
#
# 改了这里要同步改 Container.BOOTSTRAP_VERSION
# -------------------------------------------
# 标记文件里记着「装到第几版清单」；只加不升版本号的话，已装好的老用户
# 会被跳过，新加的依赖永远装不上。
#
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/memo-role}"

# 容器内无人交互；不设这个，apt 会在某些提示上停下来等输入
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

if [ ! -d "$PROJECT_DIR" ]; then
    echo "找不到项目目录：$PROJECT_DIR" >&2
    exit 1
fi

echo "==> [1/4] 更新软件包索引"
apt-get update -qq

echo "==> [2/4] 安装 Python 运行环境"
# 只要最小集合：--no-install-recommends 能省下近百 MB（手机存储宝贵）
apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-venv ca-certificates

# ----------------------------------------------------------------------
# [3/4] 补齐 llama-server 要的系统库
#
# llama-server 是随 APK 分发的预编译二进制（宿主侧装到 /usr/local/lib/llama），
# 它链接的部分库由容器提供。Ubuntu base 镜像里**没有 libgomp.so.1**，
# 缺了它只会得到一句 `error while loading shared libraries` 然后退出。
#
# 只装缺的：base 镜像已经带了 libssl.so.3 / libstdc++.so.6 之类，
# 没必要为了「保险」再走一遍 apt。
# ----------------------------------------------------------------------
echo "==> [3/4] 补齐 llama-server 需要的系统库"

# usrmerge 之后 /lib 只是 /usr/lib 的软链，两个名字都可能出现，所以都找。
#
# 问动态链接器最准：`ldconfig -p` 认了，llama-server 就一定加载得到 —— 这才是
# 我们真正关心的事。为什么不直接 ls 文件路径：容器跑在 proot 下，proot 要改写
# 每次系统调用里的路径，而 `ls` 走的是**不跟随符号链接**的 lstat，在多架构目录
# （libgomp.so.1 是个指向 libgomp.so.1.0.0 的软链）上并不可靠 —— 实测同一路径
# `[ -e ]` 为真、`ls -l` 却报 No such file。曾经因此把「装上了」判成「装不上」，
# 又把整个初始化带崩（见 ensure_lib）。
have_lib() {
    local so="$1"
    local path
    if ldconfig -p 2>/dev/null | grep -q -- "$so"; then
        return 0
    fi
    # 链接器缓存可能还没刷新（刚解包、ldconfig 触发器尚未跑），退回看文件本身
    for path in /usr/lib/*/"$so" /lib/*/"$so" /usr/lib/"$so" /lib/"$so"; do
        [ -e "$path" ] && return 0
    done
    return 1
}

# 已存在就跳过；不存在则按候选包名依次尝试（不同 Ubuntu 版本包名会变，
# 例如 24.04 的 OpenSSL 3 叫 libssl3t64、22.04 叫 libssl3）。
#
# 装不上**绝不能**中断整个初始化，所以这里一律返回 0：缺这一样只让「本地模型」
# 不可用，第三方 API 那条路不依赖任何本地库，仍然能正常聊天。这里原本是
# `return 1`，被脚本开头的 `set -e` 一放大，后面的「安装 Python 依赖」也跟着
# 不执行 —— 一个可选库把整个初始化带崩，日志里只有一句「装不上」，看不出因果。
ensure_lib() {
    local so="$1"
    shift
    local pkg
    if have_lib "$so"; then
        echo "    $so：已有，跳过"
        return 0
    fi
    for pkg in "$@"; do
        # apt 的输出不吞：真装不上时，那几行报错是唯一的线索
        if apt-get install -y -qq --no-install-recommends "$pkg" && have_lib "$so"; then
            echo "    $so：由 $pkg 提供"
            return 0
        fi
    done
    echo "    ⚠ $so：装不上（候选包：$*）。本地模型将无法启动，第三方 API 不受影响。" >&2
    return 0
}

ensure_lib libgomp.so.1 libgomp1
ensure_lib libssl.so.3 libssl3t64 libssl3
ensure_lib libcrypto.so.3 libssl3t64 libssl3
ensure_lib libstdc++.so.6 libstdc++6
ensure_lib libgcc_s.so.1 libgcc-s1

# ----------------------------------------------------------------------
# [4/4] 安装 Python 依赖，并试跑一次 llama-server
# ----------------------------------------------------------------------
echo "==> [4/4] 安装 Python 依赖"
cd "$PROJECT_DIR"
# Ubuntu 24.04 起系统 Python 标记为 externally-managed，直接 pip install 会被拒。
# 这里是专用的一次性容器，用它自己的解释器没有影响，所以显式放行。
python3 -m pip install --break-system-packages --no-cache-dir \
    --disable-pip-version-check -r requirements.txt

python3 -c "import fastapi, pydantic, numpy; print('fastapi', fastapi.__version__, '/ pydantic', pydantic.VERSION, '/ numpy', numpy.__version__)"

# 试跑一次：把「漏文件 / 缺库」当场暴露在初始化日志里，而不是等用户发起
# 第一次对话才报错。--version 只打印版本就退出，不加载模型，几秒钟的事。
#
# 结果落盘到 SELFTEST：首页状态栏据此区分「已就绪」与「装进来了但起不来」。
# 只看文件在不在是不够的 —— 缺一个 libgomp 时文件一样齐，状态栏却会骗人。
echo "==> 试跑本地推理引擎"
SELFTEST=/usr/local/lib/llama/SELFTEST
if [ -x /usr/local/bin/llama-server ]; then
    if /usr/local/bin/llama-server --version >/tmp/llama-server-version.txt 2>&1; then
        # 它先打印一行带时间戳的启动日志，版本在 "version: ..." 那一行
        ver="$(sed -n 's/^version: //p' /tmp/llama-server-version.txt | head -n 1)"
        echo "    llama-server 可用：${ver:-$(head -n 1 /tmp/llama-server-version.txt)}"
        printf 'ok %s\n' "${ver:-unknown}" >"$SELFTEST" 2>/dev/null || true
    else
        echo "    ⚠ llama-server 起不来（本地模型不可用，第三方 API 不受影响）：" >&2
        sed 's/^/      /' /tmp/llama-server-version.txt >&2 || true
        { printf 'fail '; tail -n 1 /tmp/llama-server-version.txt; } >"$SELFTEST" 2>/dev/null || true
    fi
else
    echo "    ⚠ 容器里没有 /usr/local/bin/llama-server（本地模型不可用，第三方 API 不受影响）" >&2
    rm -f "$SELFTEST"
fi

echo "==> 依赖安装完成"
