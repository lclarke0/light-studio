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
