package com.memorole.app;

import android.system.Os;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.zip.GZIPInputStream;

/**
 * 极简 tar.gz 解压器，只处理 Ubuntu base rootfs 里会出现的那几种条目。
 *
 * <p>为什么不用系统自带的 tar，也不引第三方库：
 * <ul>
 *   <li>安卓自带的 toybox tar 对符号链接、pax 扩展头、设备文件的支持随版本变化，
 *       而 rootfs 里有几千个符号链接（/usr/lib 下一大堆），不能赌；</li>
 *   <li>引 commons-compress 就得为它开一条 Gradle / AAR 依赖链，
 *       而本项目的构建刻意不依赖 Gradle。</li>
 * </ul>
 * tar 格式本身很简单（512 字节定长头 + 数据块），自己实现反而是最可控的。
 *
 * <p>已处理：普通文件、目录、符号链接、硬链接、pax 扩展头（路径超长时出现）、
 * GNU 长文件名（'L' / 'K'）。未处理：字符/块设备文件（rootfs 里是普通文件占位，
 * 解压后容器内自己会重建或忽略）。
 */
public final class TarGz {

    /** 解压进度回调：已读字节数。用于给用户一个「还在动」的反馈。 */
    public interface Progress {
        void onBytes(long read);
    }

    private static final int BLOCK = 512;

    private TarGz() {
    }

    /**
     * 把 gzip 压缩的 tar 解到目标目录。
     *
     * @param archive  本地 .tar.gz 文件
     * @param destDir  目标目录（会自动创建）
     */
    public static void extract(File archive, File destDir, Progress progress) throws IOException {
        if (!destDir.exists() && !destDir.mkdirs()) {
            throw new IOException("无法创建目录：" + destDir);
        }
        String destPath = destDir.getCanonicalPath();
        CountingInputStream counter = null;
        try (InputStream raw = new BufferedInputStream(new FileInputStream(archive), 1 << 16)) {
            counter = new CountingInputStream(raw);
            try (InputStream in = new GZIPInputStream(counter, 1 << 16)) {
                byte[] header = new byte[BLOCK];
                String paxPath = null;      // 来自 'x' 条目
                String gnuName = null;      // 来自 'L' 条目
                String gnuLink = null;      // 来自 'K' 条目
                long lastReport = 0;

                while (true) {
                    if (!readFully(in, header, BLOCK)) {
                        break;              // 正常结束
                    }
                    if (isZeroBlock(header)) {
                        // tar 以两个全零块收尾；读一个就够，剩下可能被截断
                        break;
                    }

                    String name = readString(header, 0, 100);
                    String prefix = readString(header, 345, 155);
                    long mode = readOctal(header, 100, 8);
                    long size = readOctal(header, 124, 12);
                    char type = (char) (header[156] & 0xFF);
                    String linkName = readString(header, 157, 100);

                    if (type == 'x' || type == 'g') {
                        byte[] data = readData(in, size);
                        if (type == 'x') {
                            paxPath = parsePaxPath(data);
                        }
                        continue;
                    }
                    if (type == 'L') {
                        gnuName = new String(readData(in, size), "UTF-8").trim();
                        continue;
                    }
                    if (type == 'K') {
                        gnuLink = new String(readData(in, size), "UTF-8").trim();
                        continue;
                    }

                    String fullName = resolveName(gnuName, paxPath, prefix, name);
                    String fullLink = gnuLink != null ? gnuLink : linkName;
                    paxPath = null;
                    gnuName = null;
                    gnuLink = null;

                    if (fullName.isEmpty()) {
                        skipData(in, size);
                        continue;
                    }

                    File target = safeResolve(destPath, fullName);
                    switch (type) {
                        case '5':
                            if (!target.isDirectory() && !target.mkdirs() && !target.isDirectory()) {
                                throw new IOException("无法创建目录：" + target);
                            }
                            applyMode(target, mode);
                            skipData(in, size);
                            break;
                        case '2':
                            makeSymlink(target, fullLink, destPath);
                            skipData(in, size);
                            break;
                        case '1':
                            makeHardlink(target, safeResolve(destPath, fullLink));
                            skipData(in, size);
                            break;
                        case '0':
                        case '\0':
                            writeFile(in, target, size, mode);
                            break;
                        default:
                            // 设备文件等：rootfs 里不会真正用到，跳过但保持流对齐
                            skipData(in, size);
                            break;
                    }

                    if (progress != null && counter.count() - lastReport > (1 << 20)) {
                        lastReport = counter.count();
                        progress.onBytes(counter.count());
                    }
                }
            }
        }
        if (progress != null) {
            progress.onBytes(counter == null ? 0 : counter.count());
        }
    }

