#!/usr/bin/env python3
"""把 llama.cpp 的官方发布包裁剪成「只够跑 llama-server」的一份，供 APK 内置。

为什么要裁剪
------------
官方 ubuntu-arm64 发布包约 32 MB，里面除了 ``llama-server`` 还有 ``llama-cli`` /
``llama-bench`` / ``llama-quantize`` / RPC 等一堆用不上的东西。手机上只需要推理服务，
所以这里只保留**动态链接闭包**能覆盖到的文件：从 ``llama-server`` 出发按 ``DT_NEEDED``
一路展开，再加上 ggml 运行时会 ``dlopen`` 的 CPU 变体库。32 MB → 约 27 MB 原始、
约 12 MB 压缩。

为什么改名
----------
官方包里 ``libllama.so`` 是符号链接，链接链是
``libllama.so -> libllama.so.0 -> libllama.so.0.5.0``。安卓的 APK assets **不保留
符号链接**，所以这里统一按 ``DT_NEEDED`` / ``dlopen`` 真正使用的那个名字（SONAME）
落地成实体文件，进 APK 后名字就是对的。

用法
----
    python3 tools/vendor_llama.py                      # 下载默认版本再处理
    python3 tools/vendor_llama.py --archive /tmp/llama-ubuntu-arm64.tar.gz --tag b11191
    python3 tools/vendor_llama.py --from-dir /tmp/llama/llama-b11191 --tag b11191

产物
----
    vendor/llama-<tag>-ubuntu-arm64.tar.gz   # 进仓库的成品（含 LICENSE / NOTICE / VERSION）
    assets/llama/                            # 解出来的文件，构建 APK 时打进包（.gitignore）
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_TAG = "b11191"
DEFAULT_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/{tag}/"
    "llama-{tag}-bin-ubuntu-arm64.tar.gz"
)

#: 容器（Ubuntu 基础镜像 + bootstrap 装的包）负责提供的库，不随 APK 分发。
#: 这份清单同时是 bootstrap.sh 的「要装哪些包」的依据，也是校验脚本的白名单。
SYSTEM_LIBS = [
    "ld-linux-aarch64.so.1",
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "libpthread.so.0",
    "librt.so.1",
    "libstdc++.so.6",
    "libgcc_s.so.1",
    "libgomp.so.1",
    "libssl.so.3",
    "libcrypto.so.3",
]

#: 入口可执行文件（唯一需要随包分发的程序本体）。
ENTRY = "llama-server"

#: ggml 会在运行时按 CPU 特性 dlopen 这些文件，静态分析看不到，必须显式带上。
RUNTIME_DLOPEN_GLOBS = ["libggml-cpu-*.so"]

HERE = Path(__file__).resolve().parent          # android/tools
ANDROID = HERE.parent                           # android/


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def elf_needed(path: Path) -> list[str]:
    """读某个 ELF 的 DT_NEEDED 列表。"""
    out = run(["readelf", "-d", str(path)])
    libs = []
    for line in out.splitlines():
        if "(NEEDED)" in line and "[" in line:
            libs.append(line.split("[", 1)[1].split("]", 1)[0])
    return libs


def resolve(root: Path, name: str) -> Path | None:
    """在解出的包里按文件名找人（跟着符号链接走到实体文件）。"""
    p = root / name
    if not p.exists():
        return None
    return p.resolve()


def collect(root: Path) -> list[tuple[str, Path]]:
    """算出要随包分发的文件：{落地名: 源文件}。

    BFS 展开 ``DT_NEEDED``；`llama-server` 自己用原名落地。
    """
    picked: dict[str, Path] = {}
    queue = [ENTRY]
    while queue:
        name = queue.pop(0)
        if name in picked or name in SYSTEM_LIBS:
            continue
        src = resolve(root, name)
        if src is None:
            sys.exit(f"上游包里找不到 {name}（可能发布包结构与预期不符）")
        picked[name] = src
        for needed in elf_needed(src):
            if needed not in picked and needed not in SYSTEM_LIBS:
                queue.append(needed)
    # 运行时 dlopen 的 CPU 变体
    for pattern in RUNTIME_DLOPEN_GLOBS:
        for src in sorted(root.glob(pattern)):
            picked.setdefault(src.name, src.resolve())
    return sorted(picked.items())


NOTICE_TEMPLATE = """# 本地推理引擎（llama.cpp / llama-server）

本目录里的文件来自 llama.cpp 的官方预编译发布包，随 APK 一起分发，
用于在容器的 `/usr/local/lib/llama` 下运行本地 GGUF 模型。

| 项 | 值 |
| --- | --- |
| 上游项目 | https://github.com/ggml-org/llama.cpp |
| 发布版本 | {tag} |
| 下载地址 | {url} |
| 压缩包 sha256 | `{sha256}` |
| 许可协议 | MIT（见同目录 `LICENSE`，版权归 ggml 组织及贡献者） |
| 目标平台 | linux/arm64（aarch64），glibc ≥ 2.38 |

