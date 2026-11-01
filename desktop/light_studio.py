#!/usr/bin/env python3
"""
Light Studio - make, edit and play music light shows on a Govee LAN strip (e.g. H618C),
with separate light sections for drums, bass, vocals and other instruments.

HOW TO RUN
  python light_studio.py              -> opens http://127.0.0.1:8765 in your browser
  options:  --port 8765   --no-browser

  * Needs only Python 3.8+. Use Chrome or Edge.
  * In the Govee Home app: your strip -> settings (gear) -> turn on "LAN Control".
  * Songs and shows are saved in the "light_studio_library" folder next to this file.

OPTIONAL: Bluetooth lights (e.g. Govee H6010 BR30 bulbs without LAN Control)
  pip install bleak
  Then restart Light Studio and use Devices -> Find Bluetooth lights.

OPTIONAL: add songs from YouTube links
  pip install yt-dlp        (if YouTube downloads start failing later: pip install -U yt-dlp)

OPTIONAL: real instrument separation (AI)
  pip install demucs
  Then restart Light Studio. Without it, instruments are estimated from frequency ranges.
  (Demucs pulls in PyTorch, a large download. It runs on your GPU if PyTorch has CUDA,
   otherwise on the CPU - roughly 1-3 minutes per song.)
"""

import argparse
import asyncio
import base64
import importlib.util
import io
import json
import mimetypes
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / "light_studio_library"
CONFIG_PATH = LIBRARY / "config.json"
DEFAULT_CONFIG = {"send": True, "latency_ms": 0, "rate": 20, "master": 100, "devices": []}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".webm", ".mp4"}
YT_RE = re.compile(r"^https?://((www|m|music)\.)?(youtube\.com|youtu\.be)/", re.I)
MAX_MINUTES = 30
MAX_UPLOAD = 400 * 1024 * 1024
SONG_RE = re.compile(r"^/api/song/([0-9a-f]{12})(?:/(audio|show|features|separate))?$")
SHOW_KEYS = {"name", "cues", "tracks", "analysis", "duration", "loopA", "loopB", "segCount", "segCounts", "version"}

GOVEE_CMD_PORT = 4003
GOVEE_RESP_PORT = 4002
GOVEE_MULTICAST = ("239.255.255.250", 4001)

_file_lock = threading.Lock()


# ----------------------------------------------------------------------------- storage

def write_json(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def clean_device(d):
    kind = "ble" if d.get("kind") == "ble" else "lan"
    did = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("id") or ""))[:24] or "d" + secrets.token_hex(4)
    try:
        segs = max(1, min(60, int(float(d.get("segments") or 15))))
    except (TypeError, ValueError):
        segs = 15
    mode = d.get("mode") if d.get("mode") in ("segments", "whole") else "segments"
    return {
        "id": did, "kind": kind,
        "name": str(d.get("name") or ("Bluetooth light" if kind == "ble" else "Wi-Fi light"))[:40],
        "ip": str(d.get("ip") or "").strip()[:64], "address": str(d.get("address") or "").strip()[:64],
        "sku": str(d.get("sku") or "")[:16],
        "mode": "whole" if kind == "ble" else mode, "segments": 1 if kind == "ble" else segs,
        "reverse": bool(d.get("reverse")), "enabled": d.get("enabled", True) is not False,
        "variant": d.get("variant") if d.get("variant") in ("a", "b", "c") else "b",
    }


def load_config():
    try:
        saved = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        saved = {}
    cfg = {**DEFAULT_CONFIG, **{k: v for k, v in saved.items() if k in DEFAULT_CONFIG}}
    if not cfg["devices"] and saved.get("ip"):          # settings from the single-strip version
        cfg["devices"] = [{"id": "strip", "name": "Light strip", "kind": "lan", "ip": saved["ip"],
                           "mode": saved.get("mode", "segments"), "segments": saved.get("segments", 15),
                           "reverse": saved.get("reverse", False)}]
    cfg["devices"] = [clean_device(d) for d in cfg["devices"] if isinstance(d, dict)]
    return cfg


def clean_config(new):
    cfg = load_config()
    if "send" in new:
        cfg["send"] = bool(new["send"])
    if "latency_ms" in new:
        cfg["latency_ms"] = max(-3000, min(3000, int(float(new["latency_ms"] or 0))))
    if "rate" in new:
        cfg["rate"] = max(2, min(40, int(float(new["rate"] or 20))))
    if "master" in new:
        cfg["master"] = max(5, min(100, int(float(new["master"] or 100))))
    if isinstance(new.get("devices"), list):
        seen, devs = set(), []
        for d in new["devices"][:24]:
            if isinstance(d, dict):
                d = clean_device(d)
                if d["id"] not in seen:
                    seen.add(d["id"])
                    devs.append(d)
        cfg["devices"] = devs
    return cfg


def read_show(sid):
    return json.loads((LIBRARY / sid / "show.json").read_text())


def cue_count(show):
    if isinstance(show.get("tracks"), list):
        return sum(len(t.get("cues") or []) for t in show["tracks"] if isinstance(t, dict))
    return len(show.get("cues") or [])


def list_library():
    items = []
    for d in LIBRARY.iterdir():
        f = d / "show.json"
        if not (d.is_dir() and f.exists()):
            continue
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        feats = d / "features.json"
        source = None
        if feats.exists():
            try:
                source = json.loads(feats.read_text()).get("source")
            except (OSError, ValueError):
                pass
        items.append({
            "id": d.name, "name": s.get("name") or "Untitled", "updated": s.get("updated", 0),
            "duration": s.get("duration"), "tempo": (s.get("analysis") or {}).get("tempo"),
            "cues": cue_count(s), "ai": source == "demucs",
        })
    items.sort(key=lambda i: i["updated"], reverse=True)
    return items


# ----------------------------------------------------------------------------- Govee strip

def razer_b64(cmd, data):
    """DreamView streaming packet: BB 00 <len> <cmd> <data...> <xor>, base64-encoded."""
    pkt = bytes([0xBB, 0x00, len(data), cmd]) + bytes(data)
    x = 0
    for b in pkt:
        x ^= b
    return base64.b64encode(pkt + bytes([x])).decode()


