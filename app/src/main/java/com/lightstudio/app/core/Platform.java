package com.lightstudio.app.core;

import java.io.File;
import java.io.IOException;

/** What the core server needs from the device it runs on (Android, or a desktop JVM for testing). */
public interface Platform {
    /** "android" or "desktop". */
    String name();

    byte[] indexHtml() throws IOException;

    /** Folder holding songs, shows and settings. */
    File dataDir();

    BleBackend ble();

    /** Null when YouTube import isn't available. */
    YouTubeBackend youtube();

    /** Asks for Bluetooth permissions if needed (may show a dialog and block); true when granted. */
    boolean ensureBlePermissions();

    /** Lets multicast packets through (needed by Wi-Fi discovery on Android). */
    void multicast(boolean on);

    void log(String msg);
}
