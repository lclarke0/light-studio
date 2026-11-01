package com.lightstudio.app.core;

import java.util.List;
import java.util.Map;

/** Bluetooth lights: keeps a connection per address and sends it the latest color. */
public interface BleBackend {
    boolean available();

    void setColor(String address, int[] rgb, String variant);

    /** "idle", "connecting", "connected", or a problem description. */
    String status(String address);

    boolean waitConnected(String address, long timeoutMs);

    void release(String address);

    void shutdown();

    /** Each entry: address, name, sku. */
    List<Map<String, Object>> scan(long millis) throws Exception;
}
