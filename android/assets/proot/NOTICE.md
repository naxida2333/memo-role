# 第三方二进制说明

`proot/` 里的文件不是本项目的代码，是从 Termux 官方软件仓库取出的 aarch64 预编译产物，
用于在未 root 的安卓设备上提供一个 Linux 容器（UserLAnd / Andronix 那一类应用也是这么做的）。

| 文件 | 来源包 | 版本 | 许可证 | 装到 App 的哪里 |
| --- | --- | --- | --- | --- |
| `proot` | `proot` | 5.1.107.95 | GPL-2.0-or-later | `usr/bin/proot` |
| `loader` | `proot` | 5.1.107.95 | GPL-2.0-or-later | `usr/libexec/proot/loader` |
| `loader32` | `proot` | 5.1.107.95 | GPL-2.0-or-later | `usr/libexec/proot/loader32` |
| `libtalloc.so.2` | `libtalloc` | 2.4.3 | LGPL-3.0-or-later | `usr/lib/libtalloc.so.2` |
| `libandroid-shmem.so` | `libandroid-shmem` | 0.7 | MIT | `usr/lib/libandroid-shmem.so` |

下载地址（Termux 官方仓库，aarch64）：

```
https://packages.termux.dev/apt/termux-main/pool/main/p/proot/proot_5.1.107.95_aarch64.deb
https://packages.termux.dev/apt/termux-main/pool/main/libt/libtalloc/libtalloc_2.4.3_aarch64.deb
https://packages.termux.dev/apt/termux-main/pool/main/liba/libandroid-shmem/libandroid-shmem_0.7_aarch64.deb
```

上游源码：

- proot：<https://github.com/proot-me/proot>
- talloc：<https://talloc.samba.org/>
- libandroid-shmem：<https://github.com/termux/libandroid-shmem>

## 这些文件各自为什么必需

- **`proot`**：容器本体，ELF 解释器是安卓系统自带的 `/system/bin/linker64`，
  所以能脱离 Termux 直接运行；依赖只有下面两个 `.so`。
- **`loader` / `loader32`**：**不能省**。执行容器里的程序时，内核会拿 ELF 的
  `PT_INTERP`（例如 `/lib/ld-linux-aarch64.so.1`）去**宿主**找解释器，安卓上没有这个
  路径，于是直接 `ENOENT`；proot 靠这个 loader 把它替换成容器内的路径。
  proot 编译时把 loader 的位置写死在 Termux 的
  `/data/data/com.termux/files/usr/libexec/proot/`，所以在别的应用里必须通过
  `PROOT_LOADER` / `PROOT_LOADER_32` 环境变量指到自己的路径 —— 这一点在 `Container.java`
  的 `builder()` 里处理。
  缺了它会报 `execve("/usr/bin/env"): No such file or directory`，看着像容器坏了，
  其实只是少了这个 18 KB 的静态可执行文件。
- **`libtalloc.so.2` / `libandroid-shmem.so`**：proot 的动态依赖，必须通过
  `LD_LIBRARY_PATH` 能被找到。

## 为什么可以随 APK 分发

- 这些都是可自由再分发的许可证（GPL / LGPL / MIT）。
- GPL-2.0 与 LGPL-3.0 要求分发二进制时提供对应源码。上游源码地址见上；
  本项目**未修改**这些二进制，仅原样复制。
- `proot` 与 `libtalloc.so.2` 的 ELF 解释器是安卓系统自带的 `/system/bin/linker64`；
  `loader` 是静态链接（无解释器、无动态依赖），因此都能在应用私有目录下直接执行。

> 容器内的 Ubuntu rootfs 不随 APK 分发，由 App 首次启动时从 Ubuntu 官方
> `cdimage.ubuntu.com` 下载（约 30 MB），因此其许可证与再分发问题由用户
> 按 Ubuntu 的条款自行承担，本项目不分发该内容。
