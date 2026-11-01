package com.lightstudio.app.core;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Light Studio's local server: the same HTTP API as the desktop (Python) version, so the
 * shared web UI runs unchanged. Listens on 127.0.0.1 only; API calls need the session cookie.
 */
public final class LightServer {
    private static final Pattern SONG_RE = Pattern.compile("^/api/song/([0-9a-f]{12})(?:/(audio|show|features|separate))?$");
    private static final Pattern YT_JOB_RE = Pattern.compile("^/api/youtube/([0-9a-f]{8})$");
    public static final Pattern YT_RE = Pattern.compile("^https?://((www|m|music)\\.)?(youtube\\.com|youtu\\.be)/.*", Pattern.CASE_INSENSITIVE);
    private static final Set<String> AUDIO_EXTS = new HashSet<>(Arrays.asList(
            ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".webm", ".mp4"));
    private static final Set<String> SHOW_KEYS = new HashSet<>(Arrays.asList(
            "name", "cues", "tracks", "analysis", "duration", "loopA", "loopB", "segCount", "segCounts", "version"));
    private static final long MAX_UPLOAD = 400L * 1024 * 1024;
    private static final int MAX_MINUTES = 30;
    private static final SecureRandom RNG = new SecureRandom();

    private final Platform pf;
    private final File lib, cfgFile;
    private final String token = hex(16);
    private final Object fileLock = new Object();
    private final LanLights lan;
    private final Map<String, Long> hold = new ConcurrentHashMap<>();
    private final Map<String, Map<String, Object>> ytJobs = new ConcurrentHashMap<>();
    private final Map<String, Object[]> summaryCache = new ConcurrentHashMap<>();
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private ServerSocket server;
    private volatile boolean running;

    public LightServer(Platform pf) throws IOException {
        this.pf = pf;
        this.lib = pf.dataDir();
        if (!lib.isDirectory() && !lib.mkdirs()) throw new IOException("Can't create " + lib);
        this.cfgFile = new File(lib, "config.json");
        this.lan = new LanLights();
    }

    public String token() { return token; }

    /** Starts listening; tries the preferred port first (keeps browser storage stable), then any free port. */
    public int start(int preferredPort) throws IOException {
        InetAddress lo = InetAddress.getByName("127.0.0.1");
        try {
            server = new ServerSocket(preferredPort, 50, lo);
        } catch (IOException e) {
            server = new ServerSocket(0, 50, lo);
        }
        running = true;
        Thread t = new Thread(() -> {
            while (running) {
                try {
                    Socket s = server.accept();
                    pool.execute(() -> serve(s));
                } catch (IOException e) {
                    if (!running) break;
                }
            }
        }, "light-server");
        t.setDaemon(true);
        t.start();
        return server.getLocalPort();
    }

    /** Hands every light back to the Govee app. */
    public void releaseLights() {
        lan.forgetAll();
        pf.ble().shutdown();
    }

    public void stop() {
        running = false;
        try { server.close(); } catch (IOException ignored) { }
        releaseLights();
        pool.shutdownNow();
    }

    // =================================================================== HTTP plumbing

    private static final class Req {
        String method, path, query;
        final Map<String, String> headers = new LinkedHashMap<>();
        InputStream body;
        long length;

        String header(String k) { return headers.get(k.toLowerCase(Locale.ROOT)); }
    }

