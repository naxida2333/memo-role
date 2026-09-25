package com.memorole.app;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** 极简 HTTP 下载工具（只用 JDK 自带的 HttpURLConnection，不引第三方库）。 */
public final class Net {

    /** 下载进度：已收字节 / 总字节（总长未知时为 -1）。 */
    public interface Progress {
        void onProgress(long done, long total);
    }

    /**
     * 带 UA 的下载。
     *
     * <p>UA 不是可选项：GitHub 与 HuggingFace 都会对没有 UA 的请求直接返回 403，
     * 而默认的 Java UA 恰好是它们重点拦截的对象。
     */
    private static final String UA = "memo-role-android/0.2 (+https://github.com/naxida2333/memo-role)";

    private static final int CONNECT_TIMEOUT = 20000;
    private static final int READ_TIMEOUT = 30000;

    private Net() {
    }

    /** 下载到文件；失败时留下 .part 供排查，不污染目标文件。 */
    public static void download(String url, File dest, Progress progress) throws IOException {
        File parent = dest.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs() && !parent.isDirectory()) {
            throw new IOException("无法创建目录：" + parent);
        }
        File part = new File(dest.getAbsolutePath() + ".part");

        HttpURLConnection conn = open(url);
        try {
            int code = conn.getResponseCode();
            if (code != HttpURLConnection.HTTP_OK) {
                throw new IOException("下载失败：HTTP " + code + "（" + url + "）");
            }
            long total = conn.getContentLengthLong();
            long done = 0;
            try (InputStream in = new BufferedInputStream(conn.getInputStream(), 1 << 16);
                 OutputStream out = new FileOutputStream(part)) {
                byte[] buf = new byte[1 << 16];
                int n;
                long lastReport = 0;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    done += n;
                    // 每 256 KB 回调一次，避免刷爆 UI 线程消息队列
                    if (progress != null && done - lastReport > (256 << 10)) {
                        lastReport = done;
                        progress.onProgress(done, total);
                    }
                }
            }
            if (progress != null) {
                progress.onProgress(done, total);
            }
            if (!part.renameTo(dest)) {
                throw new IOException("无法重命名到：" + dest);
            }
        } finally {
            conn.disconnect();
        }
    }

    /** 探测网址是否可访问，返回 HTTP 状态码（失败抛异常）。 */
    public static int head(String url) throws IOException {
        HttpURLConnection conn = open(url);
        try {
            conn.setRequestMethod("HEAD");
            return conn.getResponseCode();
        } finally {
            conn.disconnect();
        }
    }

    private static HttpURLConnection open(String url) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setInstanceFollowRedirects(true);
        conn.setConnectTimeout(CONNECT_TIMEOUT);
        conn.setReadTimeout(READ_TIMEOUT);
        conn.setRequestProperty("User-Agent", UA);
        return conn;
    }
}
