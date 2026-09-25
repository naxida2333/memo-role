#!/usr/bin/env bash
#
# 校验「清理残留服务」这条机制：App 里那条 pkill 的 pattern 必须
#   1) 命中容器里真正在跑的 python 服务；
#   2) **不**命中 App 自己（包名、proot 路径、以及执行命令的那个 shell）。
#
# 为什么值得单独钉住
# ----------------
# 这不是理论风险：如果 pattern 写成裸的 `memo-role`，执行这条命令的
# proot/bash 自己的命令行里就带着同一个字面量，pkill 会把自己一起杀掉
# （实测踩过：测试脚手架连同假服务一起被杀）。反过来，pattern 写得太窄
# 又清不掉残留进程，就会退回「磁盘是新代码、在跑的是旧代码」那种状态 ——
# 表现是页面能开、点什么都报 Not Found，极难从现象反推。
#
# 用法：./check-service-supervision.sh     （离线，不需要安卓 SDK）
#
set -euo pipefail

cd "$(dirname "$0")/../src/com/memorole/app"

fail=0
report() {
  if [ "$1" = bad ]; then echo "❌ $2"; fail=1; else echo "✅ $2"; fi
}

echo "== 1/3 从 Container.java 取出实际使用的 pattern"
# 匹配 exec("pkill -9 -f '...' ...") 里的单引号内容
PATTERN="$(grep -oE "pkill -9 -f '[^']+'" Container.java | head -1 | sed "s/.*-f '//; s/'$//")"
if [ -z "$PATTERN" ]; then
  report bad "没找到 pkill 命令（killStaleServices 被改动了？）"
  exit 1
fi
report ok "pattern = $PATTERN"

echo "== 2/3 必须命中容器里真正的服务进程"
# 服务是 startService 里拼出来的：cd /root/memo-role && exec python3 -m memo_role ...
SERVICE_CMDLINE="proot --link2symlink --kill-on-exit -0 -r /data/data/com.memorole.app/files/rootfs -b /dev /usr/bin/env -i HOME=/root /bin/bash -lc cd /root/memo-role && exec python3 -m memo_role --host 127.0.0.1 --port 8000"
if printf '%s' "$SERVICE_CMDLINE" | grep -qE -- "$PATTERN"; then
  report ok "能命中服务进程（含 python3 -m memo_role 与 /root/memo-role）"
else
  report bad "命中不了服务进程，残留进程清不掉"
fi

echo "== 3/3 绝不能命中 App 自己"
# 执行 pkill 时，这条命令自己的命令行里带着 pattern 的字面量 —— 必须匹配不上
SELF_CMDLINE="proot --link2symlink -0 -r /data/data/com.memorole.app/files/rootfs /usr/bin/env -i /bin/bash -lc pkill -9 -f '$PATTERN' 2>/dev/null; exit 0"
PROOT_CMDLINE="proot --link2symlink --kill-on-exit -0 -r /data/data/com.memorole.app/files/rootfs -w /root /usr/bin/env -i HOME=/root /bin/bash -lc cd /root/memo-role && exec python3 -m memo_role --host 127.0.0.1 --port 8000"

# 逐个检查：每一个都**必须**匹配不上（除了服务那条）
for label in "App 包名:com.memorole.app" \
             "proot 路径:/data/data/com.memorole.app/files/usr/bin/proot" \
             "App 数据目录:/data/data/com.memorole.app/files" \
             "执行 pkill 的命令行:$SELF_CMDLINE"; do
  name="${label%%:*}"
  text="${label#*:}"
  if printf '%s' "$text" | grep -qE -- "$PATTERN"; then
    report bad "$name 会被自己的 pkill 命中（命令会杀掉自己）"
  else
    report ok "$name 不会被误杀"
  fi
done

# 服务那条要反过来：必须命中（否则整条逻辑白写）
if printf '%s' "$PROOT_CMDLINE" | grep -qE -- "$PATTERN"; then
  report ok "proot 包裹的服务命令行能被命中"
else
  report bad "proot 包裹的服务命令行命中不了"
fi

echo
if [ "$fail" = 1 ]; then
  echo "结论：❌ 清理残留服务的 pattern 有问题"
  exit 1
fi
echo "结论：✅ pattern 既能清掉残留服务，也不会误杀自己"