    private static final class HttpError extends RuntimeException {
        final int code;
        HttpError(int code, String msg) { super(msg); this.code = code; }
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') break;
            if (c != '\r') b.write(c);
            if (b.size() > 16384) throw new IOException("Header line too long");
        }
        if (c == -1 && b.size() == 0) return null;
        return b.toString("ISO-8859-1");
    }

    private void serve(Socket sock) {
        try (Socket s = sock) {
            s.setSoTimeout(60000);
            InputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = new BufferedOutputStream(s.getOutputStream(), 65536);
            String line = readLine(in);
            if (line == null || line.isEmpty()) return;
            String[] parts = line.split(" ");
            if (parts.length < 2) return;
            Req r = new Req();
            r.method = parts[0];
            String target = parts[1];
            int q = target.indexOf('?');
            r.path = q >= 0 ? target.substring(0, q) : target;
            r.query = q >= 0 ? target.substring(q + 1) : "";
            String h;
            while ((h = readLine(in)) != null && !h.isEmpty()) {
                int c = h.indexOf(':');
                if (c > 0) r.headers.put(h.substring(0, c).trim().toLowerCase(Locale.ROOT), h.substring(c + 1).trim());
            }
            String cl = r.header("content-length");
            r.length = cl == null ? 0 : Long.parseLong(cl.trim());
            r.body = new Limited(in, r.length);
            try {
                route(r, out);
            } catch (HttpError e) {
                json(out, e.code, Json.obj("error", e.getMessage()));
            } catch (FileNotFoundException e) {
                json(out, 404, Json.obj("error", "That song isn't in the library anymore"));
            } catch (IllegalArgumentException | ClassCastException | IndexOutOfBoundsException e) {
                json(out, 400, Json.obj("error", String.valueOf(e.getMessage())));
            } catch (IOException e) {
                json(out, 502, Json.obj("error", "Network error talking to a light: " + e.getMessage()));
            } catch (RuntimeException e) {
                pf.log("Server error: " + e);
                json(out, 500, Json.obj("error", e.toString()));
            }
            out.flush();
        } catch (IOException | RuntimeException ignored) {
            // connection dropped
        }
    }

    /** Body stream that stops at Content-Length. */
    private static final class Limited extends InputStream {
        private final InputStream in;
        private long left;

        Limited(InputStream in, long n) { this.in = in; this.left = n; }

        @Override public int read() throws IOException {
            if (left <= 0) return -1;
            int c = in.read();
            if (c >= 0) left--;
            return c;
        }

        @Override public int read(byte[] b, int off, int len) throws IOException {
            if (left <= 0) return -1;
            int n = in.read(b, off, (int) Math.min(len, left));
            if (n > 0) left -= n;
            return n;
        }
    }

    private static String statusText(int code) {
        switch (code) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 413: return "Payload Too Large";
            case 502: return "Bad Gateway";
            default: return "Error";
        }
    }

    private static void head(OutputStream out, int code, String ctype, long len, String extra) throws IOException {
        String h = "HTTP/1.1 " + code + " " + statusText(code) + "\r\n"
                + "Content-Type: " + ctype + "\r\n"
                + "Content-Length: " + len + "\r\n"
                + "Cache-Control: no-store\r\n"
                + (extra == null ? "" : extra)
                + "Connection: close\r\n\r\n";
        out.write(h.getBytes(StandardCharsets.ISO_8859_1));
    }

    private static void bytes(OutputStream out, int code, String ctype, byte[] body, String extra) throws IOException {
        head(out, code, ctype, body.length, extra);
        out.write(body);
    }

    private static void json(OutputStream out, int code, Object o) throws IOException {
        bytes(out, code, "application/json", Json.write(o).getBytes(StandardCharsets.UTF_8), null);
    }

    private static void json(OutputStream out, Object o) throws IOException { json(out, 200, o); }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[65536];
        int n;
        while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
        return b.toByteArray();
    }

    private static Map<String, Object> bodyJson(Req r) throws IOException {
        if (r.length > 40L * 1024 * 1024) throw new IllegalArgumentException("Request too large");
        return Json.parseObject(new String(readAll(r.body), StandardCharsets.UTF_8));
    }

    private static byte[] readFile(File f) throws IOException {
        if (!f.isFile()) throw new FileNotFoundException(f.getPath());
        try (InputStream in = new FileInputStream(f)) { return readAll(in); }
    }

    private static void writeJson(File f, Object o) throws IOException {
        File tmp = new File(f.getPath() + ".tmp");
        try (OutputStream out = new FileOutputStream(tmp)) { out.write(Json.write(o).getBytes(StandardCharsets.UTF_8)); }
        if (!tmp.renameTo(f)) {
            f.delete();
            if (!tmp.renameTo(f)) throw new IOException("Couldn't save " + f.getName());
        }
    }

    private static Map<String, Object> readJson(File f) throws IOException {
        return Json.parseObject(new String(readFile(f), StandardCharsets.UTF_8));
    }

    private static String hex(int bytes) {
        byte[] b = new byte[bytes];
        RNG.nextBytes(b);
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(String.format("%02x", x & 0xFF));
        return s.toString();
    }

    private static void deleteTree(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteTree(k);
        f.delete();
    }

    private boolean authorized(Req r) {
        String c = r.header("cookie");
        if (c == null) return false;
        for (String part : c.split(";")) {
            String p = part.trim();
            if (p.equals("ls=" + token)) return true;
        }
        return false;
    }

    // =================================================================== settings

    private static int clampInt(Object v, int def, int lo, int hi) {
        double d = Json.num(v, def);
        if (Double.isNaN(d)) d = def;
        return (int) Math.max(lo, Math.min(hi, (long) d));
    }

    private static String cut(String s, int n) { return s.length() > n ? s.substring(0, n) : s; }

    static Map<String, Object> cleanDevice(Map<String, Object> d) {
        String kind = "ble".equals(d.get("kind")) ? "ble" : "lan";
        String id = cut(Json.str(d.get("id"), "").replaceAll("[^A-Za-z0-9_-]", ""), 24);
        if (id.isEmpty()) id = "d" + hex(4);
        int segs = clampInt(d.get("segments") == null || Json.num(d.get("segments"), 0) == 0 ? 15 : d.get("segments"), 15, 1, 60);
        Object m = d.get("mode");
        String mode = "whole".equals(m) ? "whole" : "segments";
        Object v = d.get("variant");
        String name = Json.str(d.get("name"), "");
        if (name.isEmpty()) name = kind.equals("ble") ? "Bluetooth light" : "Wi-Fi light";
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("id", id);
        o.put("kind", kind);
        o.put("name", cut(name, 40));
        o.put("ip", cut(Json.str(d.get("ip"), "").trim(), 64));
        o.put("address", cut(Json.str(d.get("address"), "").trim(), 64));
        o.put("sku", cut(Json.str(d.get("sku"), ""), 16));
        o.put("mode", kind.equals("ble") ? "whole" : mode);
        o.put("segments", kind.equals("ble") ? 1L : (long) segs);
        o.put("reverse", Json.truthy(d.get("reverse")));
        o.put("enabled", !Boolean.FALSE.equals(d.get("enabled")));
        o.put("variant", "a".equals(v) || "b".equals(v) || "c".equals(v) ? v : "b");
        return o;
    }

    @SuppressWarnings("unchecked")
    Map<String, Object> loadConfig() {
        Map<String, Object> saved;
        try { saved = readJson(cfgFile); } catch (IOException | RuntimeException e) { saved = new LinkedHashMap<>(); }
        Map<String, Object> cfg = new LinkedHashMap<>();
        cfg.put("send", saved.containsKey("send") ? Json.truthy(saved.get("send")) : Boolean.TRUE);
        cfg.put("latency_ms", (long) clampInt(saved.get("latency_ms"), 0, -3000, 3000));
        cfg.put("rate", (long) clampInt(saved.get("rate"), 20, 2, 40));
        cfg.put("master", (long) clampInt(saved.get("master"), 100, 5, 100));
        List<Object> devs = new ArrayList<>();
        Object raw = saved.get("devices");
        if (raw instanceof List) for (Object d : (List<Object>) raw) if (d instanceof Map) devs.add(cleanDevice((Map<String, Object>) d));
        cfg.put("devices", devs);
        return cfg;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> cleanConfig(Map<String, Object> in) {
        Map<String, Object> cfg = loadConfig();
        if (in.containsKey("send")) cfg.put("send", Json.truthy(in.get("send")));
        if (in.containsKey("latency_ms")) cfg.put("latency_ms", (long) clampInt(in.get("latency_ms"), 0, -3000, 3000));
        if (in.containsKey("rate")) cfg.put("rate", (long) clampInt(in.get("rate"), 20, 2, 40));
        if (in.containsKey("master")) cfg.put("master", (long) clampInt(in.get("master"), 100, 5, 100));
        if (in.get("devices") instanceof List) {
            List<Object> devs = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (Object d : (List<Object>) in.get("devices")) {
                if (!(d instanceof Map) || devs.size() >= 24) continue;
                Map<String, Object> c = cleanDevice((Map<String, Object>) d);
                if (seen.add((String) c.get("id"))) devs.add(c);
            }
            cfg.put("devices", devs);
        }
        return cfg;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> devices(Map<String, Object> cfg) {
        return (List<Map<String, Object>>) (List<?>) cfg.get("devices");
    }

    private Map<String, Object> findDevice(String id) {
        for (Map<String, Object> d : devices(loadConfig())) if (id.equals(d.get("id"))) return d;
        return null;
    }

    // =================================================================== library

    @SuppressWarnings("unchecked")
    private static long cueCount(Map<String, Object> show) {
        Object tracks = show.get("tracks");
        if (tracks instanceof List) {
            long n = 0;
            for (Object t : (List<Object>) tracks)
                if (t instanceof Map && ((Map<String, Object>) t).get("cues") instanceof List)
                    n += ((List<Object>) ((Map<String, Object>) t).get("cues")).size();
            return n;
        }
        Object cues = show.get("cues");
        return cues instanceof List ? ((List<Object>) cues).size() : 0;
    }

    @SuppressWarnings("unchecked")
    private List<Object> listLibrary() {
        List<Object[]> items = new ArrayList<>();
        File[] dirs = lib.listFiles();
        if (dirs != null) for (File d : dirs) {
            File f = new File(d, "show.json");
            if (!d.isDirectory() || !f.isFile()) continue;
            File ff = new File(d, "features.json");
            String key = d.getName();
            long stamp = f.lastModified() * 31 + (ff.isFile() ? ff.lastModified() : 0);
            Object[] cached = summaryCache.get(key);
            if (cached != null && (Long) cached[0] == stamp) { items.add(cached); continue; }
            try {
                Map<String, Object> s = readJson(f);
                boolean ai = false;
                if (ff.isFile()) {
                    try { ai = "demucs".equals(readJson(ff).get("source")); } catch (IOException | RuntimeException ignored) { }
                }
                Object an = s.get("analysis");
                Map<String, Object> m = Json.obj(
                        "id", key, "name", Json.str(s.get("name"), "Untitled"), "updated", s.getOrDefault("updated", 0L),
                        "duration", s.get("duration"),
                        "tempo", an instanceof Map ? ((Map<String, Object>) an).get("tempo") : null,
                        "cues", cueCount(s), "ai", ai);
                Object[] entry = {stamp, m};
                summaryCache.put(key, entry);
                items.add(entry);
            } catch (IOException | RuntimeException ignored) { /* unreadable show */ }
        }
        items.sort((a, b) -> Double.compare(Json.num(((Map<String, Object>) b[1]).get("updated"), 0),
                Json.num(((Map<String, Object>) a[1]).get("updated"), 0)));
        List<Object> out = new ArrayList<>();
        for (Object[] e : items) out.add(e[1]);
        return out;
    }

    private static String sniffExt(File f) {
        byte[] h = new byte[12];
        int n;
        try (InputStream in = new FileInputStream(f)) { n = in.read(h); } catch (IOException e) { return null; }
        if (n < 12) return null;
        String a = new String(h, 0, 4, StandardCharsets.ISO_8859_1);
        if (a.startsWith("ID3") || ((h[0] & 0xFF) == 0xFF && (h[1] & 0xE0) == 0xE0)) return ".mp3";
        if (a.equals("RIFF")) return ".wav";
        if (a.equals("OggS")) return ".ogg";
        if (a.equals("fLaC")) return ".flac";
        if (new String(h, 4, 4, StandardCharsets.ISO_8859_1).equals("ftyp")) return ".m4a";
        if ((h[0] & 0xFF) == 0x1A && (h[1] & 0xFF) == 0x45 && (h[2] & 0xFF) == 0xDF && (h[3] & 0xFF) == 0xA3) return ".webm";
        return null;
    }

    private static String mime(String name) {
        String n = name.toLowerCase(Locale.ROOT);
        if (n.endsWith(".mp3")) return "audio/mpeg";
        if (n.endsWith(".wav")) return "audio/wav";
        if (n.endsWith(".ogg") || n.endsWith(".oga") || n.endsWith(".opus")) return "audio/ogg";
        if (n.endsWith(".m4a") || n.endsWith(".mp4") || n.endsWith(".aac")) return "audio/mp4";
        if (n.endsWith(".flac")) return "audio/flac";
        if (n.endsWith(".webm")) return "audio/webm";
        return "application/octet-stream";
    }

    // =================================================================== routes

    private void route(Req r, OutputStream out) throws IOException {
        String p = r.path;
        if (p.equals("/") || p.equals("/index.html")) {
            String extra = null;
            for (String kv : r.query.split("&"))
                if (kv.equals("t=" + token)) extra = "Set-Cookie: ls=" + token + "; Path=/; HttpOnly; SameSite=Strict\r\n";
            bytes(out, 200, "text/html; charset=utf-8", pf.indexHtml(), extra);
            return;
        }
        if (p.equals("/favicon.ico")) { bytes(out, 404, "text/plain", new byte[0], null); return; }
        if (!authorized(r)) throw new HttpError(403, "Not allowed");
        switch (r.method) {
            case "GET": get(r, out); break;
            case "POST": post(r, out); break;
            case "PUT": put(r, out); break;
            case "DELETE": delete(r, out); break;
            default: throw new HttpError(404, "Not found");
        }
    }

    private void get(Req r, OutputStream out) throws IOException {
        String p = r.path;
        switch (p) {
            case "/api/config": json(out, loadConfig()); return;
            case "/api/capabilities":
                json(out, Json.obj("demucs", false, "bluetooth", pf.ble().available(), "youtube", pf.youtube() != null, "platform", pf.name()));
                return;
            case "/api/library": json(out, listLibrary()); return;
            case "/api/discover": {
                pf.multicast(true);
                try {
                    Object[] res = LanLights.discover(2500);
                    json(out, Json.obj("devices", res[0], "error", res[1]));
                } finally {
                    pf.multicast(false);
                }
                return;
            }
            case "/api/devices/status": {
                Map<String, Object> st = new LinkedHashMap<>();
                for (Map<String, Object> d : devices(loadConfig()))
                    st.put((String) d.get("id"), "ble".equals(d.get("kind")) ? pf.ble().status((String) d.get("address")) : "");
                json(out, st);
                return;
            }
            case "/api/ble/scan": {
                requireBle();
                try {
                    json(out, Json.obj("devices", pf.ble().scan(6000)));
                } catch (IllegalStateException e) {
                    throw new HttpError(400, e.getMessage());
                } catch (Exception e) {
                    throw new HttpError(500, "Bluetooth scan failed (" + e.getMessage() + "). Is Bluetooth turned on?");
                }
                return;
            }
            default: break;
        }
        Matcher yj = YT_JOB_RE.matcher(p);
        if (yj.matches()) {
            Map<String, Object> job = ytJobs.get(yj.group(1));
            json(out, job != null ? job : Json.obj("state", "error", "msg", "Unknown download"));
            return;
        }
        Matcher m = SONG_RE.matcher(p);
        if (!m.matches()) throw new HttpError(404, "Not found");
        String sid = m.group(1), what = m.group(2);
        File d = new File(lib, sid);
        if ("show".equals(what)) { bytes(out, 200, "application/json", readFile(new File(d, "show.json")), null); return; }
        if ("features".equals(what)) {
            File f = new File(d, "features.json");
            if (!f.isFile()) json(out, new LinkedHashMap<>());
            else bytes(out, 200, "application/json", readFile(f), null);
            return;
        }
        if ("separate".equals(what)) { json(out, Json.obj("state", "idle")); return; }
        if ("audio".equals(what)) {
            Map<String, Object> show = readJson(new File(d, "show.json"));
            File f = new File(d, Json.str(show.get("file"), "audio.mp3"));
            if (!f.isFile() || !f.getCanonicalPath().startsWith(d.getCanonicalPath())) throw new FileNotFoundException();
            head(out, 200, mime(f.getName()), f.length(), null);
            try (InputStream in = new FileInputStream(f)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            }
            return;
        }
        throw new HttpError(404, "Not found");
    }

    private void post(Req r, OutputStream out) throws IOException {
        String p = r.path;
        switch (p) {
            case "/api/frame": frame(r, out); return;
            case "/api/config": {
                Map<String, Object> cfg;
                Map<String, Object> old;
                synchronized (fileLock) {
                    old = loadConfig();
                    cfg = cleanConfig(bodyJson(r));
                    writeJson(cfgFile, cfg);
                }
                Map<String, Map<String, Object>> now = new LinkedHashMap<>();
                for (Map<String, Object> d : devices(cfg)) now.put((String) d.get("id"), d);
                boolean send = Json.truthy(cfg.get("send"));
                for (Map<String, Object> d : devices(old)) {
                    Map<String, Object> n = now.get(d.get("id"));
                    boolean gone = n == null || !Json.truthy(n.get("enabled")) || !send;
                    if ("lan".equals(d.get("kind")) && (gone || !n.get("ip").equals(d.get("ip")) || !n.get("mode").equals(d.get("mode"))))
                        lan.forget((String) d.get("id"));
                    if ("ble".equals(d.get("kind")) && (gone || !n.get("address").equals(d.get("address"))))
                        pf.ble().release((String) d.get("address"));
                }
                json(out, cfg);
                return;
            }
            case "/api/devices/test":
            case "/api/devices/probe": {
                Map<String, Object> d = findDevice(Json.str(bodyJson(r).get("id"), ""));
                if (d == null) throw new HttpError(404, "That light isn't in your device list");
                if (p.endsWith("test")) testDevice(d); else probeDevice(d);
                json(out, Json.obj("ok", true));
                return;
            }
            case "/api/upload": upload(r, out); return;
            case "/api/youtube": {
                String url = Json.str(bodyJson(r).get("url"), "").trim();
                if (!YT_RE.matcher(url).matches()) throw new HttpError(400, "That doesn't look like a YouTube link");
                if (pf.youtube() == null) throw new HttpError(400, "YouTube import isn't available in this version.");
                String jid = hex(4);
                ytJobs.put(jid, new ConcurrentHashMap<>(Json.obj("state", "running", "msg", "Looking up the video…")));
                pool.execute(() -> youtubeJob(jid, url));
                json(out, Json.obj("job", jid));
                return;
            }
            default: break;
        }
        Matcher m = SONG_RE.matcher(p);
        if (m.matches() && "show".equals(m.group(2))) { saveShow(m.group(1), r, out); return; }   // sendBeacon on page close
        if (m.matches() && "separate".equals(m.group(2)))
            throw new HttpError(400, "AI instrument separation is only in the desktop version of Light Studio.");
        throw new HttpError(404, "Not found");
    }

    private void put(Req r, OutputStream out) throws IOException {
        Matcher m = SONG_RE.matcher(r.path);
        if (m.matches() && "show".equals(m.group(2))) { saveShow(m.group(1), r, out); return; }
        if (m.matches() && "features".equals(m.group(2))) {
            File d = new File(lib, m.group(1));
            if (!d.isDirectory()) throw new FileNotFoundException();
            Map<String, Object> in = bodyJson(r);
            File f = new File(d, "features.json");
            synchronized (fileLock) {
                if (f.isFile() && !"demucs".equals(in.get("source"))) {
                    try {
                        if ("demucs".equals(readJson(f).get("source"))) { json(out, Json.obj("ok", false, "kept", "demucs")); return; }
                    } catch (IOException | RuntimeException ignored) { }
                }
                writeJson(f, in);
            }
            json(out, Json.obj("ok", true));
            return;
        }
        throw new HttpError(404, "Not found");
    }

    private void delete(Req r, OutputStream out) throws IOException {
        Matcher m = SONG_RE.matcher(r.path);
        if (m.matches() && m.group(2) == null) {
            File d = new File(lib, m.group(1));
            if (d.isDirectory()) deleteTree(d);
            summaryCache.remove(m.group(1));
            json(out, Json.obj("ok", true));
            return;
        }
        throw new HttpError(404, "Not found");
    }

    // =================================================================== handlers

    private void saveShow(String sid, Req r, OutputStream out) throws IOException {
        Map<String, Object> in = bodyJson(r);
        double updated;
        synchronized (fileLock) {
            File f = new File(new File(lib, sid), "show.json");
            Map<String, Object> show = readJson(f);
            for (String k : SHOW_KEYS) if (in.containsKey(k)) show.put(k, in.get(k));
            updated = System.currentTimeMillis() / 1000.0;
            show.put("updated", updated);
            writeJson(f, show);
        }
        json(out, Json.obj("ok", true, "updated", updated));
    }

    private void upload(Req r, OutputStream out) throws IOException {
        String name = URLDecoder.decode(Json.str(r.header("x-filename"), "song"), "UTF-8");
        if (r.length <= 0) throw new HttpError(400, "Empty file");
        if (r.length > MAX_UPLOAD) throw new HttpError(413, "File is larger than 400 MB");
        int slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
        if (slash >= 0) name = name.substring(slash + 1);
        int dot = name.lastIndexOf('.');
        String ext = dot > 0 ? name.substring(dot).toLowerCase(Locale.ROOT) : "";
        String stem = dot > 0 ? name.substring(0, dot) : name;
        String sid = hex(6);
        File d = new File(lib, sid);
        if (!d.mkdirs()) throw new IOException("Couldn't create a folder for the song");
        File tmp = new File(d, "upload.tmp");
        try (OutputStream f = new FileOutputStream(tmp)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = r.body.read(buf)) > 0) f.write(buf, 0, n);
        }
        if (!AUDIO_EXTS.contains(ext)) ext = sniffExt(tmp);
        if (ext == null) {
            deleteTree(d);
            throw new HttpError(400, "That doesn't look like an audio file. Use mp3, wav, ogg, m4a, flac or opus.");
        }
        File audio = new File(d, "audio" + ext);
        if (!tmp.renameTo(audio)) throw new IOException("Couldn't save the song");
        if (stem.trim().isEmpty() || stem.matches("^(msf|primary|document|audio|image):?\\d*$")) stem = "Untitled song";
        double now = System.currentTimeMillis() / 1000.0;
        Map<String, Object> show = Json.obj("name", cut(stem.trim(), 120), "file", audio.getName(), "created", now, "updated", now,
                "version", 2L, "duration", null, "analysis", null, "tracks", new ArrayList<>(), "loopA", null, "loopB", null);
        writeJson(new File(d, "show.json"), show);
        json(out, Json.obj("id", sid));
    }

    private static int[] brightest(List<int[]> segs) {
        int[] best = {0, 0, 0};
        int top = -1;
        for (int[] c : segs) {
            int m = Math.max(c[0], Math.max(c[1], c[2]));
            if (m > top) { top = m; best = c; }
        }
        return best;
    }

    @SuppressWarnings("unchecked")
    private void frame(Req r, OutputStream out) throws IOException {
        Map<String, Object> body = bodyJson(r);
        Map<String, Object> cfg = loadConfig();
        if (!Json.truthy(cfg.get("send"))) { json(out, Json.obj("sent", false)); return; }
        Map<String, Map<String, Object>> devs = new LinkedHashMap<>();
        for (Map<String, Object> d : devices(cfg)) if (Json.truthy(d.get("enabled"))) devs.put((String) d.get("id"), d);
        long now = System.currentTimeMillis();
        Map<String, Object> errors = new LinkedHashMap<>();
        Object all = body.get("devices");
        if (all instanceof Map) for (Map.Entry<String, Object> e : ((Map<String, Object>) all).entrySet()) {
            Map<String, Object> d = devs.get(e.getKey());
            Long until = hold.get(e.getKey());
            if (d == null || (until != null && until > now) || !(e.getValue() instanceof List)) continue;
            List<int[]> segs = new ArrayList<>();
            for (Object s : (List<Object>) e.getValue()) {
                if (!(s instanceof List) || segs.size() >= 84) continue;
                List<Object> c = (List<Object>) s;
                int[] rgb = new int[3];
                for (int k = 0; k < 3; k++) rgb[k] = (int) Math.max(0, Math.min(255, Json.num(k < c.size() ? c.get(k) : 0, 0)));
                segs.add(rgb);
            }
            if (segs.isEmpty()) continue;
            String ip = (String) d.get("ip");
            try {
                if ("ble".equals(d.get("kind"))) {
                    pf.ble().setColor((String) d.get("address"), brightest(segs), (String) d.get("variant"));
                } else if (!ip.isEmpty()) {
                    if ("whole".equals(d.get("mode"))) {
                        int[] c = brightest(segs);
                        int top = Math.max(c[0], Math.max(c[1], c[2]));
                        if (top == 0) lan.whole(e.getKey(), ip, new int[]{0, 0, 0}, 100);
                        else lan.whole(e.getKey(), ip, new int[]{c[0] * 255 / top, c[1] * 255 / top, c[2] * 255 / top},
                                Math.max(1, Math.min(100, Math.round(top / 255f * 100))));
                    } else {
                        lan.sections(e.getKey(), ip, segs);
                    }
                }
            } catch (IOException ex) {
                errors.put(e.getKey(), String.valueOf(ex.getMessage()));
            }
        }
        json(out, Json.obj("sent", true, "errors", errors));
    }

    private void requireBle() {
        if (!pf.ble().available()) throw new HttpError(400, "This device doesn't have Bluetooth.");
        if (!pf.ensureBlePermissions())
            throw new HttpError(400, "Light Studio needs the Bluetooth (and Location) permission to find and control bulbs. Allow it in Android Settings → Apps → Light Studio → Permissions.");
    }

    private void testDevice(Map<String, Object> d) {
        String id = (String) d.get("id");
        if ("lan".equals(d.get("kind"))) {
            String ip = (String) d.get("ip");
            if (ip.isEmpty()) throw new HttpError(400, "This light has no IP address yet");
            hold.put(id, System.currentTimeMillis() + 4000);
            pool.execute(() -> {
                try { lan.test(id, ip); } catch (IOException | InterruptedException e) { pf.log("Test failed: " + e); }
            });
            return;
        }
        requireBle();
        String addr = (String) d.get("address"), variant = (String) d.get("variant");
        pool.execute(() -> {
            hold.put(id, System.currentTimeMillis() + 30000);
            BleBackend ble = pf.ble();
            ble.setColor(addr, new int[]{0, 0, 0}, variant);
            ble.waitConnected(addr, 15000);
            for (int[] c : new int[][]{{255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 255}}) {
                ble.setColor(addr, c, variant);
                sleep(800);
            }
            hold.remove(id);
        });
    }

    /** Tries each Bluetooth command set: A shows red, B green, C blue. */
    private void probeDevice(Map<String, Object> d) {
        if (!"ble".equals(d.get("kind"))) throw new HttpError(400, "Only Bluetooth lights have command sets");
        requireBle();
        String id = (String) d.get("id"), addr = (String) d.get("address");
        pool.execute(() -> {
            hold.put(id, System.currentTimeMillis() + 40000);
            BleBackend ble = pf.ble();
            ble.setColor(addr, new int[]{0, 0, 0}, "b");
            ble.waitConnected(addr, 15000);
            sleep(500);
            String[] vs = {"a", "b", "c"};
            int[][] cs = {{255, 0, 0}, {0, 255, 0}, {0, 0, 255}};
            for (int k = 0; k < 3; k++) {
                ble.setColor(addr, cs[k], vs[k]);
                sleep(2000);
            }
            hold.remove(id);
        });
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }

    private void youtubeJob(String jid, String url) {
        Map<String, Object> job = ytJobs.get(jid);
        String sid = hex(6);
        File d = new File(lib, sid);
        try {
            if (!d.mkdirs()) throw new IOException("Couldn't create a folder for the song");
            YouTubeBackend.Result res = pf.youtube().fetch(url, d, MAX_MINUTES * 60L, MAX_UPLOAD, msg -> job.put("msg", msg));
            if (res.file == null || !res.file.isFile()) throw new IOException("the download didn't produce an audio file");
            String title = res.title == null ? "" : res.title.replaceAll("\\s+", " ").trim();
            if (title.isEmpty()) title = "YouTube song";
            double now = System.currentTimeMillis() / 1000.0;
            writeJson(new File(d, "show.json"), Json.obj(
                    "name", cut(title, 120), "file", res.file.getName(), "source", res.source == null ? url : res.source,
                    "created", now, "updated", now, "version", 2L,
                    "duration", res.duration > 0 ? res.duration : null, "analysis", null, "tracks", new ArrayList<>(),
                    "loopA", null, "loopB", null));
            ytJobs.put(jid, new ConcurrentHashMap<>(Json.obj("state", "done", "id", sid, "msg", "Done")));
        } catch (Exception e) {
            deleteTree(d);
            String msg = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage().replace("ERROR: ", "").trim();
            pf.log("YouTube import failed: " + e);
            ytJobs.put(jid, new ConcurrentHashMap<>(Json.obj("state", "error", "msg", cut(msg, 400))));
        }
    }
}
