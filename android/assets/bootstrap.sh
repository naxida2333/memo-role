#!/bin/bash
#
# 在容器（rootfs 内的 Ubuntu）里执行：装齐 Python 依赖。
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

echo "==> [1/3] 更新软件包索引"
apt-get update -qq

echo "==> [2/3] 安装 Python 运行环境"
# 只要最小集合：--no-install-recommends 能省下近百 MB（手机存储宝贵）
apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-venv ca-certificates

echo "==> [3/3] 安装 Python 依赖"
cd "$PROJECT_DIR"
# Ubuntu 24.04 起系统 Python 标记为 externally-managed，直接 pip install 会被拒。
# 这里是专用的一次性容器，用它自己的解释器没有影响，所以显式放行。
python3 -m pip install --break-system-packages --no-cache-dir \
    --disable-pip-version-check -r requirements.txt

echo "==> 依赖安装完成"
python3 -c "import fastapi, pydantic, numpy; print('fastapi', fastapi.__version__, '/ pydantic', pydantic.VERSION, '/ numpy', numpy.__version__)"
