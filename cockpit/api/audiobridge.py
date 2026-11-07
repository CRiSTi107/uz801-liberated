"""
audiobridge.py - "virtual mic/speaker over IP" for the UZ801 voice path.

The board has NO physical codec/mic/speaker, but the MSM8916 DSP exposes the
voice PCM in software via tinyalsa (ALSA card ``msm8x16sndcardm``). This module
bridges that PCM to/from a browser over a WebSocket:

  * DOWNLINK (capture): ``tinycap`` reads the far-end voice PCM from the DSP and
    we stream it to the browser as binary WS frames  (browser plays it).
  * UPLINK   (playback): the browser captures its mic and sends binary WS frames
    back; we feed them to ``tinyplay`` which injects them as the near-end voice
    (the "virtual microphone").

Everything hardware-specific is a CONFIG CONSTANT at the top of this file. The
exact PCM device indices and the tinymix routing are UNKNOWN until a hardware
spike (see README "Hardware spike"); the values below are sensible PLACEHOLDERS
based on /proc/asound inspection and MUST be verified/filled on the device.

The bridge is FEATURE-FLAGGED: if tinyalsa or the devices are absent it degrades
gracefully (the WS endpoint reports unavailable and the REST layer advertises
``audio: false``). It can run in-process on its own port, or as a separate small
process (``python3 audiobridge.py --config config.json``).

Pure stdlib (uses wsframe.py); no third-party packages.
"""

import json
import os
import shutil
import socket
import subprocess
import threading

from logutil import get
import wsframe

log = get("cockpit.audio")

# ===========================================================================
# CONFIG CONSTANTS - FILL THESE IN DURING THE HARDWARE SPIKE
# ===========================================================================
# Candidate PCM endpoints seen in /proc/asound/pcm on this device:
#   AFE-PROXY RX  = card 0, device 7  (playback  -> inject to DSP / "virtual mic")
#   AFE-PROXY TX  = card 0, device 8  (capture    <- from DSP)
#   Voice Downlink Capture  = card 0, device 35  (what the far end says)
#   Voice Farend Playback   = card 0, device 36  (inject audio to the far end)
#   Voice Uplink Capture    = card 0, device 34
#
# Start with AFE-PROXY (generic software audio in/out). If the live voice call
# audio is not routed to the proxy, switch to the Voice Downlink/Farend devices
# and add the matching tinymix routes below.
CARD = 0                       # ALSA card index (msm8x16sndcardm)

# Capture = DOWNLINK (far end -> browser speaker)
CAP_DEVICE = 8                 # AFE-PROXY TX (capture). Candidate; VERIFY.
CAP_RATE = 8000                # 8000 for narrowband CS voice; 16000 for WB/VoLTE
CAP_CHANNELS = 1
CAP_BITS = 16                  # S16_LE

# Playback = UPLINK (browser mic -> far end / "virtual microphone")
PLAY_DEVICE = 7                # AFE-PROXY RX (playback). Candidate; VERIFY.
PLAY_RATE = 8000
PLAY_CHANNELS = 1
PLAY_BITS = 16                 # S16_LE

# tinymix routing commands to connect the live voice session to the proxy /
# farend ports. These are PLACEHOLDERS - the exact control names/values come
# from `tinymix` (595 controls on this device) during the spike. Each entry is
# a full argv list passed to subprocess. Leave empty to skip auto-routing.
TINYMIX_ROUTES_ON = [
    # Examples (names illustrative - confirm with `tinymix` on the device):
    # ["tinymix", "set", "PROXY_RX Audio Mixer MultiMedia1", "1"],
    # ["tinymix", "set", "Voice Rx Device Mute", "0"],
    # ["tinymix", "set", "Voice Tx Device Mute", "0"],
]
TINYMIX_ROUTES_OFF = [
    # Reverse of the above, applied on teardown.
]

# Tool names (overridable if they live at non-PATH locations on the device).
TINYCAP = "tinycap"
TINYPLAY = "tinyplay"
TINYMIX = "tinymix"
TINYPCMINFO = "tinypcminfo"

# How many bytes to read per WS frame from the capture stream.
FRAME_BYTES = 640              # 20 ms @ 8kHz/16-bit mono = 320 samples * 2 B


# ===========================================================================
# Availability check (feature flag)
# ===========================================================================
def tinyalsa_available():
    """True if the tinyalsa CLIs are on PATH (quick capability probe)."""
    return all(shutil.which(tool) for tool in (TINYCAP, TINYPLAY))


def probe():
    """Return a dict describing audio-bridge availability for /api/system."""
    return {
        "tinycap": shutil.which(TINYCAP) is not None,
        "tinyplay": shutil.which(TINYPLAY) is not None,
        "tinymix": shutil.which(TINYMIX) is not None,
        "available": tinyalsa_available(),
        "card": CARD,
        "capture_device": CAP_DEVICE,
        "playback_device": PLAY_DEVICE,
    }


# ===========================================================================
# tinymix routing
# ===========================================================================
def apply_routes(routes):
    for argv in routes:
        try:
            subprocess.run(argv, check=False, capture_output=True, timeout=5)
            log.event(10, "audio.tinymix", cmd=" ".join(argv))
        except (OSError, subprocess.SubprocessError) as exc:
            log.event(30, "audio.tinymix_failed", cmd=" ".join(argv), err=str(exc))


