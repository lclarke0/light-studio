package com.lightstudio.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.util.Log;

import com.lightstudio.app.core.BleBackend;
import com.lightstudio.app.core.Platform;
import com.lightstudio.app.core.YouTubeBackend;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

final class AndroidPlatform implements Platform {
    static final int REQ_BLE = 8;

    private final Context ctx;
    private final AndroidBle ble;
    private final YouTubeBackend youtube;
    private volatile Activity activity;
    private volatile CountDownLatch permLatch;
    private WifiManager.MulticastLock lock;
    private byte[] index;

    AndroidPlatform(Context c) {
        ctx = c.getApplicationContext();
        ble = new AndroidBle(ctx);
        youtube = new NewPipeYouTube();
    }

    void setActivity(Activity a) { activity = a; }

    void onPermissionResult() {
        CountDownLatch l = permLatch;
        if (l != null) l.countDown();
    }

    @Override public String name() { return "android"; }

    @Override public synchronized byte[] indexHtml() throws IOException {
        if (index == null) {
            try (InputStream in = ctx.getAssets().open("index.html")) {
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
                index = b.toByteArray();
            }
        }
        return index;
    }

    @Override public File dataDir() { return new File(ctx.getFilesDir(), "light_studio_library"); }

    @Override public BleBackend ble() { return ble; }

    @Override public YouTubeBackend youtube() { return youtube; }

    private boolean granted(String p) { return ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED; }

    private boolean bleGranted() {
        if (Build.VERSION.SDK_INT >= 31)
            return granted(Manifest.permission.BLUETOOTH_SCAN) && granted(Manifest.permission.BLUETOOTH_CONNECT);
        return granted(Manifest.permission.ACCESS_FINE_LOCATION);
    }

    @Override public boolean ensureBlePermissions() {
        String[] wanted = Build.VERSION.SDK_INT >= 31
                ? new String[]{Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.ACCESS_FINE_LOCATION}
                : new String[]{Manifest.permission.ACCESS_FINE_LOCATION};
        List<String> missing = new ArrayList<>();
        for (String p : wanted) if (!granted(p)) missing.add(p);
        if (missing.isEmpty()) return true;
        Activity a = activity;
        if (a == null) return bleGranted();
        CountDownLatch l = new CountDownLatch(1);
        permLatch = l;
        String[] ask = missing.toArray(new String[0]);
        a.runOnUiThread(() -> a.requestPermissions(ask, REQ_BLE));
        try { l.await(90, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        permLatch = null;
        return bleGranted();
    }

    @Override public synchronized void multicast(boolean on) {
        try {
            WifiManager wm = (WifiManager) ctx.getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return;
            if (on) {
                if (lock == null) {
                    lock = wm.createMulticastLock("lightstudio");
                    lock.setReferenceCounted(false);
                }
                lock.acquire();
            } else if (lock != null && lock.isHeld()) {
                lock.release();
            }
        } catch (RuntimeException e) {
            log("Multicast lock: " + e);
        }
    }

    @Override public void log(String msg) { Log.i("LightStudio", msg); }
}
