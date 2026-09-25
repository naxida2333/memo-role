import com.memorole.app.TarGz;

import java.io.File;

/**
 * 用 TarGz 解压一个 tar.gz，供测试脚本与系统 tar 的结果做对比。
 *
 * <p>用法：java Harness &lt;archive.tar.gz&gt; &lt;destDir&gt;
 */
public final class Harness {

    private Harness() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            System.err.println("用法: Harness <archive.tar.gz> <destDir>");
            System.exit(2);
        }
        File archive = new File(args[0]);
        File dest = new File(args[1]);
        TarGz.extract(archive, dest, new TarGz.Progress() {
            @Override
            public void onBytes(long read) {
                // 测试里不需要进度，忽略
            }
        });
        System.out.println("解压完成: " + dest);
    }
}
