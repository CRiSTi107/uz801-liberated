/* ==========================================================================
   audio.js - Audio bridge (beta) over WebSocket /ws/audio.

   Transport contract (consumed here; the API agent must match):
     - Binary frames  = raw PCM, signed 16-bit little-endian (S16_LE), mono.
                         Sample rate comes from a control message (default 8000).
     - Text frames    = JSON control messages:
         server -> client: {type:"start", rate:8000, format:"S16_LE", channels:1}
                           {type:"status", state:"...", ...}
                           {type:"rate", rate:16000}
                           {type:"stop"}
         client -> server: {type:"start"}   sent on connect to request audio
                           {type:"stop"}    sent on disconnect
   Downlink (far end) PCM -> Web Audio playback.
   Uplink: getUserMedia mic -> downsample to the negotiated rate -> binary frames.

   Everything degrades gracefully: if WS, AudioContext or getUserMedia are
   unavailable the panel reports the reason and stays usable.
   ========================================================================== */

"use strict";

(function () {
  const AC = window.AudioContext || window.webkitAudioContext;

  class AudioBridge {
    constructor(handlers) {
      this.h = handlers || {};
      this.ws = null;
      this.ctx = null;
      this.rate = 8000;        // negotiated PCM sample rate
      this.playTime = 0;       // scheduling cursor for gap-free playback
      this.micStream = null;
      this.micNode = null;
      this.micSource = null;
      this.micMuted = false;
      this.rxFrames = 0;
      this.txFrames = 0;
      this.connected = false;
    }

    get supported() { return !!(window.WebSocket && AC); }

    _status(state, msg) {
      if (this.h.onStatus) this.h.onStatus(state, msg, {
        rate: this.rate, rx: this.rxFrames, tx: this.txFrames,
      });
    }

    async connect() {
      if (!window.WebSocket) { this._status("error", "WebSocket not supported by this browser."); return; }
      if (!AC) { this._status("error", "Web Audio API not supported by this browser."); return; }

      // The audio bridge runs on its own port (default 8089) on the same host,
      // reported by GET /api/session as SESSION.audio.{port,path}. Cookies are
      // host-scoped (not port-scoped) so the session cookie is still sent.
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const acfg = (window.API && window.API.SESSION && window.API.SESSION.audio) || {};
      const port = acfg.port || 8089;
      const path = acfg.path || "/ws/audio";
      const url = proto + "//" + location.hostname + ":" + port + path;

      let ws;
      try { ws = new WebSocket(url); }
      catch (e) { this._status("error", "Could not open audio WebSocket."); return; }
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      this._status("connecting", "Opening audio channel...");

      ws.onopen = () => {
        this.connected = true;
        try { this.ctx = new AC(); } catch (e) { /* created lazily on first frame */ }
        this.playTime = this.ctx ? this.ctx.currentTime : 0;
        // Request audio start from the server.
        this._send({ type: "start" });
        this._status("connected", "Connected. Waiting for audio...");
        this._startMic().catch((err) => {
          this._status("connected", "Playback only - mic unavailable: " + err.message);
        });
      };

      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          this._onControl(ev.data);
        } else {
          this._onPcm(ev.data);
        }
      };

      ws.onerror = () => {
        this._status("error", "Audio WebSocket error (is the bridge enabled on the API?).");
      };
      ws.onclose = () => {
        this.connected = false;
        this._teardownMic();
        this._status("disconnected", "Audio channel closed.");
      };
    }

    _onControl(text) {
      let msg;
      try { msg = JSON.parse(text); } catch (e) { return; }
      if ((msg.type === "start" || msg.type === "rate" || msg.type === "status") && msg.rate) {
        this.rate = Number(msg.rate) || this.rate;
      }
      if (msg.type === "status" && msg.message) {
        this._status(this.connected ? "connected" : "disconnected", msg.message);
      } else {
        this._status("connected", "Streaming at " + this.rate + " Hz.");
      }
    }

    _onPcm(arrayBuf) {
      if (!this.ctx) { try { this.ctx = new AC(); this.playTime = this.ctx.currentTime; } catch (e) { return; } }
      const pcm = new Int16Array(arrayBuf);
      if (!pcm.length) return;
      this.rxFrames += 1;

      const f32 = new Float32Array(pcm.length);
      let peak = 0;
      for (let i = 0; i < pcm.length; i++) {
        const v = pcm[i] / 32768;
        f32[i] = v;
        const a = Math.abs(v);
        if (a > peak) peak = a;
      }
      if (this.h.onRxLevel) this.h.onRxLevel(peak);

      // Build a buffer at the PCM rate; the context resamples on playback.
      const buf = this.ctx.createBuffer(1, f32.length, this.rate);
      buf.getChannelData(0).set(f32);
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.ctx.destination);

      const now = this.ctx.currentTime;
      if (this.playTime < now) this.playTime = now + 0.02; // small jitter cushion
      src.start(this.playTime);
      this.playTime += buf.duration;

      if ((this.rxFrames & 7) === 0) this._status("connected", "Streaming at " + this.rate + " Hz.");
    }

    async _startMic() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("getUserMedia unavailable");
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        video: false,
      });
      this.micStream = stream;
      if (!this.ctx) this.ctx = new AC();
      const source = this.ctx.createMediaStreamSource(stream);
      this.micSource = source;

      // ScriptProcessor is deprecated but universally available and needs no
      // AudioWorklet module file (which a no-build, offline SPA cannot load).
      const buf = 2048;
      const node = this.ctx.createScriptProcessor(buf, 1, 1);
      this.micNode = node;
      node.onaudioprocess = (e) => {
        if (!this.connected || this.micMuted) return;
        const input = e.inputBuffer.getChannelData(0);
        const down = this._downsample(input, this.ctx.sampleRate, this.rate);
        if (!down) return;
        const pcm = this._floatToS16(down);
        let peak = 0;
        for (let i = 0; i < input.length; i++) { const a = Math.abs(input[i]); if (a > peak) peak = a; }
        if (this.h.onTxLevel) this.h.onTxLevel(this.micMuted ? 0 : peak);
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(pcm.buffer);
          this.txFrames += 1;
        }
      };
      source.connect(node);
      // Connect to a muted gain so the processor runs without local echo.
      const sink = this.ctx.createGain();
      sink.gain.value = 0;
      node.connect(sink);
      sink.connect(this.ctx.destination);
      this._status("connected", "Mic live (virtual mic active).");
    }

    _downsample(input, inRate, outRate) {
      if (outRate >= inRate) return Float32Array.from(input); // no upsampling
      const ratio = inRate / outRate;
      const outLen = Math.floor(input.length / ratio);
      const out = new Float32Array(outLen);
      let pos = 0;
      for (let i = 0; i < outLen; i++) {
        const next = Math.floor((i + 1) * ratio);
        let sum = 0, count = 0;
        for (let j = Math.floor(i * ratio); j < next && j < input.length; j++) { sum += input[j]; count++; }
        out[i] = count ? sum / count : input[Math.floor(i * ratio)] || 0;
        pos = next;
      }
      return out;
    }

    _floatToS16(f32) {
      const out = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        let s = Math.max(-1, Math.min(1, f32[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return out;
    }

    setMicMuted(muted) { this.micMuted = !!muted; }

    _send(obj) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try { this.ws.send(JSON.stringify(obj)); } catch (e) { /* ignore */ }
      }
    }

    _teardownMic() {
      if (this.micNode) { try { this.micNode.disconnect(); this.micNode.onaudioprocess = null; } catch (e) {} this.micNode = null; }
      if (this.micSource) { try { this.micSource.disconnect(); } catch (e) {} this.micSource = null; }
      if (this.micStream) { this.micStream.getTracks().forEach((t) => t.stop()); this.micStream = null; }
      if (this.h.onTxLevel) this.h.onTxLevel(0);
    }

    disconnect() {
      this._send({ type: "stop" });
      this._teardownMic();
      if (this.ws) { try { this.ws.close(); } catch (e) {} this.ws = null; }
      if (this.ctx) { try { this.ctx.close(); } catch (e) {} this.ctx = null; }
      this.connected = false;
      if (this.h.onRxLevel) this.h.onRxLevel(0);
    }
  }

  window.AudioBridge = AudioBridge;
})();
