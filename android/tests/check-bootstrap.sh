#!/usr/bin/env bash
#
# 校验初始化脚本里「补齐系统库」这一步的两条命脉：**判断得准**，以及判断失误时
# **不会被放大**成整体失败。
#
# 为什么值得单独钉住
# ----------------
# 真机上踩过：容器里 apt 明明把 libgomp1 装上了，日志却写「装不上」，紧接着整个
# 初始化失败 —— 连后面的「安装 Python 依赖」都没跑，安卓侧只看到一句
# 「依赖安装失败（退出码 1）」。两个原因叠在一起：
#
#   1. 判断方式不可靠：用 `ls` 按路径去找 .so。容器跑在 proot 下，proot 要改写
#      每次系统调用里的路径，这种「按路径 stat/ls」的探测会误判 —— 实测同一路径
#      `ls` 报 No such file，`ldconfig -p` 却能查到它；
#   2. 判断失误被放大：ensure_lib 返回 1，被脚本开头的 `set -e` 抓住，于是
#      「一个可选库没装上」升级成「整个初始化失败」—— 而它只该影响本地模型。
#
# 这里不复制一份逻辑来测（那样测的是副本），而是把 bootstrap.sh 里**真实的**
# 函数与自检块抽出来，配上假的 ldconfig / apt-get 跑一遍。
#
# 用法：./check-bootstrap.sh      （离线，不需要安卓 SDK，也不需要容器）
#
set -euo pipefail
cd "$(dirname "$0")"

