#!/usr/bin/env bash
#
# 离线校验 TarGz：把同一份 tar.gz 分别交给 TarGz 和系统 tar，逐项对比产物。
#
# 为什么值得单独测
# ----------------
# 解压容器是整个安卓初始化流程的第 3 步（下载 → 解压 → 拉代码 → 装依赖 → 启动），
# 这一步错了后面全卡住，而它在真机上没法调试。TarGz 又是自己手写的（为了绕开
# 安卓 toybox tar 对符号链接 / pax 头的支持差异），所以必须能离线验证。
#
# 覆盖的边界：普通文件、目录、符号链接（相对与绝对）、硬链接、可执行与 setuid 权限、
# 空文件、二进制内容、超长路径（GNU 与 POSIX(pax) 两种打包格式各测一遍）。
#
# 用法：./run.sh       （需要 JDK 11+ 与 python3、GNU tar；不需要安卓 SDK）
#
set -euo pipefail

cd "$(dirname "$0")"

JAVAC="${JAVAC:-javac}"
JAVA="${JAVA:-java}"

for tool in "$JAVAC" "$JAVA" python3 tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "缺少命令：$tool（JDK 11+ / python3 / GNU tar 都是必需的）" >&2
    exit 1
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> 1/3 编译（TarGz + 桌面版 Os 替身）"
mkdir -p "$WORK/out"
"$JAVAC" -encoding UTF-8 -d "$WORK/out" \
  android/system/Os.java \
  ../src/com/memorole/app/TarGz.java \
  Harness.java

echo "==> 2/3 生成测试包"
python3 - "$WORK" <<'PY'
import io, os, sys, tarfile

work = sys.argv[1]

def build(fmt_name, fmt):
    path = os.path.join(work, fmt_name + ".tar.gz")
    with tarfile.open(path, "w:gz", format=fmt) as tf:
        def add(name, data=b"", mode=0o644, type=tarfile.REGTYPE, link=""):
            ti = tarfile.TarInfo(name)
            ti.type = type
            ti.mode = mode
            ti.linkname = link
            if type == tarfile.REGTYPE:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            else:
                tf.addfile(ti)

        add("a", type=tarfile.DIRTYPE, mode=0o755)
        add("a/b", type=tarfile.DIRTYPE, mode=0o755)
        add("a/empty", b"")
        add("a/hello.txt", "你好，容器\n".encode())
        add("a/run.sh", b"#!/bin/sh\necho hi\n", mode=0o755)
        # setuid / setgid：真实 rootfs 里有 12 个这样的文件（passwd、su、mount…），
        # 权限位丢了就会与系统 tar 的结果不一致
        add("a/setuid", b"binary\n", mode=0o4755)
        add("a/setgid", b"binary\n", mode=0o2755)
        # 二进制内容：确保不会按文本处理而截断
        add("a/binary.bin", bytes(range(256)) * 8)
        # 相对与绝对符号链接（usrmerge 的 /bin 就属于前者）
        add("a/rel-link", type=tarfile.SYMTYPE, link="hello.txt")
        add("a/abs-link", type=tarfile.SYMTYPE, link="/etc/hostname")
        # 硬链接：TarGz 在硬链接失败时会退化成复制，内容仍需一致
        add("a/hard-link", type=tarfile.LNKTYPE, link="a/hello.txt")
        # 超长路径：GNU 用 'L' 条目，POSIX 用 'x'(pax) 条目 —— 两条分支都要覆盖
        add("a/" + ("很长的目录名" * 12) + ".txt", b"long path\n")
        add("a/emptydir", type=tarfile.DIRTYPE, mode=0o700)

    print(f"    {fmt_name}.tar.gz（{os.path.getsize(path)} 字节）")

build("gnu", tarfile.GNU_FORMAT)
build("pax", tarfile.PAX_FORMAT)
PY

echo "==> 3/3 解压并逐项对比"
for fmt in gnu pax; do
  mkdir -p "$WORK/mine-$fmt" "$WORK/ref-$fmt"
  "$JAVA" -cp "$WORK/out" Harness "$WORK/$fmt.tar.gz" "$WORK/mine-$fmt" >/dev/null
  tar -xzf "$WORK/$fmt.tar.gz" -C "$WORK/ref-$fmt"
done

python3 - "$WORK" <<'PY'
import hashlib, os, stat, sys

work = sys.argv[1]

def scan(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ["link", os.readlink(full), None, st.st_ino]
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ["dir", None, stat.S_IMODE(st.st_mode), st.st_ino]
            elif stat.S_ISREG(st.st_mode):
                h = hashlib.md5()
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                out[rel] = ["file", h.hexdigest(), stat.S_IMODE(st.st_mode), st.st_ino]
            else:
                out[rel] = ["other", None, stat.S_IMODE(st.st_mode), st.st_ino]
    return out

failed = False
for fmt in ("gnu", "pax"):
    mine = scan(os.path.join(work, "mine-" + fmt))
    ref = scan(os.path.join(work, "ref-" + fmt))
    problems = []

    for p in sorted(set(ref) - set(mine)):
        problems.append(f"漏掉条目: {p}")
    for p in sorted(set(mine) - set(ref)):
        problems.append(f"多出条目: {p}")
    for p in sorted(set(mine) & set(ref)):
        if mine[p][0] != ref[p][0]:
            problems.append(f"类型不符: {p} 系统={ref[p][0]} 我们={mine[p][0]}")
            continue
        if mine[p][0] == "link" and mine[p][1] != ref[p][1]:
            problems.append(f"链接目标不符: {p} 系统={ref[p][1]} 我们={mine[p][1]}")
        if mine[p][0] == "file" and mine[p][1] != ref[p][1]:
            problems.append(f"内容不符: {p}")
        if mine[p][0] in ("file", "dir") and mine[p][2] != ref[p][2]:
            problems.append(f"权限不符: {p} 系统={oct(ref[p][2])} 我们={oct(mine[p][2])}")

    # 硬链接两边都必须与 a/hello.txt 共用 inode
    for label, tree in (("系统", ref), ("我们", mine)):
        a, b = tree.get("a/hello.txt"), tree.get("a/hard-link")
        if a and b and a[3] != b[3]:
            problems.append(f"硬链接未共用 inode（{label}）")

    if problems:
        failed = True
        print(f"    {fmt}: ❌ {len(problems)} 处不一致")
        for line in problems[:15]:
            print("        " + line)
    else:
        print(f"    {fmt}: ✅ 与系统 tar 一致（{len(mine)} 个条目，链接目标/内容/权限/硬链接全对）")

print("\n结论：" + ("❌ TarGz 与系统 tar 存在差异，安卓上解压容器会出错" if failed else "✅ TarGz 可用于解压容器"))
sys.exit(1 if failed else 0)
PY
