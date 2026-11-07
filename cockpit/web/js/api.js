/* ==========================================================================
   api.js - centralized fetch client for the UZ801 Cockpit secure API.

   Every REST call goes through api() so that CSRF, auth (401) handling,
   offline detection and error normalisation live in one place.

   Contract (consumed by this SPA; the API agent implements the same):

   Auth / session (cookie-based; see README.md for the full contract):
     GET    /api/session                      -> { first_run, authenticated, csrf, user }
     POST   /api/setup    {username,password}  (first run only)
     POST   /api/login    {username,password}
     POST   /api/logout

   All state-changing requests (POST/PUT/DELETE) send header  X-CSRF: <token>
   from the session. A 401 response anywhere kicks the UI back to login.
   ========================================================================== */

"use strict";

/* Same-origin API root. The secure API serves this SPA, so no CORS. */
const API_ROOT = "/api";

/* Session state shared across modules (plain global; no module system). */
const SESSION = {
  authenticated: false,
  firstRun: false,
  user: null,
  csrf: null,
  /* Audio bridge transport config, reported by GET /api/session.
     The bridge runs on its own port (default 8089); see audio.js. */
  audio: { enabled: false, port: 8089, path: "/ws/audio", rate: 8000 },
};

/* Normalised error with flags the UI reacts to. */
class ApiError extends Error {
  constructor(message, { status = 0, offline = false, needLogin = false, data = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.offline = offline;
    this.needLogin = needLogin;
    this.data = data;
  }
}

/* Optional callback invoked whenever a 401 is seen, so app.js can show login. */
let onUnauthorized = null;
function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

/**
 * Core request helper.
 * @param {string} method  GET | POST | PUT | DELETE
 * @param {string} path    e.g. "/status" (API_ROOT is prepended)
 * @param {object} [body]  JSON body for state-changing requests
 * @returns {Promise<any>} parsed JSON (or {} for empty 204 responses)
 * @throws {ApiError}
 */
async function request(method, path, body) {
  const headers = {};
  const opts = { method, credentials: "same-origin", headers };

  const isWrite = method !== "GET" && method !== "HEAD";
  if (isWrite) {
    if (SESSION.csrf) headers["X-CSRF"] = SESSION.csrf;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }

  let resp;
  try {
    resp = await fetch(API_ROOT + path, opts);
  } catch (networkErr) {
    throw new ApiError("Cannot reach the dongle API.", { offline: true });
  }

  if (resp.status === 401) {
    SESSION.authenticated = false;
    if (onUnauthorized) onUnauthorized();
    throw new ApiError("Session expired. Please sign in again.", {
      status: 401, needLogin: true,
    });
  }

  // 502/503/504 from the API's own proxy => treat as device/backend unreachable.
  if (resp.status === 502 || resp.status === 503 || resp.status === 504) {
    throw new ApiError("Backend unavailable (" + resp.status + ").", {
      status: resp.status, offline: true,
    });
  }

  let data = null;
  const text = await resp.text();
  if (text) {
    try { data = JSON.parse(text); }
    catch (e) { data = { raw: text }; }
  }

  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || ("Request failed (" + resp.status + ")");
    throw new ApiError(msg, { status: resp.status, data });
  }

  return data === null ? {} : data;
}

