package com.memorole.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.system.Os;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 在未 root 的安卓设备上，用 proot 起一个 Ubuntu 容器，并在里面跑 memo-role。
 *
 * <p>它做了什么（首次启动会按顺序走一遍，之后只做已完成项之外的步骤）：
 * <ol>
 *   <li>把 APK 里自带的 proot 运行时复制到应用私有目录；</li>
 *   <li>从 Ubuntu 官方下载 arm64 的 base rootfs（约 30 MB）并解压；</li>
 *   <li>从 GitHub 拉取项目代码（tarball，不需要在容器里装 git）；</li>
 *   <li>在容器里 apt 装 Python、pip 装项目依赖；</li>
 *   <li>启动 Web 服务，界面再指向 127.0.0.1。</li>
 * </ol>
 *
 * <p><b>为什么 targetSdk 必须是 28</b>：Android 10（API 29）起禁止「从应用可写
 * 主目录执行文件」（W^X）。容器里的 bash / python3 正好都在应用数据目录下，
 * targetSdk 一旦 ≥ 29 就会全部无法 exec。这不是选择，是硬约束。
 *
 * <p>所有耗时操作都是同步的，调用方需要放在后台线程里执行。
 */
public class Container {

    /** Ubuntu 官方 base rootfs（arm64）。体积小、无多余包，正好当容器底子。 */
    public static final String ROOTFS_URL =
            "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/"
                    + "ubuntu-base-24.04.5-base-arm64.tar.gz";

    /** GitHub 的源码 tarball：公开仓库无需鉴权，也省掉容器里装 git 的空间。 */
    public static final String DEFAULT_REPO_URL =
            "https://codeload.github.com/naxida2333/memo-role/tar.gz/refs/heads/main";

    public static final int DEFAULT_PORT = 8000;

    private static final String PREFS = "memo_role";
    private static final String KEY_REPO = "repo_url";
    private static final String KEY_PORT = "port";

    /** 日志回调（在后台线程被调用，不能直接碰 UI）。 */
    public interface Log {
        void log(String line);
    }

    /** 下载/解压进度回调。total 为 -1 表示未知。 */
    public interface Progress {
        void progress(String what, long done, long total);
    }

    private final Context ctx;
    private final File base;
    private final File binDir;
    private final File libDir;
    private final File rootfs;
    private final File cacheDir;
    private final File tmpDir;
    private final File logsDir;
    private final File projectDir;

    private Process service;

    public Container(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.base = this.ctx.getFilesDir();
        this.binDir = new File(base, "usr/bin");
        this.libDir = new File(base, "usr/lib");
        this.rootfs = new File(base, "rootfs");
        this.cacheDir = new File(base, "cache");
        this.tmpDir = new File(base, "tmp");
        this.logsDir = new File(base, "logs");
        this.projectDir = new File(rootfs, "root/memo-role");
        this.tmpDir.mkdirs();
        this.logsDir.mkdirs();
        this.cacheDir.mkdirs();
    }

    // ------------------------------------------------------------------
    // 状态查询
    // ------------------------------------------------------------------

    public File getRootfs() {
        return rootfs;
    }

    public File getProjectDir() {
        return projectDir;
    }

    public File getLogsDir() {
        return logsDir;
    }

    /** proot 运行时是否已就位。 */
    public boolean isRuntimeInstalled() {
        return new File(binDir, "proot").isFile() && new File(libDir, "libtalloc.so.2").isFile();
    }

    /**
     * 容器底子是否已解压完整。
     *
     * <p>注意用 {@code usr/bin/bash} 而不是 {@code bin/bash}：Ubuntu 用了 usrmerge，
     * 镜像里的 {@code /bin} 只是一个指向 {@code usr/bin} 的符号链接，
     * 拿符号链接路径去判断会依赖「解压顺序恰好正确」，不如直接看真实文件。
     */
    public boolean isRootfsReady() {
        return new File(rootfs, "usr/bin/bash").isFile()
                && new File(rootfs, "usr/bin/apt-get").isFile();
    }

    public boolean isProjectReady() {
        return new File(projectDir, "memo_role/__main__.py").isFile();
    }

