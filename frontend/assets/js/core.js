/* ICU Monitoring System — core API client, session, formatting, toasts. */
"use strict";

const ICU = (() => {
  const API = "/api/v1";

  async function request(path, opts = {}) {
    const options = {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      credentials: "same-origin",
    };
    if (opts.body !== undefined) options.body = JSON.stringify(opts.body);
    let res;
    try {
      res = await fetch(API + path, options);
    } catch (e) {
      throw new NetError("Network error — is the backend running?");
    }
    if (res.status === 401 && !path.startsWith("/auth/")) {
      window.location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.search);
      throw new NetError("Session expired");
    }
    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch { data = { detail: text }; }
    }
    if (!res.ok) {
      let msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      if (Array.isArray(msg)) {
        msg = msg.map((it) => it && typeof it === "object"
          ? `${(it.loc || []).filter((x) => x !== "body").join(".") || "field"}: ${it.msg}`
          : String(it)).join("; ");
      }
      throw new ApiError(msg, res.status, data);
    }
    return data;
  }

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }
  class NetError extends Error {}

  const get = (p, q) => request(p + (q ? "?" + new URLSearchParams(q) : ""));
  const post = (p, body) => request(p, { method: "POST", body: body ?? {} });
  const del = (p) => request(p, { method: "DELETE" });

  /* ---------- session ---------- */
  let _me = null;
  async function me(force) {
    if (_me && !force) return _me;
    _me = await get("/auth/me");
    return _me;
  }
  function clearMe() { _me = null; }
  async function logout() {
    try { await post("/auth/logout"); } catch { /* ignore */ }
    clearMe();
    window.location.href = "/login.html";
  }

  /* ---------- toasts ---------- */
  function toast(message, type = "info", ttl = 4200) {
    let root = document.getElementById("toast-root");
    if (!root) {
      root = document.createElement("div");
      root.id = "toast-root";
      document.body.appendChild(root);
    }
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => el.remove(), ttl);
  }

  /* ---------- formatting ---------- */
  const fmtTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(d)) return "—";
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const fmtClock = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(d)) return "—";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const ageLabel = (seconds) => {
    if (seconds == null) return "never";
    if (seconds < 5) return "just now";
    if (seconds < 90) return Math.round(seconds) + "s ago";
    if (seconds < 5400) return Math.round(seconds / 60) + "m ago";
    return Math.round(seconds / 3600) + "h ago";
  };
  const num = (v, digits = 0) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(digits));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const riskBadge = (cat) => `<span class="badge ${esc(cat || "low")}">${esc(cat || "low")}</span>`;
  const sevBadge = (sev) => `<span class="badge ${esc(sev)}">${esc(sev)}</span>`;
  const qualityBadge = (q) => `<span class="badge ${esc(q || "unknown")}">${esc(q || "unknown")}</span>`;
  const simBadge = (isSim) => (isSim ? `<span class="badge simulated" title="Simulated test data — not a real measurement">SIMULATED</span>` : "");

  /* ---------- icons ---------- */
  function icons(root) {
    if (window.lucide) {
      try { window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } }); } catch { /* noop */ }
    }
  }

  /* ---------- live connection (WebSocket with polling fallback) ---------- */
  function live(patientId, onPayload, opts = {}) {
    let ws = null;
    let closed = false;
    let pollTimer = null;
    let wsTimer = null;
    const statusEl = opts.statusEl ? document.querySelector(opts.statusEl) : null;

    function setStatus(state) {
      if (!statusEl) return;
      const labels = { connected: "Live", polling: "Polling", disconnected: "Offline" };
      statusEl.className = "conn-pill " + state;
      statusEl.innerHTML = `<span class="dot"></span>${labels[state] || state}`;
    }

    async function pollOnce() {
      try {
        const data = await get(`/live/${patientId}`);
        onPayload(data);
        if (!ws || ws.readyState !== 1) setStatus("polling");
      } catch (e) {
        setStatus("disconnected");
      }
    }

    function startPolling() {
      if (pollTimer) return;
      setStatus("polling");
      pollOnce();
      pollTimer = setInterval(pollOnce, opts.pollMs || 5000);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function connect() {
      if (closed) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      try {
        ws = new WebSocket(`${proto}//${location.host}${API}/ws/live/${patientId}`);
      } catch (e) {
        startPolling();
        return;
      }
      ws.onopen = () => {
        setStatus("connected");
        stopPolling();
        if (wsTimer) clearInterval(wsTimer);
        wsTimer = setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, opts.pingMs || 10000);
        try { ws.send("ping"); } catch { /* noop */ }
      };
      ws.onmessage = (ev) => {
        try { onPayload(JSON.parse(ev.data)); } catch { /* noop */ }
      };
      ws.onclose = () => {
        if (wsTimer) { clearInterval(wsTimer); wsTimer = null; }
        if (!closed) {
          startPolling();
          setTimeout(connect, 3500);
        }
      };
      ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
    }

    // Also refresh when the tab becomes visible again
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollOnce();
    });

    connect();
    startPolling();

    return {
      close() {
        closed = true;
        stopPolling();
        if (wsTimer) clearInterval(wsTimer);
        try { ws && ws.close(); } catch { /* noop */ }
      },
      refresh: pollOnce,
    };
  }

  /* ---------- charts ---------- */
  const CHART_COLORS = {
    hr: "#f43f5e",
    spo2: "#3b82f6",
    temp: "#d97706",
    hum: "#0d9488",
    risk: "#7c3aed",
  };

  function baseOptions(unit, title) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        title: title ? { display: true, text: title, font: { size: 12, weight: "600" }, color: "#64748b" } : undefined,
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y ?? "no data"}${unit || ""}`,
          },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 9, color: "#94a3b8", font: { size: 10 } },
          grid: { color: "rgba(148,163,184,.12)" },
        },
        y: {
          ticks: { color: "#94a3b8", font: { size: 10 } },
          grid: { color: "rgba(148,163,184,.14)" },
          title: { display: !!unit, text: unit, color: "#94a3b8", font: { size: 10 } },
        },
      },
      spanGaps: false, // never draw a line through missing/invalid data
      elements: { point: { radius: 2, hoverRadius: 5 }, line: { tension: 0.25, borderWidth: 2 } },
    };
  }

  function makeChart(canvas, labels, values, { color = CHART_COLORS.hr, unit = "", label = "", fill = false, min = undefined, max = undefined } = {}) {
    return new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: label || "Value",
          data: values,          // null values create visible gaps
          borderColor: color,
          backgroundColor: fill ? color + "22" : "transparent",
          fill,
          spanGaps: false,
          stepped: false,
        }],
      },
      options: (() => {
        const o = baseOptions(unit);
        if (min !== undefined) o.scales.y.min = min;
        if (max !== undefined) o.scales.y.max = max;
        return o;
      })(),
    });
  }

  function destroyChart(ch) { try { ch && ch.destroy(); } catch { /* noop */ } }

  return {
    API, get, post, del, request, me, clearMe, logout,
    toast, fmtTime, fmtClock, ageLabel, num, esc,
    riskBadge, sevBadge, qualityBadge, simBadge,
    icons, live, makeChart, destroyChart, CHART_COLORS,
    ApiError, NetError,
  };
})();

document.addEventListener("DOMContentLoaded", () => ICU.icons());