/* Convenience verbs. */
const api = {
  get:  (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
  put:  (path, body) => request("PUT", path, body),
  del:  (path, body) => request("DELETE", path, body),
};

/* --- Session lifecycle ---------------------------------------------------- */

/** GET /api/session and cache it. Returns the session object. */
async function loadSession() {
  const s = await api.get("/session");
  SESSION.firstRun = !!s.first_run;
  SESSION.authenticated = !!s.authenticated;
  SESSION.user = s.user || null;
  SESSION.csrf = s.csrf || null;
  if (s.audio && typeof s.audio === "object") {
    SESSION.audio = Object.assign({ enabled: false, port: 8089,
      path: "/ws/audio", rate: 8000 }, s.audio);
  }
  return s;
}

async function setupAdmin(username, password) {
  const s = await api.post("/setup", { username, password });
  // Setup logs the user in and returns a fresh session (incl. csrf).
  if (s && s.csrf) SESSION.csrf = s.csrf;
  SESSION.authenticated = true;
  SESSION.firstRun = false;
  SESSION.user = (s && s.user) || username;
  return s;
}

async function login(username, password) {
  const s = await api.post("/login", { username, password });
  if (s && s.csrf) SESSION.csrf = s.csrf;
  SESSION.authenticated = true;
  SESSION.user = (s && s.user) || username;
  return s;
}

async function logout() {
  try { await api.post("/logout", {}); } catch (e) { /* ignore */ }
  SESSION.authenticated = false;
  SESSION.user = null;
}

/* ==========================================================================
   Pure formatting / math helpers (no device access)
   ========================================================================== */

/** RSRP dBm -> number of signal bars 0..5 (LTE scale). */
function rsrpToBars(rsrp) {
  const r = Number(rsrp);
  if (!Number.isFinite(r) || r === 0) return 0;
  if (r >= -80) return 5;
  if (r >= -90) return 4;
  if (r >= -100) return 3;
  if (r >= -110) return 2;
  if (r >= -120) return 1;
  return 0;
}

/** RSRP dBm -> 0..100 percentage for a meter (clamped -120..-70). */
function rsrpToPercent(rsrp) {
  const r = Number(rsrp);
  if (!Number.isFinite(r) || r === 0) return 0;
  const pct = ((r + 120) / 50) * 100;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

/** CSQ rssi (0..31, 99=unknown) -> approximate dBm, or null. */
function csqToDbm(csq) {
  const r = Number(csq);
  if (!Number.isFinite(r) || r === 99 || r < 0) return null;
  return -113 + 2 * Math.min(r, 31);
}

/** Human-readable byte count. */
function humanBytes(n) {
  let v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return (i === 0 ? v : v.toFixed(2)) + " " + units[i];
}

/** Seconds -> "1d 02:03:04" / "02:03:04". */
function humanUptime(s) {
  let sec = Number(s);
  if (!Number.isFinite(sec) || sec < 0) return "--";
  const d = Math.floor(sec / 86400); sec -= d * 86400;
  const h = Math.floor(sec / 3600); sec -= h * 3600;
  const m = Math.floor(sec / 60); sec -= m * 60;
  const pad = (x) => String(x).padStart(2, "0");
  const hms = pad(h) + ":" + pad(m) + ":" + pad(Math.floor(sec));
  return d > 0 ? d + "d " + hms : hms;
}

/** Seconds -> mm:ss for call timers. */
function mmss(s) {
  let sec = Math.max(0, Math.floor(Number(s) || 0));
  const m = Math.floor(sec / 60);
  const ss = sec % 60;
  return String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}

/** Unix seconds or ISO string -> local short datetime. */
function formatDate(ts) {
  if (ts === undefined || ts === null || ts === "") return "";
  let d;
  if (typeof ts === "number") d = new Date(ts * 1000);
  else if (/^\d+$/.test(String(ts))) d = new Date(Number(ts) * 1000);
  else d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  return d.toLocaleString([], { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" });
}

/**
 * SMS segment calculator. Detects whether the text fits GSM-7 or needs UCS-2,
 * then computes the number of segments.
 * @returns {{encoding:"GSM-7"|"UCS-2", chars:number, segments:number, perSeg:number}}
 */
const GSM7_BASIC =
  "@£$¥èéùìòÇ\nØø\rÅå" +
  "Δ_ΦΓΛΩΠΨΣΘΞÆæßÉ" +
  " !\"#¤%&'()*+,-./0123456789:;<=>?" +
  "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§" +
  "¿abcdefghijklmnopqrstuvwxyzäöñüà";
const GSM7_EXT = "\f^{}\\[~]|€"; // each counts as 2 septets

function smsSegments(text) {
  const str = String(text || "");
  let isGsm = true;
  let septets = 0;
  for (const ch of str) {
    if (GSM7_BASIC.indexOf(ch) !== -1) septets += 1;
    else if (GSM7_EXT.indexOf(ch) !== -1) septets += 2;
    else { isGsm = false; break; }
  }
  const chars = [...str].length;
  if (isGsm) {
    // GSM-7: 160 septets single, 153 per segment when concatenated.
    const segments = septets <= 160 ? 1 : Math.ceil(septets / 153);
    return { encoding: "GSM-7", chars, segments, perSeg: segments <= 1 ? 160 : 153 };
  }
  // UCS-2: 70 code units single, 67 per segment when concatenated.
  const units = str.length;
  const segments = units <= 70 ? 1 : Math.ceil(units / 67);
  return { encoding: "UCS-2", chars, segments, perSeg: segments <= 1 ? 70 : 67 };
}

/* Export everything app.js / audio.js need as plain globals. */
window.API = {
  api, request, SESSION,
  ApiError, setUnauthorizedHandler,
  loadSession, setupAdmin, login, logout,
  rsrpToBars, rsrpToPercent, csqToDbm,
  humanBytes, humanUptime, mmss, formatDate, smsSegments,
};
