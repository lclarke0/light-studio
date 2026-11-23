package com.lightstudio.app.core;

import java.io.File;
import java.io.OutputStream;

/** Decodes a song file to low-rate mono PCM so very long songs can be analyzed in pieces. */
public interface AudioDecoder {
    /** Length in seconds, or -1 when unknown. */
    double duration(File audio);

    /** Writes mono signed 16-bit little-endian samples at {@code rate} Hz to {@code out}. */
    void decode(File audio, int rate, OutputStream out) throws Exception;
}