class Strip:
    """Sends Govee LAN commands. Whole-strip mode uses the documented colorwc command;
    section mode streams per-section colors with the DreamView ("razer") command."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.ip = None
        self.last_rgb = None
        self.last_br = None
        self.stream_ip = None
        self.stream_at = 0.0

    def _send(self, ip, cmd, data):
        msg = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        self.sock.sendto(msg, (ip, GOVEE_CMD_PORT))

    def _stop_stream(self):
        if self.stream_ip:
            try:
                self._send(self.stream_ip, "razer", {"pt": razer_b64(0xB1, [0])})
            except OSError:
                pass
            self.stream_ip = None

    def forget(self):
        with self.lock:
            self._stop_stream()
            self.ip = None
            self.last_rgb = self.last_br = None

    def whole(self, ip, rgb, br):
        with self.lock:
            self._stop_stream()
            if ip != self.ip:
                self.ip, self.last_rgb, self.last_br = ip, None, None
                self._send(ip, "turn", {"value": 1})
            if rgb != self.last_rgb:
                r, g, b = rgb
                self._send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
                self.last_rgb = rgb
            if br != self.last_br:
                self._send(ip, "brightness", {"value": br})
                self.last_br = br

    def sections(self, ip, segs):
        with self.lock:
            now = time.time()
            if ip != self.stream_ip:
                self._stop_stream()
                self.ip = None
                self._send(ip, "turn", {"value": 1})
                self._send(ip, "brightness", {"value": 100})
            if ip != self.stream_ip or now - self.stream_at > 15:
                self._send(ip, "razer", {"pt": razer_b64(0xB1, [1])})   # (re)enter streaming mode
                self.stream_ip, self.stream_at = ip, now
            data = [0x00, len(segs)]
            for r, g, b in segs:
                data += [r, g, b]
            self._send(ip, "razer", {"pt": razer_b64(0xB0, data)})

    def test(self, ip):
        with self.lock:
            self._stop_stream()
            self._send(ip, "turn", {"value": 1})
            for r, g, b in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]:
                self._send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
                self._send(ip, "brightness", {"value": 100})
                time.sleep(0.6)
            self.ip = None
            self.last_rgb = self.last_br = None


STRIPS = {}            # device id -> Strip (one per Wi-Fi light)
HOLD = {}              # device id -> time until which show frames are ignored (during Test / probe)


def lan_strip(did):
    if did not in STRIPS:
        STRIPS[did] = Strip()
    return STRIPS[did]


def brightest(segs):
    return max(segs, key=lambda c: max(c)) if segs else (0, 0, 0)


# ----------------------------------------------------------------------------- Bluetooth lights

BLE_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b11"


def ble_packet(payload):
    body = bytes(payload)[:19].ljust(19, b"\0")
    x = 0
    for b in body:
        x ^= b
    return body + bytes([x])


def ble_color_packet(rgb, variant):
    r, g, b = rgb
    if variant == "a":      # older Govee lights
        return ble_packet([0x33, 0x05, 0x02, r, g, b])
    if variant == "c":      # segmented (RGBIC) lights, all segments
        return ble_packet([0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, 0xFF, 0x7F])
    return ble_packet([0x33, 0x05, 0x0D, r, g, b])   # newer Govee lights


BLE_KEEPALIVE = ble_packet([0xAA, 0x01])
BLE_POWER_ON = ble_packet([0x33, 0x01, 0x01])


class BleManager:
    """Keeps one Bluetooth connection per light and sends it the latest color (max ~15/s)."""

    def __init__(self):
        self.available = importlib.util.find_spec("bleak") is not None
        self.loop = None
        self.lock = threading.Lock()
        self.devs = {}

    def _ensure_loop(self):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def set_color(self, address, rgb, variant):
        if not self.available or not address:
            return
        self._ensure_loop()
        with self.lock:
            d = self.devs.setdefault(address, {"target": rgb, "variant": variant, "status": "idle", "running": False})
            d.update(target=tuple(rgb), variant=variant, touched=time.time(), stop=False)
            if not d["running"]:
                d["running"] = True
                d["status"] = "connecting"
                asyncio.run_coroutine_threadsafe(self._worker(address), self.loop)

    def status(self, address):
        d = self.devs.get(address)
        return d["status"] if d else "idle"

    def wait_connected(self, address, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if self.status(address) == "connected":
                return True
            time.sleep(0.2)
        return False

    def release(self, address):
        d = self.devs.get(address)
        if d:
            d["stop"] = True

    def shutdown(self):
        for d in list(self.devs.values()):
            d["stop"] = True
        if self.devs:
            time.sleep(0.8)

    async def _write(self, client, data):
        try:
            await client.write_gatt_char(BLE_CHAR, data, response=False)
        except Exception:
            await client.write_gatt_char(BLE_CHAR, data, response=True)

    async def _worker(self, address):
        from bleak import BleakClient
        d = self.devs[address]
        client, last, last_ka = None, None, 0.0
        try:
            while not d.get("stop") and time.time() - d["touched"] < 60:
                if client is None or not client.is_connected:
                    d["status"] = "connecting"
                    try:
                        client = BleakClient(address, timeout=12)
                        await client.connect()
                        await self._write(client, BLE_POWER_ON)
                        d["status"], last = "connected", None
                    except Exception as e:
                        d["status"] = f"can't connect ({type(e).__name__}: {e})"[:160]
                        client = None
                        await asyncio.sleep(3)
                        continue
                want = (d["target"], d["variant"])
                try:
                    if want != last:
                        await self._write(client, ble_color_packet(*want))
                        last = want
                    elif time.time() - last_ka > 2:
                        await self._write(client, BLE_KEEPALIVE)
                        last_ka = time.time()
                except Exception as e:
                    d["status"] = f"lost connection ({type(e).__name__})"
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    client = None
                    await asyncio.sleep(1)
                    continue
                await asyncio.sleep(1 / 15)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            with self.lock:
                d["running"] = False
                if not str(d["status"]).startswith("can't"):
                    d["status"] = "idle"

    def scan(self, timeout=6.0):
        from bleak import BleakScanner
        self._ensure_loop()
        found = asyncio.run_coroutine_threadsafe(BleakScanner.discover(timeout=timeout), self.loop).result(timeout + 10)
        out = []
        for dv in found:
            name = dv.name or ""
            if re.search(r"govee|ihoment|minger|^gbk_|^h[0-9]{4}", name, re.I):
                sku = re.search(r"H[0-9A-F]{4}", name, re.I)
                out.append({"address": dv.address, "name": name, "sku": sku.group(0).upper() if sku else ""})
        return out


BLE = BleManager()


def find_device(did):
    return next((d for d in load_config()["devices"] if d["id"] == did), None)


def test_device(d):
    """Red, green, blue, white so the person can see the light respond."""
    if d["kind"] == "lan":
        if not d["ip"]:
            raise ValueError("This light has no IP address yet")
        HOLD[d["id"]] = time.time() + 4
        threading.Thread(target=lan_strip(d["id"]).test, args=(d["ip"],), daemon=True).start()
        return
    if not BLE.available:
        raise ValueError("Bluetooth support isn't installed. Run: pip install bleak  — then restart Light Studio.")

    def run():
        HOLD[d["id"]] = time.time() + 30
        BLE.set_color(d["address"], (0, 0, 0), d["variant"])
        BLE.wait_connected(d["address"])
        for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]:
            BLE.set_color(d["address"], rgb, d["variant"])
            time.sleep(0.8)
        HOLD.pop(d["id"], None)
    threading.Thread(target=run, daemon=True).start()


def probe_device(d):
    """Try each Bluetooth command set: A shows red, B green, C blue."""
    if not BLE.available:
        raise ValueError("Bluetooth support isn't installed. Run: pip install bleak  — then restart Light Studio.")

    def run():
        HOLD[d["id"]] = time.time() + 40
        BLE.set_color(d["address"], (0, 0, 0), "b")
        BLE.wait_connected(d["address"])
        time.sleep(0.5)
        for variant, rgb in [("a", (255, 0, 0)), ("b", (0, 255, 0)), ("c", (0, 0, 255))]:
            BLE.set_color(d["address"], rgb, variant)
            time.sleep(2)
        HOLD.pop(d["id"], None)
    threading.Thread(target=run, daemon=True).start()


def discover(timeout=2.5):
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        recv.bind(("", GOVEE_RESP_PORT))
    except OSError:
        return None, ("UDP port 4002 is busy. Close Home Assistant/Homebridge or anything else "
                      "using the Govee LAN API on this computer, or type the IP in by hand.")
    recv.settimeout(0.3)
    send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    scan = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    found, end, next_ping = {}, time.time() + timeout, 0.0
    try:
        while time.time() < end:
            if time.time() >= next_ping:
                try:
                    send.sendto(scan, GOVEE_MULTICAST)
                except OSError as e:
                    return None, f"Couldn't send the scan: {e}"
                next_ping = time.time() + 0.8
            try:
                data, addr = recv.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                info = json.loads(data.decode())["msg"]["data"]
            except (ValueError, KeyError, TypeError):
                continue
            ip = info.get("ip", addr[0])
            found[ip] = {"ip": ip, "sku": info.get("sku", "?"), "device": info.get("device", "")}
    finally:
        recv.close()
        send.close()
    return list(found.values()), None


# ----------------------------------------------------------------------------- AI instrument separation

def demucs_available():
    return all(importlib.util.find_spec(m) is not None for m in ("demucs", "torch", "numpy"))


JOBS = {}
JOBS_LOCK = threading.Lock()
SEPARATE_LOCK = threading.Lock()   # one separation at a time; it's CPU/GPU heavy
_MODEL = {}


def _job(sid, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(sid, {}).update(kw)


def read_wav_bytes(raw):
    import numpy as np
    with wave.open(io.BytesIO(raw)) as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        frames = w.readframes(n)
    if sw != 2:
        raise ValueError("expected 16-bit WAV")
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return sr, data.reshape(-1, ch).T.copy()


def _pick_onsets(flux, fps, env, env_rate, t_offset):
    import numpy as np
    n = len(flux)
    if n < 10:
        return []
    w = max(1, int(round(fps * 0.15)))
    k = np.ones(2 * w + 1, dtype=np.float32) / (2 * w + 1)
    d = np.maximum(0.0, flux - np.convolve(flux, k, mode="same"))
    sd = float(d.std()) or 1.0
    z = d / sd
    r = max(1, int(round(fps * 0.04)))
    gap = int(round(fps * 0.08))
    peaks, last = [], -10 ** 9
    for i in range(r, n - r):
        v = z[i]
        if v < 1.5 or v < z[i - r:i + r + 1].max() or i - last < gap:
            continue
        t = i / fps + t_offset
        j = int(t * env_rate)
        if j >= len(env) or env[j:j + 4].max() <= 0.05:   # ignore "hits" while the instrument is silent
            continue
        peaks.append((t, float(v)))
        last = i
    if not peaks:
        return []
    ref = float(np.percentile([p[1] for p in peaks], 95)) or 1.0
    return [[round(t, 3), round(min(1.0, v / ref), 2)] for t, v in peaks]


def stem_features(stems, sr, env_rate=50):
    """stems: {name: mono float32 array}. Returns the same format the browser makes."""
    import numpy as np
    raw = {}
    for name, x in stems.items():
        hop = sr // env_rate
        n = len(x) // hop
        rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1)) if n else np.zeros(0)
        N, H = 1024, sr // 100
        nf = max(0, (len(x) - N) // H + 1)
        win = np.hanning(N).astype(np.float32)
        top = min(N // 2, int(10000 / (sr / N)))
        flux = np.zeros(nf, dtype=np.float32)
        prev = None
        for start in range(0, nf, 1500):
            idx = (np.arange(start, min(nf, start + 1500))[:, None] * H) + np.arange(N)
            mag = np.log1p(100 * np.abs(np.fft.rfft(x[idx] * win, axis=1)[:, 1:top]))
            first = mag[:1] if prev is None else prev
            diff = np.diff(np.vstack([first, mag]), axis=0)
            flux[start:start + len(mag)] = np.maximum(diff, 0).sum(axis=1)
            prev = mag[-1:]
        raw[name] = (rms, flux, sr / H, N / 2 / sr)
    p99 = {k: float(np.percentile(v[0], 99)) if len(v[0]) else 0.0 for k, v in raw.items()}
    ref = max(p99.values() or [1.0]) or 1.0
    tracks = {}
    for name, (rms, flux, fps, t_off) in raw.items():
        env = np.clip(rms / max(p99[name], 0.12 * ref, 1e-9), 0, 1)
        env[env < 0.05] = 0
        tracks[name] = {"env": [round(float(v), 3) for v in env],
                        "onsets": _pick_onsets(flux, fps, env, env_rate, t_off)}
    return {"source": "demucs", "model": "htdemucs", "rate": env_rate, "tracks": tracks}


# ----------------------------------------------------------------------------- YouTube import

YT_JOBS = {}


def youtube_available():
    return importlib.util.find_spec("yt_dlp") is not None


def youtube_job(jid, url):
    YT_JOBS[jid] = {"state": "running", "msg": "Looking up the video…"}
    sid = secrets.token_hex(6)
    d = LIBRARY / sid
    try:
        import yt_dlp

        def hook(h):
            if h.get("status") == "downloading":
                total = h.get("total_bytes") or h.get("total_bytes_estimate")
                got = h.get("downloaded_bytes") or 0
                YT_JOBS[jid]["msg"] = (f"Downloading audio… {got * 100 // total}%" if total
                                       else f"Downloading audio… {got // 1048576} MB")
            elif h.get("status") == "finished":
                YT_JOBS[jid]["msg"] = "Download finished, adding to your library…"

        d.mkdir(parents=True)
        opts = {"format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio", "outtmpl": str(d / "audio.%(ext)s"),
                "noplaylist": True, "quiet": True, "no_warnings": True, "progress_hooks": [hook],
                "max_filesize": MAX_UPLOAD}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                raise ValueError("that's a playlist link — open one song and copy its link instead")
            if info.get("is_live"):
                raise ValueError("live streams can't be downloaded")
            if (info.get("duration") or 0) > MAX_MINUTES * 60:
                raise ValueError(f"the video is longer than {MAX_MINUTES} minutes")
            YT_JOBS[jid]["msg"] = "Downloading audio…"
            ydl.download([info.get("webpage_url") or url])
        files = [f for f in d.glob("audio.*") if f.suffix.lower() in AUDIO_EXTS]
        if not files:
            raise ValueError("the download didn't produce an audio file")
        title = re.sub(r"\s+", " ", str(info.get("title") or "YouTube song")).strip()[:120]
        now = time.time()
        write_json(d / "show.json", {
            "name": title, "file": files[0].name, "source": info.get("webpage_url") or url,
            "created": now, "updated": now, "version": 2,
            "duration": info.get("duration"), "analysis": None, "tracks": [], "loopA": None, "loopB": None,
        })
        YT_JOBS[jid] = {"state": "done", "id": sid, "msg": "Done"}
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        msg = str(e).replace("ERROR: ", "").strip()
        if "Sign in" in msg or "unavailable" in msg.lower() or "403" in msg:
            msg += " (If this keeps happening, update the downloader: pip install -U yt-dlp)"
        YT_JOBS[jid] = {"state": "error", "msg": msg[:400]}


def separate_job(sid, raw_wav):
    _job(sid, state="queued", msg="Waiting for another separation to finish…", started=time.time())
    with SEPARATE_LOCK:
        try:
            _job(sid, state="running", msg="Loading the AI model (first time downloads ~80 MB)…", started=time.time())
            import numpy as np
            import torch
            from demucs.apply import apply_model
            from demucs.pretrained import get_model
            if "htdemucs" not in _MODEL:
                m = get_model("htdemucs")
                m.eval()
                _MODEL["htdemucs"] = m
            model = _MODEL["htdemucs"]
            sr, data = read_wav_bytes(raw_wav)
            if sr != model.samplerate:
                raise ValueError(f"expected {model.samplerate} Hz audio, got {sr}")
            if data.shape[0] == 1:
                data = np.repeat(data, 2, axis=0)
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _job(sid, msg=f"Separating drums, bass, vocals and other on your {'GPU' if device == 'cuda' else 'CPU'} "
                          f"(usually 1–3 min)…")
            wav = torch.from_numpy(data[:2])
            ref = wav.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                out = apply_model(model, ((wav - mean) / std)[None], device=device, split=True,
                                  overlap=0.25, progress=False)[0]
            out = out * std + mean
            _job(sid, msg="Analyzing each instrument…")
            stems = {name: out[i].mean(0).cpu().numpy().astype(np.float32) for i, name in enumerate(model.sources)}
            feats = stem_features(stems, sr)
            d = LIBRARY / sid
            if not d.is_dir():
                raise FileNotFoundError("the song was deleted")
            with _file_lock:
                write_json(d / "features.json", feats)
            _job(sid, state="done", msg="Done", finished=time.time())
        except Exception as e:  # report anything to the browser
            traceback.print_exc()
            _job(sid, state="error", msg=f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "LightStudio/2.0"

    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._reply(code, json.dumps(obj).encode())

    def _error(self, code, message):
        self._json({"error": message}, code)

    def _body(self, limit=40 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large")
        chunks, left = [], n
        while left:
            c = self.rfile.read(min(1 << 20, left))
            if not c:
                break
            chunks.append(c)
            left -= len(c)
        return b"".join(chunks)

    def _body_json(self):
        return json.loads(self._body().decode() or "{}")

    def _route(self, fn):
        try:
            fn()
        except FileNotFoundError:
            self._error(404, "That song isn't in the library anymore")
        except (ValueError, KeyError, TypeError) as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            self._error(502, f"Network error talking to the strip: {e}")

    def do_GET(self):
        self._route(self._get)

    def do_POST(self):
        self._route(self._post)

    def do_PUT(self):
        self._route(self._put)

    def do_DELETE(self):
        self._route(self._delete)

    def _get(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._reply(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        if path == "/api/config":
            return self._json(load_config())
        if path == "/api/capabilities":
            return self._json({"demucs": demucs_available(), "bluetooth": BLE.available, "youtube": youtube_available()})
        m = re.match(r"^/api/youtube/([0-9a-f]{8})$", path)
        if m:
            return self._json(YT_JOBS.get(m.group(1), {"state": "error", "msg": "Unknown download"}))
        if path == "/api/devices/status":
            out = {}
            for d in load_config()["devices"]:
                out[d["id"]] = BLE.status(d["address"]) if d["kind"] == "ble" else ""
            return self._json(out)
        if path == "/api/ble/scan":
            if not BLE.available:
                return self._error(400, "Bluetooth support isn't installed. Run: pip install bleak  — then restart Light Studio.")
            try:
                return self._json({"devices": BLE.scan()})
            except Exception as e:
                return self._error(500, f"Bluetooth scan failed ({type(e).__name__}: {e}). Is Bluetooth turned on?")
        if path == "/api/library":
            return self._json(list_library())
        if path == "/api/discover":
            devices, err = discover()
            return self._json({"devices": devices or [], "error": err})
        m = SONG_RE.match(path)
        if not m:
            return self._error(404, "Not found")
        sid, what = m.groups()
        if what == "show":
            return self._json(read_show(sid))
        if what == "audio":
            return self._send_audio(sid)
        if what == "features":
            f = LIBRARY / sid / "features.json"
            if not f.exists():
                return self._json({})   # none yet; the browser makes an estimate
            return self._reply(200, f.read_bytes())
        if what == "separate":
            with JOBS_LOCK:
                job = dict(JOBS.get(sid, {"state": "idle"}))
            if job.get("started"):
                job["elapsed"] = round((job.get("finished") or time.time()) - job["started"], 1)
            return self._json(job)
        self._error(404, "Not found")

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/frame":
            return self._frame()
        if path == "/api/config":
            old = load_config()
            cfg = clean_config(self._body_json())
            with _file_lock:
                write_json(CONFIG_PATH, cfg)
            new = {d["id"]: d for d in cfg["devices"]}
            for d in old["devices"]:
                n = new.get(d["id"])
                gone = n is None or not n["enabled"] or not cfg["send"]
                if d["kind"] == "lan" and (gone or n["ip"] != d["ip"] or n["mode"] != d["mode"]):
                    if d["id"] in STRIPS:
                        STRIPS.pop(d["id"]).forget()      # hand the light back to the Govee app
                if d["kind"] == "ble" and (gone or n["address"] != d["address"]):
                    BLE.release(d["address"])
            return self._json(cfg)
        if path in ("/api/devices/test", "/api/devices/probe"):
            d = find_device(str(self._body_json().get("id", "")))
            if not d:
                return self._error(404, "That light isn't in your device list")
            (test_device if path.endswith("test") else probe_device)(d)
            return self._json({"ok": True})
        if path == "/api/upload":
            return self._upload()
        if path == "/api/youtube":
            url = str(self._body_json().get("url", "")).strip()
            if not YT_RE.match(url):
                return self._error(400, "That doesn't look like a YouTube link")
            if not youtube_available():
                return self._error(400, "YouTube support isn't installed. Run: pip install yt-dlp  — then restart Light Studio.")
            jid = secrets.token_hex(4)
            threading.Thread(target=youtube_job, args=(jid, url), daemon=True).start()
            return self._json({"job": jid})
        m = SONG_RE.match(path)
        if m and m.group(2) == "show":          # sendBeacon save when the page closes
            return self._save_show(m.group(1))
        if m and m.group(2) == "separate":
            return self._start_separation(m.group(1))
        self._error(404, "Not found")

    def _put(self):
        m = SONG_RE.match(urlparse(self.path).path)
        if m and m.group(2) == "show":
            return self._save_show(m.group(1))
        if m and m.group(2) == "features":
            return self._save_features(m.group(1))
        self._error(404, "Not found")

    def _delete(self):
        m = SONG_RE.match(urlparse(self.path).path)
        if m and not m.group(2):
            d = LIBRARY / m.group(1)
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            return self._json({"ok": True})
        self._error(404, "Not found")

    # -- handlers
    def _frame(self):
        body = self._body_json()
        cfg = load_config()
        if not cfg["send"]:
            return self._json({"sent": False})
        devs = {d["id"]: d for d in cfg["devices"] if d["enabled"]}
        now, errors = time.time(), {}
        for did, raw in (body.get("devices") or {}).items():
            d = devs.get(did)
            if not d or HOLD.get(did, 0) > now:
                continue
            segs = [tuple(max(0, min(255, int(v))) for v in s[:3]) for s in raw][:84]
            if not segs:
                continue
            try:
                if d["kind"] == "ble":
                    BLE.set_color(d["address"], brightest(segs), d["variant"])
                elif d["ip"]:
                    st = lan_strip(did)
                    if d["mode"] == "whole":
                        r, g, b = brightest(segs)
                        top = max(r, g, b)
                        if top == 0:
                            st.whole(d["ip"], (0, 0, 0), 100)
                        else:
                            st.whole(d["ip"], (r * 255 // top, g * 255 // top, b * 255 // top),
                                     max(1, min(100, round(top / 255 * 100))))
                    else:
                        st.sections(d["ip"], segs)
            except OSError as e:
                errors[did] = str(e)
        self._json({"sent": True, "errors": errors})

    def _upload(self):
        name = unquote(self.headers.get("X-Filename") or "song.mp3")
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTS:
            return self._error(400, f"Unsupported file type '{ext}'. Use mp3, wav, ogg, m4a, flac or opus.")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._error(400, "Empty file")
        if n > MAX_UPLOAD:
            return self._error(413, "File is larger than 400 MB")
        sid = secrets.token_hex(6)
        d = LIBRARY / sid
        d.mkdir(parents=True)
        with open(d / f"audio{ext}", "wb") as f:
            left = n
            while left:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        now = time.time()
        write_json(d / "show.json", {
            "name": Path(name).stem, "file": f"audio{ext}", "created": now, "updated": now, "version": 2,
            "duration": None, "analysis": None, "tracks": [], "loopA": None, "loopB": None,
        })
        self._json({"id": sid})

    def _save_show(self, sid):
        incoming = self._body_json()
        with _file_lock:
            show = read_show(sid)
            for k in SHOW_KEYS:
                if k in incoming:
                    show[k] = incoming[k]
            show["updated"] = time.time()
            write_json(LIBRARY / sid / "show.json", show)
        self._json({"ok": True, "updated": show["updated"]})

    def _save_features(self, sid):
        incoming = self._body_json()
        f = LIBRARY / sid / "features.json"
        if not (LIBRARY / sid).is_dir():
            raise FileNotFoundError(sid)
        with _file_lock:
            if f.exists() and incoming.get("source") != "demucs":
                try:
                    if json.loads(f.read_text()).get("source") == "demucs":
                        return self._json({"ok": False, "kept": "demucs"})
                except (OSError, ValueError):
                    pass
            write_json(f, incoming)
        self._json({"ok": True})

    def _start_separation(self, sid):
        if not (LIBRARY / sid).is_dir():
            raise FileNotFoundError(sid)
        if not demucs_available():
            return self._error(400, "Demucs isn't installed. Run: pip install demucs  — then restart Light Studio.")
        with JOBS_LOCK:
            if JOBS.get(sid, {}).get("state") in ("queued", "running"):
                return self._json({"ok": True, "already": True})
        raw = self._body(limit=MAX_UPLOAD)
        threading.Thread(target=separate_job, args=(sid, raw), daemon=True).start()
        self._json({"ok": True})

    def _send_audio(self, sid):
        show = read_show(sid)
        f = LIBRARY / sid / show["file"]
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(f.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(f, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)


def main():
    ap = argparse.ArgumentParser(description="Light Studio for Govee lights")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    LIBRARY.mkdir(exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"Port {args.port} is in use. Is Light Studio already running? "
                 f"Otherwise try: python light_studio.py --port {args.port + 1}")
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    print(f"Light Studio is running at {url}")
    print(f"Library folder: {LIBRARY}")
    print("AI instrument separation:", "ready (Demucs found)" if demucs_available()
          else "not installed (optional: pip install demucs)")
    print("YouTube links:", "ready (yt-dlp found)" if youtube_available()
          else "not installed (optional: pip install yt-dlp)")
    print("Bluetooth lights:", "ready (bleak found)" if BLE.available
          else "not installed (optional: pip install bleak)")
    print("Press Ctrl+C here to quit.")
    if not args.no_browser:
        threading.Timer(0.7, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        for st in STRIPS.values():   # hand the lights back to the Govee app
            st.forget()
        BLE.shutdown()


# ============================================================================ web app

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Light Studio</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<link rel="icon" href="data:,">
<style>
:root{
  --bg:#0c0f14; --panel:#141922; --panel2:#1a202b; --line:#262e3c; --text:#e7ebf2; --muted:#8a94a6;
  --accent:#ff9f1c; --accent2:#ffbf5c; --danger:#ff5d5d; --head:150px;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select{font:inherit;color:inherit}
button{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 11px;cursor:pointer;white-space:nowrap}
button:hover{border-color:#3a4558;background:#202838}
button.primary{background:var(--accent);border-color:var(--accent);color:#1a1205;font-weight:600}
button.primary:hover{background:var(--accent2)}
button.on{background:#3b2a0c;border-color:var(--accent);color:var(--accent2)}
button:disabled{opacity:.45;cursor:default}
input[type=text],input[type=number],select{background:#0f131a;border:1px solid var(--line);border-radius:6px;padding:5px 8px}
input[type=number]{width:76px}
input[type=color]{width:46px;height:30px;padding:0;border:1px solid var(--line);border-radius:6px;background:none;cursor:pointer}
input[type=range]{accent-color:var(--accent)}
label{display:inline-flex;align-items:center;gap:6px;color:var(--muted)}
.muted{color:var(--muted)}
header{display:flex;align-items:center;gap:18px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
.brand i{width:12px;height:12px;border-radius:50%;background:linear-gradient(135deg,#ff3d6e,#ffb000,#22d3ee);box-shadow:0 0 10px #ff9f1c}
.stripbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto}
#stripStatus{font-size:12px;color:var(--muted);max-width:360px}
#app{display:flex;height:calc(100% - 53px)}
aside{width:250px;flex:none;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
.sidehead{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:12px 12px 8px}
.sidehead>span{margin-left:auto}
.sidehead h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin:0}
#libList{overflow:auto;flex:1;padding:0 8px 8px}
.libItem{position:relative;padding:9px 30px 9px 10px;border-radius:8px;cursor:pointer;margin-bottom:3px;border:1px solid transparent}
.libItem:hover{background:var(--panel2)}
.libItem.active{background:#241c0e;border-color:#5c4316}
.libName{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.libMeta{font-size:12px;color:var(--muted)}
.libDel{position:absolute;right:6px;top:9px;padding:1px 6px;font-size:12px;opacity:0;background:none;border:none;color:var(--muted)}
.libItem:hover .libDel{opacity:1}
.libDel:hover{color:var(--danger)}
.drophint{font-size:12px;color:var(--muted);padding:10px 14px;border-top:1px solid var(--line)}
main{flex:1;min-width:0;overflow:auto;padding:14px 18px 30px}
#empty{max-width:540px;margin:12vh auto;text-align:center}
#empty h1{font-size:22px;margin-bottom:6px}
.titlebar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
#songName{font-size:18px;font-weight:650;background:transparent;border:1px solid transparent;padding:4px 6px;min-width:200px;flex:0 1 380px}
#songName:hover,#songName:focus{border-color:var(--line);background:#0f131a}
#saveState{font-size:12px;color:var(--muted)}
.spacer{flex:1}
#sepBar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:#121a26;border:1px solid #23324a;border-radius:9px;padding:8px 12px;margin-bottom:10px;font-size:13px}
#sepBar[hidden]{display:none}
#sepBar code{background:#0b1018;border:1px solid #2a3a55;border-radius:5px;padding:1px 6px}
#preview{background:#07090c;border:1px solid var(--line);border-radius:10px;padding:12px 14px 6px;margin-bottom:12px}
.ledRow{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.ledName{width:130px;flex:none;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ledCells{flex:1;display:flex;gap:4px}
.ledCells div{flex:1;height:16px;border-radius:6px;background:#000}
.ledCells.one div{flex:none;width:56px;border-radius:50%;height:20px}
.segRow{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.segName{width:130px;flex:none;font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.segBoxes{flex:1;display:flex;gap:5px;min-width:0}
.segBoxes.one .segBox{flex:none;width:90px}
.devCard{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px;background:var(--panel2)}
.devCard.off{opacity:.55}
.devTop{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.devAddr{font-size:12px}
.devStatus{font-size:12px;margin-left:auto}
.devOpts{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.devProbe{margin-top:10px;font-size:13px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#devMsg{font-size:13px;margin-top:8px;min-height:18px}
.transport{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
#playBtn{width:46px;font-size:16px}
#timeDisp{font-variant-numeric:tabular-nums;min-width:140px;font-family:ui-monospace,Consolas,monospace}
#loopInfo{font-size:12px;color:var(--accent2);font-family:ui-monospace,Consolas,monospace}
.sep{width:1px;height:24px;background:var(--line);margin:0 4px}
#volIcon{background:none;border:none;padding:4px;font-size:16px}
#tlRow{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#0f1218}
#heads{position:relative;width:var(--head);flex:none;border-right:1px solid var(--line);background:#121720}
.head{position:absolute;left:0;right:0;display:flex;align-items:center;gap:6px;padding:0 6px 0 8px;border-top:1px solid #1c2330;cursor:pointer}
.head:hover{background:#171e29}
.head.active{background:#22190b;box-shadow:inset 3px 0 0 var(--accent)}
.head i{width:10px;height:10px;border-radius:3px;flex:none}
.head .hname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600}
.head.muted .hname{opacity:.4;text-decoration:line-through}
.mini{padding:1px 6px;font-size:11px;border-radius:5px}
.headlabel{position:absolute;left:8px;font-size:10px;letter-spacing:1px;color:#5f6a7d;text-transform:uppercase}
#tlWrap{position:relative;flex:1;min-width:0;user-select:none}
#tl{display:block;cursor:crosshair}
#scroll{width:100%;margin:6px 0 12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
#inspector{display:flex;flex-wrap:wrap;gap:12px 18px;align-items:center}
#insTitle{font-weight:650;min-width:110px}
#swatches{display:flex;gap:4px;flex-wrap:wrap}
#swatches span{width:20px;height:20px;border-radius:5px;cursor:pointer;border:1px solid rgba(255,255,255,.15)}
#swatches span:hover{transform:scale(1.15)}
.insBtns{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.panelHead{display:flex;align-items:baseline;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.prow{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.pill{border-radius:999px;padding:4px 11px;display:inline-flex;align-items:center;gap:6px}
.pill i{width:9px;height:9px;border-radius:50%}
.pill.on{background:#2c2210;border-color:var(--accent)}
#segGrid{user-select:none}
.segBox{flex:1;min-width:0;height:64px;border:1px solid var(--line);border-radius:7px;background:#0d1117;display:flex;flex-direction:column;align-items:stretch;justify-content:flex-end;gap:2px;padding:3px;cursor:pointer;position:relative}
.segBox:hover{border-color:#4a5872}
.segBox b{position:absolute;top:3px;left:0;right:0;text-align:center;font-size:10px;color:#6b7690;font-weight:500}
.segBox i{height:7px;border-radius:3px}
.help{font-size:12px;color:var(--muted);line-height:1.8}
kbd{background:#1d2430;border:1px solid #2d3647;border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:11px;font-family:ui-monospace,Consolas,monospace;color:#c9d1de}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:20px 22px;width:min(620px,94vw)}
dialog::backdrop{background:rgba(0,0,0,.6)}
dialog h3{margin:0 0 4px}
dialog .row{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
dialog menu{display:flex;justify-content:flex-end;gap:8px;padding:0;margin:16px 0 0}
#gTracks{display:grid;grid-template-columns:auto 1fr 1fr;gap:8px 10px;align-items:center;margin:14px 0}
#gTracks .gname{color:var(--text);font-weight:600}
#gTracks select{width:100%}
#busy{position:fixed;inset:0;background:rgba(5,7,10,.72);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:50}
#busy[hidden]{display:none}
.spinner{width:38px;height:38px;border-radius:50%;border:4px solid #2b3445;border-top-color:var(--accent);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#dropOverlay{position:fixed;inset:0;border:3px dashed var(--accent);background:rgba(255,159,28,.07);display:none;align-items:center;justify-content:center;font-size:22px;z-index:40;pointer-events:none}
#dropOverlay.show{display:flex}
#libToggle{display:none;font-size:18px;padding:3px 10px}
#scrim{display:none}
#tl{touch-action:pan-y}
.segBoxes{touch-action:none}
.touchHelp{display:none}
@media (hover:none){
  .libDel{opacity:1}
  .drophint{display:none}
  .mouseHelp,.keysHelp{display:none}
  .touchHelp{display:inline}
}
@media (max-width:820px){
  :root{--head:122px}
  header{padding:8px 10px;gap:8px}
  #libToggle{display:inline-block}
  .stripbar{margin-left:auto;gap:6px 10px;justify-content:flex-end}
  #stripStatus{order:9;width:100%;max-width:none;text-align:right}
  #app{display:block;height:auto}
  main{overflow:visible;padding:10px 10px 30px}
  aside{position:fixed;top:0;bottom:0;left:0;width:min(300px,86vw);z-index:60;transform:translateX(-102%);transition:transform .2s ease;box-shadow:4px 0 24px rgba(0,0,0,.5)}
  body.libOpen aside{transform:none}
  body.libOpen #scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:55}
  #empty{margin:6vh auto}
  .titlebar{gap:8px}
  #songName{flex:1 1 100%;min-width:0;font-size:16px}
  #timeDisp{min-width:0;font-size:13px}
  .transport{gap:6px}
  .transport .sep{display:none}
  .head{padding:0 4px 0 6px;gap:4px}
  .head .hname{font-size:12px}
  .mini{padding:1px 4px}
  .ledName,.segName{width:78px}
  .segBoxes{gap:3px}
  .segBox{padding:2px}
  .segBox b{font-size:9px}
  #inspector{gap:10px 12px}
  .insBtns{margin-left:0}
  .panel{padding:10px}
}
</style>
</head>
<body>
<header>
  <button id="libToggle" title="Library">☰</button>
  <div class="brand"><i></i>Light Studio</div>
  <div class="stripbar">
    <span id="stripStatus"></span>
    <button id="devBtn" title="Add, test and set up your lights">💡 Devices</button>
    <label title="Turn off to edit without the real lights changing"><input type="checkbox" id="sendChk"> Send to lights</label>
    <label title="If the lights lag behind the music, raise this so lights are sent earlier">Light delay fix <input type="number" id="latency" step="10" style="width:64px"> ms</label>
    <label title="Overall brightness for every light">Brightness <input type="range" id="master" min="5" max="100" style="width:100px"></label>
  </div>
</header>

<div id="app">
  <aside>
    <div class="sidehead"><h2>Library</h2><span style="display:flex;gap:6px"><button id="ytBtn" title="Add a song from a YouTube link">▶ YouTube</button><button class="primary" id="addSongBtn">+ Add song</button></span></div>
    <input type="file" id="fileInput" accept="audio/*,.mp3,.wav,.ogg,.m4a,.flac,.opus" hidden>
    <div id="libList"></div>
    <div class="drophint">Tip: drop an audio file anywhere on this window to add it.</div>
  </aside>

  <main>
    <div id="empty">
      <h1>Make a light show</h1>
      <p class="muted">Add a song and Light Studio splits it into drums, bass, vocals and other instruments, gives each its own part of your strip, and builds a show automatically. Then fine-tune any moment and play it back synced to your lights.</p>
      <p><button class="primary" id="addSongBtn2">+ Add a song</button> &nbsp; <button id="ytBtn2">▶ From a YouTube link</button></p>
    </div>

    <div id="editor" hidden>
      <div class="titlebar">
        <input type="text" id="songName" title="Click to rename">
        <span id="songMeta" class="muted"></span>
        <span id="saveState"></span>
        <span class="spacer"></span>
        <button id="genBtn">✨ Auto-generate…</button>
      </div>
      <div id="sepBar" hidden></div>

      <div id="preview"><div id="leds"></div></div>

      <div class="transport">
        <button id="back5" title="Back 5 seconds">⏪</button>
        <button id="playBtn" class="primary" title="Play / pause (Space)">▶</button>
        <button id="fwd5" title="Forward 5 seconds">⏩</button>
        <span id="timeDisp">0:00.00 / 0:00</span>
        <button id="volIcon" title="Mute / unmute (M)">🔊</button>
        <input type="range" id="vol" min="0" max="100" value="80" title="Volume" style="width:110px">
        <span class="sep"></span>
        <button id="loopBtn" title="Loop (L). Loops the A–B section if set, otherwise the whole song">⟲ Loop</button>
        <button id="setA" title="Set loop start at playhead ([)">Set A</button>
        <button id="setB" title="Set loop end at playhead (])">Set B</button>
        <button id="clearAB">Clear A–B</button>
        <span id="loopInfo"></span>
        <span class="sep"></span>
        <label title="Hold Alt while dragging to place freely"><input type="checkbox" id="snapChk" checked> Snap</label>
        <label>Zoom <input type="range" id="zoom" min="0" max="100" value="0" style="width:110px"></label>
        <button id="undoBtn" title="Undo (Ctrl+Z)">↶</button>
        <button id="redoBtn" title="Redo (Ctrl+Shift+Z)">↷</button>
      </div>

      <div id="tlRow">
        <div id="heads"></div>
        <div id="tlWrap"><canvas id="tl"></canvas></div>
      </div>
      <input type="range" id="scroll" min="0" max="1000" value="0">

      <div class="panel" id="inspector">
        <div id="insTitle">No cue selected</div>
        <span id="insFields" style="display:contents">
          <label>Time <input type="number" id="iTime" step="0.01" min="0"> s</label>
          <label>Color <input type="color" id="iColor"></label>
          <div id="swatches"></div>
          <label>Brightness <input type="range" id="iBright" min="1" max="100"> <input type="number" id="iBrightN" min="1" max="100" style="width:60px"></label>
          <label title="Seconds to blend from the previous look into this one (0 = instant)">Fade <input type="number" id="iFade" step="0.05" min="0" style="width:66px"> s</label>
        </span>
        <div class="insBtns">
          <button id="addCueBtn" title="C — adds to the highlighted lane">+ Cue at playhead</button>
          <button id="copyBtn" title="Ctrl+C">Copy</button>
          <button id="pasteBtn" title="Ctrl+V">Paste at playhead</button>
          <button id="delBtn" title="Delete">Delete</button>
        </div>
      </div>

      <div class="panel" id="layoutPanel">
        <div class="panelHead"><b>Light sections</b><span class="muted">Choose which lights — and which parts of them — each instrument uses. Pick an instrument, then click or drag across sections.</span></div>
        <div class="prow"><span class="muted">Paint with:</span><span id="paintPills" style="display:flex;gap:6px;flex-wrap:wrap"></span><span class="spacer"></span><button id="splitBtn">Split evenly</button></div>
        <div id="segGrid"></div>
      </div>

      <div class="help">
        <b>Lanes:</b> each instrument has its own lane and lights only the sections you give it (on any of your lights). Black means "off", so a lane lower in the list (like Accents) shows through only where it's lit.
        <b>M</b> mutes a lane's lights, <b>S</b> shows only that lane.<br>
        <span class="mouseHelp"><b>Timeline:</b> click to jump · drag the top ruler to set a loop section · double-click a lane to add a cue ·
        drag across lanes to select several cues · drag cues to move them (height = brightness) · Ctrl+scroll to zoom<br></span>
        <span class="touchHelp"><b>Timeline:</b> tap to jump · press and hold in a lane to add a cue · drag a cue to move it · pinch to zoom, two fingers to scroll · drag along the top ruler to set a loop section<br></span>
        <span class="keysHelp"><b>Keys:</b> <kbd>Space</kbd> play/pause · <kbd>C</kbd> add cue to highlighted lane (tap while playing to mark hits) · <kbd>↑</kbd><kbd>↓</kbd> change lane ·
        <kbd>Del</kbd> delete · <kbd>Ctrl</kbd>+<kbd>C</kbd>/<kbd>V</kbd> copy/paste · <kbd>Ctrl</kbd>+<kbd>Z</kbd> undo · <kbd>L</kbd> loop · <kbd>[</kbd> <kbd>]</kbd> set A/B ·
        <kbd>←</kbd><kbd>→</kbd> skip 1s (Shift 5s) · <kbd>Alt</kbd>+<kbd>←</kbd><kbd>→</kbd> nudge cues · <kbd>M</kbd> mute audio · <kbd>Ctrl</kbd>+<kbd>A</kbd> select all</span>
      </div>
    </div>
  </main>
</div>

<dialog id="genDlg">
  <form method="dialog">
    <h3>Auto-generate light show</h3>
    <div class="muted" id="gSource" style="font-size:13px"></div>
    <div id="gTracks"></div>
    <div class="row"><label>Change colors every
      <select id="gBpc"><option value="1">1 beat</option><option value="2">2 beats</option><option value="4" selected>4 beats (1 bar)</option><option value="8">8 beats</option><option value="16">16 beats</option></select></label></div>
    <div class="row"><label>Apply to
      <select id="gScope"><option value="all">Whole song (replaces the checked lanes)</option><option value="region">Only the A–B loop section</option></select></label></div>
    <menu><button value="cancel">Cancel</button><button value="ok" class="primary">Generate</button></menu>
  </form>
</dialog>

<dialog id="devDlg">
  <form method="dialog">
    <h3>Devices</h3>
    <div class="muted" style="font-size:13px;margin-bottom:12px">Wi‑Fi lights need <b>LAN Control</b> turned on in the Govee app. Lights without it (like H6010 bulbs) can use Bluetooth if this computer or phone is nearby.</div>
    <div id="devList"></div>
    <div class="row" style="margin-top:12px">
      <button type="button" id="findLan">Find Wi‑Fi lights</button>
      <button type="button" id="findBle">Find Bluetooth lights</button>
      <button type="button" id="addIp">Add Wi‑Fi light by IP…</button>
    </div>
    <div id="devMsg" class="muted"></div>
    <menu><button value="close" class="primary">Done</button></menu>
  </form>
</dialog>

<div id="busy" hidden><div class="spinner"></div><div id="busyMsg">Working…</div></div>
<div id="dropOverlay">Drop to add this song</div>
<div id="scrim"></div>
<audio id="audio" preload="auto"></audio>

<script>
"use strict";
// ======================================================== ANALYSIS-START (pure functions)

function mixToMono(ab){
  const n = ab.length, ch = ab.numberOfChannels, out = new Float32Array(n);
  for (let c = 0; c < ch; c++){ const d = ab.getChannelData(c); for (let i = 0; i < n; i++) out[i] += d[i] / ch; }
  return out;
}

function fftInPlace(re, im){
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++){
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j){ let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
  }
  for (let len = 2; len <= n; len <<= 1){
    const ang = -2 * Math.PI / len, wr = Math.cos(ang), wi = Math.sin(ang), h = len >> 1;
    for (let i = 0; i < n; i += len){
      let cr = 1, ci = 0;
      for (let k = 0; k < h; k++){
        const a = i + k, b = a + h;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        const ncr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = ncr;
      }
    }
  }
}

function percentile(arr, p){
  const a = Array.from(arr).sort((x, y) => x - y);
  if (!a.length) return 0;
  return a[Math.min(a.length - 1, Math.max(0, Math.round(p / 100 * (a.length - 1))))];
}

function downsample(x, sr){
  if (sr <= 32000) return [x, sr];
  const y = new Float32Array(x.length >> 1);
  for (let i = 0; i < y.length; i++) y[i] = (x[2 * i] + x[2 * i + 1]) * 0.5;
  return [y, sr / 2];
}

// Tempo, beat times, loudness per beat and the biggest hits of the full mix.
function analyzeSamples(x, sr){
  [x, sr] = downsample(x, sr);
  const N = 1024, H = 256, half = N >> 1;
  const nF = Math.max(0, Math.floor((x.length - N) / H) + 1);
  const win = new Float32Array(N); for (let i = 0; i < N; i++) win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / N);
  const re = new Float32Array(N), im = new Float32Array(N), prev = new Float32Array(half);
  const onsetRaw = new Float32Array(nF), rms = new Float32Array(nF);
  const maxBin = Math.min(half, Math.round(10000 / (sr / N)));
  for (let f = 0; f < nF; f++){
    const off = f * H; let ss = 0;
    for (let i = 0; i < N; i++){ const v = x[off + i]; ss += v * v; re[i] = v * win[i]; im[i] = 0; }
    fftInPlace(re, im);
    let flux = 0;
    for (let k = 1; k < maxBin; k++){
      const m = Math.log1p(100 * Math.sqrt(re[k] * re[k] + im[k] * im[k]));
      const d = m - prev[k]; if (d > 0) flux += d; prev[k] = m;
    }
    onsetRaw[f] = f === 0 ? 0 : flux; rms[f] = Math.sqrt(ss / N);
  }
  const fps = sr / H, w = Math.round(fps * 0.5), onset = new Float32Array(nF);
  { const cs = new Float64Array(nF + 1); for (let f = 0; f < nF; f++) cs[f + 1] = cs[f] + onsetRaw[f];
    for (let f = 0; f < nF; f++){ const a = Math.max(0, f - w), b = Math.min(nF, f + w + 1); onset[f] = Math.max(0, onsetRaw[f] - (cs[b] - cs[a]) / (b - a)); } }
  let mean = 0; for (let f = 0; f < nF; f++) mean += onset[f]; mean /= Math.max(1, nF);
  let sd = 0; for (let f = 0; f < nF; f++) sd += (onset[f] - mean) ** 2; sd = Math.sqrt(sd / Math.max(1, nF)) || 1;
  for (let f = 0; f < nF; f++) onset[f] /= sd;

  const minLag = Math.floor(fps * 60 / 190), maxLag = Math.ceil(fps * 60 / 65);
  const ac = new Float64Array(maxLag + 2);
  for (let lag = minLag; lag <= maxLag + 1; lag++){
    let s = 0; for (let t = lag; t < nF; t++) s += onset[t] * onset[t - lag];
    ac[lag] = s / Math.max(1, nF - lag);
  }
  let bestLag = minLag, bestScore = -Infinity;
  for (let lag = minLag + 1; lag <= maxLag; lag++){
    const bpm = 60 * fps / lag, wgt = Math.exp(-0.5 * (Math.log2(bpm / 120) / 0.9) ** 2), sc = ac[lag] * wgt;
    if (sc > bestScore){ bestScore = sc; bestLag = lag; }
  }
  let P = bestLag;
  { const a = ac[bestLag - 1], b = ac[bestLag], c = ac[bestLag + 1], den = a - 2 * b + c; if (den < 0) P = bestLag + 0.5 * (a - c) / den; }
  const tempo = 60 * fps / P;

  const tight = 100, cs = new Float64Array(nF), back = new Int32Array(nF).fill(-1);
  const lo = Math.round(2 * P), hi = Math.max(1, Math.round(P / 2));
  for (let t = 0; t < nF; t++){
    let best = -Infinity, bi = -1;
    for (let p = Math.max(0, t - lo); p <= t - hi; p++){
      const r = Math.log((t - p) / P), v = cs[p] - tight * r * r;
      if (v > best){ best = v; bi = p; }
    }
    if (bi >= 0 && best > 0){ cs[t] = onset[t] + best; back[t] = bi; } else cs[t] = onset[t];
  }
  const peaks = [];
  for (let t = 1; t < nF - 1; t++) if (cs[t] >= cs[t - 1] && cs[t] >= cs[t + 1]) peaks.push(cs[t]);
  const thr = 0.5 * percentile(peaks, 50);
  let end = nF - 1;
  for (let t = nF - 2; t > 0; t--) if (cs[t] >= cs[t - 1] && cs[t] >= cs[t + 1] && cs[t] > thr){ end = t; break; }
  const frames = []; for (let t = end; t >= 0; t = back[t]) frames.push(t);
  frames.reverse();

  const rmsMax = percentile(rms, 99) || 1, toTime = f => (f * H + N / 2) / sr;
  const rawLoud = [], beatTimes = [];
  for (const f of frames){
    let s = 0, c = 0;
    for (let k = Math.max(0, f - 4); k <= Math.min(nF - 1, f + 4); k++){ s += rms[k]; c++; }
    const L = s / c;
    if (L < rmsMax * 0.02) continue;
    rawLoud.push(L); beatTimes.push(Math.round(toTime(f) * 1000) / 1000);
  }
  const l0 = percentile(rawLoud, 10), l1 = percentile(rawLoud, 98);
  const loud = rawLoud.map(v => Math.round(Math.min(1, Math.max(0, (v - l0) / Math.max(1e-9, l1 - l0))) * 1000) / 1000);

  const loudThr = percentile(rms, 60), cand = [];
  for (let f = 3; f < nF - 3; f++){
    let isMax = true; for (let k = f - 3; k <= f + 3; k++) if (onset[k] > onset[f]){ isMax = false; break; }
    if (isMax && rms[Math.min(nF - 1, f + 2)] >= loudThr) cand.push([onset[f], f]);
  }
  cand.sort((a, b) => b[0] - a[0]);
  const want = Math.max(1, Math.round(nF / fps / 10)), hits = [], strongest = cand.length ? cand[0][0] : 0;
  for (const [v, f] of cand){
    if (hits.length >= want || v < strongest * 0.55) break;
    const t = toTime(f);
    if (hits.every(h => Math.abs(h - t) > 1.5)) hits.push(Math.round(t * 1000) / 1000);
  }
  hits.sort((a, b) => a - b);
  return { version: 1, tempo: Math.round(tempo * 10) / 10, beats: beatTimes, loud, hits };
}

// Onset times + strength (0..1) from a flux curve; skips anything where gate(t) is false.
function pickOnsets(flux, fps, toTime, gate){
  const n = flux.length, w = Math.max(1, Math.round(fps * 0.15)), cs = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) cs[i + 1] = cs[i] + flux[i];
  const d = new Float32Array(n);
  for (let i = 0; i < n; i++){ const a = Math.max(0, i - w), b = Math.min(n, i + w + 1); d[i] = Math.max(0, flux[i] - (cs[b] - cs[a]) / (b - a)); }
  let m = 0; for (let i = 0; i < n; i++) m += d[i]; m /= Math.max(1, n);
  let sd = 0; for (let i = 0; i < n; i++) sd += (d[i] - m) ** 2; sd = Math.sqrt(sd / Math.max(1, n)) || 1;
  const r = Math.max(1, Math.round(fps * 0.04)), gap = Math.round(fps * 0.08), pk = [];
  let last = -1e9;
  for (let i = r; i < n - r; i++){
    const v = d[i] / sd; if (v < 1.5 || i - last < gap) continue;
    let ok = true; for (let k = i - r; k <= i + r; k++) if (d[k] > d[i]){ ok = false; break; }
    if (!ok) continue;
    const t = toTime(i); if (gate && !gate(t)) continue;
    pk.push([t, v]); last = i;
  }
  const ref = percentile(pk.map(p => p[1]), 95) || 1;
  return pk.map(([t, v]) => [Math.round(t * 1000) / 1000, Math.round(Math.min(1, v / ref) * 100) / 100]);
}

// Estimate instruments from frequency ranges (used when AI separation isn't available).
const BANDS = { drums: [[30, 150], [4000, 11000]], bass: [[30, 250]], vocals: [[300, 3400]], other: [[3400, 11000]] };
function bandFeatures(x, sr){
  [x, sr] = downsample(x, sr);
  const N = 1024, H = 256, half = N >> 1, binHz = sr / N, rate = 50;
  const nF = Math.max(0, Math.floor((x.length - N) / H) + 1), names = Object.keys(BANDS);
  const ranges = {}; for (const k of names) ranges[k] = BANDS[k].map(([a, b]) => [Math.max(1, Math.round(a / binHz)), Math.min(half - 1, Math.round(b / binHz))]);
  const energy = {}, flux = {}; for (const k of names){ energy[k] = new Float32Array(nF); flux[k] = new Float32Array(nF); }
  const win = new Float32Array(N); for (let i = 0; i < N; i++) win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / N);
  const re = new Float32Array(N), im = new Float32Array(N), prev = new Float32Array(half), cur = new Float32Array(half), mag2 = new Float32Array(half);
  for (let f = 0; f < nF; f++){
    const off = f * H;
    for (let i = 0; i < N; i++){ re[i] = x[off + i] * win[i]; im[i] = 0; }
    fftInPlace(re, im);
    for (let k = 0; k < half; k++){ const m2 = re[k] * re[k] + im[k] * im[k]; mag2[k] = m2; cur[k] = Math.log1p(100 * Math.sqrt(m2)); }
    for (const nm of names){
      let e = 0, fl = 0;
      for (const [a, b] of ranges[nm]){       // each range counts equally, so a narrow kick range isn't drowned out by hi-hats
        let rf = 0; for (let k = a; k <= b; k++){ e += mag2[k]; const dd = cur[k] - prev[k]; if (dd > 0) rf += dd; }
        fl += rf / (b - a + 1);
      }
      energy[nm][f] = Math.sqrt(e); flux[nm][f] = f ? fl : 0;
    }
    prev.set(cur);
  }
  const fps = sr / H, toTime = f => (f * H + N / 2) / sr, nE = Math.ceil((x.length / sr) * rate) + 1;
  const envRaw = {}, p99 = {};
  for (const nm of names){
    const s = new Float32Array(nE), c = new Uint16Array(nE);
    for (let f = 0; f < nF; f++){ const j = Math.min(nE - 1, Math.floor(toTime(f) * rate)); s[j] += energy[nm][f]; c[j]++; }
    let lastV = 0; for (let j = 0; j < nE; j++){ if (c[j]) lastV = s[j] / c[j]; s[j] = c[j] ? s[j] / c[j] : lastV * 0.5; }
    envRaw[nm] = s; p99[nm] = percentile(s, 99);
  }
  const ref = Math.max(...Object.values(p99)) || 1, tracks = {};
  for (const nm of names){
    const norm = Math.max(p99[nm], 0.12 * ref, 1e-9), env = Array.from(envRaw[nm], v => { const e = Math.min(1, v / norm); return e < 0.05 ? 0 : Math.round(e * 1000) / 1000; });
    const onsets = pickOnsets(flux[nm], fps, toTime, t => { const j = Math.floor(t * rate); return Math.max(env[j] || 0, env[j + 1] || 0, env[j + 2] || 0, env[j + 3] || 0) > 0.05; });
    tracks[nm] = { env, onsets };
  }
  return { source: "bands", rate, tracks };
}

function buildEnvelope(x, sr, rate){
  const step = sr / rate, n = Math.ceil(x.length / step), env = new Float32Array(n);
  let mx = 1e-9;
  for (let i = 0; i < n; i++){
    const a = Math.floor(i * step), b = Math.min(x.length, Math.floor((i + 1) * step));
    let m = 0; for (let k = a; k < b; k++){ const v = Math.abs(x[k]); if (v > m) m = v; }
    env[i] = m; if (m > mx) mx = m;
  }
  for (let i = 0; i < n; i++) env[i] /= mx;
  return env;
}

function hexToRgb(h){ const n = parseInt(String(h).slice(1), 16) || 0; return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
function rgbToHex(r, g, b){ return "#" + [r, g, b].map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join(""); }
function hsv(h, s, v){ const f = n => { const k = (n + h * 6) % 6; return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1)))); }; return [f(5), f(3), f(1)]; }

const PALETTES = {
  fire:    { name: "Fire",         colors: ["#ff2000", "#ff6a00", "#ffae00", "#ff0030"] },
  ocean:   { name: "Ocean",        colors: ["#0040ff", "#00a8ff", "#00ffd0", "#5a00ff"] },
  sunset:  { name: "Sunset",       colors: ["#ff3d6e", "#ff8a3d", "#ffcf3d", "#b03dff"] },
  forest:  { name: "Forest",       colors: ["#00ff6a", "#a4ff00", "#00c48c", "#ffd000"] },
  neon:    { name: "Neon rainbow", colors: ["#ff0040", "#ff8000", "#ffe000", "#00ff80", "#00c0ff", "#4000ff", "#c000ff"] },
  ice:     { name: "Ice",          colors: ["#ffffff", "#9fe8ff", "#3da5ff", "#c9b8ff"] },
  spooky:  { name: "Spooky",       colors: ["#ff6000", "#7a00ff", "#39ff14", "#ff2000"] },
  redblue: { name: "Red & blue",   colors: ["#ff0020", "#0030ff"] },
  purple:  { name: "Purple haze",  colors: ["#a000ff", "#ff00d0", "#6a00ff", "#ff4df0"] },
  white:   { name: "White",        colors: ["#ffffff"] },
};

// cues sorted by t: {t, color, brightness, fade}. Returns f(time) -> [r, g, b, brightness]. Black before the first cue.
function makeEvaluator(cues){
  const n = cues.length, rgb = cues.map(c => hexToRgb(c.color)), starts = new Array(n);
  const apply = (i, T, s) => {
    const c = cues[i], g = rgb[i], a = c.fade > 0 ? Math.min(1, Math.max(0, (T - c.t) / c.fade)) : 1;
    return [s[0] + (g[0] - s[0]) * a, s[1] + (g[1] - s[1]) * a, s[2] + (g[2] - s[2]) * a, s[3] + (c.brightness - s[3]) * a];
  };
  let st = [0, 0, 0, 100];
  for (let i = 0; i < n; i++){ starts[i] = st; st = apply(i, i + 1 < n ? cues[i + 1].t : Infinity, st); }
  return T => {
    let lo = 0, hi = n;
    while (lo < hi){ const m = (lo + hi) >> 1; if (cues[m].t <= T) lo = m + 1; else hi = m; }
    return lo === 0 ? [0, 0, 0, 100] : apply(lo - 1, T, starts[lo - 1]);
  };
}

function envMax(f, a, b){
  const r = f.rate || 50, env = f.env, i0 = Math.max(0, Math.floor(a * r)), i1 = Math.min(env.length, Math.max(i0 + 1, Math.ceil(b * r)));
  let m = 0; for (let i = i0; i < i1; i++) if (env[i] > m) m = env[i];
  return m;
}
function medianGap(beats){ const g = []; for (let i = 0; i + 1 < beats.length; i++) g.push(beats[i + 1] - beats[i]); return g.length ? percentile(g, 50) : 0.5; }

// One instrument lane. style: hits | follow | pulse | off. Black = off (lets lower lanes show).
function generateTrack(style, f, an, o, t0, t1){
  const r3 = v => Math.round(v * 1000) / 1000, out = [];
  const pal = (PALETTES[o.palette] || PALETTES.neon).colors, bpc = Math.max(1, o.bpc | 0), beats = an.beats;
  const beatIdx = t => { let lo = 0, hi = beats.length; while (lo < hi){ const m = (lo + hi) >> 1; if (beats[m] <= t + 0.02) lo = m + 1; else hi = m; } return Math.max(0, lo - 1); };
  const colorAt = t => pal[Math.floor(beatIdx(t) / bpc) % pal.length];
  if (t0 <= 0.001) out.push({ t: 0, color: "#000000", brightness: 100, fade: 0 });
  if (!f || style === "off") return out;
  const bl = medianGap(beats), endT = Math.min(t1, f.env.length / (f.rate || 50));
  if (style === "hits"){
    const ons = f.onsets.filter(([t, s]) => t >= t0 && t < t1 && s >= (o.minHit ?? 0.18));
    let last = -1;
    for (let k = 0; k < ons.length; k++){
      const [t, s] = ons[k]; if (t - last < 0.07) continue; last = t;
      const next = k + 1 < ons.length ? ons[k + 1][0] : t + 1, br = Math.round(35 + 65 * s);
      out.push({ t: r3(t), color: colorAt(t), brightness: br, fade: 0 });
      const ds = t + Math.min(0.05, (next - t) * 0.3);
      out.push({ t: r3(ds), color: "#000000", brightness: br, fade: r3(Math.max(0.05, Math.min(0.45, (next - ds) * 0.85))) });
    }
  } else if (style === "follow"){
    const step = 0.1, a = o.smooth ?? 0.55;
    let sm = 0, prevBr = 0, prevCol = "#000000";
    for (let t = t0; t < endT; t += step){
      sm += (envMax(f, t, t + step) - sm) * a;
      const on = sm >= 0.08, br = on ? Math.round(15 + 85 * Math.min(1, sm)) : 0, col = on ? colorAt(t) : "#000000";
      if (!on){ if (prevBr > 0){ out.push({ t: r3(t), color: "#000000", brightness: prevBr, fade: 0.2 }); prevBr = 0; prevCol = "#000000"; } continue; }
      if (prevBr === 0 || col !== prevCol || Math.abs(br - prevBr) >= 6){
        out.push({ t: r3(t), color: col, brightness: br, fade: prevBr === 0 ? 0.08 : (col !== prevCol ? 0.35 : step) });
        prevBr = br; prevCol = col;
      }
    }
  } else if (style === "pulse"){
    for (const t of beats){
      if (t < t0 || t >= t1) continue;
      const v = envMax(f, t - 0.05, t + 0.15); if (v < 0.08) continue;
      const br = Math.round(25 + 75 * v), col = colorAt(t);
      out.push({ t: r3(t), color: col, brightness: br, fade: 0 });
      out.push({ t: r3(t + bl * 0.3), color: "#000000", brightness: br, fade: r3(bl * 0.55) });
    }
  } else if (style === "punch"){
    // Dim, high-contrast glow that follows the instrument's level, a hard flash on every hit that
    // decays back down, and a strobe burst + full-power section after each drop.
    const drops = o.drops || [], dropLen = bl * 16;
    const inDrop = t => drops.some(d => t >= d - 0.02 && t < d + dropLen);
    const baseAt = t => { const e = envMax(f, t, t + 0.1); return e < 0.12 ? 0 : Math.round(4 + 46 * Math.pow((e - 0.12) / 0.88, 1.8)); };
    const busy = [], ons = f.onsets.filter(([t, s]) => t >= t0 && t < t1 && s >= 0.15);
    let last = -1;
    for (let k = 0; k < ons.length; k++){
      const [t, s] = ons[k]; if (t - last < 0.08) continue; last = t;
      const dm = inDrop(t), next = k + 1 < ons.length ? ons[k + 1][0] : t + 1;
      let col = dm ? pal[beatIdx(t) % pal.length] : colorAt(t);
      if (s > 0.85 || (dm && s > 0.5)) col = mixWhite(col, 0.35);
      const br = dm ? 100 : Math.round(55 + 45 * s), decay = Math.max(0.08, Math.min(dm ? 0.3 : 0.45, (next - t) * 0.85)), rest = baseAt(t + decay);
      out.push({ t: r3(t), color: col, brightness: br, fade: 0 });
      out.push({ t: r3(t + 0.03), color: rest ? colorAt(t) : "#000000", brightness: rest || br, fade: r3(decay) });
      busy.push([t, t + 0.03 + decay]);
    }
    for (const d of drops){
      if (d < t0 || d >= t1) continue;
      for (let i = out.length - 1; i >= 0; i--) if (out[i].t >= d - 0.01 && out[i].t < d + 0.5) out.splice(i, 1);
      for (let k = 0; k < 6; k++) out.push({ t: r3(d + k * 0.06), color: k % 2 ? "#000000" : "#ffffff", brightness: 100, fade: 0 });
      const c = pal[beatIdx(d) % pal.length];
      out.push({ t: r3(d + 0.36), color: c, brightness: 100, fade: 0 }, { t: r3(d + 0.4), color: c, brightness: 60, fade: r3(Math.max(0.1, bl - 0.4)) });
      busy.push([d, d + Math.max(0.5, bl)]);
    }
    busy.sort((a, b) => a[0] - b[0]);
    const isBusy = t => { for (const [a, b] of busy){ if (a > t) break; if (t < b) return true; } return false; };
    let prevBr = -1, prevCol = null;
    for (let t = t0; t < endT; t += 0.1){
      if (isBusy(t)){ prevBr = -1; continue; }
      const br = baseAt(t), col = br ? colorAt(t) : "#000000";
      if (prevBr === -1 || col !== prevCol || Math.abs(br - prevBr) >= 6){ out.push({ t: r3(t), color: col, brightness: br || 100, fade: 0.1 }); prevBr = br; prevCol = col; }
    }
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

function mixWhite(hex, a){ const [r, g, b] = hexToRgb(hex); return rgbToHex(r + (255 - r) * a, g + (255 - g) * a, b + (255 - b) * a); }

// Bass drops: the bass level jumps well above what it was over the previous few seconds
// (typically after a build-up). Returns the drop times, snapped to the nearest beat.
function detectDrops(f, an){
  if (!f || !f.env || f.env.length < 400) return [];
  const r = f.rate || 50, env = f.env, n = env.length, cs = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) cs[i + 1] = cs[i] + env[i];
  const at = s => Math.max(0, Math.min(n, Math.round(s * r)));
  const avg = (a, b) => { const i0 = at(a), i1 = at(b); return i1 > i0 ? (cs[i1] - cs[i0]) / (i1 - i0) : 0; };
  const beats = (an && an.beats) || [], out = [];
  for (let t = 4; t < n / r - 1; t += 0.05){
    const before = avg(t - 4, t - 0.3), after = avg(t, t + 1.5);
    if (after >= 0.5 && after - before >= 0.3 && after >= before * 1.8){
      let i = at(t - 0.3); while (i < n && env[i] < after * 0.5) i++;
      let td = i / r; const b = beats.find(x => Math.abs(x - td) < 0.12); if (b != null) td = b;
      if (!out.length || td - out[out.length - 1] > 10) out.push(Math.round(td * 1000) / 1000);
      t = td + 10;
    }
  }
  return out;
}

// Whole-strip accents lane: white flash on the biggest hits, or a pulse on every beat.
function generateAccents(style, an, o, t0, t1){
  const r3 = v => Math.round(v * 1000) / 1000, out = [], pal = (PALETTES[o.palette] || PALETTES.white).colors, drops = o.drops || [];
  if (t0 <= 0.001) out.push({ t: 0, color: "#000000", brightness: 100, fade: 0 });
  if (style === "hits"){
    for (const h of an.hits || []){ if (h < t0 || h >= t1 || drops.some(d => Math.abs(d - h) < 1)) continue;
      out.push({ t: r3(h), color: pal[0], brightness: 100, fade: 0 }, { t: r3(h + 0.06), color: "#000000", brightness: 100, fade: 0.3 }); }
    for (const d of drops){ if (d < t0 || d >= t1) continue;          // whole-room strobe on every bass drop
      for (let k = 0; k < 8; k++) out.push({ t: r3(d + k * 0.06), color: k % 2 ? "#000000" : pal[0], brightness: 100, fade: 0 });
      out.push({ t: r3(d + 0.48), color: pal[0], brightness: 100, fade: 0 }, { t: r3(d + 0.52), color: "#000000", brightness: 100, fade: 0.5 }); }
  } else if (style === "pulse"){
    const bl = medianGap(an.beats), bpc = Math.max(1, o.bpc | 0);
    an.beats.forEach((t, i) => { if (t < t0 || t >= t1) return;
      const br = Math.round(25 + 75 * (an.loud[i] ?? 0.5)), col = pal[Math.floor(i / bpc) % pal.length];
      out.push({ t: r3(t), color: col, brightness: br, fade: 0 }, { t: r3(t + bl * 0.3), color: "#000000", brightness: br, fade: r3(bl * 0.55) }); });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}
// ======================================================== ANALYSIS-END

const $ = id => document.getElementById(id);
const audio = $("audio");
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const r3 = v => Math.round(v * 1000) / 1000;
const tick = () => new Promise(r => setTimeout(r, 30));
function fmtTime(t, hund){
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return hund ? `${m}:${s.toFixed(2).padStart(5, "0")}` : `${m}:${String(Math.floor(s)).padStart(2, "0")}`;
}
async function api(path, opts = {}){
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e){ /* no body */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function showBusy(msg){ $("busyMsg").textContent = msg; $("busy").hidden = false; }
function hideBusy(){ $("busy").hidden = true; }
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const INSTRUMENTS = ["drums", "bass", "vocals", "other", "guitar", "piano"];
const TRACK_DEFAULTS = {
  drums:   { name: "Drums",   style: "hits",   palette: "fire" },
  bass:    { name: "Bass",    style: "punch",  palette: "ocean" },
  vocals:  { name: "Vocals",  style: "follow", palette: "sunset" },
  other:   { name: "Other",   style: "follow", palette: "forest" },
  guitar:  { name: "Guitar",  style: "follow", palette: "neon" },
  piano:   { name: "Piano",   style: "follow", palette: "ice" },
  accents: { name: "Accents", style: "hits",   palette: "white" },
};
const STYLE_NAMES = { punch: "Punch — hits, drops & big dynamics", hits: "Flash on each hit", follow: "Follow its volume", pulse: "Pulse on the beat", off: "Off" };
const ACCENT_STYLES = { hits: "Flash on big hits + strobe on drops", pulse: "Pulse whole strip on the beat", off: "Off" };
const computeDrops = () => detectDrops(S.feat && S.feat.tracks && S.feat.tracks.bass, S.an);

const S = {
  id: null, show: null, tracks: [], an: null, feat: null, ab: null, env: null, envRate: 200, dur: 0,
  sel: new Set(), cueTrack: new Map(), active: 0, paint: 0, view: 0, pps: 20, loop: false, A: null, B: null,
  undo: [], redo: [], editOpen: false, edited: false, staticDirty: true, clip: null, lastColor: "#ff0040",
  cfg: { send: true, latency_ms: 0, rate: 20, master: 100, devices: [] },
  caps: { demucs: false, bluetooth: false }, identify: {}, devStatus: {},
};
let audioUrl = null, saveTimer = null, pollTimer = null;

// ---------------------------------------------------------------- tracks & show data
function normCue(c){
  const col = /^#[0-9a-f]{6}$/i.test(c.color) ? c.color.toLowerCase() : "#ffffff";
  return { t: Math.max(0, +c.t || 0), color: col, brightness: clamp(Math.round(+c.brightness || 100), 1, 100), fade: Math.max(0, +c.fade || 0) };
}
function normTrack(t){
  const d = TRACK_DEFAULTS[t.feature || t.id] || TRACK_DEFAULTS.accents;
  const segs = (t.segs || []).map(x => typeof x === "number" ? `${fallbackDevId()}:${x}` : String(x));   // numbers = single-strip version
  return { id: String(t.id), name: t.name || d.name, feature: t.feature ?? null, segs,
           cues: (t.cues || []).map(normCue), mute: !!t.mute, solo: !!t.solo, style: t.style || d.style, palette: t.palette || d.palette };
}
function trackColor(tr){ return (PALETTES[tr.palette] || PALETTES.white).colors[0]; }
function defaultTracks(feat){
  const have = INSTRUMENTS.filter(k => feat && feat.tracks && feat.tracks[k]);
  const list = have.map(k => normTrack({ id: k, feature: k }));
  list.push(normTrack({ id: "accents", feature: null }));
  return list;
}
// ---- lights: every light has 1+ sections, addressed as "deviceId:index"
const enabledDevs = () => (S.cfg.devices || []).filter(d => d.enabled);
const nSec = d => d.kind === "lan" && d.mode === "segments" ? d.segments : 1;
const secKeys = d => Array.from({ length: nSec(d) }, (_, i) => `${d.id}:${i}`);
const fallbackDevId = () => (S.cfg.devices && S.cfg.devices[0] && S.cfg.devices[0].id) || "strip";
const segCounts = () => Object.fromEntries((S.cfg.devices || []).map(d => [d.id, nSec(d)]));
// Give one light's sections to the instruments: long lights are split between them, single-color lights take turns.
function layoutDevice(d, turn){
  const inst = S.tracks.filter(t => t.feature), keys = secKeys(d), k = inst.length, pre = d.id + ":";
  for (const t of S.tracks) t.segs = t.segs.filter(x => !x.startsWith(pre));
  if (k){
    if (keys.length > 1) keys.forEach((key, i) => inst[Math.min(k - 1, Math.floor(i * k / keys.length))].segs.push(key));
    else inst[turn % k].segs.push(keys[0]);
  }
  for (const t of S.tracks) if (!t.feature) t.segs.push(...keys);
}
function defaultLayout(){
  for (const t of S.tracks) t.segs = [];
  let turn = 0;
  for (const d of enabledDevs()) layoutDevice(d, nSec(d) === 1 ? turn++ : 0);
}
function fillUnassigned(){            // lights that no lane uses yet get a sensible default
  let turn = 0, did = false;
  for (const d of enabledDevs()){
    const pre = d.id + ":";
    if (!S.tracks.some(t => t.segs.some(x => x.startsWith(pre)))){ layoutDevice(d, turn); did = true; }
    if (nSec(d) === 1) turn++;
  }
  return did;
}
function rescaleDevice(id, oldN, newN){
  if (!oldN || oldN === newN) return;
  const pre = id + ":";
  for (const t of S.tracks){
    const had = new Set(t.segs.filter(x => x.startsWith(pre)).map(x => +x.slice(pre.length)));
    const keep = t.segs.filter(x => !x.startsWith(pre));
    for (let j = 0; j < newN; j++) if (had.has(Math.floor(j * oldN / newN))) keep.push(pre + j);
    t.segs = keep;
  }
}
function genTrackCues(tr, o, t0, t1){
  const opts = { palette: tr.palette, bpc: o.bpc, smooth: tr.feature === "vocals" ? 0.35 : 0.6, drops: S.drops || [] };
  if (!tr.feature) return generateAccents(tr.style, S.an, opts, t0, t1);
  return generateTrack(tr.style, S.feat && S.feat.tracks[tr.feature], S.an, opts, t0, t1);
}
function generateLanes(ids, o, region){
  const t0 = region ? S.A : 0, t1 = region ? S.B : S.dur + 1;
  for (const tr of S.tracks){
    if (!ids.includes(tr.id)) continue;
    const gen = genTrackCues(tr, o, t0, t1);
    if (region){
      const after = tr._ev(t1), kept = tr.cues.filter(c => c.t < t0 || c.t >= t1);
      if (!kept.some(c => Math.abs(c.t - t1) < 0.001)) gen.push({ t: r3(t1), color: rgbToHex(after[0], after[1], after[2]), brightness: clamp(Math.round(after[3]), 1, 100), fade: 0 });
      tr.cues = kept.concat(gen);
    } else tr.cues = gen;
  }
}
function rebuild(){
  S.cueTrack = new Map();
  for (const tr of S.tracks){ tr.cues.sort((a, b) => a.t - b.t); tr._ev = makeEvaluator(tr.cues); for (const c of tr.cues) S.cueTrack.set(c, tr); }
  S.staticDirty = true;
}
function changed(){ rebuild(); scheduleSave(); renderInspector(); }
const snapshot = () => JSON.stringify(S.tracks.map(t => ({ id: t.id, segs: t.segs, cues: t.cues })));
function restore(json){
  const data = JSON.parse(json), byId = new Map(data.map(d => [d.id, d]));
  for (const t of S.tracks){ const d = byId.get(t.id); if (d){ t.segs = d.segs; t.cues = d.cues; } }
  S.sel.clear(); changed(); renderLayout(); renderHeads();
}
function pushUndo(){ S.undo.push(snapshot()); if (S.undo.length > 120) S.undo.shift(); S.redo = []; S.edited = true; }
function undo(){ if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); }
function redo(){ if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); }

function showBody(){
  return { version: 2, name: S.show.name, tracks: S.tracks.map(({ _ev, ...t }) => t), analysis: S.an,
           duration: r3(S.dur), loopA: S.A, loopB: S.B, segCounts: segCounts() };
}
function scheduleSave(){ $("saveState").textContent = "Unsaved changes…"; clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 800); }
async function saveNow(){
  clearTimeout(saveTimer); saveTimer = null;
  if (!S.id) return;
  try { await api(`/api/song/${S.id}/show`, { method: "PUT", body: JSON.stringify(showBody()) }); $("saveState").textContent = "Saved ✓"; loadLibrary(); }
  catch (e){ $("saveState").textContent = "Save failed – " + e.message; }
}
window.addEventListener("pagehide", () => {
  if (saveTimer && S.id) navigator.sendBeacon(`/api/song/${S.id}/show`, new Blob([JSON.stringify(showBody())], { type: "application/json" }));
});

// ---------------------------------------------------------------- global light settings
function setStatus(msg){ $("stripStatus").textContent = msg; }
let capsReadyResolve; const capsReady = new Promise(r => { capsReadyResolve = r; });
async function loadConfig(){
  try { S.cfg = await api("/api/config"); S.caps = await api("/api/capabilities"); }
  catch (e){ setStatus("Can't reach Light Studio – is it still running?"); }
  $("sendChk").checked = S.cfg.send; $("latency").value = S.cfg.latency_ms; $("master").value = S.cfg.master;
  buildLeds(); updateDevBtn(); capsReadyResolve();
  if (!S.cfg.devices.length) setStatus("Click 💡 Devices to add your lights.");
}
async function saveConfig(patch){
  Object.assign(S.cfg, patch);
  try { S.cfg = await api("/api/config", { method: "POST", body: JSON.stringify(S.cfg) }); lastKey = ""; }
  catch (e){ setStatus("Couldn't save settings: " + e.message); }
}
$("sendChk").addEventListener("change", e => saveConfig({ send: e.target.checked }));
$("latency").addEventListener("change", e => saveConfig({ latency_ms: +e.target.value || 0 }));
$("master").addEventListener("input", e => { S.cfg.master = +e.target.value; });
$("master").addEventListener("change", e => saveConfig({ master: +e.target.value }));

// ---------------------------------------------------------------- devices dialog
const SKU_NAMES = { H618A: "Light strip", H618C: "Light strip", H618E: "Light strip", H6076: "Floor lamp", H6010: "BR30 bulb" };
const SKU_SECTIONS = { H6076: 10 };
function newDevice(kind, fields, batch = []){
  const base = (fields.sku && SKU_NAMES[fields.sku]) || (fields.sku ? `Govee ${fields.sku}` : kind === "ble" ? "Bluetooth light" : "Wi‑Fi light");
  const taken = n => S.cfg.devices.some(d => d.name === n) || batch.some(d => d.name === n);
  let name = taken(base) || batch.some(d => d.name.startsWith(base)) ? `${base} 2` : base, n = 3;
  while (taken(name)) name = `${base} ${n++}`;
  return { id: "d" + Math.random().toString(16).slice(2, 10), kind, name, enabled: true, mode: kind === "ble" ? "whole" : "segments",
           segments: SKU_SECTIONS[fields.sku] || 15, reverse: false, variant: "b", ip: "", address: "", sku: "", ...fields };
}
function updateDevBtn(){
  const devs = S.cfg.devices || [], bad = devs.filter(d => d.kind === "ble" && d.enabled && /can't|lost/.test(S.devStatus[d.id] || ""));
  $("devBtn").textContent = `💡 Devices (${devs.filter(d => d.enabled).length})`;
  if (bad.length) setStatus(`${bad[0].name}: ${S.devStatus[bad[0].id]}`);
}
function devMsg(m){ $("devMsg").textContent = m; }
async function devicesChanged(oldCounts){
  if (S.id){
    let touched = false;
    for (const d of S.cfg.devices){ const o = oldCounts[d.id]; if (o && o !== nSec(d)){ if (!touched) pushUndo(); touched = true; rescaleDevice(d.id, o, nSec(d)); } }
    if (fillUnassigned()) touched = true;
    if (touched) changed();
  }
  buildLeds(); renderLayout(); renderHeads(); updateDevBtn();
}
async function updateDevice(id, patch, rerender){
  const old = segCounts(), d = S.cfg.devices.find(x => x.id === id); if (!d) return;
  Object.assign(d, patch);
  await saveConfig({ devices: S.cfg.devices });
  await devicesChanged(old);
  if (rerender) renderDevices();
}
async function addDevices(list){
  if (!list.length) return 0;
  const old = segCounts();
  S.cfg.devices.push(...list);
  await saveConfig({ devices: S.cfg.devices });
  await devicesChanged(old);
  renderDevices();
  return list.length;
}
function renderDevices(){
  const list = $("devList"); list.innerHTML = "";
  if (!S.cfg.devices.length) list.innerHTML = `<p class="muted">No lights yet — use the buttons below to find them.</p>`;
  for (const d of S.cfg.devices){
    const card = document.createElement("div"); card.className = "devCard" + (d.enabled ? "" : " off");
    const where = d.kind === "ble" ? `Bluetooth · ${d.address}` : `Wi‑Fi · ${d.ip || "no IP"}`;
    const lanOpts = `<label>Output <select data-k="mode"><option value="segments">Separate sections</option><option value="whole">One color</option></select></label>
        <label${d.mode === "whole" ? " hidden" : ""} title="How many sections the light has — use Identify to check">Sections <input type="number" data-k="segments" min="1" max="60" style="width:60px"></label>
        <label${d.mode === "whole" ? " hidden" : ""}><input type="checkbox" data-k="reverse"> Reverse</label>
        <button type="button" data-act="identify"${d.mode === "whole" ? " hidden" : ""} title="Section 1 white, then a rainbow toward the end, for 8 seconds">Identify sections</button>`;
    const bleOpts = `<label title="Govee lights understand slightly different Bluetooth commands">Command set <select data-k="variant"><option value="a">A</option><option value="b">B</option><option value="c">C</option></select></label>
        <button type="button" data-act="probe">Find command set</button>`;
    card.innerHTML = `<div class="devTop"><input type="checkbox" data-k="enabled" title="Use this light">
        <input type="text" data-k="name" style="flex:1;min-width:140px;max-width:240px">
        <span class="muted devAddr">${esc(where)}${d.sku ? " · " + esc(d.sku) : ""}</span><span class="devStatus" data-st="${esc(d.id)}"></span></div>
      <div class="devOpts">${d.kind === "lan" ? lanOpts : bleOpts}<span class="spacer"></span><button type="button" data-act="test">Test</button><button type="button" data-act="remove">Remove</button></div>
      <div class="devProbe" hidden></div>`;
    const f = k => card.querySelector(`[data-k="${k}"]`);
    f("enabled").checked = d.enabled; f("name").value = d.name;
    f("enabled").onchange = e => { card.classList.toggle("off", !e.target.checked); updateDevice(d.id, { enabled: e.target.checked }); };
    f("name").onchange = e => updateDevice(d.id, { name: e.target.value.trim() || d.name });
    if (d.kind === "lan"){
      f("mode").value = d.mode; f("segments").value = d.segments; f("reverse").checked = d.reverse;
      f("mode").onchange = e => updateDevice(d.id, { mode: e.target.value }, true);
      f("segments").onchange = e => { const n = clamp(Math.round(+e.target.value || 15), 1, 60); e.target.value = n; updateDevice(d.id, { segments: n }); };
      f("reverse").onchange = e => updateDevice(d.id, { reverse: e.target.checked });
    } else {
      f("variant").value = d.variant;
      f("variant").onchange = e => updateDevice(d.id, { variant: e.target.value });
    }
    card.querySelectorAll("[data-act]").forEach(b => b.onclick = () => deviceAction(d.id, b.dataset.act, card));
    list.appendChild(card);
  }
  paintDevStatus();
}
function paintDevStatus(){
  for (const el of $("devList").querySelectorAll("[data-st]")){
    const d = S.cfg.devices.find(x => x.id === el.dataset.st); if (!d) continue;
    const st = S.devStatus[d.id] || "";
    el.textContent = d.kind === "ble" ? (st === "connected" ? "● connected" : st === "connecting" ? "○ connecting…" : st === "idle" || !st ? "○ not connected" : "⚠ " + st) : "";
    el.style.color = st === "connected" ? "#4ade80" : /can't|lost/.test(st) ? "#ff8a8a" : "";
  }
}
async function deviceAction(id, act, card){
  const d = S.cfg.devices.find(x => x.id === id); if (!d) return;
  if (act === "remove"){
    if (!confirm(`Remove "${d.name}"?`)) return;
    const pre = d.id + ":";
    if (S.id){ pushUndo(); for (const t of S.tracks) t.segs = t.segs.filter(x => !x.startsWith(pre)); changed(); }
    S.cfg.devices = S.cfg.devices.filter(x => x.id !== id);
    await saveConfig({ devices: S.cfg.devices }); await devicesChanged({}); renderDevices(); return;
  }
  if (act === "identify"){
    if (!S.cfg.send) return devMsg("Turn on “Send to lights” first.");
    if (!S.id) return devMsg("Open a song first, then Identify — the show output drives the light.");
    S.identify[id] = performance.now() + 8000; lastKey = "";
    return devMsg(`${d.name}: section 1 is white, then a rainbow red → purple toward the far end. Count the color blocks and set Sections to match.`);
  }
  if (act === "test"){
    devMsg(d.kind === "ble" ? `${d.name}: connecting over Bluetooth, then red → green → blue → white…` : `${d.name}: red → green → blue → white…`);
    try { await api("/api/devices/test", { method: "POST", body: JSON.stringify({ id }) }); lastKey = ""; }
    catch (e){ devMsg(e.message); }
    return;
  }
  if (act === "probe"){
    const box = card.querySelector(".devProbe"); box.hidden = false;
    box.innerHTML = `<span>Connecting, then watch the bulb: it tries <b style="color:#ff5252">red</b>, then <b style="color:#4ade80">green</b>, then <b style="color:#60a5fa">blue</b>, 2 seconds each. Which color did it turn <i>first</i>?</span>
      <button type="button" data-v="a">Red</button><button type="button" data-v="b">Green</button><button type="button" data-v="c">Blue</button><button type="button" data-v="">Nothing</button>`;
    box.querySelectorAll("button").forEach(b => b.onclick = async () => {
      if (b.dataset.v){ card.querySelector('[data-k="variant"]').value = b.dataset.v; await updateDevice(id, { variant: b.dataset.v }); box.innerHTML = `Saved command set ${b.dataset.v.toUpperCase()}. Press Test to double-check.`; }
      else box.innerHTML = `No luck. Make sure the bulb is switched on, fully close the Govee app on your phone (it can hold the Bluetooth connection), then try again.`;
    });
    try { await api("/api/devices/probe", { method: "POST", body: JSON.stringify({ id }) }); } catch (e){ box.textContent = e.message; }
  }
}
$("devBtn").onclick = () => { devMsg(""); renderDevices(); $("devDlg").showModal(); };
$("findLan").onclick = async () => {
  devMsg("Searching your Wi‑Fi for Govee lights with LAN Control…");
  try {
    const r = await api("/api/discover");
    if (r.error) return devMsg(r.error);
    const fresh = [];
    for (const x of r.devices) if (!S.cfg.devices.some(d => d.kind === "lan" && d.ip === x.ip)) fresh.push(newDevice("lan", { ip: x.ip, sku: x.sku }, fresh));
    const n = await addDevices(fresh);
    devMsg(!r.devices.length ? "No lights answered. Is LAN Control on in the Govee app, and is this computer on the same Wi‑Fi?"
      : n ? `Added ${n} light${n > 1 ? "s" : ""}. Use Identify to check how many sections each has.` : "Found only lights you already have.");
  } catch (e){ devMsg("Search failed: " + e.message); }
};
$("findBle").onclick = async () => {
  if (!S.caps.bluetooth) return devMsg(S.caps.platform === "android" ? "This device doesn't have Bluetooth." : "Bluetooth support isn't installed. In a terminal run:  pip install bleak  — then restart Light Studio.");
  devMsg("Scanning for Govee Bluetooth lights (about 6 seconds)… close the Govee app on your phone first if it's open.");
  try {
    const r = await api("/api/ble/scan");
    const fresh = [];
    for (const x of r.devices) if (!S.cfg.devices.some(d => d.kind === "ble" && d.address === x.address)) fresh.push(newDevice("ble", { address: x.address, sku: x.sku }, fresh));
    const n = await addDevices(fresh);
    devMsg(!r.devices.length ? "No Govee Bluetooth lights found. Make sure they're on, close the Govee app on your phone, and try again."
      : n ? `Added ${n} light${n > 1 ? "s" : ""}. Press “Find command set” on one to see which commands it understands.` : "Found only lights you already have.");
  } catch (e){ devMsg(e.message); }
};
$("addIp").onclick = async () => {
  const ip = (prompt("IP address of the Govee light (LAN Control must be on):") || "").trim();
  if (!ip) return;
  if (!/^[0-9.]+$/.test(ip)) return devMsg("That doesn't look like an IP address, e.g. 192.168.1.42");
  await addDevices([newDevice("lan", { ip })]); devMsg("Added. Press Test to check it responds.");
};
setInterval(async () => {
  if (!(S.cfg.devices || []).some(d => d.kind === "ble")) return;
  try { S.devStatus = await api("/api/devices/status"); } catch (e){ return; }
  updateDevBtn(); if ($("devDlg").open) paintDevStatus();
}, 1500);

// ---------------------------------------------------------------- output: preview + lights
let lastKey = "", inFlight = false, lastSendAt = 0, ledCache = {};
function buildLeds(){
  const box = $("leds"); box.innerHTML = ""; ledCache = {};
  const devs = enabledDevs();
  if (!devs.length){ box.innerHTML = `<div class="muted" style="font-size:13px;padding-bottom:6px">No lights added yet — click <b>💡 Devices</b> at the top to add your strip, lamp or bulbs.</div>`; return; }
  for (const d of devs){
    const row = document.createElement("div"); row.className = "ledRow";
    row.innerHTML = `<span class="ledName"></span><div class="ledCells${nSec(d) === 1 ? " one" : ""}"></div>`;
    row.querySelector(".ledName").textContent = d.name; row.querySelector(".ledName").title = d.name;
    const cells = row.querySelector(".ledCells"); cells.dataset.dev = d.id;
    for (let i = 0; i < nSec(d); i++){ const c = document.createElement("div"); c.title = `${d.name} · section ${i + 1}`; cells.appendChild(c); }
    box.appendChild(row);
  }
}
function compositeAt(t){
  const out = {};
  for (const d of enabledDevs()) out[d.id] = Array.from({ length: nSec(d) }, () => [0, 0, 0]);
  const solo = S.tracks.some(tr => tr.solo);
  for (const tr of S.tracks){
    if (tr.mute || (solo && !tr.solo) || !tr._ev) continue;
    const st = tr._ev(t), k = st[3] / 100, c = [st[0] * k, st[1] * k, st[2] * k];
    if (c[0] + c[1] + c[2] < 6) continue;
    for (const key of tr.segs){ const p = key.lastIndexOf(":"), arr = out[key.slice(0, p)], i = +key.slice(p + 1); if (arr && i < arr.length) arr[i] = c; }
  }
  return out;
}
const identifyFrame = n => Array.from({ length: n }, (_, i) => i === 0 ? [255, 255, 255] : hsv((i - 1) / Math.max(1, n - 1) * 0.8, 1, 1));
function updatePreview(frame){
  for (const cells of $("leds").querySelectorAll(".ledCells")){
    const segs = frame[cells.dataset.dev] || [], kids = cells.children;
    for (let i = 0; i < segs.length && i < kids.length; i++){
      const [r, g, b] = segs[i].map(v => v | 0), key = `${r},${g},${b}`, ck = cells.dataset.dev + i;
      if (ledCache[ck] === key) continue; ledCache[ck] = key;
      kids[i].style.background = `rgb(${r},${g},${b})`;
      kids[i].style.boxShadow = r + g + b > 30 ? `0 0 12px 1px rgb(${r},${g},${b})` : "none";
    }
  }
}
setInterval(() => {
  if (!S.id) return;
  if (S.loop && regionOk() && !audio.paused && audio.currentTime >= S.B) audio.currentTime = S.A;
  const now = performance.now(), devs = enabledDevs(), ident = d => (S.identify[d.id] || 0) > now;
  const view = compositeAt(audio.currentTime);
  for (const d of devs) if (ident(d)) view[d.id] = identifyFrame(nSec(d));
  updatePreview(view);
  if (!S.cfg.send || !devs.length) return;
  const fr = compositeAt(audio.currentTime + (S.cfg.latency_ms || 0) / 1000), m = (S.cfg.master || 100) / 100, payload = {};
  for (const d of devs){
    let segs = ident(d) ? identifyFrame(nSec(d)) : fr[d.id].map(c => c.map(v => Math.round(v * m)));
    if (d.reverse) segs = segs.slice().reverse();
    payload[d.id] = segs;
  }
  const key = JSON.stringify(payload);
  if (inFlight || now - lastSendAt < 1000 / (S.cfg.rate || 20)) return;
  if (key === lastKey && now - lastSendAt < 1500) return;   // resend now and then to keep connections alive
  inFlight = true; lastSendAt = now; lastKey = key;
  fetch("/api/frame", { method: "POST", body: JSON.stringify({ devices: payload }) })
    .then(r => r.json()).then(d => { const e = d && d.errors && Object.entries(d.errors)[0]; if (e) setStatus(`Couldn't reach a light: ${e[1]}`); })
    .catch(() => setStatus("Lost connection to Light Studio – is it still running?"))
    .finally(() => { inFlight = false; });
}, 10);

// ---------------------------------------------------------------- library
async function loadLibrary(){
  let list = [];
  try { list = await api("/api/library"); } catch (e){ $("libList").innerHTML = `<div class="muted" style="padding:8px">Can't reach Light Studio.</div>`; return; }
  const el = $("libList"); el.innerHTML = "";
  if (!list.length) el.innerHTML = `<div class="muted" style="padding:8px">No songs yet.</div>`;
  for (const s of list){
    const d = document.createElement("div");
    d.className = "libItem" + (s.id === S.id ? " active" : "");
    d.innerHTML = `<div class="libName"></div><div class="libMeta"></div><button class="libDel" title="Delete song and show">✕</button>`;
    d.querySelector(".libName").textContent = s.name;
    d.querySelector(".libMeta").textContent = [s.duration ? fmtTime(s.duration) : null, s.tempo ? `${Math.round(s.tempo)} BPM` : null, `${s.cues} cues`, s.ai ? "AI split" : null].filter(Boolean).join(" · ");
    d.onclick = () => { closeDrawer(); if (s.id !== S.id) openSong(s.id); };
    d.querySelector(".libDel").onclick = async ev => {
      ev.stopPropagation();
      if (!confirm(`Delete "${s.name}" and its light show?`)) return;
      await api(`/api/song/${s.id}`, { method: "DELETE" });
      if (s.id === S.id) closeSong();
      loadLibrary();
    };
    el.appendChild(d);
  }
}
function closeSong(){
  audio.pause(); audio.removeAttribute("src"); audio.load(); clearTimeout(pollTimer);
  Object.assign(S, { id: null, show: null, tracks: [], ab: null, feat: null }); S.sel.clear();
  $("editor").hidden = true; $("empty").hidden = false;
}
async function uploadFile(file){
  if (!file) return;
  if (!/\.(mp3|wav|ogg|oga|m4a|aac|flac|opus|webm|mp4)$/i.test(file.name) && !/^(audio|video)\//.test(file.type)){ alert("That doesn't look like an audio file. Use mp3, wav, ogg, m4a, flac or opus."); return; }
  if (saveTimer) await saveNow();
  showBusy("Adding song…");
  try {
    const r = await api("/api/upload", { method: "POST", headers: { "X-Filename": encodeURIComponent(file.name) }, body: file });
    await loadLibrary();
    await openSong(r.id, true);
  } catch (e){ alert("Upload failed: " + e.message); }
  finally { hideBusy(); }
}

async function openSong(id, isNew){
  if (saveTimer) await saveNow();
  audio.pause(); clearTimeout(pollTimer);
  showBusy("Loading song…");
  try {
    const show = await api(`/api/song/${id}/show`);
    const res = await fetch(`/api/song/${id}/audio`);
    if (!res.ok) throw new Error("couldn't load the audio file");
    const buf = await res.arrayBuffer();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(new Blob([buf], { type: res.headers.get("Content-Type") || "audio/mpeg" }));
    showBusy("Reading audio…"); await tick();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    let ab;
    try { ab = await ctx.decodeAudioData(buf.slice(0)); }
    catch (e){ throw new Error("your browser can't decode this file. Try converting it to mp3."); }
    finally { ctx.close(); }
    const mono = mixToMono(ab);
    S.env = buildEnvelope(mono, ab.sampleRate, S.envRate);
    let an = show.analysis, fresh = false;
    if (!an || !an.beats){ showBusy("Finding the beat…"); await tick(); an = analyzeSamples(mono, ab.sampleRate); fresh = true; }
    let feat = null;
    try { feat = await api(`/api/song/${id}/features`); } catch (e){ /* none yet */ }
    if (!feat || !feat.tracks){
      showBusy("Listening for each instrument…"); await tick();
      feat = bandFeatures(mono, ab.sampleRate);
      api(`/api/song/${id}/features`, { method: "PUT", body: JSON.stringify(feat) }).catch(() => {});
    }
    audio.src = audioUrl;
    Object.assign(S, { id, show, an, feat, ab, dur: ab.duration, A: show.loopA ?? null, B: show.loopB ?? null, undo: [], redo: [], edited: false, active: 0, paint: 0 });
    S.drops = computeDrops();
    S.sel.clear();
    if (Array.isArray(show.tracks) && show.tracks.length){
      S.tracks = show.tracks.map(normTrack);
      const saved = show.segCounts || (show.segCount ? { [fallbackDevId()]: show.segCount } : {});
      for (const d of S.cfg.devices) if (saved[d.id] && saved[d.id] !== nSec(d)){ rescaleDevice(d.id, saved[d.id], nSec(d)); fresh = true; }
      if (fillUnassigned()) fresh = true;
    } else {
      S.tracks = defaultTracks(feat); defaultLayout(); rebuild();
      if (Array.isArray(show.cues) && show.cues.length){        // show made with the first version of Light Studio
        S.tracks.push(normTrack({ id: "whole", name: "Whole strip", feature: null, style: "off", palette: "white", cues: show.cues, segs: enabledDevs().flatMap(secKeys) }));
      } else generateLanes(S.tracks.map(t => t.id), { bpc: 4 }, false);
      fresh = true;
    }
    rebuild();
    $("empty").hidden = true; $("editor").hidden = false;
    $("songName").value = show.name || "Untitled";
    $("songMeta").textContent = `${fmtTime(S.dur)} · ~${Math.round(an.tempo)} BPM`;
    $("saveState").textContent = "";
    buildLeds(); renderHeads(); renderLayout(); resizeCanvas(); fitView(); updateLoopUI(); renderInspector();
    if (fresh) await saveNow();
    loadLibrary();
    await checkSeparation(id, isNew);
  } catch (e){ alert("Couldn't open this song: " + e.message); console.error(e); }
  finally { hideBusy(); }
}

$("addSongBtn").onclick = $("addSongBtn2").onclick = () => $("fileInput").click();
$("ytBtn").onclick = $("ytBtn2").onclick = importYouTube;
const YT_RE = /^https?:\/\/((www|m|music)\.)?(youtube\.com|youtu\.be)\//i;
async function importYouTube(sharedUrl){
  if (!S.caps.youtube){ alert("To add songs from YouTube links, open a terminal and run:\n\n    pip install yt-dlp\n\nthen restart Light Studio."); return; }
  const url = (typeof sharedUrl === "string" ? sharedUrl : prompt("Paste a YouTube link (only for songs you're allowed to download):") || "").trim();
  if (!url) return;
  if (!YT_RE.test(url)){ alert("That doesn't look like a YouTube link (youtube.com/… or youtu.be/…)."); return; }
  if (saveTimer) await saveNow();
  showBusy("Contacting YouTube…");
  try {
    const { job } = await api("/api/youtube", { method: "POST", body: JSON.stringify({ url }) });
    for (;;){
      await new Promise(r => setTimeout(r, 700));
      const st = await api(`/api/youtube/${job}`);
      if (st.state === "done"){ hideBusy(); await loadLibrary(); await openSong(st.id, true); return; }
      if (st.state === "error") throw new Error(st.msg);
      showBusy(st.msg || "Downloading…");
    }
  } catch (e){ hideBusy(); alert("Couldn't add that video: " + e.message); }
}
$("fileInput").onchange = e => { uploadFile(e.target.files[0]); e.target.value = ""; };
let dragDepth = 0;
window.addEventListener("dragenter", e => { if (e.dataTransfer.types.includes("Files")){ dragDepth++; $("dropOverlay").classList.add("show"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0){ dragDepth = 0; $("dropOverlay").classList.remove("show"); } });
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => { e.preventDefault(); dragDepth = 0; $("dropOverlay").classList.remove("show"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
$("songName").addEventListener("change", async e => { if (!S.show) return; S.show.name = e.target.value.trim() || "Untitled"; await saveNow(); });

// ---------------------------------------------------------------- AI instrument separation
function setSep(html, buttons = []){
  const bar = $("sepBar");
  if (!html){ bar.hidden = true; return; }
  bar.hidden = false; bar.innerHTML = `<span>${html}</span>`;
  for (const [label, fn, primary] of buttons){ const b = document.createElement("button"); b.textContent = label; if (primary) b.className = "primary"; b.onclick = fn; bar.appendChild(b); }
}
function sepOffer(){
  if (S.feat && S.feat.source === "demucs") return setSep("🎛️ Instruments separated with AI (Demucs).", [["Hide", () => setSep("")]]);
  if (S.caps.demucs) return setSep("🎚️ Instruments are <b>estimated</b> from frequency ranges.", [["Separate instruments with AI", startSeparation, true], ["Hide", () => setSep("")]]);
  if (S.caps.platform === "android") return setSep("🎚️ Instruments are <b>estimated</b> from frequency ranges. (AI separation of drums / bass / vocals is in the desktop version.)", [["Hide", () => setSep("")]]);
  setSep("🎚️ Instruments are <b>estimated</b> from frequency ranges. For real drums / bass / vocals separation, run <code>pip install demucs</code> and restart Light Studio.", [["Hide", () => setSep("")]]);
}
async function checkSeparation(id, autoStart){
  let st = { state: "idle" };
  try { st = await api(`/api/song/${id}/separate`); } catch (e){ /* ignore */ }
  if (st.state === "running" || st.state === "queued") return pollSep(id);
  if (st.state === "done" && S.feat.source !== "demucs") return applyAiFeatures(id);
  if (autoStart && S.caps.demucs && S.feat.source !== "demucs") return startSeparation();
  sepOffer();
}
async function wavBlob(ab){
  let buf = ab;
  if (ab.sampleRate !== 44100 || ab.numberOfChannels !== 2){
    const oc = new OfflineAudioContext(2, Math.ceil(ab.duration * 44100), 44100), src = oc.createBufferSource();
    src.buffer = ab; src.connect(oc.destination); src.start(); buf = await oc.startRendering();
  }
  const n = buf.length, L = buf.getChannelData(0), R = buf.getChannelData(1), dv = new DataView(new ArrayBuffer(44 + n * 4));
  const str = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); dv.setUint32(4, 36 + n * 4, true); str(8, "WAVE"); str(12, "fmt "); dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); dv.setUint16(22, 2, true); dv.setUint32(24, 44100, true); dv.setUint32(28, 44100 * 4, true);
  dv.setUint16(32, 4, true); dv.setUint16(34, 16, true); str(36, "data"); dv.setUint32(40, n * 4, true);
  for (let i = 0, o = 44; i < n; i++, o += 4){
    const l = clamp(L[i], -1, 1), r = clamp(R[i], -1, 1);
    dv.setInt16(o, l < 0 ? l * 0x8000 : l * 0x7fff, true); dv.setInt16(o + 2, r < 0 ? r * 0x8000 : r * 0x7fff, true);
  }
  return new Blob([dv.buffer], { type: "audio/wav" });
}
async function startSeparation(){
  const id = S.id; if (!id || !S.ab) return;
  setSep("🎛️ Preparing audio for AI separation…");
  try {
    const blob = await wavBlob(S.ab);
    if (id !== S.id) return;
    await api(`/api/song/${id}/separate`, { method: "POST", body: blob });
    pollSep(id);
  } catch (e){ setSep("Couldn't start AI separation: " + esc(e.message), [["Hide", () => setSep("")]]); }
}
async function pollSep(id){
  clearTimeout(pollTimer);
  if (id !== S.id) return;
  let st; try { st = await api(`/api/song/${id}/separate`); } catch (e){ pollTimer = setTimeout(() => pollSep(id), 3000); return; }
  if (id !== S.id) return;
  if (st.state === "running" || st.state === "queued"){
    setSep(`<span class="spinner" style="width:14px;height:14px;border-width:2px;display:inline-block;vertical-align:-2px"></span>&nbsp; ${esc(st.msg)} <span class="muted">${Math.round(st.elapsed || 0)}s</span> — you can keep editing.`);
    pollTimer = setTimeout(() => pollSep(id), 2000);
  } else if (st.state === "done") applyAiFeatures(id);
  else if (st.state === "error") setSep("AI separation failed: " + esc(st.msg), [["Try again", startSeparation], ["Hide", () => setSep("")]]);
  else sepOffer();
}
async function applyAiFeatures(id){
  let feat; try { feat = await api(`/api/song/${id}/features`); } catch (e){ return sepOffer(); }
  if (id !== S.id || !feat || feat.source !== "demucs") return sepOffer();
  S.feat = feat; S.drops = computeDrops();
  const regen = () => {
    pushUndo();
    let added = false;
    for (const k of INSTRUMENTS) if (feat.tracks[k] && !S.tracks.some(t => t.feature === k)){
      const i = S.tracks.findIndex(t => !t.feature); S.tracks.splice(i < 0 ? S.tracks.length : i, 0, normTrack({ id: k, feature: k })); added = true;
    }
    if (added) defaultLayout();
    generateLanes(S.tracks.filter(t => t.feature).map(t => t.id), { bpc: +$("gBpc").value || 4 }, false);
    changed(); renderHeads(); renderLayout(); resizeCanvas();
    setSep("🎛️ Instruments separated with AI — lanes rebuilt from the real drums, bass, vocals and other parts.", [["Undo", undo], ["Hide", () => setSep("")]]);
  };
  if (!S.edited) regen();
  else setSep("🎛️ AI separation finished. Rebuild the instrument lanes from it? (Your Accents lane is kept.)", [["Rebuild instrument lanes", regen, true], ["Not now", () => setSep("")]]);
}

// ---------------------------------------------------------------- transport, volume & loop
function regionOk(){ return S.A != null && S.B != null && S.B - S.A > 0.05; }
function seek(t){ if (S.id) audio.currentTime = clamp(t, 0, S.dur); }
function togglePlay(){ if (!S.id) return; if (audio.paused){ if (S.loop && regionOk() && audio.currentTime >= S.B) audio.currentTime = S.A; audio.play().catch(() => {}); } else audio.pause(); }
audio.addEventListener("play", () => { $("playBtn").textContent = "❚❚"; });
audio.addEventListener("pause", () => { $("playBtn").textContent = "▶"; });
audio.addEventListener("ended", () => { if (S.loop && regionOk()){ audio.currentTime = S.A; audio.play(); } });
$("playBtn").onclick = togglePlay;
function setVolume(v){ audio.volume = clamp(v, 0, 100) / 100; $("vol").value = Math.round(audio.volume * 100); updateVolIcon(); try { localStorage.setItem("lightStudioVol", $("vol").value); } catch (e){ /* ignore */ } }
function updateVolIcon(){ $("volIcon").textContent = audio.muted || audio.volume === 0 ? "🔇" : audio.volume < 0.5 ? "🔉" : "🔊"; }
function toggleMute(){ audio.muted = !audio.muted; updateVolIcon(); }
$("vol").addEventListener("input", e => { audio.muted = false; setVolume(+e.target.value); });
$("volIcon").onclick = toggleMute;
try { const v = localStorage.getItem("lightStudioVol"); setVolume(v == null ? 80 : +v); } catch (e){ setVolume(80); }
function updateLoopUI(){
  audio.loop = S.loop && !regionOk();
  $("loopBtn").classList.toggle("on", S.loop);
  $("loopInfo").textContent = regionOk() ? `A ${fmtTime(S.A, true)} → B ${fmtTime(S.B, true)}` : (S.A != null ? `A ${fmtTime(S.A, true)} → set B` : "");
  S.staticDirty = true;
}
function toggleLoop(){ S.loop = !S.loop; updateLoopUI(); }
function setA(){ S.A = r3(audio.currentTime); if (S.B != null && S.B <= S.A + 0.05) S.B = null; updateLoopUI(); scheduleSave(); }
function setB(){ const t = r3(audio.currentTime); if (S.A == null || t <= S.A + 0.05){ S.B = null; S.A = t; } else S.B = t; updateLoopUI(); scheduleSave(); }
$("loopBtn").onclick = toggleLoop; $("setA").onclick = setA; $("setB").onclick = setB;
$("clearAB").onclick = () => { S.A = S.B = null; updateLoopUI(); scheduleSave(); };
$("undoBtn").onclick = undo; $("redoBtn").onclick = redo;

// ---------------------------------------------------------------- lane headers
function setActive(i){ S.active = clamp(i, 0, S.tracks.length - 1); renderHeads(); S.staticDirty = true; }
function renderHeads(){
  const h = $("heads"); h.innerHTML = "";
  h.style.height = TH() + "px";
  const lab = document.createElement("div"); lab.className = "headlabel"; lab.style.top = (G.ruler + 6) + "px"; lab.textContent = "waveform"; h.appendChild(lab);
  S.tracks.forEach((tr, i) => {
    const d = document.createElement("div");
    d.className = "head" + (i === S.active ? " active" : "") + (tr.mute ? " muted" : "");
    d.style.top = laneTop(i) + "px"; d.style.height = G.lane + "px";
    d.title = `${tr.name}: lights ${tr.segs.length} section(s)`;
    d.innerHTML = `<i style="background:${trackColor(tr)}"></i><span class="hname"></span><button class="mini${tr.mute ? " on" : ""}" title="Mute this lane's lights">M</button><button class="mini${tr.solo ? " on" : ""}" title="Show only this lane">S</button>`;
    d.querySelector(".hname").textContent = tr.name;
    const [mb, sb] = d.querySelectorAll("button");
    mb.onclick = e => { e.stopPropagation(); tr.mute = !tr.mute; renderHeads(); S.staticDirty = true; scheduleSave(); };
    sb.onclick = e => { e.stopPropagation(); tr.solo = !tr.solo; renderHeads(); S.staticDirty = true; scheduleSave(); };
    d.onclick = () => setActive(i);
    h.appendChild(d);
  });
}

// ---------------------------------------------------------------- strip sections layout
let painting = null;
function renderLayout(){
  const pills = $("paintPills"); pills.innerHTML = "";
  S.paint = clamp(S.paint, 0, Math.max(0, S.tracks.length - 1));
  S.tracks.forEach((tr, i) => {
    const b = document.createElement("button"); b.className = "pill" + (i === S.paint ? " on" : "");
    b.innerHTML = `<i style="background:${trackColor(tr)}"></i>`; b.append(tr.name);
    b.onclick = () => { S.paint = i; renderLayout(); };
    pills.appendChild(b);
  });
  const grid = $("segGrid"); grid.innerHTML = "";
  const devs = enabledDevs();
  if (!devs.length){ grid.innerHTML = `<div class="muted" style="font-size:13px">Add lights with <b>💡 Devices</b> to assign instruments to them.</div>`; return; }
  for (const d of devs){
    const row = document.createElement("div"); row.className = "segRow";
    row.innerHTML = `<span class="segName"></span><div class="segBoxes${nSec(d) === 1 ? " one" : ""}"></div>`;
    row.querySelector(".segName").textContent = d.name;
    const boxes = row.querySelector(".segBoxes");
    for (const key of secKeys(d)){
      const box = document.createElement("div"); box.className = "segBox";
      box.innerHTML = `<b>${nSec(d) === 1 ? (d.kind === "ble" ? "bulb" : "all") : +key.split(":").pop() + 1}</b>` +
        S.tracks.filter(t => t.segs.includes(key)).map(t => `<i style="background:${trackColor(t)}" title="${esc(t.name)}"></i>`).join("");
      box.dataset.key = key;
      box.onpointerdown = e => { e.preventDefault(); const tr = S.tracks[S.paint]; if (!tr) return; pushUndo(); painting = { add: !tr.segs.includes(key), last: key }; paintSeg(key); };
      boxes.appendChild(box);
    }
    grid.appendChild(row);
  }
}
function paintSeg(key){
  const tr = S.tracks[S.paint], set = new Set(tr.segs);
  if (painting.add) set.add(key); else set.delete(key);
  tr.segs = [...set];
  renderLayout(); renderHeads();
}
window.addEventListener("pointermove", e => {
  if (!painting) return;
  const el = document.elementFromPoint(e.clientX, e.clientY), box = el && el.closest(".segBox");
  if (box && box.dataset.key && box.dataset.key !== painting.last){ painting.last = box.dataset.key; paintSeg(box.dataset.key); }
});
for (const ev of ["pointerup", "pointercancel"]) window.addEventListener(ev, () => { if (painting){ painting = null; changed(); } });
$("splitBtn").onclick = () => { pushUndo(); defaultLayout(); changed(); renderLayout(); renderHeads(); };

// ---------------------------------------------------------------- timeline
const cv = $("tl"), cx = cv.getContext("2d"), off = document.createElement("canvas"), ox = off.getContext("2d");
const G = { ruler: 24, wave: 62, gap: 6, lane: 50 };
const laneTop = i => G.ruler + 2 + G.wave + G.gap + i * G.lane;
const TH = () => laneTop(S.tracks.length) + 2;
const laneAt = y => { const i = Math.floor((y - laneTop(0)) / G.lane); return i >= 0 && i < S.tracks.length ? i : -1; };
const cueY = (c, i) => laneTop(i) + 19 + (1 - c.brightness / 100) * (G.lane - 27);
const MAXPPS = 600;
let W = 800, dpr = 1;

function resizeCanvas(){
  dpr = window.devicePixelRatio || 1; W = Math.max(200, $("tlWrap").clientWidth);
  const h = TH();
  for (const c of [cv, off]){ c.width = Math.round(W * dpr); c.height = Math.round(h * dpr); }
  cv.style.width = W + "px"; cv.style.height = h + "px"; $("heads").style.height = h + "px";
  cx.setTransform(dpr, 0, 0, dpr, 0, 0); ox.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.pps = Math.max(S.pps, minPps()); clampView(); syncZoomUI(); S.staticDirty = true;
}
window.addEventListener("resize", () => { if (S.id) resizeCanvas(); });
const xOf = t => (t - S.view) * S.pps, tOf = x => S.view + x / S.pps;
const minPps = () => W / Math.max(1, S.dur);
function fitView(){ S.pps = minPps(); S.view = 0; syncZoomUI(); clampView(); }
function clampView(){
  const vis = W / S.pps, max = Math.max(0, S.dur - vis); S.view = clamp(S.view, 0, max);
  $("scroll").value = max ? Math.round(S.view / max * 1000) : 0; $("scroll").disabled = !max; S.staticDirty = true;
}
function syncZoomUI(){ const a = minPps(); $("zoom").value = Math.round(100 * Math.log(S.pps / a) / Math.log(MAXPPS / a)); }
function setPps(p, ax){ const t = tOf(ax); S.pps = clamp(p, minPps(), MAXPPS); S.view = t - ax / S.pps; clampView(); syncZoomUI(); }
$("zoom").addEventListener("input", e => { const a = minPps(); setPps(a * Math.pow(MAXPPS / a, e.target.value / 100), clamp(xOf(audio.currentTime), 0, W)); });
$("scroll").addEventListener("input", e => { const max = Math.max(0, S.dur - W / S.pps); S.view = max * e.target.value / 1000; clampView(); });

function firstIndexAt(arr, t, key){ let lo = 0, hi = arr.length; while (lo < hi){ const m = (lo + hi) >> 1; if ((key ? arr[m][key] : arr[m]) < t) lo = m + 1; else hi = m; } return lo; }
function snapT(t, thrSec){
  if (!$("snapChk").checked || !S.an || !S.an.beats.length) return t;
  const b = S.an.beats, i = firstIndexAt(b, t), cand = [];
  for (const k of [i - 1, i]) if (k >= 0 && k < b.length){ cand.push(b[k]); if (k + 1 < b.length) cand.push((b[k] + b[k + 1]) / 2); }
  if (S.feat && S.tracks[S.active] && S.tracks[S.active].feature){        // also snap to this instrument's hits
    const ons = (S.feat.tracks[S.tracks[S.active].feature] || {}).onsets || [];
    const j = firstIndexAt(ons.map(o => o[0]), t); for (const k of [j - 1, j]) if (k >= 0 && k < ons.length) cand.push(ons[k][0]);
  }
  let best = t, bd = thrSec ?? 10 / S.pps;
  for (const c of cand){ const d = Math.abs(c - t); if (d < bd){ bd = d; best = c; } }
  return best;
}

function drawStatic(){
  const g = ox, t0 = S.view, t1 = tOf(W), H = TH();
  g.fillStyle = "#0f1218"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#151a23"; g.fillRect(0, 0, W, G.ruler);
  S.tracks.forEach((tr, i) => { g.fillStyle = i === S.active ? "#14110b" : (i % 2 ? "#0d1016" : "#0b0e13"); g.fillRect(0, laneTop(i), W, G.lane); });
  if (regionOk()){
    const xa = xOf(S.A), xb = xOf(S.B);
    g.fillStyle = S.loop ? "rgba(255,180,0,.14)" : "rgba(255,180,0,.06)"; g.fillRect(xa, 0, xb - xa, H);
    g.fillStyle = "#ffb000"; g.fillRect(xa, 0, 1.5, H); g.fillRect(xb - 1.5, 0, 1.5, H);
    g.font = "bold 11px system-ui"; g.fillText("A", xa + 4, 12); g.fillText("B", xb - 12, 12);
  } else if (S.A != null){ g.fillStyle = "#ffb000"; g.fillRect(xOf(S.A), 0, 1.5, H); }
  for (const d of S.drops || []){                    // bass drop markers
    const x = Math.round(xOf(d)) + 0.5; if (x < -40 || x > W + 40) continue;
    g.fillStyle = "rgba(255,90,40,.18)"; g.fillRect(x, G.ruler, 2, TH() - G.ruler);
    g.fillStyle = "#ff6a3d"; g.beginPath(); g.moveTo(x, G.ruler); g.lineTo(x + 8, G.ruler - 6); g.lineTo(x, G.ruler - 12); g.closePath(); g.fill();
    g.font = "bold 10px system-ui"; g.fillText("DROP", x + 10, G.ruler - 3);
  }
  const step = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120].find(s => s * S.pps >= 70) || 120;
  g.font = "11px system-ui"; g.fillStyle = "#8a94a6"; g.strokeStyle = "#2b3342"; g.lineWidth = 1;
  for (let t = Math.floor(t0 / step) * step; t <= t1; t += step){
    const x = Math.round(xOf(t)) + 0.5; g.beginPath(); g.moveTo(x, 14); g.lineTo(x, G.ruler); g.stroke(); g.fillText(fmtTime(t, step < 1), x + 3, 12);
  }
  if (S.an && medianGap(S.an.beats) * S.pps >= 5){
    const b = S.an.beats;
    for (let i = Math.max(0, firstIndexAt(b, t0) - 1); i < b.length && b[i] <= t1; i++){
      const x = Math.round(xOf(b[i])) + 0.5; g.strokeStyle = i % 4 === 0 ? "rgba(255,255,255,.10)" : "rgba(255,255,255,.04)";
      g.beginPath(); g.moveTo(x, G.ruler); g.lineTo(x, H); g.stroke();
    }
  }
  if (S.env){
    const top = G.ruler + 2, mid = top + G.wave / 2, amp = G.wave / 2 - 3; g.fillStyle = "#4a5d84";
    for (let x = 0; x < W; x++){
      const a = Math.floor(tOf(x) * S.envRate), b = Math.max(a + 1, Math.floor(tOf(x + 1) * S.envRate));
      let m = 0; for (let k = Math.max(0, a); k < b && k < S.env.length; k++) if (S.env[k] > m) m = S.env[k];
      const h = Math.max(0.5, m * amp); g.fillRect(x, mid - h, 1, 2 * h);
    }
  }
  const solo = S.tracks.some(t => t.solo);
  S.tracks.forEach((tr, li) => {
    const y0 = laneTop(li), dim = tr.mute || (solo && !tr.solo);
    // lane light strip
    for (let x = 0; x < W; x += 2){
      const st = tr._ev(tOf(x + 1)), k = st[3] / 100 * (dim ? 0.3 : 1);
      g.fillStyle = `rgb(${st[0] * k | 0},${st[1] * k | 0},${st[2] * k | 0})`; g.fillRect(x, y0 + 3, 2, 10);
    }
    // instrument activity (faint)
    const f = S.feat && tr.feature && S.feat.tracks[tr.feature];
    if (f){
      g.fillStyle = "rgba(140,160,200,.10)";
      for (let x = 0; x < W; x += 2){ const v = envMax(f, tOf(x), tOf(x + 2)); if (v > 0) g.fillRect(x, y0 + G.lane - 3 - v * (G.lane - 18), 2, v * (G.lane - 18)); }
    }
    const cues = tr.cues, ci = Math.max(0, firstIndexAt(cues, t0 - 10, "t"));
    g.lineWidth = 2;
    for (let i = ci; i < cues.length && cues[i].t <= t1 + 0.1; i++){
      const c = cues[i]; if (!(c.fade > 0)) continue;
      const x = xOf(c.t), y = cueY(c, li); g.globalAlpha = 0.5; g.strokeStyle = c.color === "#000000" ? "#555" : c.color;
      g.beginPath(); g.moveTo(x, y); g.lineTo(x + c.fade * S.pps, y); g.stroke(); g.globalAlpha = 1;
    }
    const big = S.pps > 40;
    for (let i = ci; i < cues.length && cues[i].t <= t1 + 0.1; i++){
      const c = cues[i], x = xOf(c.t), y = cueY(c, li), sel = S.sel.has(c), r = sel ? 6 : (big ? 4.5 : 3.5);
      g.beginPath(); g.moveTo(x, y - r); g.lineTo(x + r, y); g.lineTo(x, y + r); g.lineTo(x - r, y); g.closePath();
      g.fillStyle = c.color; g.fill();
      g.lineWidth = sel ? 2 : 1; g.strokeStyle = sel ? "#ffffff" : (c.color === "#000000" ? "rgba(150,160,180,.7)" : "rgba(0,0,0,.6)"); g.stroke();
    }
    g.fillStyle = "#1c2330"; g.fillRect(0, y0, W, 1);
  });
  S.staticDirty = false;
}

let drag = null;
function frame(){
  requestAnimationFrame(frame);
  if (!S.id) return;
  const t = audio.currentTime, H = TH();
  if (!audio.paused && !drag){ const x = xOf(t); if (x > W * 0.92 || x < 0){ S.view = t - (W / S.pps) * 0.08; clampView(); } }
  if (S.staticDirty) drawStatic();
  cx.clearRect(0, 0, W, H); cx.drawImage(off, 0, 0, W, H);
  if (drag && drag.kind === "box" && (Math.abs(drag.x1 - drag.x0) > 3 || Math.abs(drag.y1 - drag.y0) > 3)){
    const a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1), ya = Math.min(drag.y0, drag.y1), yb = Math.max(drag.y0, drag.y1);
    cx.fillStyle = "rgba(120,170,255,.15)"; cx.fillRect(a, ya, b - a, yb - ya);
    cx.strokeStyle = "rgba(120,170,255,.8)"; cx.lineWidth = 1; cx.strokeRect(a + 0.5, ya + 0.5, b - a, yb - ya);
  }
  if (drag && drag.kind === "ruler" && Math.abs(drag.x1 - drag.x0) > 4){
    const a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1); cx.fillStyle = "rgba(255,180,0,.2)"; cx.fillRect(a, 0, b - a, H);
  }
  const px = Math.round(xOf(t)) + 0.5;
  cx.strokeStyle = "#ff4d4d"; cx.lineWidth = 1.5; cx.beginPath(); cx.moveTo(px, 0); cx.lineTo(px, H); cx.stroke();
  cx.fillStyle = "#ff4d4d"; cx.beginPath(); cx.moveTo(px - 6, 0); cx.lineTo(px + 6, 0); cx.lineTo(px, 8); cx.closePath(); cx.fill();
  const td = `${fmtTime(t, true)} / ${fmtTime(S.dur)}`; if ($("timeDisp").textContent !== td) $("timeDisp").textContent = td;
}
requestAnimationFrame(frame);

function canvasPos(e){ const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
function hitCue(x, y){
  const li = laneAt(y); if (li < 0) return null;
  const cues = S.tracks[li].cues; let best = null, bd = 1e9;
  for (let i = firstIndexAt(cues, tOf(x - 8), "t"); i < cues.length && cues[i].t <= tOf(x + 8); i++){
    const c = cues[i], dx = Math.abs(xOf(c.t) - x), dy = Math.abs(cueY(c, li) - y);
    if (dx <= 8 && dy <= 10 && dx + dy < bd){ bd = dx + dy; best = c; }
  }
  return best;
}
const ptrs = new Map();
let pinch = null, pressTimer = null;
function endPress(){ clearTimeout(pressTimer); pressTimer = null; }
cv.addEventListener("pointerdown", e => {
  if (!S.id || (e.pointerType === "mouse" && e.button !== 0)) return;
  ptrs.set(e.pointerId, canvasPos(e));
  try { cv.setPointerCapture(e.pointerId); } catch (err){ /* ignore */ }
  if (ptrs.size === 2){                                   // two fingers: pinch to zoom, drag to scroll
    endPress();
    if (drag && drag.kind === "move" && drag.moved) scheduleSave();
    drag = null;
    const [a, b] = [...ptrs.values()], mid = (a.x + b.x) / 2;
    pinch = { d0: Math.max(20, Math.abs(a.x - b.x)), pps0: S.pps, t0: tOf(mid) };
    return;
  }
  if (ptrs.size > 2) return;
  const { x, y } = canvasPos(e), li = laneAt(y);
  if (li >= 0){
    if (li !== S.active) setActive(li);
    const hit = hitCue(x, y);
    if (hit){
      if (e.shiftKey || e.ctrlKey || e.metaKey){ if (S.sel.has(hit)) S.sel.delete(hit); else S.sel.add(hit); }
      else if (!S.sel.has(hit)) S.sel = new Set([hit]);
      renderInspector(); S.staticDirty = true;
      if (S.sel.has(hit)) drag = { kind: "move", anchor: hit, x0: x, orig: new Map([...S.sel].map(c => [c, c.t])), snap: snapshot(), moved: false };
    } else {
      drag = { kind: "box", x0: x, x1: x, y0: y, y1: y, add: e.shiftKey || e.ctrlKey || e.metaKey };
      if (e.pointerType !== "mouse"){                       // touch: press and hold an empty spot to add a cue
        endPress();
        pressTimer = setTimeout(() => {
          pressTimer = null;
          if (drag && drag.kind === "box" && Math.abs(drag.x1 - drag.x0) < 8 && Math.abs(drag.y1 - drag.y0) < 8){
            drag = null; addCueAt(li, snapT(tOf(x)));
            if (navigator.vibrate) navigator.vibrate(15);
          }
        }, 500);
      }
    }
  } else if (y < G.ruler) drag = { kind: "ruler", x0: x, x1: x };
  else { seek(tOf(x)); drag = { kind: "scrub" }; }
});
cv.addEventListener("pointermove", e => {
  if (!ptrs.has(e.pointerId)) return;
  ptrs.set(e.pointerId, canvasPos(e));
  if (pinch && ptrs.size >= 2){
    const [a, b] = [...ptrs.values()], mid = (a.x + b.x) / 2, d = Math.max(20, Math.abs(a.x - b.x));
    S.pps = clamp(pinch.pps0 * d / pinch.d0, minPps(), MAXPPS); S.view = pinch.t0 - mid / S.pps; clampView(); syncZoomUI();
    return;
  }
  if (!drag) return;
  const { x, y } = canvasPos(e);
  if (drag.kind === "move"){
    if (!drag.moved && Math.abs(x - drag.x0) < 3) return;
    if (!drag.moved){ S.undo.push(drag.snap); S.redo = []; S.edited = true; drag.moved = true; }
    const o = drag.orig.get(drag.anchor); let na = o + (x - drag.x0) / S.pps;
    if (!e.altKey) na = snapT(na);
    const delta = na - o;
    for (const [c, t0] of drag.orig) c.t = r3(clamp(t0 + delta, 0, S.dur));
    rebuild(); renderInspector();
  } else if (drag.kind === "box"){ drag.x1 = x; drag.y1 = y; }
  else if (drag.kind === "ruler") drag.x1 = x;
  else if (drag.kind === "scrub") seek(tOf(clamp(x, 0, W)));
});
function pointerEnd(e){
  if (!ptrs.has(e.pointerId)) return;
  ptrs.delete(e.pointerId); endPress();
  if (pinch){ if (ptrs.size < 2) pinch = null; drag = null; return; }
  if (!drag) return;
  const d = drag; drag = null;
  if (e.type === "pointercancel"){ if (d.kind === "move" && d.moved) scheduleSave(); return; }
  if (d.kind === "move" && d.moved) scheduleSave();
  else if (d.kind === "box"){
    if (Math.abs(d.x1 - d.x0) < 3 && Math.abs(d.y1 - d.y0) < 3){ if (!d.add) S.sel.clear(); seek(tOf(d.x0)); }
    else {
      const a = tOf(Math.min(d.x0, d.x1)), b = tOf(Math.max(d.x0, d.x1));
      const l0 = Math.max(0, laneAt(Math.max(laneTop(0), Math.min(d.y0, d.y1)))), l1raw = laneAt(Math.max(d.y0, d.y1)), l1 = l1raw < 0 ? S.tracks.length - 1 : l1raw;
      if (!d.add) S.sel.clear();
      for (let li = l0; li <= l1; li++) for (const c of S.tracks[li].cues) if (c.t >= a && c.t <= b) S.sel.add(c);
    }
    renderInspector(); S.staticDirty = true;
  } else if (d.kind === "ruler"){
    if (Math.abs(d.x1 - d.x0) < 4) seek(tOf(d.x0));
    else {
      S.A = r3(clamp(snapT(tOf(Math.min(d.x0, d.x1))), 0, S.dur)); S.B = r3(clamp(snapT(tOf(Math.max(d.x0, d.x1))), 0, S.dur));
      updateLoopUI(); scheduleSave(); if (audio.currentTime < S.A || audio.currentTime > S.B) seek(S.A);
    }
  }
}
cv.addEventListener("pointerup", pointerEnd);
cv.addEventListener("pointercancel", pointerEnd);
cv.addEventListener("dblclick", e => { const { x, y } = canvasPos(e), li = laneAt(y); if (li < 0) return; setActive(li); addCueAt(li, snapT(tOf(x))); });
cv.addEventListener("wheel", e => {
  if (!S.id) return; e.preventDefault();
  const { x } = canvasPos(e);
  if (e.ctrlKey || e.metaKey) setPps(S.pps * Math.exp(-e.deltaY * 0.0025), x);
  else { S.view += (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) / S.pps; clampView(); }
}, { passive: false });

// ---------------------------------------------------------------- cue editing
function addCueAt(li, t){
  const tr = S.tracks[li]; if (!tr) return;
  t = clamp(t, 0, S.dur);
  const st = tr._ev(t), dark = st[0] + st[1] + st[2] < 12;
  pushUndo();
  const c = { t: r3(t), color: dark ? (S.lastColor || trackColor(tr)) : rgbToHex(st[0], st[1], st[2]), brightness: dark ? 100 : clamp(Math.round(st[3]), 1, 100), fade: 0 };
  tr.cues.push(c); S.sel = new Set([c]); changed();
}
function deleteSel(){ if (!S.sel.size) return; pushUndo(); for (const tr of S.tracks) tr.cues = tr.cues.filter(c => !S.sel.has(c)); S.sel.clear(); changed(); }
function copySel(){
  if (!S.sel.size) return;
  const arr = [...S.sel].sort((a, b) => a.t - b.t), base = arr[0].t;
  S.clip = arr.map(c => ({ track: S.cueTrack.get(c).id, cue: { ...c, t: c.t - base } }));
  $("saveState").textContent = `Copied ${arr.length} cue${arr.length > 1 ? "s" : ""}`;
}
function paste(){
  if (!S.clip) return;
  pushUndo();
  const t0 = snapT(audio.currentTime), news = [], byId = new Map(S.tracks.map(t => [t.id, t])), single = new Set(S.clip.map(c => c.track)).size === 1;
  for (const { track, cue } of S.clip){
    const tr = single ? S.tracks[S.active] : (byId.get(track) || S.tracks[S.active]), c = { ...cue, t: r3(t0 + cue.t) };
    if (tr && c.t <= S.dur){ tr.cues.push(c); news.push(c); }
  }
  S.sel = new Set(news); changed();
}
function nudge(dt){ if (!S.sel.size) return; pushUndo(); for (const c of S.sel) c.t = r3(clamp(c.t + dt, 0, S.dur)); changed(); }
$("addCueBtn").onclick = () => addCueAt(S.active, snapT(audio.currentTime, 0.08));
$("delBtn").onclick = deleteSel; $("copyBtn").onclick = copySel; $("pasteBtn").onclick = paste;

const SWATCHES = ["#ffffff", "#ff0000", "#ff4000", "#ff8000", "#ffd000", "#a0ff00", "#00ff40", "#00ffb0", "#00e0ff", "#0080ff", "#0020ff", "#6000ff", "#b000ff", "#ff00c0", "#ff0060", "#000000"];
for (const col of SWATCHES){
  const s = document.createElement("span"); s.style.background = col; s.title = col === "#000000" ? "Off (black)" : col;
  s.onclick = () => { if (!S.sel.size) return; pushUndo(); for (const c of S.sel) c.color = col; if (col !== "#000000") S.lastColor = col; changed(); };
  $("swatches").appendChild(s);
}
function renderInspector(){
  const n = S.sel.size, first = [...S.sel].sort((a, b) => a.t - b.t)[0];
  const lanes = new Set([...S.sel].map(c => S.cueTrack.get(c)).filter(Boolean).map(t => t.name));
  $("insTitle").textContent = n ? `${n === 1 ? "1 cue" : n + " cues"} · ${[...lanes].join(", ")}` : "No cue selected";
  $("insFields").style.display = n ? "contents" : "none";
  $("delBtn").disabled = $("copyBtn").disabled = !n; $("pasteBtn").disabled = !S.clip;
  S.staticDirty = true;
  if (!n) return;
  const act = document.activeElement, set = (id, v) => { if ($(id) !== act) $(id).value = v; };
  $("iTime").disabled = n !== 1;
  set("iTime", n === 1 ? first.t.toFixed(3) : "");
  set("iColor", first.color); set("iBright", first.brightness); set("iBrightN", first.brightness); set("iFade", first.fade);
}
function editSel(fn){
  if (!S.sel.size) return;
  if (!S.editOpen){ pushUndo(); S.editOpen = true; }
  for (const c of S.sel) fn(c);
  rebuild(); scheduleSave();
}
for (const id of ["iTime", "iColor", "iBright", "iBrightN", "iFade"]){
  $(id).addEventListener("change", () => { S.editOpen = false; renderInspector(); });
  $(id).addEventListener("blur", () => { S.editOpen = false; });
}
$("iColor").addEventListener("input", e => { S.lastColor = e.target.value; editSel(c => c.color = e.target.value); });
$("iBright").addEventListener("input", e => { const v = +e.target.value; $("iBrightN").value = v; editSel(c => c.brightness = v); });
$("iBrightN").addEventListener("input", e => { const v = clamp(Math.round(+e.target.value || 1), 1, 100); $("iBright").value = v; editSel(c => c.brightness = v); });
$("iFade").addEventListener("input", e => { const v = Math.max(0, +e.target.value || 0); editSel(c => c.fade = r3(v)); });
$("iTime").addEventListener("change", e => { const v = clamp(+e.target.value || 0, 0, S.dur); editSel(c => c.t = r3(v)); S.editOpen = false; renderInspector(); });

// ---------------------------------------------------------------- generator dialog
function palOptions(sel){ return Object.entries(PALETTES).map(([k, p]) => `<option value="${k}"${k === sel ? " selected" : ""}>${p.name}</option>`).join(""); }
$("genBtn").onclick = () => {
  const src = S.feat && S.feat.source === "demucs" ? "Using instruments separated with AI." : "Using instruments estimated from frequency ranges" + (S.caps.demucs ? " (AI separation can improve this)." : ".");
  $("gSource").textContent = `~${Math.round(S.an.tempo)} BPM · ${src}`;
  $("gTracks").innerHTML = `<span class="muted">Lane</span><span class="muted">Effect</span><span class="muted">Colors</span>` + S.tracks.map(tr => {
    const styles = tr.feature ? STYLE_NAMES : ACCENT_STYLES;
    return `<label class="gname"><input type="checkbox" data-id="${esc(tr.id)}" checked> ${esc(tr.name)}</label>
      <select data-style="${esc(tr.id)}">${Object.entries(styles).map(([k, v]) => `<option value="${k}"${k === tr.style ? " selected" : ""}>${v}</option>`).join("")}</select>
      <select data-pal="${esc(tr.id)}">${palOptions(tr.palette)}</select>`;
  }).join("");
  const reg = $("gScope").querySelector('[value="region"]'); reg.disabled = !regionOk();
  $("gScope").value = regionOk() ? "region" : "all";
  $("genDlg").showModal();
};
$("genDlg").addEventListener("close", () => {
  if ($("genDlg").returnValue !== "ok" || !S.id) return;
  const ids = [...$("gTracks").querySelectorAll("input[data-id]")].filter(i => i.checked).map(i => i.dataset.id);
  if (!ids.length) return;
  pushUndo();
  for (const tr of S.tracks){
    const st = $("gTracks").querySelector(`[data-style="${CSS.escape(tr.id)}"]`), pl = $("gTracks").querySelector(`[data-pal="${CSS.escape(tr.id)}"]`);
    if (st) tr.style = st.value; if (pl) tr.palette = pl.value;
  }
  generateLanes(ids, { bpc: +$("gBpc").value }, $("gScope").value === "region" && regionOk());
  S.sel.clear(); changed(); renderHeads(); renderLayout();
});

// ---------------------------------------------------------------- keyboard
document.addEventListener("click", e => { const b = e.target.closest("button"); if (b) b.blur(); });
document.addEventListener("keydown", e => {
  if (!S.id || $("genDlg").open || $("devDlg").open) return;
  const el = e.target, typing = el.tagName === "INPUT" && ["text", "number", "search"].includes(el.type);
  const mod = e.ctrlKey || e.metaKey, k = e.key;
  if (typing && mod && /^[zy]$/i.test(k) && el.closest("#inspector")){ el.blur(); S.editOpen = false; }
  else if (typing || el.tagName === "SELECT"){ if (k === "Escape") el.blur(); return; }
  if (k === " "){ e.preventDefault(); togglePlay(); }
  else if (mod && k.toLowerCase() === "z"){ e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && k.toLowerCase() === "y"){ e.preventDefault(); redo(); }
  else if (mod && k.toLowerCase() === "a"){ e.preventDefault(); S.sel = new Set(S.tracks.flatMap(t => t.cues)); renderInspector(); }
  else if (mod && k.toLowerCase() === "c"){ e.preventDefault(); copySel(); renderInspector(); }
  else if (mod && k.toLowerCase() === "v"){ e.preventDefault(); paste(); }
  else if (!mod && k.toLowerCase() === "c"){ const t = audio.paused ? audio.currentTime : audio.currentTime - 0.05; addCueAt(S.active, snapT(t, Math.max(10 / S.pps, 0.08))); }
  else if (!mod && k.toLowerCase() === "m") toggleMute();
  else if (k === "Delete" || k === "Backspace"){ e.preventDefault(); deleteSel(); }
  else if (k === "Escape"){ S.sel.clear(); renderInspector(); }
  else if (!mod && k.toLowerCase() === "l") toggleLoop();
  else if (k === "[") setA();
  else if (k === "]") setB();
  else if (k === "ArrowUp" || k === "ArrowDown"){ e.preventDefault(); setActive(S.active + (k === "ArrowUp" ? -1 : 1)); S.staticDirty = true; }
  else if (k === "ArrowLeft" || k === "ArrowRight"){
    e.preventDefault(); const dir = k === "ArrowLeft" ? -1 : 1;
    if (e.altKey && S.sel.size) nudge(dir * (e.shiftKey ? 0.1 : 0.01)); else seek(audio.currentTime + dir * (e.shiftKey ? 5 : 1));
  }
  else if (k === "Home") seek(0);
  else if (k === "+" || k === "=") setPps(S.pps * 1.4, clamp(xOf(audio.currentTime), 0, W));
  else if (k === "-") setPps(S.pps / 1.4, clamp(xOf(audio.currentTime), 0, W));
});

// ---------------------------------------------------------------- phones: library drawer, skip buttons, app hooks
const narrow = () => window.matchMedia("(max-width: 820px)").matches;
function closeDrawer(){ document.body.classList.remove("libOpen"); }
$("libToggle").onclick = () => document.body.classList.toggle("libOpen");
$("scrim").onclick = closeDrawer;
$("back5").onclick = () => seek(audio.currentTime - 5);
$("fwd5").onclick = () => seek(audio.currentTime + 5);
for (const id of ["addSongBtn", "ytBtn"]) $(id).addEventListener("click", closeDrawer);
window.lsBack = () => {                                   // Android back button: true = handled
  for (const dl of ["genDlg", "devDlg"]) if ($(dl).open){ $(dl).close(); return true; }
  if (document.body.classList.contains("libOpen")){ closeDrawer(); return true; }
  if (S.sel.size){ S.sel.clear(); renderInspector(); return true; }
  return false;
};
window.lsFlush = () => { if (saveTimer) saveNow(); };
window.lsPause = () => { if (!audio.paused) audio.pause(); };
window.lsImportUrl = async url => { await capsReady; importYouTube(url); };

loadConfig();
loadLibrary().then(() => { if (narrow() && !S.id && $("libList").querySelector(".libItem")) document.body.classList.add("libOpen"); });
renderInspector();

</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