    /** 可以启动服务了（代码与容器都在）。 */
    public boolean isReady() {
        return isRuntimeInstalled() && isRootfsReady() && isProjectReady();
    }

    public boolean isServiceRunning() {
        return service != null && service.isAlive();
    }

    public int port() {
        return prefs().getInt(KEY_PORT, DEFAULT_PORT);
    }

    public void setPort(int port) {
        prefs().edit().putInt(KEY_PORT, port).apply();
    }

    public String repoUrl() {
        return prefs().getString(KEY_REPO, DEFAULT_REPO_URL);
    }

    public void setRepoUrl(String url) {
        prefs().edit().putString(KEY_REPO, url).apply();
    }

    /** 界面用的服务地址。 */
    public String baseUrl() {
        return "http://127.0.0.1:" + port();
    }

    private SharedPreferences prefs() {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ------------------------------------------------------------------
    // 一、安装 proot 运行时
    // ------------------------------------------------------------------

    public void installRuntime() throws IOException {
        binDir.mkdirs();
        libDir.mkdirs();
        copyAsset("proot/proot", new File(binDir, "proot"), true);
        copyAsset("proot/libtalloc.so.2", new File(libDir, "libtalloc.so.2"), false);
        copyAsset("proot/libandroid-shmem.so", new File(libDir, "libandroid-shmem.so"), false);
    }

    private void copyAsset(String assetPath, File dest, boolean executable) throws IOException {
        try (InputStream in = ctx.getAssets().open(assetPath);
             OutputStream out = new FileOutputStream(dest)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        }
        try {
            // proot 必须可执行。libtalloc / libandroid-shmem 只需可读，
            // 但统一给 755 更省事，也不影响加载。
            Os.chmod(dest.getAbsolutePath(), executable ? 0755 : 0644);
        } catch (Exception e) {
            throw new IOException("无法设置权限：" + dest, e);
        }
    }

    // ------------------------------------------------------------------
    // 二、下载并解压容器
    // ------------------------------------------------------------------

    public void downloadRootfs(final Progress progress) throws IOException {
        File archive = new File(cacheDir, "rootfs.tar.gz");
        if (archive.isFile() && archive.length() > (20L << 20)) {
            return; // 已经下过（>20MB 说明是完整的），不必重下
        }
        Net.download(ROOTFS_URL, archive, new Net.Progress() {
            @Override
            public void onProgress(long done, long total) {
                progress.progress("下载 Ubuntu 容器", done, total);
            }
        });
    }

    public void extractRootfs(final Progress progress) throws IOException {
        File archive = new File(cacheDir, "rootfs.tar.gz");
        if (!archive.isFile()) {
            throw new IOException("找不到容器包：" + archive);
        }
        // 重新解压前先清干净：半途失败的残留会让后续 apt 行为诡异
        deleteRecursively(rootfs);
        rootfs.mkdirs();
        TarGz.extract(archive, rootfs, new TarGz.Progress() {
            @Override
            public void onBytes(long read) {
                progress.progress("解压容器", read, archive.length());
            }
        });
    }

    /** 写 DNS 配置。
     *
     * <p>安卓不用 /etc/resolv.conf，容器里没有这个文件就解析不了域名 ——
     * apt 与 pip 会以「Temporary failure resolving」失败。优先取系统当前的
     * DNS 服务器，取不到再退回公共 DNS。
     */
    public void writeResolvConf() throws IOException {
        List<String> servers = systemDnsServers();
        if (servers.isEmpty()) {
            servers.add("8.8.8.8");
            servers.add("1.1.1.1");
        }
        File etc = new File(rootfs, "etc");
        etc.mkdirs();
        File resolv = new File(etc, "resolv.conf");
        Writer w = new OutputStreamWriter(new FileOutputStream(resolv), StandardCharsets.UTF_8);
        try {
            for (String s : servers) {
                w.write("nameserver " + s + "\n");
            }
        } finally {
            w.close();
        }
    }

    private List<String> systemDnsServers() {
        List<String> out = new ArrayList<String>();
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return out;
            }
            LinkProperties lp = cm.getLinkProperties(cm.getActiveNetwork());
            if (lp == null) {
                return out;
            }
            for (InetAddress addr : lp.getDnsServers()) {
                String host = addr.getHostAddress();
                if (host != null && host.indexOf(':') < 0) { // 只写 IPv4，容器里 IPv6 常不通
                    out.add(host);
                }
            }
        } catch (Exception ignored) {
            // 拿不到就用公共 DNS 兜底
        }
        return out;
    }

    /** ubuntu-base 里可能不带 apt 源；缺了就补上，否则 apt-get update 必然失败。 */
    public void ensureAptSources() throws IOException {
        File apt = new File(rootfs, "etc/apt");
        apt.mkdirs();
        File listD = new File(apt, "sources.list.d");
        File list = new File(apt, "sources.list");
        boolean hasSource = list.isFile() && !readText(list).trim().isEmpty();
        if (!hasSource && listD.isDirectory()) {
            File[] files = listD.listFiles();
            hasSource = files != null && files.length > 0;
        }
        if (hasSource) {
            return;
        }
        String codename = readCodename();
        StringBuilder sb = new StringBuilder();
        for (String comp : new String[]{"main", "universe", "restricted", "multiverse"}) {
            sb.append("deb http://ports.ubuntu.com/ubuntu-ports ").append(codename)
                    .append(' ').append(comp).append('\n');
        }
        writeText(list, sb.toString());
    }

    /** 从 /etc/os-release 读发行版代号（arm64 用的是 ports 源）。 */
    private String readCodename() {
        File osRelease = new File(rootfs, "etc/os-release");
        if (osRelease.isFile()) {
            for (String line : readText(osRelease).split("\n")) {
                if (line.startsWith("VERSION_CODENAME=")) {
                    String v = line.substring("VERSION_CODENAME=".length()).trim();
                    v = v.replace("\"", "");
                    if (!v.isEmpty()) {
                        return v;
                    }
                }
            }
        }
        return "noble"; // Ubuntu 24.04
    }

    // ------------------------------------------------------------------
    // 三、拉取项目代码
    // ------------------------------------------------------------------

    public void fetchProject(final Progress progress) throws IOException {
        File archive = new File(cacheDir, "project.tar.gz");
        Net.download(repoUrl(), archive, new Net.Progress() {
            @Override
            public void onProgress(long done, long total) {
                progress.progress("下载项目代码", done, total);
            }
        });
        File home = new File(rootfs, "root");
        home.mkdirs();
        deleteRecursively(projectDir);
        // GitHub 的 tarball 顶层是「仓库名-分支名」一层目录，用 target 再套一层伪装：
        // 解到 projectDir 的父目录，再把唯一的子目录改名成 memo-role。
        File staging = new File(home, ".unpack");
        deleteRecursively(staging);
        staging.mkdirs();
        TarGz.extract(archive, staging, new TarGz.Progress() {
            @Override
            public void onBytes(long read) {
                progress.progress("解压项目代码", read, archive.length());
            }
        });
        File[] children = staging.listFiles();
        if (children == null || children.length != 1 || !children[0].isDirectory()) {
            throw new IOException("项目包结构异常：解压后不是单个目录");
        }
        if (!children[0].renameTo(projectDir)) {
            throw new IOException("无法移动到：" + projectDir);
        }
        deleteRecursively(staging);
        archive.delete();
    }

    // ------------------------------------------------------------------
    // 四、容器内安装依赖
    // ------------------------------------------------------------------

    /**
     * 在容器里跑 bootstrap.sh（apt 装 Python、pip 装依赖）。
     *
     * <p>输出会实时回传：这一步在手机上要几分钟，没有输出用户会以为卡死。
     */
    public void runBootstrap(final Log log) throws IOException, InterruptedException {
        copyAsset("bootstrap.sh", new File(rootfs, "root/bootstrap.sh"), false);
        List<String> cmd = prootCommand("/bin/bash /root/bootstrap.sh");
        ProcessBuilder pb = builder(cmd);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        pipe(p.getInputStream(), log);
        int code = p.waitFor();
        if (code != 0) {
            throw new IOException("依赖安装失败（退出码 " + code + "），详见上方输出");
        }
    }

    // ------------------------------------------------------------------
    // 五、启停服务
    // ------------------------------------------------------------------

    public void startService(final Log log) throws IOException {
        stopService();
        File logFile = new File(logsDir, "service.log");
        List<String> cmd = prootCommand(
                "cd /root/memo-role && exec python3 -m memo_role "
                        + "--host 127.0.0.1 --port " + port());
        ProcessBuilder pb = builder(cmd);
        pb.redirectErrorStream(true);
        pb.redirectOutput(ProcessBuilder.Redirect.appendTo(logFile));
        service = pb.start();
        log.log("服务已启动，日志：" + logFile.getAbsolutePath());
    }

    public void stopService() {
        if (service == null) {
            return;
        }
        service.destroy();
        try {
            if (!service.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                service.destroyForcibly();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            service.destroyForcibly();
        }
        service = null;
    }

    /** 轮询 Web 端口，等到服务真的能响应为止。 */
    public boolean waitForService(long timeoutMs, Log log, int port) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (httpUp(port)) {
                return true;
            }
            if (service != null && !service.isAlive()) {
                log.log("服务进程已退出，请看下方日志");
                return false;
            }
            Thread.sleep(500);
        }
        return false;
    }

    private boolean httpUp(int port) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/").openConnection();
            conn.setConnectTimeout(1500);
            conn.setReadTimeout(2500);
            int code = conn.getResponseCode();
            return code > 0;
        } catch (IOException e) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ------------------------------------------------------------------
    // 六、在容器里执行任意命令（终端用）
    // ------------------------------------------------------------------

    /**
     * 执行一条命令并返回合并后的输出。
     *
     * <p>不是完整终端：没有 PTY、没有行编辑，交互式程序（vi、apt 的确认提示）
     * 不能正常工作。用于排障足够了 —— 这也是这个界面存在的目的。
     */
    public String exec(String command, long timeoutMs) {
        StringBuilder out = new StringBuilder();
        Process p = null;
        try {
            ProcessBuilder pb = builder(prootCommand(command));
            pb.redirectErrorStream(true);
            p = pb.start();
            final Process proc = p;
            final StringBuilder sink = out;
            Thread reader = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        BufferedReader r = new BufferedReader(
                                new InputStreamReader(proc.getInputStream(), StandardCharsets.UTF_8));
                        String line;
                        while ((line = r.readLine()) != null) {
                            sink.append(line).append('\n');
                        }
                    } catch (IOException ignored) {
                        // 进程被杀时读会抛异常，忽略即可
                    }
                }
            });
            reader.setDaemon(true);
            reader.start();

            if (!p.waitFor(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                p.destroy();
                out.append("\n[超时 ").append(timeoutMs / 1000).append(" 秒，已终止]");
            }
            reader.join(2000);
        } catch (Exception e) {
            out.append("执行失败：").append(e);
        } finally {
            if (p != null) {
                p.destroy();
            }
        }
        return out.toString();
    }

    // ------------------------------------------------------------------
    // proot 命令构造
    // ------------------------------------------------------------------

    /**
     * 拼出进入容器执行命令的完整 argv。
     *
     * <p>几个参数都不是可选的：
     * <ul>
     *   <li>{@code --link2symlink}：安卓文件系统不支持 proot 的硬链接需求，没有它会报错；</li>
     *   <li>{@code -0}：把当前用户伪装成 root，apt 才会认为自己有权安装；</li>
     *   <li>{@code -b /dev -b /proc -b /sys}：容器里没有真实的这三个目录，
     *       不绑过去 apt、pip 都会出问题；</li>
     *   <li>{@code /usr/bin/env -i}：清空宿主环境，避免安卓的环境变量渗进容器。</li>
     * </ul>
     */
    private List<String> prootCommand(String shellCommand) {
        List<String> cmd = new ArrayList<String>();
        cmd.add(new File(binDir, "proot").getAbsolutePath());
        cmd.add("--link2symlink");
        // 服务是 proot 的子进程；不加这个，App 里 destroy() 之后容器内可能留下
        // 仍在跑的 python 进程，再启动就会撞端口
        cmd.add("--kill-on-exit");
        cmd.add("-0");
        cmd.add("-r");
        cmd.add(rootfs.getAbsolutePath());
        cmd.add("-b");
        cmd.add("/dev");
        cmd.add("-b");
        cmd.add("/proc");
        cmd.add("-b");
        cmd.add("/sys");
        cmd.add("-b");
        cmd.add(new File(rootfs, "tmp").getAbsolutePath() + ":/dev/shm");
        cmd.add("-w");
        cmd.add("/root");
        cmd.add("/usr/bin/env");
        cmd.add("-i");
        cmd.add("HOME=/root");
        cmd.add("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
        cmd.add("TERM=xterm-256color");
        cmd.add("LANG=C.UTF-8");
        cmd.add("LC_ALL=C.UTF-8");
        cmd.add("/bin/bash");
        cmd.add("-lc");
        cmd.add(shellCommand);
        return cmd;
    }

    private ProcessBuilder builder(List<String> cmd) {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        java.util.Map<String, String> env = pb.environment();
        // proot 自身是 Bionic 程序，需要能找到 libtalloc / libandroid-shmem
        env.put("LD_LIBRARY_PATH", libDir.getAbsolutePath());
        // proot 会在 TMPDIR 下放它的 loader；安卓的 /tmp 应用不可写，必须改到这里
        env.put("PROOT_TMP_DIR", tmpDir.getAbsolutePath());
        env.put("PROOT_NO_SECCOMP", "1");
        pb.directory(base);
        return pb;
    }

    // ------------------------------------------------------------------
    // 杂项
    // ------------------------------------------------------------------

    /** 供界面展示的容器状态摘要。 */
    public String statusSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("proot 运行时：").append(isRuntimeInstalled() ? "已就绪" : "未安装").append('\n');
        sb.append("Ubuntu 容器：").append(isRootfsReady() ? "已就绪" : "未安装").append('\n');
        sb.append("项目代码：").append(isProjectReady() ? "已拉取" : "未拉取").append('\n');
        sb.append("服务：").append(isServiceRunning() ? "运行中" : "未运行").append('\n');
        sb.append("地址：").append(baseUrl());
        return sb.toString();
    }

    /** 读取服务日志尾部。 */
    public String tailServiceLog(int maxBytes) throws IOException {
        File logFile = new File(logsDir, "service.log");
        if (!logFile.isFile()) {
            return "（暂无日志）";
        }
        long len = logFile.length();
        long from = Math.max(0, len - maxBytes);
        java.io.RandomAccessFile raf = new java.io.RandomAccessFile(logFile, "r");
        try {
            raf.seek(from);
            byte[] buf = new byte[(int) (len - from)];
            raf.readFully(buf);
            return new String(buf, StandardCharsets.UTF_8);
        } finally {
            raf.close();
        }
    }

    private static void pipe(InputStream in, Log log) {
        try {
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String line;
            while ((line = r.readLine()) != null) {
                log.log(line);
            }
        } catch (IOException ignored) {
            // 进程结束时读会抛异常，属正常
        }
    }

    private static String readText(File f) {
        try {
            byte[] buf = new byte[(int) Math.min(f.length(), 1 << 16)];
            java.io.FileInputStream in = new java.io.FileInputStream(f);
            try {
                int off = 0;
                while (off < buf.length) {
                    int n = in.read(buf, off, buf.length - off);
                    if (n < 0) {
                        break;
                    }
                    off += n;
                }
                return new String(buf, 0, off, StandardCharsets.UTF_8);
            } finally {
                in.close();
            }
        } catch (IOException e) {
            return "";
        }
    }

    private static void writeText(File f, String text) throws IOException {
        File parent = f.getParentFile();
        if (parent != null) {
            parent.mkdirs();
        }
        Writer w = new OutputStreamWriter(new FileOutputStream(f), StandardCharsets.UTF_8);
        try {
            w.write(text);
        } finally {
            w.close();
        }
    }

    static void deleteRecursively(File f) {
        if (f == null || !f.exists()) {
            return;
        }
        if (f.isDirectory() && !isSymlink(f)) {
            File[] children = f.listFiles();
            if (children != null) {
                for (File c : children) {
                    deleteRecursively(c);
                }
            }
        }
        f.delete();
    }

    private static boolean isSymlink(File f) {
        try {
            return !f.getCanonicalPath().equals(f.getAbsolutePath());
        } catch (IOException e) {
            return false;
        }
    }
}
