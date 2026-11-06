/* ==========================================================================
   api.js - thin client for the UZ801 /ajax API, plus small helpers.

   Every funcNo call in the app goes through api() so that error handling,
   the login gate and offline detection live in one place.
   ========================================================================== */

"use strict";

/* The device API is reachable at the same origin under /ajax.
   When this UI is served by the on-device busybox httpd on :8088, that server
   reverse-proxies /ajax to the stock web server on 127.0.0.1:80, so there is
   no cross-origin / CORS problem and no PC-side proxy is involved. */
const AJAX_URL = "/ajax";

/* Error thrown by api(). `offline` marks proxy/device-unreachable cases so the
   UI can switch to its offline state instead of showing a normal error toast. */
class ApiError extends Error {
  constructor(message, { funcNo, offline = false, needLogin = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.funcNo = funcNo;
    this.offline = offline;
    this.needLogin = needLogin;
  }
}

/**
 * Call a device funcNo.
 * @param {number} funcNo
 * @param {object} params  extra JSON fields merged into the request body
 * @returns {Promise<{results:Array, first:object, raw:object}>}
 * @throws {ApiError}
 */
async function api(funcNo, params = {}) {
  const body = JSON.stringify(Object.assign({ funcNo }, params));

  let resp;
  try {
    resp = await fetch(AJAX_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (networkErr) {
    // fetch() itself failed -> device/network unreachable.
    throw new ApiError("Cannot reach the dongle. Check your WiFi/USB connection to 192.168.100.1.", {
      funcNo,
      offline: true,
    });
  }

  const text = await resp.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (parseErr) {
    throw new ApiError("Device returned a non-JSON response.", {
      funcNo,
      offline: resp.status === 502,
    });
  }

  // The proxy returns 502 with flag:"0" when the device is unreachable.
  const flag = String(data.flag);
  if (flag !== "1") {
    const info =
      data.error_info && data.error_info !== "none"
        ? data.error_info
        : "Request failed (flag=" + flag + ")";
    throw new ApiError(info, {
      funcNo,
      offline: resp.status === 502,
    });
  }

  const results = Array.isArray(data.results) ? data.results : [];
  return { results, first: results[0] || {}, raw: data };
}

/**
 * Multipart firmware upload to /ajax using XHR so we get progress events.
 * @param {File} file
 * @param {(pct:number)=>void} onProgress
 * @returns {Promise<object>} parsed JSON response
 */
function uploadFirmware(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    // Field name per the API contract: `upload_file`.
    form.append("upload_file", file, file.name);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", AJAX_URL);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = () => {
      try {
        resolve(JSON.parse(xhr.responseText));
      } catch (e) {
        reject(new ApiError("Upload: non-JSON response from device.", {}));
      }
    };
    xhr.onerror = () =>
      reject(new ApiError("Upload failed (network/proxy error).", { offline: true }));
    xhr.send(form);
  });
}

/* --- Helpers -------------------------------------------------------------- */

/** CSQ rssi (0..31, 99=unknown) -> approximate dBm, or null if unknown. */
function rssiToDbm(rssi) {
  const r = Number(rssi);
  if (!Number.isFinite(r) || r === 99 || r < 0) return null;
  // Standard 3GPP mapping: 0 -> -113 dBm, 31 -> -51 dBm, 2 dB per step.
  return -113 + 2 * Math.min(r, 31);
}

/** CSQ rssi -> number of signal bars 0..5. */
function rssiToBars(rssi) {
  const r = Number(rssi);
  if (!Number.isFinite(r) || r === 99 || r <= 0) return 0;
  if (r >= 20) return 5;
  if (r >= 15) return 4;
  if (r >= 10) return 3;
  if (r >= 5) return 2;
  return 1;
}

/** Human-readable byte count. Accepts number or numeric string. */
function humanBytes(n) {
  let v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (i === 0 ? v : v.toFixed(2)) + " " + units[i];
}

/** Map the device's netmode/net_mode codes to a friendly label. */
function netModeLabel(code) {
  const map = { 0: "Auto", 1: "3G only", 2: "4G only", 3: "4G", 4: "3G", 5: "2G" };
  const key = String(code);
  return map[key] !== undefined ? map[key] : (code || "--");
}

/* Make symbols available to app.js (plain globals; no module system). */
window.API = {
  api,
  uploadFirmware,
  ApiError,
  rssiToDbm,
  rssiToBars,
  humanBytes,
  netModeLabel,
};
