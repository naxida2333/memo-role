package com.memorole.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
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
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * memo-role 的安卓外壳：一个 WebView + 一个「连不上时怎么修」的引导页。
 *
 * 为什么不做成自带 Python 的独立 App
 * ----------------------------------
 * Web 层依赖 fastapi + pydantic v2，后者含 Rust 编写的 pydantic-core；
 * 打包进 APK 需要为 Android 交叉编译原生扩展，成本远高于收益。因此这里
 * 走「外壳」路线：服务仍在 Termux 里跑，App 负责把它变成一个可点开的图标。
 *
 * 三件容易踩的事，这里都处理了：
 *  1. 主页加载失败时不能只白屏 —— 要给出可照做的命令（见 showSetup）。
 *  2. 文件管理页的「上传文件」在 WebView 里默认没反应，必须实现
 *     {@link WebChromeClient#onShowFileChooser}。
 *  3. 「下载 / 导出」链接在 WebView 里默认被忽略，必须实现 DownloadListener，
 *     否则用户以为点了没反应。
 */
public class MainActivity extends Activity {

    /** 默认地址：服务与 App 在同一台手机上时就是 127.0.0.1。 */
    private static final String DEFAULT_BASE = "http://127.0.0.1:8000";
    private static final String PREFS = "memo_role";
    private static final String KEY_BASE = "base_url";
    private static final int REQ_FILE = 1001;

    private WebView web;
    private View setupPanel;
    private TextView setupDetail;
    private EditText input;
    private TextView addressLabel;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web);
        setupPanel = findViewById(R.id.setup);
        setupDetail = findViewById(R.id.setup_detail);
        input = findViewById(R.id.setup_input);
        addressLabel = findViewById(R.id.toolbar_address);

        // 允许用桌面 Chrome 的 chrome://inspect 调试这个 WebView（手机排查很有用）
        WebView.setWebContentsDebuggingEnabled(true);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        // 聊天页用 localStorage 记住上次会话，关掉这个会「每次都从零开始」
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setAllowFileAccess(false);
        s.setMediaPlaybackRequiresUserGesture(false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                // 页面真的开始渲染了才收起引导页，避免闪一下又切回来
                hideSetup();
                updateAddressLabel(url);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showSetup(getString(R.string.err_unreachable, baseUrl(), describe(error)));
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                // 文件管理页的「上传文件」按钮会走到这里
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
                // 交给系统下载器，落到「下载」目录；WebView 自己不会处理这个链接
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

        ((Button) findViewById(R.id.btn_refresh)).setOnClickListener(v -> reload());
        ((Button) findViewById(R.id.btn_settings)).setOnClickListener(v -> askAddress());
        ((Button) findViewById(R.id.setup_connect)).setOnClickListener(v -> connect());

        String saved = prefs().getString(KEY_BASE, DEFAULT_BASE);
        input.setText(saved);
        addressLabel.setText(saved);
        web.loadUrl(saved);
    }

    // ------------------------------------------------------------------
    // 引导页 / 地址
    // ------------------------------------------------------------------
    private void connect() {
        String url = normalize(input.getText().toString());
        if (url.isEmpty()) {
            toast(getString(R.string.err_empty_address));
            return;
        }
        prefs().edit().putString(KEY_BASE, url).apply();
        input.setText(url);
        addressLabel.setText(url);
        setupDetail.setText("");
        web.loadUrl(url);
    }

    private void reload() {
        String saved = prefs().getString(KEY_BASE, DEFAULT_BASE);
        web.loadUrl(saved);
    }

    /** 允许用户只填 192.168.1.5:8000 这种写法，自动补上 http:// 与去掉结尾斜杠。 */
    static String normalize(String raw) {
        String url = raw == null ? "" : raw.trim();
        if (url.isEmpty()) {
            return "";
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    private void showSetup(String detail) {
        setupDetail.setText(detail);
        setupPanel.setVisibility(View.VISIBLE);
        web.setVisibility(View.INVISIBLE);
    }

    private void hideSetup() {
        setupPanel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
    }

    private void updateAddressLabel(String url) {
        if (url == null) {
            return;
        }
        String saved = prefs().getString(KEY_BASE, DEFAULT_BASE);
        if (url.startsWith(saved)) {
            addressLabel.setText(saved);
        } else {
            addressLabel.setText(Uri.parse(url).getHost());
        }
    }

    /** 改地址：用系统对话框，省得为了一个输入框再写一个页面。 */
    private void askAddress() {
        final EditText edit = new EditText(this);
        edit.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        edit.setText(prefs().getString(KEY_BASE, DEFAULT_BASE));
        edit.setSelectAllOnFocus(true);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        FrameLayout wrap = new FrameLayout(this);
        wrap.setPadding(pad, pad / 2, pad, 0);
        wrap.addView(edit, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        new AlertDialog.Builder(this)
                .setTitle(R.string.settings_title)
                .setView(wrap)
                .setPositiveButton(R.string.action_connect, (d, w) -> {
                    input.setText(edit.getText());
                    connect();
                })
                .setNegativeButton(R.string.action_cancel, null)
                .show();
    }

    // ------------------------------------------------------------------
    // 返回键 / 文件选择回调
    // ------------------------------------------------------------------
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (setupPanel.getVisibility() == View.VISIBLE) {
                finish();
                return true;
            }
            if (web.canGoBack()) {
                web.goBack();
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (fileCallback != null) {
                // 统一交给框架解析：多选、不同 provider 返回的 Uri 结构都由它兜住
                fileCallback.onReceiveValue(
                        WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                fileCallback = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    // ------------------------------------------------------------------
    // 杂项
    // ------------------------------------------------------------------
    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private String baseUrl() {
        return prefs().getString(KEY_BASE, DEFAULT_BASE);
    }

    private static String describe(WebResourceError error) {
        if (error == null) {
            return "";
        }
        return error.getDescription() + " (" + error.getErrorCode() + ")";
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}