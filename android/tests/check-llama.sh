#!/usr/bin/env bash
#
# 校验随 APK 分发的 llama-server（本地推理引擎）是否齐全、形态是否正确。
#
# 为什么需要它
# ------------
# 和 proot 那次「漏一个 loader 就全盘哑火」是同一类问题：llama.cpp 的官方包把
# 二进制与一堆 .so 放在同目录、靠 $ORIGIN 互相找，少任何一个都只会在**手机上**
# 表现为一句 `error while loading shared libraries`。这类错误在真机上才暴露，
# 代价很高，所以在这里逐项钉死：
#
#   1. 与仓库里的 vendor 包（llama-*-ubuntu-arm64.tar.gz）逐个文件对齐；
#   2. 关键文件齐全（可执行文件、库、版本、许可）；
#   3. 架构是 aarch64、解释器路径能在容器里找到；
#   4. 全是实体文件（APK 的 assets 不保留符号链接）；
#   5. 动态依赖闭包完整（除了容器负责提供的系统库，其余都必须在目录里）；
#   6. 二进制要求的 glibc 版本不超过容器自带的 glibc。
#
# 用法：
#   ./check-llama.sh                    # 只看 APK 侧（离线，不需要安卓 SDK）
#   ./check-llama.sh /path/to/rootfs    # 额外验证系统库确实在那个 rootfs 里
#
set -euo pipefail

cd "$(dirname "$0")/.."   # android/
DIR=assets/llama
ROOTFS="${1:-}"
VENDOR="$(ls -1 vendor/llama-*-ubuntu-arm64.tar.gz 2>/dev/null | tail -1 || true)"

# 容器（Ubuntu 24.04 基础镜像 + bootstrap.sh 装的包）负责提供的库，不随 APK 分发。
# 这份清单要与 tools/vendor_llama.py 里的 SYSTEM_LIBS 保持一致。
SYSTEM_LIBS="ld-linux-aarch64.so.1 libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0
             librt.so.1 libstdc++.so.6 libgcc_s.so.1 libgomp.so.1 libssl.so.3 libcrypto.so.3"

#: Ubuntu 24.04 自带 glibc 2.39；容器就是这个版本，二进制不能要求更高。
MAX_GLIBC="2.39"

fail=0
report() {
  if [ "$1" = bad ]; then echo "❌ $2"; fail=1; else echo "✅ $2"; fi
}

if [ ! -d "$DIR" ]; then
  echo "❌ 缺少 $DIR —— 先执行：python3 tools/vendor_llama.py"
  exit 1
fi

echo "== 1/6 与仓库里的 vendor 包对齐"
if [ -z "$VENDOR" ]; then
  report bad "仓库里没有 vendor/llama-*-ubuntu-arm64.tar.gz（无法确认这些文件是从哪来的）"
else
  # 目录里的文件必须与包内完全一致（名字 + 大小），防止手工改过或解压不全
  drift="$(python3 - "$VENDOR" "$DIR" <<'PY'
import os, sys, tarfile
tar_path, dir_path = sys.argv[1], sys.argv[2]
with tarfile.open(tar_path) as tar:
    packed = {m.name.split("/")[-1]: m.size for m in tar.getmembers() if m.isfile()}
actual = {n: os.path.getsize(os.path.join(dir_path, n)) for n in os.listdir(dir_path)}
problems = []
for name in sorted(set(packed) - set(actual)):
    problems.append(f"{name}（包里有、目录里没有）")
for name in sorted(set(actual) - set(packed)):
    problems.append(f"{name}（目录里有、包里没有）")
for name in sorted(set(actual) & set(packed)):
    if actual[name] != packed[name]:
        problems.append(f"{name}（大小不符：包 {packed[name]} / 目录 {actual[name]}）")
print("; ".join(problems))
PY
)"
  if [ -n "$drift" ]; then
    report bad "$(basename "$VENDOR") 与 $DIR 不一致：$drift"
  else
    report ok "$(basename "$VENDOR") 与 $DIR 文件一致"
  fi
fi

echo "== 2/6 关键文件是否齐全"
for f in llama-server VERSION LICENSE NOTICE.md; do
  [ -f "$DIR/$f" ] || report bad "缺少 $DIR/$f"
done
[ -x "$DIR/llama-server" ] || report bad "$DIR/llama-server 没有可执行权限（打进 APK 后拷贝出来也不能跑）"
# ggml 启动时按 CPU 指令集 dlopen 一个变体库，一个都没有就等于没有 CPU 后端
if ls "$DIR"/libggml-cpu-*.so >/dev/null 2>&1; then
  report ok "关键文件齐全（$(ls "$DIR" | wc -l) 个文件，含 $(ls "$DIR"/libggml-cpu-*.so | wc -l) 个 CPU 变体）"
