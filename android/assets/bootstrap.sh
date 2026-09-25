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

# usrmerge 之后 /lib 只是 /usr/lib 的软链，两个名字都可能出现，所以都找
have_lib() {
    ls /usr/lib/*/"$1" /lib/*/"$1" /usr/lib/"$1" /lib/"$1" >/dev/null 2>&1
}

# 已存在就跳过；不存在则按候选包名依次尝试（不同 Ubuntu 版本包名会变，
# 例如 24.04 的 OpenSSL 3 叫 libssl3t64、22.04 叫 libssl3）
ensure_lib() {
    local so="$1"
    shift
    if have_lib "$so"; then
        echo "    $so：已有，跳过"
        return 0
    fi
    local pkg
    for pkg in "$@"; do
        if apt-get install -y -qq --no-install-recommends "$pkg" >/dev/null 2>&1 && have_lib "$so"; then
            echo "    $so：由 $pkg 提供"
            return 0
        fi
    done
    # 不中断整个初始化：缺这一样只会让「本地模型」不可用，
    # 第三方 API 那条路不依赖任何本地库，仍然能正常聊天
    echo "    ⚠ $so：装不上（候选包：$*）。本地模型将无法启动，第三方 API 不受影响。" >&2
    return 1
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
echo "==> 试跑本地推理引擎"
if [ -x /usr/local/bin/llama-server ]; then
    if /usr/local/bin/llama-server --version >/tmp/llama-server-version.txt 2>&1; then
        # 它先打印一行带时间戳的启动日志，版本在 "version: ..." 那一行
        ver="$(sed -n 's/^version: //p' /tmp/llama-server-version.txt | head -n 1)"
        echo "    llama-server 可用：${ver:-$(head -n 1 /tmp/llama-server-version.txt)}"
    else
        echo "    ⚠ llama-server 起不来（本地模型不可用，第三方 API 不受影响）：" >&2
        sed 's/^/      /' /tmp/llama-server-version.txt >&2 || true
    fi
else
    echo "    ⚠ 容器里没有 /usr/local/bin/llama-server（本地模型不可用，第三方 API 不受影响）" >&2
fi

echo "==> 依赖安装完成"