## 只取了运行 llama-server 需要的文件

官方包约 32 MB，除了服务端还有 `llama-cli`、`llama-bench`、`llama-quantize`、
RPC 等，手机上都用不到。这里通过「从 `llama-server` 出发按 `DT_NEEDED` 展开、
再加上 ggml 运行时 dlopen 的 CPU 变体库」裁剪出最小闭包，因此：

- 目录里没有 `llama-cli` 等命令行工具，不是漏打包；
- `libllama.so.0` / `libggml.so.0` 这类是实体文件而非符号链接 —— 安卓的 APK
  assets 不保留符号链接，所以按 SONAME 落地，加载结果与官方包一致；
- 八个 `libggml-cpu-armv8.*.so` 变体都保留：ggml 启动时按当前 CPU 指令集挑一个
  dlopen，少一个就可能在部分机型上挑不到。

重新生成（换版本时）：`python3 tools/vendor_llama.py --tag <新版本>`

## 依赖的容器内系统库（不随包分发）

{system_libs}

容器里的这些库由 `assets/bootstrap.sh` 用 apt 补齐（`libgomp1` 等），
Ubuntu 24.04 base 镜像本身不含 `libgomp.so.1`。
"""


def write_member(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    ap = argparse.ArgumentParser(description="裁剪并内置 llama-server")
    ap.add_argument("--tag", default=DEFAULT_TAG, help=f"llama.cpp 发布版本号（默认 {DEFAULT_TAG}）")
    ap.add_argument("--url", default="", help="发布包地址（默认按 tag 拼官方 GitHub 地址）")
    ap.add_argument("--archive", default="", help="已下载的发布包（跳过下载）")
    ap.add_argument("--from-dir", default="", help="已解压的发布包目录（跳过下载与解压）")
    ap.add_argument("--out", default="", help="产出的 vendor tar.gz 路径")
    args = ap.parse_args()

    url = args.url or DEFAULT_URL.format(tag=args.tag)
    out_path = Path(args.out) if args.out else ANDROID / "vendor" / f"llama-{args.tag}-ubuntu-arm64.tar.gz"
    assets_dir = ANDROID / "assets" / "llama"

    tmp = Path(tempfile.mkdtemp(prefix="vendor-llama-"))
    try:
        if args.from_dir:
            root = Path(args.from_dir)
            digest = "(取自本地目录，未核验压缩包)"
        else:
            if args.archive:
                archive = Path(args.archive)
                print(f"==> 使用本地发布包 {archive}")
            else:
                archive = tmp / "llama.tar.gz"
                print(f"==> 下载 {url}")
                with urllib.request.urlopen(url) as resp, archive.open("wb") as fh:
                    shutil.copyfileobj(resp, fh)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            print(f"    sha256 {digest}")
            print("==> 解压")
            with tarfile.open(archive) as tar:
                tar.extractall(tmp / "src", filter="data")
            children = [p for p in (tmp / "src").iterdir() if p.is_dir()]
            root = children[0] if len(children) == 1 else tmp / "src"

        entries = collect(root)
        total = sum(src.stat().st_size for _, src in entries)
        print(f"==> 需要 {len(entries)} 个文件，原始 {total / 1048576:.1f} MB")

        # 先摊到 assets/llama（构建 APK 时用），已是实体文件
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
        assets_dir.mkdir(parents=True)
        for name, src in entries:
            shutil.copyfile(src, assets_dir / name)
            os.chmod(assets_dir / name, 0o755)

        version = f"{args.tag}\n"
        (assets_dir / "VERSION").write_text(version, encoding="utf-8")
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        (assets_dir / "LICENSE").write_text(license_text, encoding="utf-8")
        notice = NOTICE_TEMPLATE.format(
            tag=args.tag,
            url=url,
            sha256=digest,
            system_libs="\n".join(f"- `{lib}`" for lib in SYSTEM_LIBS),
        )
        (assets_dir / "NOTICE.md").write_text(notice, encoding="utf-8")
        # 再打成一个可复现的 tar.gz 进仓库：解出来就是 assets/llama
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 在仓库里另留一份可读的副本：压缩包里的那份在 GitHub 上翻不到，
        # 而「随包分发的第三方二进制来自哪、什么许可」应该能直接 review
        (out_path.parent / "llama-NOTICE.md").write_text(notice, encoding="utf-8")

        with tarfile.open(out_path, "w:gz") as tar:
            for path in sorted(assets_dir.iterdir()):
                data = path.read_bytes()
                write_member(tar, path.name, data, 0o644 if path.name.endswith((".md", ".txt")) else 0o755)
        out_mb = out_path.stat().st_size / 1048576
        print(f"==> 写出 {out_path.relative_to(ANDROID.parent)}（{out_mb:.1f} MB）")
        print(f"==> 展开到 {assets_dir.relative_to(ANDROID.parent)}")
        print("==> 下一步：android/tests/check-llama.sh 校验")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
