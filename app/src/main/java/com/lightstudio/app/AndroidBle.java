package com.lightstudio.app;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.location.LocationManager;
import android.os.Build;

import com.lightstudio.app.core.BleBackend;
import com.lightstudio.app.core.Json;
import com.lightstudio.app.core.Packets;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Govee Bluetooth lights: one GATT connection per light, latest color sent up to ~15 times a second. */
final class AndroidBle implements BleBackend {
    private static final UUID CHAR = UUID.fromString(Packets.BLE_CHAR_UUID);
    private static final Pattern GOVEE = Pattern.compile("govee|ihoment|minger|^gbk_|^h[0-9]{4}", Pattern.CASE_INSENSITIVE);
    private static final Pattern SKU = Pattern.compile("H[0-9A-F]{4}", Pattern.CASE_INSENSITIVE);

    private static final class Light {
        final String address;
        volatile int[] target = {0, 0, 0};
        volatile String variant = "b";
        volatile String status = "idle";
        volatile long touched;
        volatile boolean stop, running, ready, disconnected, noChar;
        volatile BluetoothGatt gatt;
        volatile BluetoothGattCharacteristic ch;
        volatile CountDownLatch writeLatch;

        Light(String a) { address = a; }
    }

    private final Context ctx;
    private final BluetoothAdapter adapter;
    private final Map<String, Light> lights = new ConcurrentHashMap<>();

    AndroidBle(Context c) {
        ctx = c.getApplicationContext();
        BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        adapter = bm == null ? null : bm.getAdapter();
    }

    @Override public boolean available() { return adapter != null; }

    @Override public void setColor(String address, int[] rgb, String variant) {
        if (adapter == null || address == null || !BluetoothAdapter.checkBluetoothAddress(address)) return;
        Light l = lights.computeIfAbsent(address, Light::new);
        synchronized (l) {
            l.target = rgb.clone();
            l.variant = variant == null ? "b" : variant;
            l.touched = System.currentTimeMillis();
            l.stop = false;
            if (!l.running) {
                l.running = true;
                l.status = "connecting";
                Thread t = new Thread(() -> worker(l), "ble-" + address);
                t.setDaemon(true);
                t.start();
            }
        }
    }

    @Override public String status(String address) {
        Light l = address == null ? null : lights.get(address);
        return l == null ? "idle" : l.status;
    }

