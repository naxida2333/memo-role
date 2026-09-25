# 本地推理引擎（llama.cpp / llama-server）

本目录里的文件来自 llama.cpp 的官方预编译发布包，随 APK 一起分发，
用于在容器的 `/usr/local/lib/llama` 下运行本地 GGUF 模型。

| 项 | 值 |
| --- | --- |
| 上游项目 | https://github.com/ggml-org/llama.cpp |
| 发布版本 | b11191 |
| 下载地址 | https://github.com/ggml-org/llama.cpp/releases/download/b11191/llama-b11191-bin-ubuntu-arm64.tar.gz |
| 压缩包 sha256 | `ce12531c1e1f3ab2a8fa3305731a103de792449a420698d74253c56e73bcb34f` |
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

- `ld-linux-aarch64.so.1`
- `libc.so.6`
- `libm.so.6`
- `libdl.so.2`
- `libpthread.so.0`
- `librt.so.1`
- `libstdc++.so.6`
- `libgcc_s.so.1`
- `libgomp.so.1`
- `libssl.so.3`
- `libcrypto.so.3`

容器里的这些库由 `assets/bootstrap.sh` 用 apt 补齐（`libgomp1` 等），
Ubuntu 24.04 base 镜像本身不含 `libgomp.so.1`。
