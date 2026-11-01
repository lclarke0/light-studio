package com.lightstudio.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import com.lightstudio.app.core.LightServer;

import org.json.JSONObject;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Light Studio: runs the light-show server on this phone (127.0.0.1 only) and shows the
 * editor in a WebView. Songs, shows and settings live in the app's private storage.
 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 7;
    private static final int PREFERRED_PORT = 47651;
    private static final Pattern URL_RE = Pattern.compile("https?://\\S+");

    private static LightServer server;
    private static AndroidPlatform platform;
    private static int port;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private String pendingUrl;
    private boolean pageReady;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);   // lights follow the song only while the app is open
        try {
            if (server == null) {
                platform = new AndroidPlatform(getApplicationContext());
                LightServer s = new LightServer(platform);
                port = s.start(PREFERRED_PORT);
                server = s;
            }
        } catch (Exception e) {
            TextView t = new TextView(this);
            t.setText("Light Studio couldn't start: " + e);
            t.setPadding(40, 80, 40, 40);
            setContentView(t);
            return;
        }
        platform.setActivity(this);

        web = new WebView(this);
        web.setBackgroundColor(0xFF0C0F14);
        setContentView(web);
        WebView.setWebContentsDebuggingEnabled(true);

        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        st.setMediaPlaybackRequiresUserGesture(false);
        st.setAllowFileAccess(false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("127.0.0.1".equals(uri.getHost())) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } catch (ActivityNotFoundException ignored) { }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                pageReady = true;
                deliverSharedLink();
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("audio/*");
                i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"audio/*", "video/mp4", "application/ogg"});
                try {
                    startActivityForResult(Intent.createChooser(i, "Choose a song"), REQ_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage m) {
                Log.d("LightStudio", m.message() + " (" + m.sourceId() + ":" + m.lineNumber() + ")");
                return true;
            }
        });

        handleShare(getIntent());
        web.loadUrl("http://127.0.0.1:" + port + "/?t=" + server.token());
    }

    /** A YouTube link shared from another app ("Share → Light Studio"). */
    private void handleShare(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return;
        String text = intent.getStringExtra(Intent.EXTRA_TEXT);
        if (text == null) return;
        Matcher m = URL_RE.matcher(text);
        if (m.find()) pendingUrl = m.group();
    }

    private void deliverSharedLink() {
        if (!pageReady || pendingUrl == null || web == null) return;
        web.evaluateJavascript("window.lsImportUrl && lsImportUrl(" + JSONObject.quote(pendingUrl) + ")", null);
        pendingUrl = null;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleShare(intent);
        deliverSharedLink();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (fileCallback != null) fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == AndroidPlatform.REQ_BLE && platform != null) platform.onPermissionResult();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web == null) { finish(); return; }
        web.evaluateJavascript("(window.lsBack && lsBack()) ? 'handled' : 'exit'", v -> {
            if (v == null || !v.contains("handled")) finish();
        });
    }

    @Override
    protected void onPause() {
        if (web != null) {
            web.evaluateJavascript("window.lsFlush && lsFlush(); window.lsPause && lsPause();", null);
            web.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (platform != null) platform.setActivity(null);
        if (isFinishing() && server != null) server.releaseLights();   // hand the lights back to the Govee app
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