BOOTSTRAP="../assets/bootstrap.sh"
fail=0
report() {
  if [ "$1" = bad ]; then echo "❌ $2"; fail=1; else echo "✅ $2"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/probe/lib" "$WORK/probe/bin"

# 用桩替掉 ldconfig / apt-get：LD_OK=1 表示链接器缓存里有库，APT_RC 控制 apt 成败
cat > "$WORK/bin/ldconfig" <<'STUB'
#!/bin/sh
# 只模拟 `ldconfig -p`
if [ "${1:-}" = "-p" ]; then
  echo "ldconfig -p" >> "${LD_LOG:-/dev/null}"
  [ "${LD_OK:-0}" = "1" ] \
    && printf '\tlibgomp.so.1 (libc6,AArch64) => /lib/aarch64-linux-gnu/libgomp.so.1\n'
fi
exit 0
STUB
cat > "$WORK/bin/apt-get" <<'STUB'
#!/bin/sh
echo "apt-get $*" >> "${APT_LOG:-/dev/null}"
[ "${APT_RC:-0}" = "0" ] || echo "E: 安装失败（桩）" >&2
exit "${APT_RC:-0}"
STUB
chmod +x "$WORK/bin/ldconfig" "$WORK/bin/apt-get"

#------------------------------------------------------------------------
echo "== 1/3 抽出真实的 have_lib / ensure_lib 并检查判断方式"
#------------------------------------------------------------------------
sed -n '/^have_lib()/,/^}/p;/^ensure_lib()/,/^}/p' "$BOOTSTRAP" > "$WORK/funcs.sh"
# 不在这里 exit：两处缺陷（判断方式、失败后果）都报出来，才能一次看清全貌
if grep -q 'ldconfig -p' "$WORK/funcs.sh"; then
  report ok "have_lib 优先问 ldconfig -p"
else
  report bad "have_lib 没有问 ldconfig（proot 下按路径探测会误判，真机踩过）"
fi
if grep -qE '^\s+ls .*\$so' "$WORK/funcs.sh"; then
  report bad "have_lib 用 ls 探路径（proot 下报 No such file，真机踩过）"
else
  report ok "不再按路径 ls"
fi

#------------------------------------------------------------------------
echo "== 2/3 判断准确性与失败后果"
#------------------------------------------------------------------------
# A. 链接器认账 → 不该再去装（并且必须真的问过链接器，而不是撞上宿主自己的库）
: > "$WORK/apt.log"; : > "$WORK/ld.log"
set +e
( export PATH="$WORK/bin:$PATH" LD_OK=1 LD_LOG="$WORK/ld.log" APT_LOG="$WORK/apt.log"
  set -euo pipefail; . "$WORK/funcs.sh"
  ensure_lib libgomp.so.1 libgomp1 ) > "$WORK/out-a.txt" 2>&1
rc_a=$?
set -e
if [ "$rc_a" = 0 ] && grep -q '已有，跳过' "$WORK/out-a.txt" \
   && [ ! -s "$WORK/apt.log" ] && [ -s "$WORK/ld.log" ]; then
  report ok "链接器缓存里有 libgomp.so.1 时直接跳过，没去动 apt"
else
  report bad "ldconfig 认账却仍然去装（rc=$rc_a，ldconfig 调用=$([ -s "$WORK/ld.log" ] && echo 有 || echo 无)）：$(head -2 "$WORK/out-a.txt" | tr '\n' ' ')"
fi

# B. 库不在（用一个宿主上肯定没有的 soname）、apt 也装不上 → 必须只警告，
#    且**不能**把脚本带走（真机故障的根因）
set +e
( export PATH="$WORK/bin:$PATH" LD_OK=0 APT_RC=1 APT_LOG="$WORK/apt.log"
  set -euo pipefail; . "$WORK/funcs.sh"
  ensure_lib libnosuch.so.9 package-that-does-not-exist
  echo "后续步骤照常执行" ) > "$WORK/out-b.txt" 2>&1
rc_b=$?
set -e
if [ "$rc_b" = 0 ] && grep -q '装不上' "$WORK/out-b.txt" \
   && grep -q '后续步骤照常执行' "$WORK/out-b.txt"; then
  report ok "装不上时只警告、不中断（后续步骤仍会执行）"
else
  report bad "装不上时把脚本带走了（rc=$rc_b）——真机上就是「初始化失败」"
fi

# C. apt 自己的报错要看得见（曾经被 >/dev/null 吞掉，只剩一句「装不上」）
if grep -q '安装失败（桩）' "$WORK/out-b.txt"; then
  report ok "apt 的真实报错被带进日志（否则无从判断为什么装不上）"
else
  report bad "apt 的报错被吞了，日志里只剩「装不上」"
fi

#------------------------------------------------------------------------
echo "== 3/3 自检结果落盘（首页状态据此显示「已就绪 / 起不来」）"
#------------------------------------------------------------------------
# 取出真实的试跑块，只把路径换到临时目录
sed -n '/^SELFTEST=/,/^echo "==> 依赖安装完成"/p' "$BOOTSTRAP" | sed '$d' \
  | sed "s#/usr/local/lib/llama#$WORK/probe/lib#g;s#/usr/local/bin/llama-server#$WORK/probe/bin/llama-server#g" \
  > "$WORK/selftest.sh"
if ! grep -q 'SELFTEST' "$WORK/selftest.sh"; then
  report bad "没抽出试跑块（bootstrap.sh 里 SELFTEST= 那段被改动了？）"
fi

selftest_case() {
  # $1=二进制内容（空串表示不放二进制） $2=期望的 SELFTEST 前缀
  rm -f "$WORK/probe/bin/llama-server" "$WORK/probe/lib/SELFTEST"
  if [ -n "$1" ]; then
    printf '%s\n' "$1" > "$WORK/probe/bin/llama-server"
    chmod 755 "$WORK/probe/bin/llama-server"
  fi
  set +e
  ( set -euo pipefail; . "$WORK/selftest.sh" ) > "$WORK/out-self.txt" 2>&1
  local rc=$?
  set -e
  if [ "$rc" != 0 ]; then
    report bad "试跑块自身失败了（rc=$rc）：$(head -2 "$WORK/out-self.txt" | tr '\n' ' ')"
    return
  fi
  local got=""
  [ -f "$WORK/probe/lib/SELFTEST" ] && got="$(cat "$WORK/probe/lib/SELFTEST")"
  case "$2" in
    ok)   case "$got" in ok\ *)   report ok "能跑起来 → SELFTEST=$got" ;; *) report bad "应为 ok，实际「$got」" ;; esac ;;
    fail) case "$got" in fail\ *) report ok "起不来 → SELFTEST=$got" ;; *) report bad "应为 fail，实际「$got」" ;; esac ;;
    none) [ -z "$got" ] && report ok "二进制不在 → 不留下 SELFTEST（不会拿旧结果冒充）" \
                        || report bad "二进制不在却留下 SELFTEST=「$got」" ;;
  esac
}

selftest_case '#!/bin/sh
echo "version: probe-9.9"' ok
selftest_case '#!/bin/sh
echo "llama-server: error while loading shared libraries: libgomp.so.1: cannot open shared object file"
exit 127' fail
selftest_case '' none

echo
if [ "$fail" = 0 ]; then
  echo "结论：✅ 判断方式与失败后果都对（装不上只影响本地模型，不会带崩初始化）"
else
  echo "结论：❌ 初始化脚本存在会让真机安装失败的问题"
fi
exit "$fail"