/* ==========================================================================
   app.js - UI wiring, auth gate, navigation, polling and all REST actions.
   Depends on window.API (api.js) and window.AudioBridge (audio.js).
   ========================================================================== */

"use strict";

(function () {
  const {
    api, SESSION, ApiError, setUnauthorizedHandler,
    loadSession, setupAdmin, login, logout,
    rsrpToBars, rsrpToPercent, csqToDbm,
    humanBytes, humanUptime, mmss, formatDate, smsSegments,
  } = window.API;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  /* ----------------------------------------------------------------------
     App state
     ---------------------------------------------------------------------- */
  const state = {
    paused: false,
    intervalMs: 3000,
    timer: null,
    offline: false,
    activeView: "overview",
    smsBox: "inbox",
    smsAuto: true,
    knownSmsIds: new Set(),
    callTimer: null,
    callStart: null,
  };

  const savedInterval = Number(localStorage.getItem("cockpit.interval"));
  if (Number.isFinite(savedInterval) && savedInterval >= 1) {
    state.intervalMs = savedInterval * 1000;
    $("#set-interval").value = savedInterval;
  }

  /* ----------------------------------------------------------------------
     Toast
     ---------------------------------------------------------------------- */
  function toast(title, body, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.innerHTML = '<div class="t-title"></div>' + (body ? '<div class="t-body"></div>' : "");
    el.querySelector(".t-title").textContent = title;
    if (body) el.querySelector(".t-body").textContent = body;
    $("#toast-wrap").appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  /** Wrap a write call: toast on error, surface offline, optional success toast. */
  async function act(promise, successMsg) {
    try {
      const out = await promise;
      setOffline(false);
      if (successMsg) toast(successMsg, "", "ok");
      return out;
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
      else if (!(err instanceof ApiError && err.needLogin)) toast("Error", err.message, "bad");
      throw err;
    }
  }

  /* ----------------------------------------------------------------------
     Confirm dialog
     ---------------------------------------------------------------------- */
  const confirmModal = $("#confirm-modal");
  function confirmAction(title, text) {
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    confirmModal.showModal();
    return new Promise((resolve) => {
      const done = (val) => {
        confirmModal.close();
        $("#confirm-ok").onclick = null;
        $("#confirm-cancel").onclick = null;
        resolve(val);
      };
      $("#confirm-ok").onclick = () => done(true);
      $("#confirm-cancel").onclick = () => done(false);
    });
  }

  /* ----------------------------------------------------------------------
     Offline state
     ---------------------------------------------------------------------- */
  function setOffline(off) {
    state.offline = off;
    $("#offline").classList.toggle("show", off);
    const dot = $("#conn-dot");
    dot.classList.toggle("offline", off);
    if (off) dot.classList.remove("online");
  }

  function setText(id, val) {
    const el = $(id);
    if (el) el.textContent = (val === undefined || val === null || val === "") ? "--" : val;
  }

  /* ======================================================================
     Auth gate
     ====================================================================== */
  function showScreen(which) {
    $("#login").classList.toggle("show", which === "login");
    $("#setup").classList.toggle("show", which === "setup");
    $("#app").hidden = which !== "app";
    if (which === "login") setTimeout(() => $("#login-user").focus(), 30);
    if (which === "setup") setTimeout(() => $("#setup-pass").focus(), 30);
  }

  setUnauthorizedHandler(() => {
    stopPolling();
    showScreen("login");
  });

  async function boot() {
    try {
      await loadSession();
      setOffline(false);
    } catch (err) {
      if (err instanceof ApiError && err.offline) {
        setOffline(true);
        // Show login shell anyway; retry session shortly.
        showScreen("login");
        $("#login-err").textContent = "API unreachable - retrying...";
        setTimeout(boot, 3000);
        return;
      }
    }
    if (SESSION.firstRun) { showScreen("setup"); return; }
    if (!SESSION.authenticated) { showScreen("login"); return; }
    enterApp();
  }

  function enterApp() {
    showScreen("app");
    setText("#auth-state", "Signed in" + (SESSION.user ? " (" + SESSION.user + ")" : ""));
    setText("#about-user", SESSION.user);
    startPolling();
    refreshActiveView(true);
  }

  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#setup-err").textContent = "";
    const u = $("#setup-user").value.trim() || "admin";
    const p = $("#setup-pass").value;
    const p2 = $("#setup-pass2").value;
    if (p.length < 8) { $("#setup-err").textContent = "Password must be at least 8 characters."; return; }
    if (p !== p2) { $("#setup-err").textContent = "Passwords do not match."; return; }
    try {
      await setupAdmin(u, p);
      toast("Account created", "", "ok");
      enterApp();
    } catch (err) {
      $("#setup-err").textContent = err.message;
    }
  });

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#login-err").textContent = "";
    const btn = $("#login-submit");
    btn.disabled = true;
    try {
      await login($("#login-user").value.trim(), $("#login-pass").value);
      $("#login-pass").value = "";
      setOffline(false);
      toast("Signed in", "", "ok");
      enterApp();
    } catch (err) {
      $("#login-err").textContent = (err instanceof ApiError && err.offline)
        ? "API unreachable." : (err.message || "Login failed.");
    } finally { btn.disabled = false; }
  });

  $("#logout-btn").addEventListener("click", async () => {
    await logout();
    stopPolling();
    showScreen("login");
  });

  /* ======================================================================
     Navigation
     ====================================================================== */
  $("#nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (!btn) return;
    $$("#nav button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".view").forEach((v) => v.classList.remove("active"));
    const view = btn.dataset.view;
    $("#view-" + view).classList.add("active");
    state.activeView = view;
    refreshActiveView(true);
  });

  /* ======================================================================
     Polling - OVERVIEW status + SMS auto-refresh + call state
     ====================================================================== */
  function renderSignal(rsrp) {
    const bars = rsrpToBars(rsrp);
    const el = $("#o-signal");
    el.querySelectorAll(".bar").forEach((b, i) => b.classList.toggle("on", i < bars));
    el.classList.remove("weak", "none");
    if (bars === 0) el.classList.add("none"); else if (bars <= 2) el.classList.add("weak");
    const meter = $("#o-meter");
    meter.classList.remove("weak", "none");
    if (bars === 0) meter.classList.add("none"); else if (bars <= 2) meter.classList.add("weak");
    meter.firstElementChild.style.width = rsrpToPercent(rsrp) + "%";
  }

  function renderRegBadge(reg) {
    const badge = $("#o-reg-badge");
    const registered = reg && (reg.registered === true ||
      /^(registered|home|roaming)/i.test(String(reg.state || "")));
    badge.className = "badge " + (registered ? "ok" : "warn");
    badge.textContent = registered ? "registered" : (reg && reg.state ? reg.state : "not registered");
  }

  function renderBandHint(radio) {
    const hint = $("#band-hint");
    if (!radio) { hint.hidden = true; return; }
    // The API signals an unusable band (e.g. the known Band 20 antenna mismatch)
    // with band_usable:false (and optionally a message).
    if (radio.band_usable === false) {
      hint.hidden = false;
      hint.textContent = radio.band_hint ||
        ("Current band (" + (radio.band || "?") + ") is reported as unusable on this hardware - " +
         "signal may be weak or the link may drop. Consider locking to a usable band or improving the antenna.");
    } else {
      hint.hidden = true;
    }
  }

  function renderLink(link) {
    const badge = $("#link-badge");
    const txt = $("#link-txt");
    if (!link || link.ping_ms === undefined || link.ping_ms === null) {
      badge.className = "badge live warn";
      txt.textContent = "link unknown";
      setText("#o-ping", "--");
      setText("#o-ping-target", "ping target --");
      return;
    }
    const ms = Number(link.ping_ms);
    const up = ms >= 0 && (link.up === undefined ? true : !!link.up);
    badge.className = "badge live " + (up ? (ms < 120 ? "ok" : "warn") : "bad");
    txt.textContent = up ? ("link " + Math.round(ms) + "ms") : "link down";
    setText("#o-ping", up ? Math.round(ms) : "--");
    setText("#o-ping-target", "ping target " + (link.target || "--"));
  }

  function applyStatus(s) {
    const sig = s.signal || {};
    setText("#o-rsrp", sig.rsrp);
    renderSignal(sig.rsrp);
    setText("#o-rsrq", sig.rsrq !== undefined ? sig.rsrq + " dB" : "--");
    setText("#o-sinr", sig.sinr !== undefined ? sig.sinr + " dB" : "--");
    const dbm = csqToDbm(sig.csq);
    setText("#o-csq", sig.csq !== undefined ? (sig.csq + (dbm !== null ? " (~" + dbm + " dBm)" : "")) : "--");

    const radio = s.radio || {};
    setText("#o-band", radio.band);
    setText("#o-earfcn", radio.earfcn);
    setText("#o-pci", radio.pci);
    setText("#o-cell", radio.cell_id);
    renderBandHint(radio);

    const reg = s.registration || {};
    setText("#o-reg", reg.state);
    setText("#o-operator", reg.operator);
    setText("#o-rat", reg.rat);
    renderRegBadge(reg);

    const data = s.data || {};
    const connected = !!data.connected;
    const db = $("#o-data-badge");
    db.className = "badge " + (connected ? "ok" : "warn");
    db.textContent = connected ? "connected" : "disconnected";
    setText("#o-ip", "IP " + (data.ip || "--"));
    setText("#o-down", humanBytes(data.down_bytes));
    setText("#o-up", humanBytes(data.up_bytes));

    setText("#o-uptime", humanUptime(s.uptime_s));
    renderLink(s.link);

    // Network tab mirror
    setText("#net-state", reg.state);
    setText("#net-oper", reg.operator);
    setText("#net-rat", reg.rat);
    setText("#net-band", radio.band);
    setText("#net-earfcn", radio.earfcn);

    const dot = $("#conn-dot");
    dot.classList.toggle("online", connected || (reg.registered === true));
    dot.classList.remove("offline");
  }

  async function pollStatus() {
    try {
      const s = await api.get("/status");
      setOffline(false);
      applyStatus(s);
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(tick, state.intervalMs);
  }
  function stopPolling() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

  async function tick() {
    if (!SESSION.authenticated || state.paused) return;
    pollStatus();
    if (state.activeView === "sms" && state.smsAuto) loadSms();
    if (state.activeView === "calls") pollCall();
  }

  $("#pause-toggle").addEventListener("change", (e) => {
    state.paused = e.target.checked;
    toast(state.paused ? "Auto-refresh paused" : "Auto-refresh resumed", "", "");
  });
  $("#refresh-now").addEventListener("click", pollStatus);

  /** When switching tabs, kick the relevant initial load. */
  function refreshActiveView(initial) {
    switch (state.activeView) {
      case "overview": pollStatus(); break;
      case "sms": loadSms(); break;
      case "calls": pollCall(); break;
      case "sim": loadIdentity(); break;
      case "network": pollStatus(); readBand(); if (initial) readApn(); break;
      case "wifi": readWifi(); loadWifiClients(); break;
      case "system": loadSystem(); break;
      case "settings": loadTokens(); break;
    }
  }

  /* ======================================================================
     SMS
     ====================================================================== */
  $("#sms-refresh").addEventListener("click", () => loadSms(true));
  $("#sms-auto").addEventListener("change", (e) => { state.smsAuto = e.target.checked; });

  $$("#view-sms .seg-tabs button").forEach((b) => {
    b.addEventListener("click", () => {
      $$("#view-sms .seg-tabs button").forEach((x) => {
        x.classList.remove("active"); x.setAttribute("aria-selected", "false");
      });
      b.classList.add("active"); b.setAttribute("aria-selected", "true");
      state.smsBox = b.dataset.box;
      loadSms(true);
    });
  });

  async function loadSms(force) {
    const box = $("#sms-list");
    if (force) box.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';
    try {
      const res = await api.get("/sms?box=" + encodeURIComponent(state.smsBox));
      setOffline(false);
      const msgs = (res && res.messages) || [];
      renderSms(msgs);
      // Unread badge from inbox only.
      if (state.smsBox === "inbox") updateSmsBadge(msgs);
      else refreshInboxBadge();
    } catch (err) {
      if (err instanceof ApiError && err.offline) { setOffline(true); box.innerHTML = emptyHtml("&#9888;", "Offline - cannot load messages."); }
      else box.innerHTML = emptyHtml("&#9888;", "Could not load messages.");
    }
  }

  async function refreshInboxBadge() {
    try { const r = await api.get("/sms?box=inbox"); updateSmsBadge((r && r.messages) || []); }
    catch (e) { /* ignore */ }
  }

  function updateSmsBadge(inboxMsgs) {
    const unread = inboxMsgs.filter((m) => m.unread).length;
    const badge = $("#sms-badge");
    badge.textContent = unread;
    badge.classList.toggle("show", unread > 0);
    // Toast once for genuinely new unread messages.
    inboxMsgs.forEach((m) => {
      if (m.unread && !state.knownSmsIds.has(String(m.id))) {
        toast("New SMS", (m.address || "") + ": " + String(m.text || "").slice(0, 60), "ok");
      }
    });
    state.knownSmsIds = new Set(inboxMsgs.map((m) => String(m.id)));
  }

  function emptyHtml(icon, text) {
    return '<div class="empty"><span class="big">' + icon + '</span>' + text + '</div>';
  }

  function renderSms(msgs) {
    const box = $("#sms-list");
    box.innerHTML = "";
    if (!msgs.length) {
      box.innerHTML = emptyHtml("&#9993;", state.smsBox === "inbox" ? "Inbox is empty." : "No sent messages.");
      return;
    }
    msgs.forEach((m) => {
      const el = document.createElement("div");
      el.className = "msg" + (m.unread ? " unread" : "");
      const head = document.createElement("div"); head.className = "msg-head";
      const addr = document.createElement("span"); addr.className = "addr";
      addr.textContent = m.address || "(unknown)";
      const date = document.createElement("span"); date.className = "date";
      date.textContent = formatDate(m.ts || m.date);
      head.appendChild(addr); head.appendChild(date);
      const body = document.createElement("div"); body.className = "body";
      body.textContent = m.text || "";
      const foot = document.createElement("div"); foot.className = "msg-foot";
      const del = document.createElement("button");
      del.className = "btn ghost small"; del.textContent = "Delete";
      del.addEventListener("click", () => deleteSms(m.id));
      foot.appendChild(del);
      el.appendChild(head); el.appendChild(body); el.appendChild(foot);
      box.appendChild(el);
    });
  }

  async function deleteSms(id) {
    const ok = await confirmAction("Delete message?", "This permanently removes the message from the device.");
    if (!ok) return;
    try {
      await act(api.del("/sms/" + encodeURIComponent(id)), "Message deleted");
      loadSms(true);
    } catch (e) { /* toast shown */ }
  }

  function updateSegCounter() {
    const info = smsSegments($("#sms-text").value);
    setText("#sms-chars", info.chars + " chars");
    const segEl = $("#sms-segs");
    segEl.textContent = info.segments + " segment" + (info.segments === 1 ? "" : "s") + " · " + info.encoding;
    segEl.classList.toggle("over", info.segments > 1);
  }
  $("#sms-text").addEventListener("input", updateSegCounter);

  $("#sms-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const to = $("#sms-to").value.trim();
    const text = $("#sms-text").value;
    if (!to || !text) { toast("Missing field", "Enter a number and message.", "warn"); return; }
    const btn = $("#sms-send"); btn.disabled = true;
    try {
      await act(api.post("/sms", { to, text }), "Message sent");
      $("#sms-text").value = ""; updateSegCounter();
      state.smsBox = "sent";
      $$("#view-sms .seg-tabs button").forEach((x) => {
        const on = x.dataset.box === "sent";
        x.classList.toggle("active", on); x.setAttribute("aria-selected", String(on));
      });
      loadSms(true);
    } catch (e2) { /* toast shown */ } finally { btn.disabled = false; }
  });

  /* ======================================================================
     Calls
     ====================================================================== */
  const dialInput = $("#dial-input");
  $("#dialpad").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b) return;
    dialInput.value += b.dataset.k;
  });
  $("#dial-back").addEventListener("click", () => { dialInput.value = dialInput.value.slice(0, -1); });

  $("#call-dial").addEventListener("click", async () => {
    const number = dialInput.value.trim();
    if (!number) { toast("No number", "Enter a number to dial.", "warn"); return; }
    try { await act(api.post("/call", { number }), "Dialing " + number); pollCall(); }
    catch (e) { /* toast shown */ }
  });
  $("#call-answer").addEventListener("click", async () => {
    try { await act(api.post("/call/answer", {}), "Answered"); pollCall(); }
    catch (e) { /* toast shown */ }
  });
  $("#call-hangup").addEventListener("click", async () => {
    try { await act(api.post("/call/hangup", {}), "Call ended"); pollCall(); }
    catch (e) { /* toast shown */ }
  });

  function renderCall(res) {
    const calls = (res && res.calls) || [];
    const active = calls[0] || null;
    const numEl = $("#call-num"), stEl = $("#call-st"), timerEl = $("#call-timer");
    const answerBtn = $("#call-answer"), hangupBtn = $("#call-hangup");

    if (!active) {
      numEl.textContent = "No active call";
      stEl.textContent = "Idle";
      timerEl.hidden = true;
      answerBtn.disabled = true; hangupBtn.disabled = true;
      stopCallTimer();
      return;
    }
    numEl.textContent = active.number || "(unknown)";
    const st = String(active.state || "").toLowerCase();
    stEl.textContent = (active.direction ? active.direction + " · " : "") + (active.state || "active");
    const incoming = st === "incoming" || st === "waiting";
    answerBtn.disabled = !incoming;
    hangupBtn.disabled = false;
    if (st === "active") {
      timerEl.hidden = false;
      startCallTimer(active.duration);
    } else {
      timerEl.hidden = true;
      stopCallTimer();
    }
  }

  function startCallTimer(initialDur) {
    if (state.callTimer) return;
    state.callStart = Date.now() - (Number(initialDur) || 0) * 1000;
    const upd = () => { $("#call-timer").textContent = mmss((Date.now() - state.callStart) / 1000); };
    upd();
    state.callTimer = setInterval(upd, 1000);
  }
  function stopCallTimer() {
    if (state.callTimer) { clearInterval(state.callTimer); state.callTimer = null; }
  }

  async function pollCall() {
    try {
      const res = await api.get("/call");
      setOffline(false);
      renderCall(res);
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  /* --- Audio bridge (beta) --- */
  let bridge = null;
  function audioStatus(stateName, msg, stats) {
    const badge = $("#audio-badge");
    const map = { connected: "ok", connecting: "warn", disconnected: "", error: "bad" };
    badge.className = "badge " + (map[stateName] || "");
    badge.textContent = stateName;
    if (msg) setText("#audio-msg", msg);
    if (stats) {
      setText("#audio-rate", (stats.rate || "--") + " Hz");
      setText("#a-rx", stats.rx || 0);
      setText("#a-tx", stats.tx || 0);
    }
    const connected = stateName === "connected" || stateName === "connecting";
    $("#audio-connect").disabled = connected;
    $("#audio-disconnect").disabled = !connected;
  }
  $("#audio-connect").addEventListener("click", () => {
    if (!window.AudioBridge) { audioStatus("error", "Audio module not loaded."); return; }
    bridge = new window.AudioBridge({
      onStatus: audioStatus,
      onRxLevel: (p) => { $("#vu-rx").style.width = Math.round(Math.min(1, p) * 100) + "%"; },
      onTxLevel: (p) => { $("#vu-tx").style.width = Math.round(Math.min(1, p) * 100) + "%"; },
    });
    if (!bridge.supported) { audioStatus("error", "This browser lacks WebSocket or Web Audio support."); return; }
    bridge.connect();
  });
  $("#audio-disconnect").addEventListener("click", () => { if (bridge) bridge.disconnect(); });
  $("#audio-mute-mic").addEventListener("change", (e) => { if (bridge) bridge.setMicMuted(e.target.checked); });

  /* ======================================================================
     SIM / Identity
     ====================================================================== */
  $("#sim-refresh").addEventListener("click", loadIdentity);
  async function loadIdentity() {
    try {
      const s = await api.get("/sim");
      setOffline(false);
      setText("#id-imei", s.imei);
      setText("#id-imsi", s.imsi);
      setText("#id-iccid", s.iccid);
      setText("#id-sim", s.sim_status);
      setText("#id-msisdn", s.msisdn);
      if (s.imei && !$("#imei-new").value) $("#imei-new").placeholder = s.imei;
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  $("#imei-new").addEventListener("input", (e) => {
    const v = e.target.value.replace(/\D/g, "").slice(0, 15);
    e.target.value = v;
    const el = $("#imei-check");
    if (v.length === 0) { el.textContent = ""; el.style.color = ""; }
    else if (v.length < 15) { el.textContent = v.length + "/15 digits"; el.style.color = "var(--text-faint)"; }
    else { el.textContent = luhnValid(v) ? "Valid 15-digit IMEI (Luhn OK)" : "15 digits, but Luhn checksum fails"; el.style.color = luhnValid(v) ? "var(--ok)" : "var(--warn)"; }
  });

  function luhnValid(imei) {
    let sum = 0;
    for (let i = 0; i < 15; i++) {
      let d = Number(imei[i]);
      if (i % 2 === 1) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
    }
    return sum % 10 === 0;
  }

  $("#imei-write").addEventListener("click", async () => {
    const imei = $("#imei-new").value.trim();
    if (!/^\d{15}$/.test(imei)) { toast("Invalid IMEI", "IMEI must be exactly 15 digits.", "warn"); return; }
    const ok = await confirmAction("Write new IMEI?",
      "This changes the modem IMEI to " + imei + ". It may be ILLEGAL in your jurisdiction " +
      "and can break network registration. A reboot is usually required. Continue only on your own hardware.");
    if (!ok) return;
    try {
      await act(api.post("/sim/imei", { imei, confirm: true }), "IMEI write sent (reboot to apply)");
      setTimeout(loadIdentity, 1500);
    } catch (e) { /* toast shown */ }
  });

  /* ======================================================================
     Network / APN
     ====================================================================== */
  $("#apn-read").addEventListener("click", readApn);
  async function readApn() {
    try {
      const a = await api.get("/network/apn");
      setOffline(false);
      const f = $("#apn-form");
      if (a.apn !== undefined) f.apn.value = a.apn || "";
      if (a.user !== undefined) f.user.value = a.user || "";
      if (a.pwd !== undefined) f.pwd.value = a.pwd || "";
      if (a.auth !== undefined) f.auth.value = String(a.auth);
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }
  $("#apn-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await act(api.post("/network/apn", {
        apn: f.apn.value.trim(), user: f.user.value, pwd: f.pwd.value, auth: f.auth.value,
      }), "APN saved");
    } catch (e2) { /* toast shown */ }
  });
  $("#netmode-apply").addEventListener("click", async () => {
    try { await act(api.post("/network/netmode", { mode: $("#netmode-sel").value }), "Radio mode applied"); }
    catch (e) { /* toast shown */ }
  });

  /* ---- LTE band lock ---- */
  $("#band-read").addEventListener("click", readBand);
  $("#band-apply").addEventListener("click", async () => {
    try {
      await act(api.post("/band", { bands: $("#band-sel").value }), "Band lock applied");
      readBand();
    } catch (e) { /* toast shown (incl. 'not configured' guidance) */ }
  });

  async function readBand() {
    try {
      const b = await api.get("/band");
      setOffline(false);
      const sv = b.serving || {};
      const cells = (b.cells || []).map(c =>
        `${esc(c.band || "?")}${c.rsrp != null ? " " + c.rsrp + "dBm" : ""}` +
        `${c.usable === false ? " (unusable)" : ""}`).join(", ");
      $("#band-serving").innerHTML = sv.band
        ? `Serving cell: <span class="mono">${esc(sv.band)}</span>` +
          `${sv.rsrp != null ? " @ " + esc(String(sv.rsrp)) + " dBm" : ""}` +
          `${sv.usable === false ? ' — <b class="bad">unusable on this hardware</b>' : ""}` +
          `${cells ? `<br>Visible: ${esc(cells)}` : ""}`
        : "No serving cell reported yet.";
      const apply = $("#band-apply"), sel = $("#band-sel");
      if (b.configured) {
        apply.disabled = false; sel.disabled = false;
        const cur = (b.current && b.current.bands) || [];
        const saved = (b.saved && b.saved.bands) || [];
        const reg = b.reg_state ? ` · registration: ${esc(b.reg_state)}` : "";
        // Preselect the dropdown to match what's currently locked: a single
        // band -> that option; the full supported set -> "auto".
        const supported = (b.supported || []).slice().sort().join(",");
        if (cur.length === 1 && [...sel.options].some(o => o.value === cur[0])) {
          sel.value = cur[0];
        } else if (cur.slice().sort().join(",") === supported) {
          sel.value = "auto";
        }
        $("#band-note").innerHTML =
          `Band-lock is active over <b>QMI</b> (runtime; re-applied each boot).` +
          (cur.length ? ` Currently locked to <span class="mono">${esc(cur.join(", "))}</span>.` : "") +
          (saved.length ? ` Persisted: <span class="mono">${esc(saved.join(", "))}</span>.` : "") +
          reg;
      } else {
        // QMI channel unavailable (should not happen on this unit) -> read-only.
        apply.disabled = true; sel.disabled = true;
        $("#band-note").textContent = b.error
          ? ("Band-lock control unavailable: " + b.error)
          : ("Band-lock control unavailable on this device. The radio scans all " +
             "supported bands (B1/B3/B5/B8/B40) and picks the best automatically.");
      }
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  /* ======================================================================
     WiFi
     ====================================================================== */
  $("#wifi-read").addEventListener("click", readWifi);
  $("#wifi-clients-refresh").addEventListener("click", loadWifiClients);

  async function readWifi() {
    try {
      const w = await api.get("/wifi");
      setOffline(false);
      const f = $("#wifi-form");
      if (w.ssid !== undefined) f.ssid.value = w.ssid || "";
      if (w.password !== undefined) f.password.value = w.password || "";
      if (w.encryption !== undefined) f.encryption.value = String(w.encryption);
      if (w.max_clients !== undefined) f.max_clients.value = String(w.max_clients);
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }
  $("#wifi-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const ok = await confirmAction("Save WiFi settings?",
      "Connected clients will be disconnected and must reconnect with the new settings.");
    if (!ok) return;
    try {
      await act(api.post("/wifi", {
        ssid: f.ssid.value, password: f.password.value,
        encryption: f.encryption.value, max_clients: Number(f.max_clients.value),
      }), "WiFi settings saved");
    } catch (e2) { /* toast shown */ }
  });

  async function loadWifiClients() {
    const box = $("#wifi-clients");
    box.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';
    try {
      const res = await api.get("/wifi/clients");
      setOffline(false);
      const list = (res && res.clients) || [];
      if (!list.length) { box.innerHTML = emptyHtml("&#9775;", "No clients connected."); return; }
      const rows = list.map((c) =>
        "<tr><td>" + esc(c.hostname || "(unknown)") + "</td><td class='mono'>" +
        esc(c.ip || "--") + "</td><td class='mono'>" + esc(c.mac || "--") + "</td></tr>").join("");
      box.innerHTML = "<table><thead><tr><th>Host</th><th>IP</th><th>MAC</th></tr></thead><tbody>" +
        rows + "</tbody></table>";
    } catch (err) {
      if (err instanceof ApiError && err.offline) { setOffline(true); box.innerHTML = emptyHtml("&#9888;", "Offline."); }
      else box.innerHTML = emptyHtml("&#9888;", "Could not load clients.");
    }
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ======================================================================
     System
     ====================================================================== */
  $("#system-refresh").addEventListener("click", loadSystem);

  function svcBadge(id, running, labelOn, labelOff) {
    const el = $(id);
    el.className = "badge " + (running ? "ok" : "bad");
    el.textContent = running ? (labelOn || "running") : (labelOff || "stopped");
  }

  async function loadSystem() {
    try {
      const s = await api.get("/system");
      setOffline(false);
      const fw = s.firewall || {};
      svcBadge("#sys-firewall", fw.enabled, "active", "inactive");
      setText("#sys-firewall-sub", fw.summary || (fw.enabled ? "rmnet0 inbound dropped" : "no rules"));

      const ts = s.tailscale || {};
      svcBadge("#sys-tailscale", ts.running, ts.state || "up", "down");
      setText("#sys-tailscale-sub", ts.ip || ts.hostname || "--");

      const ag = s.adguard || {};
      svcBadge("#sys-adguard", ag.running, "running", "stopped");
      setText("#sys-adguard-sub", ag.summary || (ag.running ? "DNS filtering on" : "off"));

      renderServices((s.services) || []);
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  function renderServices(list) {
    const box = $("#sys-services");
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = emptyHtml("&#9881;", "No services reported."); return; }
    list.forEach((svc) => {
      const row = document.createElement("div"); row.className = "svc";
      const info = document.createElement("div");
      const name = document.createElement("div"); name.className = "svc-name"; name.textContent = svc.name;
      const desc = document.createElement("div"); desc.className = "svc-desc"; desc.textContent = svc.description || "";
      info.appendChild(name); info.appendChild(desc);
      const sp = document.createElement("div"); sp.className = "spacer";
      const badge = document.createElement("span");
      const running = svc.status === "running" || svc.running === true;
      badge.className = "badge " + (running ? "ok" : "");
      badge.textContent = svc.status || (running ? "running" : "stopped");
      const btn = document.createElement("button");
      btn.className = "btn ghost small";
      btn.textContent = running ? "Stop" : "Start";
      btn.addEventListener("click", () => serviceAction(svc.name, running ? "stop" : "start"));
      row.appendChild(info); row.appendChild(sp); row.appendChild(badge); row.appendChild(btn);
      box.appendChild(row);
    });
  }

  async function serviceAction(name, action) {
    const ok = await confirmAction((action === "stop" ? "Stop" : "Start") + " " + name + "?",
      "This will " + action + " the \"" + name + "\" service.");
    if (!ok) return;
    try { await act(api.post("/system/service", { name, action }), name + " " + action + "ed"); loadSystem(); }
    catch (e) { /* toast shown */ }
  }

  $("#sys-reboot").addEventListener("click", async () => {
    const ok = await confirmAction("Reboot device?",
      "The dongle will restart and drop all connections for ~30 seconds.");
    if (!ok) return;
    try { await act(api.post("/system/reboot", {}), "Reboot requested"); setOffline(true); }
    catch (e) { /* toast shown */ }
  });

  /* ======================================================================
     Settings
     ====================================================================== */
  $("#pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.new2.value) { toast("Mismatch", "New passwords do not match.", "warn"); return; }
    if (f.new.value.length < 8) { toast("Too short", "Password must be at least 8 characters.", "warn"); return; }
    try {
      await act(api.post("/settings/password", { current: f.current.value, new: f.new.value }), "Password updated");
      f.reset();
    } catch (e2) { /* toast shown */ }
  });

  $("#tokens-refresh").addEventListener("click", loadTokens);
  async function loadTokens() {
    const box = $("#tokens-list");
    box.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';
    try {
      const res = await api.get("/tokens");
      setOffline(false);
      const list = (res && res.tokens) || [];
      box.innerHTML = "";
      if (!list.length) { box.innerHTML = emptyHtml("&#128273;", "No API tokens."); return; }
      list.forEach((t) => {
        const row = document.createElement("div"); row.className = "token-row";
        const info = document.createElement("div");
        const nm = document.createElement("div"); nm.textContent = t.name || t.id; nm.style.fontWeight = "600";
        const sub = document.createElement("div"); sub.className = "svc-desc";
        sub.textContent = "created " + formatDate(t.created) + (t.last_used ? " · used " + formatDate(t.last_used) : " · never used");
        info.appendChild(nm); info.appendChild(sub);
        const sp = document.createElement("div"); sp.className = "spacer";
        const del = document.createElement("button"); del.className = "btn ghost small"; del.textContent = "Revoke";
        del.addEventListener("click", () => revokeToken(t.id, t.name));
        row.appendChild(info); row.appendChild(sp); row.appendChild(del);
        box.appendChild(row);
      });
    } catch (err) {
      if (err instanceof ApiError && err.offline) { setOffline(true); box.innerHTML = emptyHtml("&#9888;", "Offline."); }
      else box.innerHTML = emptyHtml("&#9888;", "Could not load tokens.");
    }
  }

  $("#token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#token-name").value.trim();
    if (!name) { toast("Name required", "Give the token a name.", "warn"); return; }
    try {
      const res = await act(api.post("/tokens", { name }), "Token created");
      $("#token-name").value = "";
      if (res && res.token) {
        const r = $("#token-reveal");
        r.hidden = false;
        r.textContent = "Copy this token now - it will not be shown again:\n" + res.token;
      }
      loadTokens();
    } catch (e2) { /* toast shown */ }
  });

  async function revokeToken(id, name) {
    const ok = await confirmAction("Revoke token?", "The token \"" + (name || id) + "\" will stop working immediately.");
    if (!ok) return;
    try { await act(api.del("/tokens/" + encodeURIComponent(id)), "Token revoked"); loadTokens(); }
    catch (e) { /* toast shown */ }
  }

  $("#set-save").addEventListener("click", () => {
    const secs = Math.max(1, Math.min(60, Number($("#set-interval").value) || 3));
    state.intervalMs = secs * 1000;
    localStorage.setItem("cockpit.interval", String(secs));
    if (SESSION.authenticated) startPolling();
    toast("Saved", "Polling every " + secs + "s", "ok");
  });

  /* ======================================================================
     Boot
     ====================================================================== */
  updateSegCounter();
  boot();
})();
