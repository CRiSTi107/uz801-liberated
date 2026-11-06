/* ==========================================================================
   app.js - UI wiring, navigation, polling, and all funcNo actions.
   Depends on window.API from api.js.
   ========================================================================== */

"use strict";

(function () {
  const {
    api, uploadFirmware, ApiError,
    rssiToDbm, rssiToBars, humanBytes, netModeLabel,
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
    loggedIn: false,
    macList: [],
  };

  /* Load saved interval preference. */
  const savedInterval = Number(localStorage.getItem("uz801.interval"));
  if (Number.isFinite(savedInterval) && savedInterval >= 1) {
    state.intervalMs = savedInterval * 1000;
    $("#set-interval").value = savedInterval;
  }

  /* ----------------------------------------------------------------------
     Toast notifications
     ---------------------------------------------------------------------- */
  function toast(title, body, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.innerHTML =
      '<div class="t-title"></div>' + (body ? '<div class="t-body"></div>' : "");
    el.querySelector(".t-title").textContent = title;
    if (body) el.querySelector(".t-body").textContent = body;
    $("#toast-wrap").appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  /** Run an api() call, show a toast on ApiError, and surface offline state. */
  async function call(funcNo, params, successMsg) {
    try {
      const out = await api(funcNo, params);
      setOffline(false);
      if (successMsg) toast(successMsg, "", "ok");
      return out;
    } catch (err) {
      if (err instanceof ApiError && err.offline) {
        setOffline(true);
      } else {
        toast("Error (funcNo " + funcNo + ")", err.message, "bad");
      }
      throw err;
    }
  }

  /* ----------------------------------------------------------------------
     Confirm dialog (returns a Promise<boolean>)
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

  /* ----------------------------------------------------------------------
     Navigation
     ---------------------------------------------------------------------- */
  $("#nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (!btn) return;
    $$("#nav button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const view = btn.dataset.view;
    $$(".view").forEach((v) => v.classList.remove("active"));
    $("#view-" + view).classList.add("active");
  });

  /* ----------------------------------------------------------------------
     Login gate (funcNo 1000)
     ---------------------------------------------------------------------- */
  const loginOverlay = $("#login");
  function showLogin() { loginOverlay.classList.add("show"); }
  function hideLogin() { loginOverlay.classList.remove("show"); }

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = $("#login-user").value || "admin";
    const password = $("#login-pass").value || "admin";
    $("#login-err").textContent = "";
    try {
      const { first } = await api(1000, { username, password });
      state.loggedIn = true;
      $("#auth-state").textContent = "Signed in (" + username + ")";
      if (first.fwversion) $("#fw-foot").textContent = "fw: " + first.fwversion;
      hideLogin();
      setOffline(false);
      toast("Signed in", "", "ok");
      refreshAll();
    } catch (err) {
      $("#login-err").textContent = err.message;
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  });

  $("#login-skip").addEventListener("click", () => {
    // Many status reads work without auth; let the user browse read-only.
    hideLogin();
    $("#auth-state").textContent = "Read-only (not signed in)";
    refreshAll();
  });
  $("#set-login").addEventListener("click", showLogin);

  /* ----------------------------------------------------------------------
     Dashboard polling (1001 status, 1002 ip/dns, 1003 traffic, 1015 sim)
     ---------------------------------------------------------------------- */
  function setText(id, val) {
    const el = $(id);
    if (el) el.textContent = (val === undefined || val === null || val === "") ? "--" : val;
  }

  function renderSignal(rssi) {
    const bars = rssiToBars(rssi);
    const dbm = rssiToDbm(rssi);
    const el = $("#d-signal");
    const kids = el.querySelectorAll(".bar");
    kids.forEach((b, i) => b.classList.toggle("on", i < bars));
    el.classList.remove("weak", "none");
    if (bars === 0) el.classList.add("none");
    else if (bars <= 2) el.classList.add("weak");
    setText("#d-dbm", dbm === null ? "--" : dbm);
    setText("#d-rssi", "rssi " + (rssi === undefined ? "--" : rssi) +
                       (bars ? " (" + bars + "/5)" : ""));
  }

  function renderConnBadge(netstatus) {
    const badge = $("#d-conn-badge");
    const dot = $("#conn-dot");
    const s = String(netstatus || "").toLowerCase();
    const connected = s.includes("connect") && !s.includes("disconnect") || s === "1";
    badge.className = "badge " + (connected ? "ok" : "warn");
    badge.textContent = connected ? "connected" : (netstatus || "unknown");
    dot.classList.toggle("online", connected);
    dot.classList.remove("offline");
  }

  async function pollDashboard() {
    if (state.paused) return;
    // Fire the reads; tolerate individual failures but detect offline once.
    try {
      const [st, ip, tr, sim] = await Promise.allSettled([
        api(1001), api(1002), api(1003), api(1015),
      ]);

      // If every call failed with offline, mark offline.
      const anyOk = [st, ip, tr, sim].some((r) => r.status === "fulfilled");
      const allOffline = [st, ip, tr, sim].every(
        (r) => r.status === "rejected" && r.reason && r.reason.offline
      );
      if (allOffline) { setOffline(true); return; }
      if (anyOk) setOffline(false);

      if (st.status === "fulfilled") {
        const f = st.value.first;
        setText("#d-oper", f.oper);
        setText("#d-netmode", "net mode " + netModeLabel(f.netmode));
        setText("#n-oper", f.oper);
        setText("#n-netstatus", f.netstatus);
        setText("#n-netmode", netModeLabel(f.netmode));
        setText("#n-rssi", f.rssi);
        setText("#d-conn", f.netstatus);
        renderSignal(f.rssi);
        renderConnBadge(f.netstatus);
      }
      if (ip.status === "fulfilled") {
        const f = ip.value.first;
        setText("#d-ip", f.IP);
        setText("#d-mask", f.mask);
        setText("#d-dns1", f.dns1);
        setText("#d-dns2", f.dns2);
        setText("#d-wlan", f.wlan_ip);
      }
      if (tr.status === "fulfilled") {
        const f = tr.value.first;
        setText("#d-down", humanBytes(f.down_bytes));
        setText("#d-up", humanBytes(f.up_bytes));
        setText("#d-clients", f.client_num);
        setText("#d-clients-max", f.maxSta ? " / " + f.maxSta : "");
      }
      if (sim.status === "fulfilled") {
        const f = sim.value.first;
        setText("#d-sim", f.sim_status);
        setText("#d-imsi", f.imsi);
        setText("#d-iccid", f.iccid);
        setText("#s-status", f.sim_status);
        setText("#s-imsi", f.imsi);
        setText("#s-iccid", f.iccid);
      }
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }

  function startPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(pollDashboard, state.intervalMs);
  }

  function refreshAll() {
    pollDashboard();
    refreshDeviceInfo();
    refreshIdentity();
    refreshSimIdentity();
  }

  $("#pause-toggle").addEventListener("change", (e) => {
    state.paused = e.target.checked;
    toast(state.paused ? "Auto-refresh paused" : "Auto-refresh resumed", "", "");
  });
  $("#refresh-now").addEventListener("click", pollDashboard);
  $("#n-refresh").addEventListener("click", pollDashboard);
  $("#s-refresh").addEventListener("click", pollDashboard);

  /* ----------------------------------------------------------------------
     Network actions
     ---------------------------------------------------------------------- */
  $("#n-mode-apply").addEventListener("click", async () => {
    const netmode = Number($("#n-mode-sel").value);
    await call(1005, { netmode }, "Net mode applied");
  });
  $("#n-conn-apply").addEventListener("click", async () => {
    // Field name unverified; send both a string and a numeric form.
    const mode = $("#n-conn-sel").value;
    await call(1004, { conn_mode: mode, mode: mode === "auto" ? 1 : 0 },
               "Connection mode applied");
  });

  /* ----------------------------------------------------------------------
     SIM / APN  (1016 read, 1017 save, 1018 connect) - UNVERIFIED fields
     ---------------------------------------------------------------------- */
  $("#apn-read").addEventListener("click", async () => {
    try {
      const { first } = await call(1016);
      const f = $("#apn-form");
      if (first.apn !== undefined) f.apn.value = first.apn || "";
      if (first.user !== undefined) f.user.value = first.user || "";
      if (first.pwd !== undefined) f.pwd.value = first.pwd || "";
      if (first.auth !== undefined) f.auth.value = String(first.auth);
    } catch (e) { /* toast already shown */ }
  });
  $("#apn-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    await call(1017, {
      apn: f.apn.value, user: f.user.value,
      pwd: f.pwd.value, auth: Number(f.auth.value),
    }, "APN profile saved");
  });
  $("#apn-connect").addEventListener("click", async () => {
    await call(1018, {}, "Connect requested");
  });

  /* ----------------------------------------------------------------------
     Identity & Modem (1029 device info, 2002 write IMEI, 1015 SIM)
     ---------------------------------------------------------------------- */
  async function refreshIdentity() {
    try {
      const { first } = await api(1029);
      setOffline(false);
      setText("#id-manu", first.manufacture);
      setText("#id-fw", first.fwversion);
      setText("#id-imei", first.imei);
      if (first.imei && !$("#id-imei-new").value) $("#id-imei-new").placeholder = first.imei;
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }
  async function refreshSimIdentity() {
    try {
      const { first } = await api(1015);
      setText("#id-sim-status", first.sim_status);
      setText("#id-imsi", first.imsi);
      setText("#id-iccid", first.iccid);
    } catch (e) { /* handled by poll offline logic */ }
  }
  $("#id-refresh").addEventListener("click", refreshIdentity);
  $("#id-sim-refresh").addEventListener("click", refreshSimIdentity);

  $("#id-imei-write").addEventListener("click", async () => {
    const imei = $("#id-imei-new").value.trim();
    if (!/^\d{15}$/.test(imei)) {
      toast("Invalid IMEI", "IMEI must be exactly 15 digits.", "warn");
      return;
    }
    const ok = await confirmAction(
      "Write new IMEI?",
      "This permanently changes the modem IMEI to " + imei + " via AT+WRIMEI. " +
      "It may be illegal in your jurisdiction and can break network registration. " +
      "A reboot is usually required. Continue?"
    );
    if (!ok) return;
    await call(2002, { imei }, "IMEI write sent (reboot the dongle to apply)");
    setTimeout(refreshIdentity, 1500);
  });

  /* ----------------------------------------------------------------------
     WiFi  (read: 1006 ssid/maxSta + 1009 security;
            save: 1007 ssid/maxSta + 1010 encryp_type/pwd)
     ---------------------------------------------------------------------- */
  $("#wifi-read").addEventListener("click", async () => {
    try {
      const { first: w } = await call(1006);          // ssid, maxSta, mac, channel...
      const f = $("#wifi-form");
      if (w.ssid !== undefined) f.ssid.value = w.ssid || "";
      if (w.maxSta !== undefined) f.maxSta.value = String(w.maxSta);
      if (w.mac) $("#wifi-mac").textContent = w.mac;
      const { first: s } = await api(1009);           // pwd, encryp_type
      if (s.pwd !== undefined) f.pwd.value = s.pwd || "";
      if (s.encryp_type !== undefined) f.encryp_type.value = String(s.encryp_type);
    } catch (e) { /* toast already shown */ }
  });
  $("#wifi-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    // 1) SSID + max clients
    await call(1007, { ssid: f.ssid.value, maxSta: Number(f.maxSta.value) },
               "SSID saved");
    // 2) Encryption + password
    await call(1010, { encryp_type: Number(f.encryp_type.value), pwd: f.pwd.value },
               "WiFi security saved");
  });

  /* ----------------------------------------------------------------------
     MAC filter (1052 read, 1055 apply) - UNVERIFIED
     ---------------------------------------------------------------------- */
  function renderMacList() {
    const tb = $("#mac-list");
    tb.innerHTML = "";
    state.macList.forEach((mac, i) => {
      const tr = document.createElement("tr");
      const td1 = document.createElement("td");
      td1.className = "mono";
      td1.textContent = mac;
      const td2 = document.createElement("td");
      const btn = document.createElement("button");
      btn.className = "btn ghost small";
      btn.textContent = "Remove";
      btn.onclick = () => { state.macList.splice(i, 1); renderMacList(); };
      td2.appendChild(btn);
      tr.appendChild(td1); tr.appendChild(td2);
      tb.appendChild(tr);
    });
  }
  $("#mac-add").addEventListener("click", () => {
    const v = $("#mac-input").value.trim();
    if (!/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(v)) {
      toast("Invalid MAC", "Use AA:BB:CC:DD:EE:FF format", "warn");
      return;
    }
    state.macList.push(v.toUpperCase());
    $("#mac-input").value = "";
    renderMacList();
  });
  $("#mac-read").addEventListener("click", async () => {
    try {
      const { first } = await call(1052);
      const list = first.mac_list || first.list || [];
      if (Array.isArray(list)) { state.macList = list.slice(); renderMacList(); }
      if (first.mode !== undefined) $("#mac-mode").value = String(first.mode);
    } catch (e) { /* handled */ }
  });
  $("#mac-apply").addEventListener("click", async () => {
    await call(1055, {
      mode: Number($("#mac-mode").value),
      mac_list: state.macList,
    }, "MAC filter applied");
  });

  /* ----------------------------------------------------------------------
     Firmware (3002 info, upload, 3000 apply, 3001 delete)
     ---------------------------------------------------------------------- */
  async function refreshDeviceInfo() {
    try {
      const { first } = await api(3002);
      setOffline(false);
      setText("#f-name", first.device_name);
      setText("#f-sw", first.sw_version);
      if (first.sw_version) $("#fw-foot").textContent = "fw: " + first.sw_version;
    } catch (err) {
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  }
  $("#f-refresh").addEventListener("click", refreshDeviceInfo);

  $("#fw-upload").addEventListener("click", async () => {
    const file = $("#fw-file").files[0];
    if (!file) { toast("No file", "Choose a firmware package first", "warn"); return; }
    $("#fw-prog-wrap").style.display = "block";
    $("#fw-upload-msg").textContent = "Uploading " + file.name + " ...";
    try {
      const resp = await uploadFirmware(file, (pct) => {
        $("#fw-prog").style.width = pct + "%";
      });
      if (String(resp.flag) === "1") {
        $("#fw-upload-msg").textContent = "Upload complete. You can now Apply upgrade.";
        toast("Upload complete", "", "ok");
      } else {
        const msg = resp.error_info && resp.error_info !== "none"
          ? resp.error_info : "Upload rejected by device";
        $("#fw-upload-msg").textContent = msg;
        toast("Upload failed", msg, "bad");
      }
    } catch (err) {
      $("#fw-upload-msg").textContent = err.message;
      toast("Upload failed", err.message, "bad");
    }
  });

  $("#fw-apply").addEventListener("click", async () => {
    const ok = await confirmAction(
      "Apply firmware upgrade?",
      "This flashes the uploaded package and reboots the dongle. A failure can " +
      "brick the device. Keep power connected. Continue?"
    );
    if (!ok) return;
    await call(3000, {}, "Upgrade started");
  });
  $("#fw-delete").addEventListener("click", async () => {
    await call(3001, {}, "Staged package deleted");
  });

  /* ----------------------------------------------------------------------
     Developer: ADB (2001), reboot (1013), factory reset (1014), raw console
     ---------------------------------------------------------------------- */
  $("#dev-adb").addEventListener("click", async () => {
    const ok = await confirmAction(
      "Enable root ADB over USB?",
      "funcNo 2001 switches USB to rndis,serial_smd,diag,adb and exposes a " +
      "ROOT ADB shell. Only enable this if you understand the security impact."
    );
    if (!ok) return;
    await call(2001, {}, "USB debug / ADB enabled (reconnect USB)");
  });
  $("#dev-reboot").addEventListener("click", async () => {
    const ok = await confirmAction("Reboot the dongle?",
      "The device will restart and drop the connection for ~30s.");
    if (!ok) return;
    await call(1013, {}, "Reboot requested");
  });
  $("#dev-factory").addEventListener("click", async () => {
    const ok = await confirmAction("Factory reset?",
      "This erases ALL settings (WiFi, APN, etc.) and restores defaults. " +
      "This cannot be undone.");
    if (!ok) return;
    await call(1014, {}, "Factory reset requested");
  });

  $("#raw-send").addEventListener("click", async () => {
    const fn = Number($("#raw-func").value);
    if (!Number.isFinite(fn)) { toast("Enter a funcNo", "", "warn"); return; }
    let params = {};
    const raw = $("#raw-params").value.trim();
    if (raw) {
      try { params = JSON.parse(raw); }
      catch (e) { toast("Invalid JSON params", e.message, "bad"); return; }
    }
    try {
      const out = await api(fn, params);
      $("#raw-out").textContent = JSON.stringify(out.raw, null, 2);
      setOffline(false);
    } catch (err) {
      $("#raw-out").textContent = "ERROR: " + err.message;
      if (err instanceof ApiError && err.offline) setOffline(true);
    }
  });

  /* ----------------------------------------------------------------------
     Settings
     ---------------------------------------------------------------------- */
  $("#set-save").addEventListener("click", () => {
    const secs = Math.max(1, Math.min(60, Number($("#set-interval").value) || 3));
    state.intervalMs = secs * 1000;
    localStorage.setItem("uz801.interval", String(secs));
    startPolling();
    toast("Saved", "Refreshing every " + secs + "s", "ok");
  });

  /* ----------------------------------------------------------------------
     Boot
     ---------------------------------------------------------------------- */
  showLogin();
  startPolling();
  // Kick an immediate poll so the dashboard isn't blank behind the login box.
  pollDashboard();
  refreshDeviceInfo();
})();
