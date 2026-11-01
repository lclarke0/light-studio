package com.lightstudio.app.core;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.MulticastSocket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** Govee Wi-Fi lights over the LAN API (UDP). Whole-light colors use the documented commands;
 *  separate sections stream with the DreamView ("razer") command. */
public final class LanLights {
    public static final int CMD_PORT = 4003, RESP_PORT = 4002, SCAN_PORT = 4001;
    public static final String MULTICAST = "239.255.255.250";

    private static final class State {
        String ip;
        int[] lastRgb;
        int lastBr = -1;
        String streamIp;
        long streamAt;
    }

    private final DatagramSocket sock;
    private final Map<String, State> states = new ConcurrentHashMap<>();

    public LanLights() throws IOException { sock = new DatagramSocket(); }

    private void send(String ip, String cmd, String dataJson) throws IOException {
        byte[] m = ("{\"msg\":{\"cmd\":\"" + cmd + "\",\"data\":" + dataJson + "}}").getBytes(StandardCharsets.UTF_8);
        sock.send(new DatagramPacket(m, m.length, InetAddress.getByName(ip), CMD_PORT));
    }

    private State st(String id) { return states.computeIfAbsent(id, k -> new State()); }

    private void stopStream(State s) {
        if (s.streamIp != null) {
            try { send(s.streamIp, "razer", "{\"pt\":\"" + Packets.razer(0xB1, new int[]{0}) + "\"}"); } catch (IOException ignored) { }
            s.streamIp = null;
        }
    }

    private static String color(int[] c) {
        return "{\"color\":{\"r\":" + c[0] + ",\"g\":" + c[1] + ",\"b\":" + c[2] + "},\"colorTemInKelvin\":0}";
    }

    /** Hands the light back to the Govee app. */
    public void forget(String id) {
        State s = states.remove(id);
        if (s != null) synchronized (s) { stopStream(s); }
    }

    public void forgetAll() { for (String id : new ArrayList<>(states.keySet())) forget(id); }

    public void whole(String id, String ip, int[] rgb, int br) throws IOException {
        State s = st(id);
        synchronized (s) {
            stopStream(s);
            if (!ip.equals(s.ip)) {
                s.ip = ip; s.lastRgb = null; s.lastBr = -1;
                send(ip, "turn", "{\"value\":1}");
            }
            if (!Arrays.equals(rgb, s.lastRgb)) { send(ip, "colorwc", color(rgb)); s.lastRgb = rgb.clone(); }
            if (br != s.lastBr) { send(ip, "brightness", "{\"value\":" + br + "}"); s.lastBr = br; }
        }
    }

    public void sections(String id, String ip, List<int[]> segs) throws IOException {
        State s = st(id);
        synchronized (s) {
            long now = System.currentTimeMillis();
            if (!ip.equals(s.streamIp)) {
                stopStream(s);
                s.ip = null;
                send(ip, "turn", "{\"value\":1}");
                send(ip, "brightness", "{\"value\":100}");
            }
            if (!ip.equals(s.streamIp) || now - s.streamAt > 15000) {
                send(ip, "razer", "{\"pt\":\"" + Packets.razer(0xB1, new int[]{1}) + "\"}");
                s.streamIp = ip; s.streamAt = now;
            }
            int[] data = new int[2 + segs.size() * 3];
            data[0] = 0; data[1] = segs.size();
            for (int k = 0; k < segs.size(); k++) {
                int[] c = segs.get(k);
                data[2 + 3 * k] = c[0]; data[3 + 3 * k] = c[1]; data[4 + 3 * k] = c[2];
            }
            send(ip, "razer", "{\"pt\":\"" + Packets.razer(0xB0, data) + "\"}");
        }
    }

    /** Red, green, blue, white. */
    public void test(String id, String ip) throws IOException, InterruptedException {
        State s = st(id);
        synchronized (s) {
            stopStream(s);
            send(ip, "turn", "{\"value\":1}");
            for (int[] c : new int[][]{{255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 255}}) {
                send(ip, "colorwc", color(c));
                send(ip, "brightness", "{\"value\":100}");
                Thread.sleep(600);
            }
            s.ip = null; s.lastRgb = null; s.lastBr = -1;
        }
    }

    /** Multicast scan; replies arrive on port 4002. Returns [devices, errorOrNull]. */
    public static Object[] discover(long timeoutMs) {
        DatagramSocket recv;
        try {
            recv = new DatagramSocket(null);
            recv.setReuseAddress(true);
            recv.bind(new InetSocketAddress(RESP_PORT));
            recv.setSoTimeout(300);
        } catch (IOException e) {
            return new Object[]{new ArrayList<>(), "UDP port 4002 is busy. Close anything else using the Govee LAN API on this device, or add the light by its IP."};
        }
        Map<String, Map<String, Object>> found = new LinkedHashMap<>();
        byte[] scan = "{\"msg\":{\"cmd\":\"scan\",\"data\":{\"account_topic\":\"reserve\"}}}".getBytes(StandardCharsets.UTF_8);
        try (MulticastSocket send = new MulticastSocket()) {
            send.setTimeToLive(2);
            InetAddress group = InetAddress.getByName(MULTICAST);
            long end = System.currentTimeMillis() + timeoutMs, nextPing = 0;
            byte[] buf = new byte[4096];
            while (System.currentTimeMillis() < end) {
                if (System.currentTimeMillis() >= nextPing) {
                    send.send(new DatagramPacket(scan, scan.length, group, SCAN_PORT));
                    nextPing = System.currentTimeMillis() + 800;
                }
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try { recv.receive(p); } catch (SocketTimeoutException e) { continue; }
                try {
                    Map<String, Object> msg = Json.parseObject(new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8));
                    @SuppressWarnings("unchecked")
                    Map<String, Object> data = (Map<String, Object>) ((Map<String, Object>) msg.get("msg")).get("data");
                    String ip = Json.str(data.get("ip"), p.getAddress().getHostAddress());
                    found.put(ip, Json.obj("ip", ip, "sku", Json.str(data.get("sku"), "?"), "device", Json.str(data.get("device"), "")));
                } catch (RuntimeException ignored) { /* not a Govee reply */ }
            }
        } catch (IOException e) {
            return new Object[]{new ArrayList<>(found.values()), "Couldn't send the scan: " + e.getMessage()};
        } finally {
            recv.close();
        }
        return new Object[]{new ArrayList<>(found.values()), null};
    }
}
