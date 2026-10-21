/* ICU Monitoring System — authenticated app shell (sidebar, nav, theme, roles). */
"use strict";

const ICUShell = (() => {
  const NAV = [
    { href: "/overview.html", label: "Overview", icon: "layout-dashboard", roles: ["admin", "doctor", "patient"] },
    { href: "/patient_monitor.html", label: "Live Monitoring", icon: "activity", roles: ["admin", "doctor", "patient"] },
    { href: "/doctor_dashboard.html", label: "Patients", icon: "users", roles: ["admin", "doctor"] },
    { href: "/doctors.html", label: "Doctors", icon: "stethoscope", roles: ["admin"] },
    { href: "/devices.html", label: "Devices", icon: "cpu", roles: ["admin", "doctor"] },
    { href: "/risk.html", label: "AI Risk Analysis", icon: "brain-circuit", roles: ["admin", "doctor", "patient"] },
    { href: "/alerts.html", label: "Alerts & Events", icon: "siren", roles: ["admin", "doctor", "patient"], badge: "activeAlerts" },
    { href: "/analytics.html", label: "Analytics & Reports", icon: "bar-chart-3", roles: ["admin", "doctor", "patient"] },
    { href: "/admin.html", label: "Admin & Audit", icon: "shield-check", roles: ["admin"] },
    { href: "/settings.html", label: "Settings", icon: "settings", roles: ["admin", "doctor", "patient"] },
  ];

  let state = { user: null, active: "", alertCount: 0 };

  function theme() {
    const saved = localStorage.getItem("icu_theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    else document.documentElement.removeAttribute("data-theme");
    return saved || "light";
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : null;
    const next = current ? "light" : "dark";
    if (next === "light") localStorage.removeItem("icu_theme");
    else localStorage.setItem("icu_theme", "dark");
    theme();
    window.dispatchEvent(new CustomEvent("icu-theme-change", { detail: next }));
  }

  function renderSidebar() {
    const user = state.user;
    const items = NAV.filter((n) => n.roles.includes(user.role));
    const navHtml = items.map((n) => `
      <a href="${n.href}" class="${state.active === n.href ? "active" : ""}" data-nav="${n.href}">
        <i data-lucide="${n.icon}"></i>
        <span>${n.label}</span>
        ${n.badge ? `<span class="badge-count hidden" id="nav-badge-${n.badge}">0</span>` : ""}
      </a>`).join("");

    const roleLabel = user.role.charAt(0).toUpperCase() + user.role.slice(1);
    return `
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <div class="logo"><i data-lucide="heart-pulse"></i></div>
          <div>
            <div class="brand-title">ICU Sentinel</div>
            <div class="brand-sub">AI Early Warning</div>
          </div>
        </div>
        <nav class="nav">
          <div class="nav-label">Monitoring</div>
          ${navHtml}
        </nav>
        <div class="user-box">
          <div class="name">${ICU.esc(user.full_name)}</div>
          <div class="role-pill">${roleLabel}</div>
          <button type="button" id="icu-logout"><i data-lucide="log-out"></i> Logout</button>
        </div>
      </aside>
      <div class="sidebar-backdrop" id="sidebar-backdrop"></div>`;
  }

  async function init({ title, active, roles, render = true }) {
    theme();
    let me;
    try {
      me = await ICU.me();
    } catch (e) {
      if (e && e.status === 401) return null;
      throw e;
    }
    const user = me.user;
    if (roles && !roles.includes(user.role)) {
      ICU.toast("You do not have access to this page.", "error");
      const fallback = user.role === "admin" ? "/overview.html" : user.role === "doctor" ? "/overview.html" : "/patient_dashboard.html";
      window.location.replace(fallback);
      return null;
    }
    // Patients get their own dashboard as their workspace
    if (user.role === "patient" && active === "/doctor_dashboard.html") {
      window.location.replace("/patient_dashboard.html");
      return null;
    }
    state.user = user;
    state.active = active || "";

    if (render) {
      document.body.insertAdjacentHTML("afterbegin", renderSidebar());
      const main = document.getElementById("main");
      if (main) main.classList.add("main");
      const topbar = document.getElementById("topbar-title");
      if (topbar && title) topbar.textContent = title;
    }

    document.getElementById("icu-logout")?.addEventListener("click", () => ICU.logout());
    document.getElementById("icu-theme")?.addEventListener("click", toggleTheme);
    document.getElementById("hamburger")?.addEventListener("click", () => document.body.classList.toggle("nav-open"));
    document.getElementById("sidebar-backdrop")?.addEventListener("click", () => document.body.classList.remove("nav-open"));

    ICU.icons();
    refreshAlertBadge();
    if (state.user.role !== "patient") {
      setInterval(refreshAlertBadge, 20000);
    } else {
      setInterval(refreshAlertBadge, 20000);
    }
    return me;
  }

  async function refreshAlertBadge() {
    try {
      const data = await ICU.get("/alerts", { status: "active", limit: 200 });
      const alerts = data.alerts || [];
      const critical = alerts.filter((a) => a.severity === "critical").length;
      state.alertCount = alerts.length;
      document.querySelectorAll("#nav-badge-activeAlerts").forEach((el) => {
        el.textContent = alerts.length;
        el.classList.toggle("hidden", alerts.length === 0);
        if (critical > 0) el.style.background = "#dc2626";
      });
      window.dispatchEvent(new CustomEvent("icu-alerts-updated", { detail: { alerts, critical } }));
    } catch { /* non-fatal */ }
  }

  return {
    init, toggleTheme, refreshAlertBadge,
    get user() { return state.user; },
    get alertCount() { return state.alertCount; },
  };
})();