    @Override public boolean waitConnected(String address, long timeoutMs) {
        long end = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < end) {
            if ("connected".equals(status(address))) return true;
            sleep(150);
        }
        return false;
    }

    @Override public void release(String address) {
        Light l = address == null ? null : lights.get(address);
        if (l != null) l.stop = true;
    }

    @Override public void shutdown() {
        for (Light l : lights.values()) l.stop = true;
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }

    private final class Callback extends BluetoothGattCallback {
        private final Light l;

        Callback(Light l) { this.l = l; }

        @Override public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                try {
                    if (!g.discoverServices()) l.disconnected = true;
                } catch (SecurityException e) {
                    l.disconnected = true;
                }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED || status != BluetoothGatt.GATT_SUCCESS) {
                l.ready = false;
                l.ch = null;
                l.disconnected = true;
                CountDownLatch w = l.writeLatch;
                if (w != null) w.countDown();
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt g, int status) {
            for (BluetoothGattService s : g.getServices()) {
                BluetoothGattCharacteristic c = s.getCharacteristic(CHAR);
                if (c != null) {
                    l.ch = c;
                    l.ready = true;
                    l.status = "connected";
                    return;
                }
            }
            l.noChar = true;
        }

        @Override public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            CountDownLatch w = l.writeLatch;
            if (w != null) w.countDown();
        }
    }

    @SuppressWarnings("deprecation")
    private boolean write(Light l, byte[] data) {
        BluetoothGatt g = l.gatt;
        BluetoothGattCharacteristic c = l.ch;
        if (g == null || c == null) return false;
        int type = (c.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0
                ? BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE : BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT;
        CountDownLatch latch = new CountDownLatch(1);
        l.writeLatch = latch;
        boolean ok;
        if (Build.VERSION.SDK_INT >= 33) {
            ok = g.writeCharacteristic(c, data, type) == BluetoothStatusCodes.SUCCESS;
        } else {
            c.setWriteType(type);
            c.setValue(data);
            ok = g.writeCharacteristic(c);
        }
        if (!ok) return false;
        try { latch.await(400, TimeUnit.MILLISECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        return true;
    }

    private static void close(BluetoothGatt g) {
        if (g == null) return;
        try { g.disconnect(); } catch (SecurityException ignored) { }
        try { g.close(); } catch (SecurityException ignored) { }
    }

    private void worker(Light l) {
        BluetoothGatt gatt = null;
        long connStart = 0, lastKa = 0;
        int[] lastRgb = null;
        String lastVar = null;
        boolean poweredOn = false;
        int failures = 0;
        try {
            while (!l.stop && System.currentTimeMillis() - l.touched < 60000) {
                long now = System.currentTimeMillis();
                if (!adapter.isEnabled()) {
                    l.status = "Bluetooth is turned off";
                    close(gatt);
                    gatt = null;
                    l.gatt = null;
                    sleep(2000);
                    continue;
                }
                if (gatt == null) {
                    l.ready = false; l.disconnected = false; l.noChar = false; l.ch = null;
                    l.status = "connecting";
                    BluetoothDevice dev = adapter.getRemoteDevice(l.address);
                    gatt = dev.connectGatt(ctx, false, new Callback(l), BluetoothDevice.TRANSPORT_LE);
                    l.gatt = gatt;
                    connStart = now; poweredOn = false; lastRgb = null; failures = 0;
                    sleep(100);
                    continue;
                }
                if (l.disconnected || l.noChar || (!l.ready && now - connStart > 15000) || failures > 25) {
                    boolean wasReady = l.ready || poweredOn;
                    l.status = l.noChar ? "connected, but it doesn't accept Govee Bluetooth commands"
                            : wasReady ? "lost connection" : "can't connect — out of range, or the Govee app is holding it";
                    close(gatt);
                    gatt = null;
                    l.gatt = null;
                    sleep(l.noChar ? 10000 : 3000);
                    continue;
                }
                if (!l.ready) { sleep(100); continue; }
                if (!poweredOn) { write(l, Packets.BLE_POWER_ON); poweredOn = true; }
                int[] want = l.target;
                String v = l.variant;
                if (lastRgb == null || !Arrays.equals(want, lastRgb) || !v.equals(lastVar)) {
                    if (write(l, Packets.bleColor(want, v))) { lastRgb = want; lastVar = v; failures = 0; } else failures++;
                } else if (now - lastKa > 2000) {
                    if (write(l, Packets.BLE_KEEPALIVE)) failures = 0; else failures++;
                    lastKa = now;
                }
                sleep(66);
            }
        } catch (SecurityException e) {
            l.status = "Bluetooth permission needed — open Devices and tap Find Bluetooth lights";
        } catch (RuntimeException e) {
            l.status = "Bluetooth error: " + e.getMessage();
        } finally {
            close(gatt);
            l.gatt = null;
            l.ready = false;
            synchronized (l) {
                l.running = false;
                String s = l.status;
                if ("connected".equals(s) || "connecting".equals(s)) l.status = "idle";
            }
        }
    }

    @Override public List<Map<String, Object>> scan(long millis) throws Exception {
        if (!adapter.isEnabled()) throw new IllegalStateException("Bluetooth is turned off — turn it on and try again.");
        BluetoothLeScanner sc = adapter.getBluetoothLeScanner();
        if (sc == null) throw new IllegalStateException("Bluetooth is turned off — turn it on and try again.");
        Map<String, Map<String, Object>> found = new ConcurrentHashMap<>();
        final int[] failed = {0};
        ScanCallback cb = new ScanCallback() {
            @Override public void onScanResult(int callbackType, ScanResult r) { add(r); }

            @Override public void onBatchScanResults(List<ScanResult> rs) { for (ScanResult r : rs) add(r); }

            @Override public void onScanFailed(int errorCode) { failed[0] = errorCode; }

            private void add(ScanResult r) {
                String name = r.getScanRecord() != null ? r.getScanRecord().getDeviceName() : null;
                if (name == null) {
                    try { name = r.getDevice().getName(); } catch (SecurityException ignored) { }
                }
                if (name == null || !GOVEE.matcher(name).find()) return;
                Matcher m = SKU.matcher(name);
                String addr = r.getDevice().getAddress();
                found.put(addr, Json.obj("address", addr, "name", name, "sku", m.find() ? m.group().toUpperCase() : ""));
            }
        };
        ScanSettings settings = new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        sc.startScan(null, settings, cb);
        try {
            Thread.sleep(millis);
        } finally {
            try { sc.stopScan(cb); } catch (RuntimeException ignored) { }
        }
        if (found.isEmpty() && failed[0] != 0) throw new IOException("code " + failed[0]);
        if (found.isEmpty() && Build.VERSION.SDK_INT >= 28) {
            LocationManager lm = (LocationManager) ctx.getSystemService(Context.LOCATION_SERVICE);
            if (lm != null && !lm.isLocationEnabled())
                throw new IllegalStateException("No lights found. Android needs Location turned on to scan for Bluetooth lights — switch it on in quick settings and try again.");
        }
        return new ArrayList<>(found.values());
    }
}
