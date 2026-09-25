package com.memorole.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 应用主界面：环境状态与初始化、容器内终端、项目自带的网页界面。
 *
 * <p>与旧版（纯 WebView 外壳）的区别：这里不再依赖手机上另外装 Termux，
 * 而是由 App 自己用 proot 拉起一个 Ubuntu 容器（见 {@link Container}）。
 *
 * <p>所有容器操作都要几秒到几分钟，因此全部丢到后台线程执行，
 * 结果通过 {@link #ui} 回主线程更新界面。
 */
public class MainActivity extends Activity {

    private static final int REQ_FILE = 1001;
    private static final String HOME_PAGE = "/";

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private Container container;

    private TextView statusPill;
    private TextView homeStatus;
    private TextView homeLog;
    private android.widget.ScrollView homeLogScroll;
    private TextView progressText;
    private ProgressBar progress;
    private Button btnSetup;
    private Button btnStart;
    private Button btnStop;
    private Button btnOpenWeb;

    private TextView termOut;
    private android.widget.ScrollView termScroll;
    private EditText termInput;

    private View panelHome;
    private View panelTerminal;
    private View panelWeb;

    private WebView web;
    private TextView webAddress;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        container = new Container(this);

        bindViews();
        setupWebView();

        btnSetup.setOnClickListener(v -> runSetup());
        btnStart.setOnClickListener(v -> startService());
        btnStop.setOnClickListener(v -> stopService());
        btnOpenWeb.setOnClickListener(v -> openWeb(HOME_PAGE));
        findViewById(R.id.btn_settings).setOnClickListener(v -> askSettings());
        findViewById(R.id.btn_reload).setOnClickListener(v -> web.reload());
        findViewById(R.id.nav_home).setOnClickListener(v -> showPanel(0));
        findViewById(R.id.nav_terminal).setOnClickListener(v -> showPanel(1));
        findViewById(R.id.nav_web).setOnClickListener(v -> showPanel(2));

        termInput.setOnEditorActionListener((v, actionId, event) -> {
            runTerminalCommand();
            return true;
        });
        findViewById(R.id.term_run).setOnClickListener(v -> runTerminalCommand());

        refreshStatus();
    }

    private void bindViews() {
        statusPill = findViewById(R.id.status_pill);
        homeStatus = findViewById(R.id.home_status);
        homeLog = findViewById(R.id.home_log);
        homeLogScroll = findViewById(R.id.home_log_scroll);
        // 日志框是首页那个 ScrollView 里嵌套的小滚动区：不声明「别抢我的触摸事件」，
        // 手指一划就会被外层 ScrollView 截走，框内根本滚不动。
        homeLogScroll.setOnTouchListener((v, e) -> {
            v.getParent().requestDisallowInterceptTouchEvent(true);
            return false;
        });
        progressText = findViewById(R.id.home_progress_text);
        progress = findViewById(R.id.home_progress);
        btnSetup = findViewById(R.id.btn_setup);
        btnStart = findViewById(R.id.btn_start);
        btnStop = findViewById(R.id.btn_stop);
        btnOpenWeb = findViewById(R.id.btn_open_web);
        termOut = findViewById(R.id.term_out);
        termScroll = findViewById(R.id.term_scroll);
        termInput = findViewById(R.id.term_input);
        panelHome = findViewById(R.id.panel_home);
        panelTerminal = findViewById(R.id.panel_terminal);
        panelWeb = findViewById(R.id.panel_web);
        web = findViewById(R.id.web);
        webAddress = findViewById(R.id.web_address);
    }

    // ------------------------------------------------------------------
    // 面板切换
    // ------------------------------------------------------------------
    private void showPanel(int index) {
        panelHome.setVisibility(index == 0 ? View.VISIBLE : View.GONE);
        panelTerminal.setVisibility(index == 1 ? View.VISIBLE : View.GONE);
        panelWeb.setVisibility(index == 2 ? View.VISIBLE : View.GONE);
    }

    private void openWeb(String path) {
        showPanel(2);
        web.loadUrl(container.baseUrl() + path);
    }

    // ------------------------------------------------------------------
    // 环境准备
    // ------------------------------------------------------------------

    private void runSetup() {
        if (container.isReady()) {
            log("环境已就绪，无需重复初始化。要更新代码请用「设置 → 更新代码」。");
            startService();
            return;
        }
        btnSetup.setEnabled(false);
        showBusy(true);
        log("开始初始化。首次需要下载约 30 MB 容器 + 安装 Python 依赖，请保持联网。");

        worker.execute(() -> {
            try {
                step("安装 proot 运行时", () -> {
                    if (!container.isRuntimeInstalled()) {
                        container.installRuntime();
                    }
                });

                step("下载 Ubuntu 容器", () -> {
                    if (!container.isRootfsReady()) {
                        container.downloadRootfs(containerProgress());
                    }
                });

                step("解压容器", () -> {
                    if (!container.isRootfsReady()) {
                        container.extractRootfs(containerProgress());
                    }
                });

                step("写入网络与软件源配置", () -> {
                    container.writeResolvConf();
                    container.ensureAptSources();
                });

                step("安装本地推理引擎（llama-server）", () -> container.installLlamaServer());

                // 已经有代码就必须走「覆盖式更新」：fetchProject 是先删后解，
                // 而 models/、data/、config.yaml 都躺在项目目录里（且在 .gitignore 里、
                // 不在 tarball 中），照那个路子走一遍，模型和聊天记录就没了。
                // 升级 APK 的老用户会走到这里（容器与代码都在，只缺新增的东西）。
                if (container.isProjectReady()) {
                    step("更新项目代码", () -> container.updateProject(containerProgress()));
                } else {
                    step("从 GitHub 拉取项目代码", () -> container.fetchProject(containerProgress()));
                }

                step("在容器内安装 Python 依赖", () -> container.runBootstrap(line -> log(line)));

                log("初始化完成。");
                ui.post(() -> {
                    refreshStatus();
                    startService();
                });
            } catch (Exception e) {
                log("初始化失败：" + e.getMessage());
                log("可在「终端」页手动排查，例如执行：proot 已由应用内置，容器路径见设置。");
                ui.post(() -> {
                    btnSetup.setEnabled(true);
                    showBusy(false);
                });
            }
        });
    }

    /** 一个可抛出异常、需要日志的步骤。 */
    private interface Step {
        void run() throws Exception;
    }

    private void step(String title, Step body) throws Exception {
        log("==> " + title);
        body.run();
    }

    private Container.Progress containerProgress() {
        return (what, done, total) -> {
            String text = what + "：" + human(done) + (total > 0 ? " / " + human(total) : "");
            ui.post(() -> {
                progressText.setText(text);
                if (total > 0) {
                    progress.setIndeterminate(false);
                    progress.setMax(1000);
                    progress.setProgress((int) (done * 1000 / total));
                } else {
                    progress.setIndeterminate(true);
                }
            });
        };
    }

    private void showBusy(boolean busy) {
        progress.setVisibility(busy ? View.VISIBLE : View.GONE);
        progressText.setVisibility(busy || !progressText.getText().toString().isEmpty()
                ? View.VISIBLE : View.GONE);
        if (busy) {
            progress.setIndeterminate(true);
        }
    }

    // ------------------------------------------------------------------
    // 服务启停
    // ------------------------------------------------------------------

    private void startService() {
        if (!container.isReady()) {
            log("环境还没准备好，请先点「一键初始化」。");
            return;
        }
        if (container.isServiceRunning()) {
            openWeb(HOME_PAGE);
            return;
        }
        btnStart.setEnabled(false);
        log("启动服务…");
        worker.execute(() -> {
            try {
                container.startService(line -> log(line));
                boolean up = container.waitForService(120000, line -> log(line), container.port());
                if (up) {
                    log("服务已就绪：" + container.baseUrl());
                    ui.post(() -> {
                        refreshStatus();
                        openWeb(HOME_PAGE);
                    });
                } else {
                    log("服务启动超时。请看「终端」页或管理后台的日志。");
                    ui.post(() -> {
                        refreshStatus();
                        btnStart.setEnabled(true);
                    });
                }
            } catch (Exception e) {
                log("启动失败：" + e.getMessage());
                ui.post(() -> {
                    refreshStatus();
                    btnStart.setEnabled(true);
                });
            }
        });
    }

    private void stopService() {
        worker.execute(() -> {
            container.stopService();
            log("服务已停止。");
            ui.post(this::refreshStatus);
        });
    }

    // ------------------------------------------------------------------
    // 终端
    // ------------------------------------------------------------------

    private void runTerminalCommand() {
        String cmd = termInput.getText().toString().trim();
        if (cmd.isEmpty()) {
            return;
        }
        termInput.setText("");
        appendTerm("$ " + cmd + "\n");

        if (!container.isRootfsReady()) {
            appendTerm("容器还没装好，先在「首页」点一键初始化。\n\n");
            return;
        }
        worker.execute(() -> {
            String out = container.exec(cmd, 60000);
            appendTerm(out.endsWith("\n") || out.isEmpty() ? out + "\n" : out + "\n\n");
        });
    }

    // ------------------------------------------------------------------
    // 设置
    // ------------------------------------------------------------------

    private void askSettings() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        box.setPadding(pad, pad / 2, pad, 0);

        TextView portLabel = new TextView(this);
        portLabel.setText(R.string.settings_port);
        box.addView(portLabel);

        final EditText portInput = new EditText(this);
        portInput.setInputType(InputType.TYPE_CLASS_NUMBER);
        portInput.setText(String.valueOf(container.port()));
        box.addView(portInput);

        TextView repoLabel = new TextView(this);
        repoLabel.setText(R.string.settings_repo);
        repoLabel.setPadding(0, pad / 2, 0, 0);
        box.addView(repoLabel);

        final EditText repoInput = new EditText(this);
        repoInput.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        repoInput.setText(container.repoUrl());
        box.addView(repoInput);

        // 代码更新：前端与后端逻辑都在容器里的项目目录中，改完只拉代码即可，
        // 不必为一次改动重装整个容器（那要重下 30MB 容器 + 150MB 依赖）。
        final Button updateBtn = new Button(this);
        updateBtn.setText(R.string.action_update_code);
        updateBtn.setPadding(0, pad / 2, 0, 0);
        box.addView(updateBtn);

        final AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(R.string.settings_title)
                .setView(box)
                .setPositiveButton(R.string.action_save, (d, w) -> {
                    try {
                        int p = Integer.parseInt(portInput.getText().toString().trim());
                        if (p < 1 || p > 65535) {
                            throw new NumberFormatException("端口超范围");
                        }
                        container.setPort(p);
                    } catch (NumberFormatException e) {
                        toast("端口不合法，仍用 " + container.port());
                    }
                    container.setRepoUrl(repoInput.getText().toString().trim());
                    refreshStatus();
                    toast("已保存。改动在下次启动服务时生效。");
                })
                .setNeutralButton(R.string.action_reset, (d, w) -> confirmReset())
                .setNegativeButton(R.string.action_cancel, null)
                .create();
        updateBtn.setOnClickListener(v -> {
            dialog.dismiss();
            updateCode();
        });
        dialog.show();
    }

    /** 只更新容器里的项目代码，不动容器本身与用户数据（模型、配置、聊天记录）。 */
    private void updateCode() {
        showPanel(0);
        if (!container.isProjectReady()) {
            log("容器里还没有项目代码，请先在首页点「一键初始化」。");
            return;
        }
        log("开始更新代码（只覆盖代码文件，模型与聊天数据不动）。");
        showBusy(true);
        worker.execute(() -> {
            boolean wasRunning = container.isServiceRunning();
            try {
                container.updateProject(containerProgress());
                log("代码已更新到最新。");
                if (wasRunning) {
                    log("重启服务以生效…");
                    container.stopService();
                }
                ui.post(() -> {
                    showBusy(false);
                    refreshStatus();
                    if (wasRunning) {
                        startService();
                    }
                });
            } catch (Exception e) {
                log("更新失败：" + e.getMessage());
                ui.post(() -> {
                    showBusy(false);
                    refreshStatus();
                });
            }
        });
    }

    private void confirmReset() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.action_reset)
                .setMessage(R.string.reset_confirm)
                .setPositiveButton(R.string.action_confirm, (d, w) -> worker.execute(() -> {
                    container.stopService();
                    Container.deleteRecursively(container.getRootfs());
                    ui.post(() -> {
                        log("已清空容器。请重新点「一键初始化」。");
                        refreshStatus();
                    });
                }))
                .setNegativeButton(R.string.action_cancel, null)
                .show();
    }

    // ------------------------------------------------------------------
    // 状态展示
    // ------------------------------------------------------------------

    private void refreshStatus() {
        ui.post(() -> {
            homeStatus.setText(container.statusSummary());
            if (container.isServiceRunning()) {
                statusPill.setText(getString(R.string.status_running, container.port()));
            } else if (container.isReady()) {
                statusPill.setText(R.string.status_ready);
            } else {
                statusPill.setText(R.string.status_not_ready);
            }
            btnSetup.setEnabled(!container.isReady());
            btnStart.setEnabled(container.isReady() && !container.isServiceRunning());
            btnStop.setEnabled(container.isServiceRunning());
            btnOpenWeb.setEnabled(container.isServiceRunning());
            webAddress.setText(container.baseUrl());
        });
    }

    // ------------------------------------------------------------------
    // 日志
    // ------------------------------------------------------------------

    private void log(String line) {
        ui.post(() -> {
            String old = homeLog.getText().toString();
            String next = old.isEmpty() ? line : old + "\n" + line;
            // 只留最后 400 行，否则初始化日志会把界面撑爆
            String[] lines = next.split("\n");
            if (lines.length > 400) {
                StringBuilder sb = new StringBuilder();
                for (int i = lines.length - 400; i < lines.length; i++) {
                    sb.append(lines[i]).append('\n');
                }
                next = sb.toString();
            }
            homeLog.setText(next);
            // 日志框高度固定，新行要自己滚到底，否则用户看到的还是最早那几行
            homeLogScroll.post(() -> homeLogScroll.scrollTo(0, homeLog.getBottom()));
        });
    }

    private void appendTerm(String text) {
        ui.post(() -> {
            termOut.append(text);
            termScroll.post(() -> termScroll.fullScroll(View.FOCUS_DOWN));
        });
    }

    // ------------------------------------------------------------------
    // WebView（项目自带的网页界面）
    // ------------------------------------------------------------------

    private void setupWebView() {
        WebView.setWebContentsDebuggingEnabled(true);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setAllowFileAccess(false);
        s.setMediaPlaybackRequiresUserGesture(false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    toast(getString(R.string.web_failed, String.valueOf(error.getDescription())));
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(Intent.createChooser(intent,
                            getString(R.string.pick_file)), REQ_FILE);
                    return true;
                } catch (Exception e) {
                    fileCallback = null;
                    toast(getString(R.string.pick_file_failed, String.valueOf(e.getMessage())));
                    return false;
                }
            }
        });

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String disposition,
                                        String mime, long size) {
                try {
                    String name = URLUtil.guessFileName(url, disposition, mime);
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                    req.setMimeType(mime);
                    req.setTitle(name);
                    req.setNotificationVisibility(
                            DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    if (dm != null) {
                        dm.enqueue(req);
                        toast(getString(R.string.downloading, name));
                    }
                } catch (Exception e) {
                    toast(getString(R.string.download_failed, String.valueOf(e.getMessage())));
                }
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (fileCallback != null) {
                fileCallback.onReceiveValue(
                        WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                fileCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (panelWeb.getVisibility() == View.VISIBLE && web.canGoBack()) {
                web.goBack();
                return true;
            }
            if (panelWeb.getVisibility() == View.VISIBLE) {
                showPanel(0);
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        // 刻意不停服务：切出去就断连，聊天机器人没法用。
        // 服务的生命周期与 App 进程一致。
        worker.shutdown();
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    // 杂项
    // ------------------------------------------------------------------

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density);
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    private static String human(long bytes) {
        if (bytes < 1024) {
            return bytes + " B";
        }
        if (bytes < 1024 * 1024) {
            return String.format(java.util.Locale.US, "%.0f KB", bytes / 1024.0);
        }
        return String.format(java.util.Locale.US, "%.1f MB", bytes / 1024.0 / 1024.0);
    }
}
