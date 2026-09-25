package android.system;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 桌面 JVM 上的 android.system.Os 替身，只用于离线测试 TarGz。
 *
 * <p>TarGz 只用到三个系统调用，把它们的语义映射到 Java NIO，就能让同一份
 * TarGz.java 在桌面 JVM 上运行 —— 从而拿真实/合成的 tar.gz 与 GNU tar 对比。
 * 真机上跑的是系统提供的 Os，这个类不会进入 APK（测试脚本单独编译它）。
 */
public final class Os {

    private Os() {
    }

    public static void chmod(String path, int mode) throws IOException {
        Path p = Paths.get(path);
        if (Files.isSymbolicLink(p)) {
            return; // 符号链接的权限无意义；真实 Os.chmod 也不会跟随
        }
        // 用 unix:mode，而不是 PosixFilePermissions：后者表达不出 setuid/setgid
        // （04755 / 02755）。真实的 Os.chmod 直接走系统调用，这些位是支持的。
        Files.setAttribute(p, "unix:mode", mode & 07777);
    }

    public static void symlink(String oldPath, String newPath) throws IOException {
        Files.createSymbolicLink(Paths.get(newPath), Paths.get(oldPath));
    }

    public static void link(String oldPath, String newPath) throws IOException {
        Files.createLink(Paths.get(newPath), Paths.get(oldPath));
    }
}
