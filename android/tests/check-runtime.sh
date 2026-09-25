#!/usr/bin/env bash
#
# 校验随 APK 分发的 proot 运行时是否齐全、形态是否正确。
#
# 为什么需要它
# ------------
# proot 官方包装的不止一个可执行文件。我最初只取了 bin/proot 与两个 .so，
# 漏掉了 usr/libexec/proot/loader —— 结果是 App 在前 5 步全部成功、第 6 步进容器
# 时只报一句 `execve("/usr/bin/env"): No such file or directory`，看着像容器坏了。
# 这类「少一个文件」的错误肉眼很难发现，所以在这里逐项钉死。
#
# 用法：./check-runtime.sh       （离线，不需要安卓 SDK）
#
set -euo pipefail

cd "$(dirname "$0")/.."   # android/
DIR=assets/proot
fail=0

# proot 包安装的可执行/库文件，一个都不能少
EXPECTED=(proot loader loader32 libtalloc.so.2 libandroid-shmem.so)
# 系统提供的库，不需要随包分发
SYSTEM_LIBS="libc.so liblog.so libm.so libdl.so libstdc++.so libandroid.so"

report() {
  if [ "$1" = "bad" ]; then
    echo "❌ $2"
    fail=1
  else
    echo "✅ $2"
  fi
}

echo "== 1/3 文件是否齐全"
for f in "${EXPECTED[@]}"; do
  if [ -f "$DIR/$f" ]; then
    report ok "$f"
  else
    report bad "缺少 $DIR/$f（proot 少了任何一个都无法工作）"
  fi
done
[ "$fail" = 1 ] && { echo; echo "结论：❌ 运行时文件不全"; exit 1; }

echo "== 2/3 架构是否正确"
# loader32 是给 32 位 guest 用的 ARM 变体，其余都是 aarch64
expected_arch() {
  case "$1" in
    loader32) echo ARM ;;
    *) echo AArch64 ;;
  esac
}
for f in "${EXPECTED[@]}"; do
  want="$(expected_arch "$f")"
  if readelf -h "$DIR/$f" 2>/dev/null | grep -q "Machine:.*$want"; then
    report ok "$f 是 $want"
  else
    report bad "$f 架构不是 $want（换设备架构时要重新取包）"
  fi
done

echo "== 3/3 能否在应用私有目录里直接执行"
# 两个 loader 会被直接 exec：必须是静态链接。若依赖宿主的 ld-linux，
# 在安卓上找不到解释器，仍会以 ENOENT 失败。
for f in loader loader32; do
  if readelf -l "$DIR/$f" 2>/dev/null | grep -qi 'interpreter'; then
    report bad "$f 是动态链接的（安卓上执行会失败）"
  else
    report ok "$f 静态链接，可直接执行"
  fi
done

# proot 自身的解释器必须是系统 linker，否则脱离 Termux 跑不起来
if readelf -l "$DIR/proot" 2>/dev/null | grep -q '/system/bin/linker64'; then
  report ok "proot 使用系统 /system/bin/linker64"
else
  report bad "proot 的解释器不是系统 linker（可能绑定了 Termux 的路径）"
fi

# proot 的动态依赖必须都能在包里找到
missing_deps=""
for lib in $(readelf -d "$DIR/proot" 2>/dev/null | sed -n 's/.*NEEDED.*\[\(.*\)\]/\1/p'); do
  case " $SYSTEM_LIBS " in
    *" $lib "*) continue ;;
  esac
  [ -f "$DIR/$lib" ] || missing_deps="$missing_deps $lib"
done
if [ -n "$missing_deps" ]; then
  report bad "proot 的动态依赖未随包分发：$missing_deps"
else
  report ok "proot 的动态依赖齐备"
fi

echo
if [ "$fail" = 1 ]; then
  echo "结论：❌ 运行时有问题，构建出的 APK 起不了容器"
  exit 1
fi
echo "结论：✅ 运行时齐备且形态正确"
