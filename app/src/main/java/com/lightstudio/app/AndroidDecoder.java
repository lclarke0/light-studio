package com.lightstudio.app;

import android.media.AudioFormat;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;

import com.lightstudio.app.core.AudioDecoder;

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.nio.ShortBuffer;

/** Decodes any song Android can play into low-rate mono PCM, streaming, without holding it in memory. */
final class AndroidDecoder implements AudioDecoder {

    private static int audioTrack(MediaExtractor ex) {
        for (int i = 0; i < ex.getTrackCount(); i++) {
            String mime = ex.getTrackFormat(i).getString(MediaFormat.KEY_MIME);
            if (mime != null && mime.startsWith("audio/")) return i;
        }
        return -1;
    }

    private static int intOr(MediaFormat f, String key, int def) {
        try { return f.containsKey(key) ? f.getInteger(key) : def; } catch (RuntimeException e) { return def; }
    }

    @Override public double duration(File audio) {
        MediaExtractor ex = new MediaExtractor();
        try {
            ex.setDataSource(audio.getPath());
            int t = audioTrack(ex);
            if (t < 0) return -1;
            MediaFormat f = ex.getTrackFormat(t);
            return f.containsKey(MediaFormat.KEY_DURATION) ? f.getLong(MediaFormat.KEY_DURATION) / 1e6 : -1;
        } catch (IOException | RuntimeException e) {
            return -1;
        } finally {
            ex.release();
        }
    }

    /** Averages input samples down to the output rate and writes 16-bit little-endian. */
    private static final class Resampler {
        private final OutputStream out;
        private final int outRate;
        private int inRate;
        private double acc, phase;
        private int count;
        private final byte[] buf = new byte[1 << 16];
        private int n;

        Resampler(OutputStream out, int inRate, int outRate) { this.out = out; this.inRate = inRate; this.outRate = outRate; }

        void setInRate(int r) { if (r > 0) inRate = r; }

        void push(float x) throws IOException {
            acc += x;
            count++;
            phase += outRate;
            if (phase >= inRate) {
                phase -= inRate;
                float v = (float) (acc / count);
                acc = 0;
                count = 0;
                int s = Math.max(-32768, Math.min(32767, Math.round(v * 32767f)));
                buf[n++] = (byte) s;
                buf[n++] = (byte) (s >> 8);
                if (n == buf.length) flush();
            }
        }

        void flush() throws IOException {
            if (n > 0) { out.write(buf, 0, n); n = 0; }
        }
    }

    @Override public void decode(File audio, int rate, OutputStream out) throws Exception {
        MediaExtractor ex = new MediaExtractor();
        MediaCodec dec = null;
        try {
            ex.setDataSource(audio.getPath());
            int t = audioTrack(ex);
            if (t < 0) throw new IOException("no audio track in this file");
            ex.selectTrack(t);
            MediaFormat fmt = ex.getTrackFormat(t);
            dec = MediaCodec.createDecoderByType(fmt.getString(MediaFormat.KEY_MIME));
            dec.configure(fmt, null, null, 0);
            dec.start();
            int channels = Math.max(1, intOr(fmt, MediaFormat.KEY_CHANNEL_COUNT, 2));
            boolean floatPcm = false;
            Resampler rs = new Resampler(out, intOr(fmt, MediaFormat.KEY_SAMPLE_RATE, 44100), rate);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            short[] shorts = new short[0];
            float[] floats = new float[0];
            boolean inEos = false, outEos = false;
            while (!outEos) {
                if (!inEos) {
                    int ii = dec.dequeueInputBuffer(10000);
                    if (ii >= 0) {
                        ByteBuffer ib = dec.getInputBuffer(ii);
                        int size = ib == null ? -1 : ex.readSampleData(ib, 0);
                        if (size < 0) {
                            dec.queueInputBuffer(ii, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            inEos = true;
                        } else {
                            dec.queueInputBuffer(ii, 0, size, ex.getSampleTime(), 0);
                            ex.advance();
                        }
                    }
                }
                int oi = dec.dequeueOutputBuffer(info, 10000);
                if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    MediaFormat of = dec.getOutputFormat();
                    channels = Math.max(1, intOr(of, MediaFormat.KEY_CHANNEL_COUNT, channels));
                    rs.setInRate(intOr(of, MediaFormat.KEY_SAMPLE_RATE, 44100));
                    floatPcm = intOr(of, MediaFormat.KEY_PCM_ENCODING, AudioFormat.ENCODING_PCM_16BIT) == AudioFormat.ENCODING_PCM_FLOAT;
                } else if (oi >= 0) {
                    ByteBuffer ob = dec.getOutputBuffer(oi);
                    if (ob != null && info.size > 0) {
                        ob.position(info.offset);
                        ob.limit(info.offset + info.size);
                        ByteBuffer o = ob.slice().order(ByteOrder.nativeOrder());
                        if (floatPcm) {
                            FloatBuffer fb = o.asFloatBuffer();
                            int len = fb.remaining();
                            if (floats.length < len) floats = new float[len];
                            fb.get(floats, 0, len);
                            for (int k = 0; k + channels <= len; k += channels) {
                                float sum = 0;
                                for (int c = 0; c < channels; c++) sum += floats[k + c];
                                rs.push(sum / channels);
                            }
                        } else {
                            ShortBuffer sb = o.asShortBuffer();
                            int len = sb.remaining();
                            if (shorts.length < len) shorts = new short[len];
                            sb.get(shorts, 0, len);
                            float scale = 1f / (32768f * channels);
                            for (int k = 0; k + channels <= len; k += channels) {
                                int sum = 0;
                                for (int c = 0; c < channels; c++) sum += shorts[k + c];
                                rs.push(sum * scale);
                            }
                        }
                    }
                    dec.releaseOutputBuffer(oi, false);
                    if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) outEos = true;
                }
            }
            rs.flush();
        } finally {
            if (dec != null) {
                try { dec.stop(); } catch (RuntimeException ignored) { }
                dec.release();
            }
            ex.release();
        }
    }
}