# ===========================================================================
# PCM streaming over one WebSocket connection
# ===========================================================================
class AudioSession:
    """Owns a tinycap (downlink) and tinyplay (uplink) subprocess pair for one
    connected browser, pumping PCM between them and the WebSocket."""

    def __init__(self, ws, rate=CAP_RATE):
        self.ws = ws
        self.rate = rate
        self._cap = None
        self._play = None
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        apply_routes(TINYMIX_ROUTES_ON)
        # Tell the browser the stream parameters up front (contract: the server
        # sends {type:"start",...} on connect). PCM is S16_LE mono at self.rate.
        try:
            self.ws.send(json.dumps({
                "type": "start", "rate": self.rate,
                "format": "S16_LE", "channels": CAP_CHANNELS,
            }))
        except wsframe.WebSocketError:
            self._stop.set()
            return
        # tinycap '-' writes raw PCM to stdout on builds that support it; many
        # Android tinycap builds only write to a file. We stream stdout here and
        # document the fallback (named pipe) in the README.
        cap_cmd = [
            TINYCAP, "-", "-D", str(CARD), "-d", str(CAP_DEVICE),
            "-c", str(CAP_CHANNELS), "-r", str(self.rate), "-b", str(CAP_BITS),
        ]
        play_cmd = [
            TINYPLAY, "-", "-D", str(CARD), "-d", str(PLAY_DEVICE),
            "-c", str(PLAY_CHANNELS), "-r", str(self.rate),
            "-b", str(PLAY_BITS),
        ]
        self._cap = subprocess.Popen(cap_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self._play = subprocess.Popen(play_cmd, stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        log.event(20, "audio.session.start")

        t_down = threading.Thread(target=self._pump_downlink, daemon=True)
        t_up = threading.Thread(target=self._pump_uplink, daemon=True)
        self._threads = [t_down, t_up]
        t_down.start()
        t_up.start()

    def _pump_downlink(self):
        """tinycap stdout -> browser (binary WS frames)."""
        try:
            while not self._stop.is_set():
                data = self._cap.stdout.read(FRAME_BYTES)
                if not data:
                    break
                self.ws.send_binary(data)
        except (wsframe.WebSocketError, OSError, ValueError):
            pass
        finally:
            self._stop.set()

    def _pump_uplink(self):
        """browser mic (binary WS frames) -> tinyplay stdin.

        Text frames are JSON control messages (e.g. {"type":"stop"}) and must
        NOT be written to the PCM sink; only binary frames are raw PCM.
        """
        try:
            while not self._stop.is_set():
                opcode, data = self.ws.recv()
                if opcode is None:
                    break
                if opcode == wsframe.OP_TEXT:
                    self._on_control(data)
                    continue
                if data and self._play and self._play.stdin:
                    self._play.stdin.write(data)
                    self._play.stdin.flush()
        except (wsframe.WebSocketError, OSError, ValueError):
            pass
        finally:
            self._stop.set()

    def _on_control(self, data):
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            return
        if msg.get("type") == "stop":
            self._stop.set()
        # {"type":"start"} from the client is just a request to begin; the
        # stream is already running, so nothing more to do here.

    def wait(self):
        for t in self._threads:
            t.join()

    def stop(self):
        self._stop.set()
        for proc in (self._cap, self._play):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        apply_routes(TINYMIX_ROUTES_OFF)
        log.event(20, "audio.session.stop")


# ===========================================================================
# Standalone WebSocket server for /ws/audio
# ===========================================================================
class AudioBridgeServer:
    """A tiny threaded WS server. Accepts one upgrade per connection at the
    ``/ws/audio`` path and runs an AudioSession."""

    def __init__(self, host="0.0.0.0", port=8089, tls_context=None,
                 auth_check=None, rate=CAP_RATE):
        self.host = host
        self.port = port
        self.rate = rate
        self.tls_context = tls_context
        # auth_check(cookie_header) -> bool ; injected by the server so the audio
        # port enforces the same login as the REST API.
        self.auth_check = auth_check
        self._sock = None
        self._stop = threading.Event()

    def serve_forever(self):
        if not tinyalsa_available():
            log.event(30, "audio.disabled_no_tinyalsa")
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(4)
        log.event(20, "audio.listen", host=self.host, port=self.port,
                  tls=bool(self.tls_context))
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr),
                             daemon=True).start()

    def _handle(self, conn, addr):
        try:
            if self.tls_context:
                conn = self.tls_context.wrap_socket(conn, server_side=True)
            head = self._read_head(conn)
            if "GET /ws/audio" not in head.split("\r\n", 1)[0]:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                conn.close()
                return
            if self.auth_check and not self.auth_check(head):
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                conn.close()
                return
            wsframe.handshake(conn, head)
            ws = wsframe.WebSocket(conn)
            session = AudioSession(ws, rate=self.rate)
            session.start()
            try:
                session.wait()
            finally:
                session.stop()
                ws.close()
        except (wsframe.WebSocketError, OSError) as exc:
            log.event(30, "audio.conn_error", peer=str(addr), err=str(exc))
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_head(conn):
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
        return buf.decode("latin-1", "replace")

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="cockpit-api audio bridge")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = {}
    if os.path.isfile(args.config):
        with open(args.config) as fh:
            cfg = json.load(fh).get("audio", {})

    host = args.host or cfg.get("host", "0.0.0.0")
    port = args.port or cfg.get("port", 8089)
    from logutil import setup
    setup(level="INFO")
    server = AudioBridgeServer(host=host, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
