# 第三方二进制说明

`proot/` 里的三个文件不是本项目的代码，是从 Termux 官方软件仓库取出的 aarch64 预编译产物，
用于在未 root 的安卓设备上提供一个 Linux 容器（UserLAnd / Andronix 那一类应用也是这么做的）。

| 文件 | 来源包 | 版本 | 许可证 |
| --- | --- | --- | --- |
| `proot` | `proot` | 5.1.107.95 | GPL-2.0-or-later |
| `libtalloc.so.2` | `libtalloc` | 2.4.3 | LGPL-3.0-or-later |
| `libandroid-shmem.so` | `libandroid-shmem` | 0.7 | MIT |

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

## 为什么可以随 APK 分发

- 这三个都是可自由再分发的许可证（GPL / LGPL / MIT）。
- GPL-2.0 与 LGPL-3.0 要求分发二进制时提供对应源码。上游源码地址见上；
  本项目**未修改**这些二进制，仅原样复制。
- `proot` 与 `libtalloc.so.2` 的 ELF 解释器是安卓系统自带的 `/system/bin/linker64`，
  这也正是它们能脱离 Termux 直接运行的原因（依赖只有 `libtalloc`、`libandroid-shmem`
  以及系统的 `libc` / `liblog`）。

> 容器内的 Ubuntu rootfs 不随 APK 分发，由 App 首次启动时从 Ubuntu 官方
> `cdimage.ubuntu.com` 下载（约 30 MB），因此其许可证与再分发问题由用户
> 按 Ubuntu 的条款自行承担，本项目不分发该内容。
