package com.memorole.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.system.Os;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
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

    /**
     * 容器内依赖清单的版本。
     *
     * <p>{@code bootstrap.sh} 里每多装一样东西就要 +1。原因：容器是一次装好的，
     * 用户升级 APK 时旧容器还在，只看「标记文件是否存在」会直接跳过，
     * 新增的依赖（比如本地推理引擎要的 libgomp1）就永远装不上。
     * 版本号变了就重跑一遍 bootstrap —— apt 与 pip 都是幂等的，缺什么补什么。
     */
    private static final int BOOTSTRAP_VERSION = 2;
    private static final String BOOTSTRAP_MARKER = "root/.bootstrap-done";

    /** 本地推理引擎（llama.cpp）在容器里的安装位置。 */
    private static final String LLAMA_DIR = "usr/local/lib/llama";
    private static final String LLAMA_BIN = "usr/local/bin/llama-server";

    /** pkill 的等待上限；容器里 pkill 随 Ubuntu base 自带的 procps 就有。 */
    private static final long PKILL_TIMEOUT_MS = 20000;
    /** 等残留服务释放端口的上限。 */
    private static final long PORT_FREE_TIMEOUT_MS = 10000;

    /**
     * llama-server 的入口脚本。
     *
     * <p>为什么不直接放软链、也不把二进制复制到 /usr/local/bin：llama.cpp 找它那
     * 八个 CPU 变体库（`libggml-cpu-*.so`）是**扫可执行文件自己所在目录**的，
     * 不是按 SONAME 走动态链接器（`GGML_BACKEND_PATH` 只能指定单个文件，
     * 指目录会报 "Is a directory"）。实测把二进制挪到别处，启动时只会得到
     * `no backends are loaded` —— 连模型都加载不了。
     *
     * <p>转一手 `exec` 真身，可执行文件路径与库目录就始终一致：动态链接器按
     * RUNPATH 里的 `$ORIGIN` 找库、ggml 按 exe 目录找 CPU 变体，两条路都不会偏。
     * （proot 会读脚本开头的 shebang 并把解释器换成容器内的路径 —— 见 proot 源码
     * `src/execve/shebang.c` 的 expand_shebang()，所以容器内直接执行本脚本没问题。）
     */
    private static final String LLAMA_WRAPPER =
            "#!/bin/sh\n"
                    + "# APK 内置的本地推理引擎（llama.cpp）。真身与它的 .so 同在\n"
                    + "# /usr/local/lib/llama（二进制自带 $ORIGIN 的 RUNPATH），这里只是转一手。\n"
                    + "exec /usr/local/lib/llama/llama-server \"$@\"\n";

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
    private final File libexecDir;
    private final File rootfs;
    private final File cacheDir;
    private final File tmpDir;
    private final File logsDir;
    private final File projectDir;

    private Process service;

    /**
     * 端口上是否有人在应答（缓存值）。
     *
     * <p>取值前要先 {@link #probePort()}。之所以缓存而不是每次现探：界面刷新在主线程，
     * 而探测是一次最长几秒的 HTTP 往返。
     */
    private volatile boolean portServing;

    public Container(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.base = this.ctx.getFilesDir();
        this.binDir = new File(base, "usr/bin");
        this.libDir = new File(base, "usr/lib");
        this.libexecDir = new File(base, "usr/libexec/proot");
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

    /**
     * proot 运行时是否已就位。
     *
     * <p>必须把 loader 也算进来：proot 执行容器里的 ELF 时，内核会先拿 PT_INTERP
     * 去**宿主**找解释器（例如 /lib/ld-linux-aarch64.so.1），安卓上没有这个文件，
     * 于是直接 ENOENT。proot 靠自己的 loader 把这个路径换成容器内的，缺失时
     * 报错只有一句 `execve("/usr/bin/env"): No such file or directory`，
     * 看起来像容器坏了，其实只是少了一个 18 KB 的辅助文件。
     */
    public boolean isRuntimeInstalled() {
        return missingRuntimeFiles().isEmpty();
    }

    /** 列出缺失的运行时文件（便于在手机上直接看出漏装了什么）。 */
    public List<String> missingRuntimeFiles() {
        List<String> missing = new ArrayList<String>();
        collectMissing(missing, new File(binDir, "proot"));
        collectMissing(missing, new File(libDir, "libtalloc.so.2"));
        collectMissing(missing, new File(libDir, "libandroid-shmem.so"));
        collectMissing(missing, new File(libexecDir, "loader"));
        return missing;
    }

    private static void collectMissing(List<String> missing, File f) {
        if (!f.isFile()) {
            missing.add(f.getName());
        }
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

    /**
     * 可以启动服务了（运行时、容器、代码、依赖、本地推理引擎都在）。
     *
     * <p>把依赖与推理引擎也算进来，是为了让**升级 APK 的老用户**能走到这里：
     * 容器和代码都还在，一键初始化会把新增的部分补上（这几步都已做过的会自动跳过）。
     */
    public boolean isReady() {
        return isRuntimeInstalled()
                && isRootfsReady()
                && isProjectReady()
                && isBootstrapCurrent()
                && isLlamaServerInstalled();
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
        libexecDir.mkdirs();
        copyAsset("proot/proot", new File(binDir, "proot"), true);
        copyAsset("proot/libtalloc.so.2", new File(libDir, "libtalloc.so.2"), false);
        copyAsset("proot/libandroid-shmem.so", new File(libDir, "libandroid-shmem.so"), false);
        // loader 是 proot 执行容器内程序的必需品（见 isRuntimeInstalled 的说明）
        copyAsset("proot/loader", new File(libexecDir, "loader"), true);
        copyAsset("proot/loader32", new File(libexecDir, "loader32"), true);
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
    // 一之半、安装本地推理引擎（llama-server）
    // ------------------------------------------------------------------

    /**
     * 把 APK 里自带的 llama-server 及其依赖库装进容器。
     *
     * <p>文件清单直接读 assets/llama 目录（不是写死的列表）：打包时带了什么就装什么，
     * 少一个都不会因为「代码里有这个名字」而被忽略。装机后由 bootstrap.sh 试跑一次
     * 验证，所以「漏文件」这类问题会在初始化日志里当场暴露，而不是等到用户聊天时。
     *
     * <p>同样的版本已装好就跳过；APK 升级带来新版本时自动覆盖更新。
     */
    public void installLlamaServer() throws IOException {
        if (isLlamaServerInstalled()) {
            return;
        }
        String[] names = ctx.getAssets().list("llama");
        if (names == null || names.length == 0) {
            throw new IOException("APK 里没有 assets/llama（打包漏了本地推理引擎）");
        }
        File dir = new File(rootfs, LLAMA_DIR);
        File binDir = new File(rootfs, "usr/local/bin");
        // 先清干净：只覆盖不清理的话，旧版本删掉的库会留下来，回头很难判断用的是哪一套
        deleteRecursively(dir);
        if (!dir.mkdirs() && !dir.isDirectory()) {
            throw new IOException("无法创建目录：" + dir);
        }
        binDir.mkdirs();
        for (String name : names) {
            copyAsset("llama/" + name, new File(dir, name), true);
        }
        File wrapper = new File(rootfs, LLAMA_BIN);
        writeText(wrapper, LLAMA_WRAPPER);
        try {
            Os.chmod(wrapper.getAbsolutePath(), 0755);
        } catch (Exception e) {
            throw new IOException("无法设置权限：" + wrapper, e);
        }
    }

    /** 本地推理引擎是否已装进容器，且版本与 APK 自带的一致。 */
    public boolean isLlamaServerInstalled() {
        String installed = installedLlamaVersion();
        if (installed.isEmpty()) {
            return false;
        }
        String bundled = bundledLlamaVersion();
        // APK 里没有版本号（异常情况）就不比对，只要装过就当作可用
        return bundled.isEmpty() || bundled.equals(installed);
    }

    /** APK 自带的版本号（assets/llama/VERSION 的第一行）。 */
    public String bundledLlamaVersion() {
        try (InputStream in = ctx.getAssets().open("llama/VERSION")) {
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String line = r.readLine();
            return line == null ? "" : line.trim();
        } catch (IOException e) {
            return "";
        }
    }

    /** 容器里已安装的版本号；没装（或缺文件）返回空串。 */
    public String installedLlamaVersion() {
        if (!new File(rootfs, LLAMA_BIN).isFile()) {
            return "";
        }
        return readText(new File(rootfs, LLAMA_DIR + "/VERSION")).trim();
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
        File unpacked = downloadAndUnpack(archive, progress);
        File home = new File(rootfs, "root");
        home.mkdirs();
        deleteRecursively(projectDir);
        if (!unpacked.renameTo(projectDir)) {
            throw new IOException("无法移动到：" + projectDir);
        }
        cleanStaging();
        archive.delete();
    }

    /**
     * 只更新代码：重新拉一份仓库代码，覆盖进已有项目目录。
     *
     * <p><b>为什么不直接复用 {@link #fetchProject}</b>：那个是「先删后解」，
     * 而 models/、data/、config.yaml 这些用户数据都躺在项目目录里，全在
     * .gitignore 里、不在 tarball 中。照那个路子更新一次，模型和聊天记录就没了。
     * 这里是覆盖式合并：包里有同名的文件被替换，没有的原样留着。
     */
    public void updateProject(final Progress progress) throws IOException {
        if (!isProjectReady()) {
            throw new IOException("容器里还没有项目代码，请先在首页做一次「一键初始化」");
        }
        File archive = new File(cacheDir, "project.tar.gz");
        File unpacked = downloadAndUnpack(archive, progress);
        copyTree(unpacked, projectDir);
        cleanStaging();
        archive.delete();
    }

    /** 下载项目 tarball 并解压，返回解压出来的唯一顶层目录。 */
    private File downloadAndUnpack(File archive, final Progress progress) throws IOException {
        Net.download(repoUrl(), archive, new Net.Progress() {
            @Override
            public void onProgress(long done, long total) {
                progress.progress("下载项目代码", done, total);
            }
        });
        // GitHub 的 tarball 顶层是「仓库名-分支名」一层目录，多解一层再自己摆正
        File staging = stagingDir();
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
        return children[0];
    }

    private File stagingDir() {
        return new File(cacheDir, "unpack");
    }

    private void cleanStaging() {
        deleteRecursively(stagingDir());
    }

    /** 递归把 src 的内容合并进 dst，同名文件覆盖、同名目录递归。 */
    private static void copyTree(File src, File dst) throws IOException {
        if (src.isDirectory()) {
            File[] children = src.listFiles();
            if (children == null) {
                return;
            }
            if (!dst.isDirectory() && !dst.mkdirs()) {
                throw new IOException("无法创建目录：" + dst);
            }
            for (File child : children) {
                copyTree(child, new File(dst, child.getName()));
            }
            return;
        }
        File parent = dst.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("无法创建目录：" + parent);
        }
        try (InputStream in = new FileInputStream(src);
             OutputStream out = new FileOutputStream(dst)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        }
    }

    // ------------------------------------------------------------------
    // 四、容器内安装依赖
    // ------------------------------------------------------------------

    /**
     * 在容器里跑 bootstrap.sh（apt 装 Python、pip 装依赖、补 llama-server 要的系统库）。
     *
     * <p>输出会实时回传：这一步在手机上要几分钟，没有输出用户会以为卡死。
     * 成功后留下带版本号的标记文件；版本一致就跳过（见 {@link #BOOTSTRAP_VERSION}）。
     */
    public void runBootstrap(final Log log) throws IOException, InterruptedException {
        if (isBootstrapCurrent()) {
            log.log("依赖此前已经装好，跳过（要重装请用设置里的重置）");
            return;
        }

        copyAsset("bootstrap.sh", new File(rootfs, "root/bootstrap.sh"), false);
        List<String> cmd = prootCommand("/bin/bash /root/bootstrap.sh");
        ProcessBuilder pb = builder(cmd);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        pipe(p.getInputStream(), log);
        int code = p.waitFor();
        if (code != 0) {
            // proot 的报错往往只有一句话，没有上下文很难判断，所以把命令行也打出来
            List<String> missing = missingRuntimeFiles();
            if (!missing.isEmpty()) {
                log.log("运行时文件缺失：" + missing);
            }
            log.log("失败的命令：");
            log.log("  " + joinCommand(cmd));
            throw new IOException("依赖安装失败（退出码 " + code + "），详见上方输出");
        }

        File markerFile = new File(rootfs, BOOTSTRAP_MARKER);
        try {
            writeText(markerFile, "ok " + BOOTSTRAP_VERSION + "\n");
        } catch (IOException e) {
            // 标记写不上不影响本次结果，下次重跑一遍而已
            log.log("提示：无法写入完成标记（" + e.getMessage() + "）");
        }
    }

    /** 容器里的依赖是否已按当前清单版本装好。 */
    public boolean isBootstrapCurrent() {
        File marker = new File(rootfs, BOOTSTRAP_MARKER);
        return marker.isFile() && ("ok " + BOOTSTRAP_VERSION).equals(readText(marker).trim());
    }

    /** 把命令拼成可直接复制去终端跑的一行（带引号，路径含空格也不会歧义）。 */
    static String joinCommand(List<String> cmd) {
        StringBuilder sb = new StringBuilder();
        for (String part : cmd) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            if (part.indexOf(' ') >= 0 || part.indexOf('\'') >= 0) {
                sb.append('\'').append(part.replace("'", "'\\''")).append('\'');
            } else {
                sb.append(part);
            }
        }
        return sb.toString();
    }

    // ------------------------------------------------------------------
    // 五、启停服务
    // ------------------------------------------------------------------

    public void startService(final Log log) throws IOException, InterruptedException {
        stopService(log);
        // 端口上可能还蹲着一个「失联的旧服务」（见 killStaleServices）：新进程绑不上端口
        // 会直接退出，而 waitForService 又能从旧进程拿到应答，于是界面显示「已就绪」，
        // 实际跑的是旧代码 —— 必须先把端口腾出来。
        ensurePortFree(log);
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
        stopService(null);
    }

    public void stopService(final Log log) {
        if (service != null) {
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
        // 句柄可能早就没了（App 被系统回收过），但端口上还有人在应答 —— 那是残留进程
        if (probePort()) {
            killStaleServices(log);
            try {
                waitPortFree(PORT_FREE_TIMEOUT_MS, log);
            } catch (InterruptedException e) {
                // 停止服务没有「被打断」这种中间态可返回，恢复中断标记后按已停处理
                Thread.currentThread().interrupt();
            }
        }
    }

    /** 轮询 Web 端口，等到服务真的能响应为止。 */
    public boolean waitForService(long timeoutMs, Log log) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (probePort()) {
                // 刚起的进程已经退出、端口却仍有人应答：那是在跑的旧服务，不是我们这次的。
                // 直接返回 true 会让用户对着「服务已就绪」用上旧代码（接口 404 / Not Found）。
                if (service == null || !service.isAlive()) {
                    log.log("端口 " + port() + " 上的应答不是刚启动的服务（旧进程没退干净）");
                    return false;
                }
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

    /**
     * 探测端口上是否有人在应答，并更新缓存。
     *
     * <p>分成「探测」与「取值」两步：探测是一次真实的 HTTP 往返（最坏要几秒），
     * 只能在后台线程做；界面每次刷新都要知道结果，读缓存就够了 ——
     * 否则光刷新状态就会把界面卡住。
     */
    public boolean probePort() {
        portServing = httpUp(port());
        return portServing;
    }

    /** 端口上是否有人在应答（缓存值，见 {@link #probePort()}）。 */
    public boolean isPortServing() {
        return portServing;
    }

    /**
     * 杀掉容器里所有还在跑的 memo-role 服务进程。
     *
     * <p>容器是 proot 起的，服务的真身其实是**宿主上的** python 进程。App 进程被
     * 系统回收（或被杀）时 proot 来不及清理，python 就可能变成孤儿一直占着端口。
     * 之后 App 重新起来点「启动服务」：新进程绑不上端口当场退出（uvicorn 报
     * `address already in use`，退出码 3），而界面上 `waitForService` 又从那个孤儿
     * 拿到应答，于是**磁盘上是新代码、在跑的是旧代码** —— 表现就是页面能开、点什么都
     * 报 `Not Found`（接口不存在）。这种错很难从现象反推，所以宁可每次启停都清一遍。
     *
     * <p>pattern 里带方括号是为了**不误杀自己**：`pkill -f` 会拿每个进程（包括我们自己
     * 这条命令所在的 shell）的命令行去匹配，而此刻我们的命令行里正含着 `memo[_-]role`
     * 这个字面量 —— 它本身匹配不上该正则，所以不会自杀。
     */
    private void killStaleServices(final Log log) {
        String out = exec("pkill -9 -f 'memo[_-]role' 2>/dev/null; exit 0", PKILL_TIMEOUT_MS);
        if (log != null && out.trim().length() > 0) {
            log.log(out.trim());
        }
    }

    /** 端口上没人应答就直接返回；有人应答就清掉残留进程并等它释放。 */
    private void ensurePortFree(final Log log) throws InterruptedException {
        if (!probePort()) {
            return;
        }
        if (log != null) {
            log.log("端口 " + port() + " 上还有服务在应答，先清理残留进程…");
        }
        killStaleServices(log);
        if (!waitPortFree(PORT_FREE_TIMEOUT_MS, log)) {
            if (log != null) {
                log.log("警告：端口 " + port() + " 仍被占用，启动可能失败");
            }
        }
    }

    /** 等端口不再应答（真释放了才返回 true）。 */
    private boolean waitPortFree(long timeoutMs, final Log log) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (!probePort()) {
                if (log != null) {
                    log.log("端口 " + port() + " 已释放");
                }
                return true;
            }
            Thread.sleep(300);
        }
        return !probePort();
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
        // proot 默认去 Termux 的目录找 loader（编译期写死），这里必须指到我们自己的路径，
        // 否则执行容器内的程序会直接 ENOENT（见 isRuntimeInstalled 的说明）
        env.put("PROOT_LOADER", new File(libexecDir, "loader").getAbsolutePath());
        env.put("PROOT_LOADER_32", new File(libexecDir, "loader32").getAbsolutePath());
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
        List<String> missing = missingRuntimeFiles();
        sb.append("proot 运行时：")
                .append(missing.isEmpty() ? "已就绪" : "缺 " + missing)
                .append('\n');
        sb.append("Ubuntu 容器：").append(isRootfsReady() ? "已就绪" : "未安装").append('\n');
        sb.append("项目代码：").append(isProjectReady() ? "已拉取" : "未拉取").append('\n');
        sb.append("Python 依赖：")
                .append(isBootstrapCurrent() ? "已安装" : "未安装")
                .append('\n');
        String llama = installedLlamaVersion();
        sb.append("本地推理引擎：")
                .append(llama.isEmpty()
                        ? "未安装（本地模型不可用；仍可在管理后台切第三方 API）"
                        : "llama-server " + llama + " 已就绪")
                .append('\n');
        sb.append("服务：").append(isServiceRunning() ? "运行中" : "未运行").append('\n');
        // 端口有人应答却不是本 App 起的：多半是上次留下的旧服务，界面看着正常、
        // 实际跑的是旧代码（点接口报 Not Found）。这里必须说出来，否则无从判断。
        if (!isServiceRunning() && isPortServing()) {
            sb.append("端口 ").append(port())
                    .append("：有进程在应答，但不是本 App 启动的（可能是上次残留的服务，")
                    .append("点「启动服务」会先清掉它）\n");
        }
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