else
  report bad "缺少 libggml-cpu-*.so（ggml 找不到 CPU 后端，llama-server 起不来）"
fi

echo "== 3/6 架构与解释器"
for f in "$DIR"/*; do
  case "$(basename "$f")" in
    VERSION|LICENSE|NOTICE.md) continue ;;
  esac
  if readelf -h "$f" 2>/dev/null | grep -q "Machine:.*AArch64"; then
    :
  else
    report bad "$(basename "$f") 不是 aarch64（手机上跑不了）"
  fi
done
if readelf -l "$DIR/llama-server" 2>/dev/null | grep -q '/lib/ld-linux-aarch64.so.1'; then
  report ok "llama-server 的解释器是容器内的 /lib/ld-linux-aarch64.so.1"
else
  report bad "llama-server 的解释器不是 /lib/ld-linux-aarch64.so.1（容器里找不到就会 ENOENT）"
fi

echo "== 4/6 必须是实体文件"
# APK 的 assets 不保留符号链接：官方包里的 libllama.so -> libllama.so.0 -> ... 
# 若这里出现符号链接，装进 APK 后要么丢文件、要么变成指向不存在目标的死链
links="$(find "$DIR" -type l -printf '%f ' 2>/dev/null || true)"
if [ -n "$links" ]; then
  report bad "存在符号链接：$links（APK assets 不保留，请用 tools/vendor_llama.py 重新生成）"
else
  report ok "目录里全是实体文件"
fi

echo "== 5/6 动态依赖闭包"
missing=""
for f in "$DIR"/*; do
  case "$(basename "$f")" in
    VERSION|LICENSE|NOTICE.md) continue ;;
  esac
  for lib in $(readelf -d "$f" 2>/dev/null | sed -n 's/.*NEEDED.*\[\(.*\)\]/\1/p'); do
    case " $SYSTEM_LIBS " in
      *" $lib "*) continue ;;   # 容器提供
    esac
    [ -f "$DIR/$lib" ] || missing="$missing $lib(<- $(basename "$f"))"
  done
done
if [ -n "$missing" ]; then
  report bad "这些依赖既没随包分发、也不在系统库白名单里：$missing"
else
  report ok "所有动态依赖都能落地：随包分发或由容器提供"
fi
# llama-server 靠 $ORIGIN 找同目录的库，RUNPATH 丢了就会去系统路径找而找不到
if readelf -d "$DIR/llama-server" 2>/dev/null | grep -q 'RUNPATH.*\$ORIGIN'; then
  report ok "llama-server 的 RUNPATH 含 \$ORIGIN（会从自己所在目录找库）"
else
  report bad "llama-server 的 RUNPATH 不含 \$ORIGIN"
fi

echo "== 6/6 glibc 版本"
need="$(for f in "$DIR"/*; do
          case "$(basename "$f")" in VERSION|LICENSE|NOTICE.md) continue ;; esac
          readelf -V "$f" 2>/dev/null | grep -oE 'GLIBC_[0-9]+\.[0-9]+' || true
        done | sed 's/GLIBC_//' | sort -t. -k1,1n -k2,2n | tail -1)"
if [ -z "$need" ]; then
  report bad "读不到 glibc 符号版本（readelf -V 没输出？）"
else
  if [ "$(printf '%s\n%s\n' "$need" "$MAX_GLIBC" | sort -t. -k1,1n -k2,2n | tail -1)" = "$MAX_GLIBC" ]; then
    report ok "最高要求 GLIBC_$need ≤ 容器自带 $MAX_GLIBC"
  else
    report bad "最高要求 GLIBC_$need > 容器自带 $MAX_GLIBC（换内核/发行版底包，或换一个更老的 llama.cpp 构建）"
  fi
fi

if [ -n "$ROOTFS" ]; then
  echo "== 附加：逐个确认系统库在 $ROOTFS 里存在"
  [ -d "$ROOTFS" ] || { report bad "$ROOTFS 不是目录"; }
  for lib in $SYSTEM_LIBS; do
    found="$(find "$ROOTFS/usr/lib" "$ROOTFS/lib" -name "$lib" -print -quit 2>/dev/null || true)"
    if [ -n "$found" ]; then
      report ok "$lib（${found#"$ROOTFS"}）"
    else
      report bad "$lib 不在这个 rootfs 里 —— bootstrap.sh 要负责装上它"
    fi
  done
fi

echo
if [ "$fail" = 1 ]; then
  echo "结论：❌ 本地推理引擎有问题，装到手机上会起不来"
  exit 1
fi
echo "结论：✅ 本地推理引擎齐备且形态正确"