    // ------------------------------------------------------------------
    // 条目处理
    // ------------------------------------------------------------------

    /**
     * 把 tar 里的路径解析到目标目录内，并挡住目录穿越。
     *
     * <p>rootfs 的条目全部形如 {@code ./usr/bin/bash}，直接拼会得到 {@code ././}；
     * 而恶意包可能写 {@code ../../}，所以这里规范一遍并校验前缀。
     */
    private static File safeResolve(String destPath, String entryName) throws IOException {
        String cleaned = entryName.replace('\\', '/');
        while (cleaned.startsWith("./")) {
            cleaned = cleaned.substring(2);
        }
        while (cleaned.startsWith("/")) {
            cleaned = cleaned.substring(1);
        }
        File f = new File(destPath, cleaned);
        String canonical = f.getCanonicalPath();
        if (!canonical.equals(destPath) && !canonical.startsWith(destPath + File.separator)) {
            throw new IOException("拒绝越界路径：" + entryName);
        }
        return f;
    }

    private static void writeFile(InputStream in, File target, long size, long mode) throws IOException {
        File parent = target.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs() && !parent.isDirectory()) {
            throw new IOException("无法创建目录：" + parent);
        }
        // 若已存在同名目录（某些包的写法），先清掉，否则写文件会失败
        if (target.isDirectory()) {
            return;
        }
        try (OutputStream out = new FileOutputStream(target)) {
            copyExactly(in, out, size);
        }
        applyMode(target, mode);
    }

    private static void makeSymlink(File link, String targetPath, String destPath) throws IOException {
        File parent = link.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs() && !parent.isDirectory()) {
            throw new IOException("无法创建目录：" + parent);
        }
        if (link.exists() || isSymlink(link)) {
            link.delete();
        }
        try {
            // 用 android.system.Os：java.nio.file.Files 要 API 26，而本项目 minSdk 24
            Os.symlink(targetPath, link.getAbsolutePath());
        } catch (Exception e) {
            throw new IOException("创建符号链接失败 " + link + " -> " + targetPath, e);
        }
    }

    private static void makeHardlink(File link, File existing) throws IOException {
        File parent = link.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs() && !parent.isDirectory()) {
            throw new IOException("无法创建目录：" + parent);
        }
        if (link.exists()) {
            link.delete();
        }
        try {
            Os.link(existing.getAbsolutePath(), link.getAbsolutePath());
        } catch (Exception e) {
            // 硬链接失败（例如目标不存在）时退化成复制，宁可多占空间也不要中断整个解压
            if (existing.isFile()) {
                try (InputStream in = new FileInputStream(existing);
                     OutputStream out = new FileOutputStream(link)) {
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                    }
                }
            }
        }
    }

    private static void applyMode(File f, long mode) {
        try {
            Os.chmod(f.getAbsolutePath(), (int) (mode & 07777));
        } catch (Exception ignored) {
            // 权限位设不上不致命：proot 用的是自己的权限模拟，关键文件后面还会显式 chmod
        }
    }

    private static boolean isSymlink(File f) {
        try {
            return !f.getCanonicalPath().equals(f.getAbsolutePath());
        } catch (IOException e) {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 头解析
    // ------------------------------------------------------------------

    private static String resolveName(String gnuName, String paxPath, String prefix, String name) {
        if (gnuName != null && !gnuName.isEmpty()) {
            return gnuName;
        }
        if (paxPath != null && !paxPath.isEmpty()) {
            return paxPath;
        }
        if (!prefix.isEmpty()) {
            return prefix + "/" + name;
        }
        return name;
    }

    /** pax 扩展头是若干条 {@code <长度> <键>=<值>\n}，只需取出 path。 */
    private static String parsePaxPath(byte[] data) {
        String text = new String(data, java.nio.charset.StandardCharsets.UTF_8);
        int i = 0;
        while (i < text.length()) {
            int sp = text.indexOf(' ', i);
            if (sp < 0) {
                break;
            }
            int len;
            try {
                len = Integer.parseInt(text.substring(i, sp).trim());
            } catch (NumberFormatException e) {
                break;
            }
            if (len <= 0 || i + len > text.length()) {
                break;
            }
            String record = text.substring(sp + 1, i + len - 1); // 去掉结尾换行
            int eq = record.indexOf('=');
            if (eq > 0 && "path".equals(record.substring(0, eq))) {
                return record.substring(eq + 1);
            }
            i += len;
        }
        return null;
    }

    private static boolean isZeroBlock(byte[] block) {
        for (byte b : block) {
            if (b != 0) {
                return false;
            }
        }
        return true;
    }

    private static String readString(byte[] buf, int off, int len) {
        int end = off;
        int limit = off + len;
        while (end < limit && buf[end] != 0) {
            end++;
        }
        return new String(buf, off, end - off, java.nio.charset.StandardCharsets.UTF_8);
    }

    /**
     * 读八进制字段。GNU tar 对超大值会用 base-256（最高位为 1），这里一并支持，
     * 否则遇到大文件会得到离谱的长度。
     */
    private static long readOctal(byte[] buf, int off, int len) {
        if ((buf[off] & 0x80) != 0) {
            long value = buf[off] & 0x7F;
            for (int i = 1; i < len; i++) {
                value = (value << 8) | (buf[off + i] & 0xFF);
            }
            return value;
        }
        long value = 0;
        for (int i = 0; i < len; i++) {
            byte b = buf[off + i];
            if (b == 0 || b == ' ') {
                if (i > 0) {
                    break;
                }
                continue;
            }
            if (b < '0' || b > '7') {
                break;
            }
            value = value * 8 + (b - '0');
        }
        return value;
    }

    // ------------------------------------------------------------------
    // 流操作
    // ------------------------------------------------------------------

    private static byte[] readData(InputStream in, long size) throws IOException {
        if (size <= 0) {
            return new byte[0];
        }
        if (size > (64L << 20)) {
            throw new IOException("tar 条目过大：" + size);
        }
        byte[] data = new byte[(int) size];
        readFully(in, data, data.length);
        skipPadding(in, size);
        return data;
    }

    private static void skipData(InputStream in, long size) throws IOException {
        copyExactly(in, null, size);
    }

    private static void copyExactly(InputStream in, OutputStream out, long size) throws IOException {
        byte[] buf = new byte[1 << 16];
        long left = size;
        while (left > 0) {
            int want = (int) Math.min(buf.length, left);
            int n = in.read(buf, 0, want);
            if (n < 0) {
                throw new IOException("tar 数据意外结束");
            }
            if (out != null) {
                out.write(buf, 0, n);
            }
            left -= n;
        }
        skipPadding(in, size);
    }

    /** tar 的数据区按 512 字节对齐，末尾补零。 */
    private static void skipPadding(InputStream in, long size) throws IOException {
        long pad = (BLOCK - (size % BLOCK)) % BLOCK;
        long left = pad;
        byte[] buf = new byte[BLOCK];
        while (left > 0) {
            int want = (int) Math.min(buf.length, left);
            int n = in.read(buf, 0, want);
            if (n < 0) {
                throw new IOException("tar 数据意外结束");
            }
            left -= n;
        }
    }

    private static boolean readFully(InputStream in, byte[] buf, int len) throws IOException {
        int off = 0;
        while (off < len) {
            int n = in.read(buf, off, len - off);
            if (n < 0) {
                return off != 0;
            }
            off += n;
        }
        return true;
    }

    /** 统计已读字节（GZIPInputStream 读的是解压后长度，所以这里包在 gzip 之前）。 */
    private static final class CountingInputStream extends InputStream {
        private final InputStream in;
        private long count;

        CountingInputStream(InputStream in) {
            this.in = in;
        }

        long count() {
            return count;
        }

        @Override
        public int read() throws IOException {
            int b = in.read();
            if (b >= 0) {
                count++;
            }
            return b;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            int n = in.read(b, off, len);
            if (n > 0) {
                count += n;
            }
            return n;
        }

        @Override
        public void close() throws IOException {
            in.close();
        }
    }
}
