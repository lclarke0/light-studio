package com.lightstudio.app.core;

import java.util.Base64;

/** Byte formats for Govee lights. */
public final class Packets {
    private Packets() { }

    /** DreamView streaming packet over Wi-Fi: BB 00 len cmd data.. xor, base64-encoded. */
    public static String razer(int cmd, int[] data) {
        byte[] p = new byte[data.length + 5];
        p[0] = (byte) 0xBB;
        p[1] = 0;
        p[2] = (byte) data.length;
        p[3] = (byte) cmd;
        for (int k = 0; k < data.length; k++) p[4 + k] = (byte) data[k];
        int x = 0;
        for (int k = 0; k < p.length - 1; k++) x ^= p[k] & 0xFF;
        p[p.length - 1] = (byte) x;
        return Base64.getEncoder().encodeToString(p);
    }

    /** 20-byte Bluetooth command: payload padded to 19 bytes + xor checksum. */
    public static byte[] ble(int... payload) {
        byte[] b = new byte[20];
        int x = 0;
        for (int k = 0; k < 19; k++) {
            b[k] = (byte) (k < payload.length ? payload[k] : 0);
            x ^= b[k] & 0xFF;
        }
        b[19] = (byte) x;
        return b;
    }

    public static byte[] bleColor(int[] rgb, String variant) {
        int r = rgb[0], g = rgb[1], b = rgb[2];
        if ("a".equals(variant)) return ble(0x33, 0x05, 0x02, r, g, b);                                     // older lights
        if ("c".equals(variant)) return ble(0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, 0xFF, 0x7F);   // segmented lights
        return ble(0x33, 0x05, 0x0D, r, g, b);                                                              // newer lights
    }

    public static final byte[] BLE_KEEPALIVE = ble(0xAA, 0x01);
    public static final byte[] BLE_POWER_ON = ble(0x33, 0x01, 0x01);
    public static final String BLE_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11";
}
