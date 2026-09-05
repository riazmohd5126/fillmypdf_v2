/** Shared clinic auth helpers. Include on UI pages: <script src="/ui/auth.js"></script>
 *
 * Clinic email/password sets an HttpOnly cookie. A leftover fmp_api_key in
 * localStorage must not outrank that session (otherwise the UI looks like admin).
 * Probe /auth/me with the cookie first; only send X-API-Key when there is no session.
 */
(function () {
  function getKey() {
    return localStorage.getItem("fmp_api_key") || "";
  }
  window.fmpGetKey = getKey;
  window.fmpUser = null;
  window.fmpAuthVia = null;

  window.fmpAuthHeaders = function () {
    const h = {};
    if (window.fmpAuthVia === "session") return h;
    const k = getKey();
    if (k) h["X-API-Key"] = k;
    return h;
  };

  window.fmpFetch = function (url, opts) {
    opts = opts || {};
    const headers = Object.assign({}, window.fmpAuthHeaders(), opts.headers || {});
    return fetch(url, Object.assign({}, opts, { headers: headers, credentials: "same-origin" }));
  };

  window.fmpAuthReady = (async function () {
    try {
      let r = await fetch("/api/v1/auth/me", { credentials: "same-origin" });
      if (r.ok) {
        const d = await r.json();
        window.fmpUser = d.user || null;
        window.fmpAuthVia = d.auth || "session";
      } else {
        const k = getKey();
        if (k) {
          r = await fetch("/api/v1/auth/me", {
            credentials: "same-origin",
            headers: { "X-API-Key": k },
          });
          if (r.ok) {
            const d = await r.json();
            window.fmpUser = d.user || null;
            window.fmpAuthVia = d.auth || "api_key";
          }
        }
      }
    } catch (e) {}
    document.dispatchEvent(new Event("fmp-auth-ready"));
    return window.fmpUser;
  })();

  window.fmpHasAuth = async function () {
    await window.fmpAuthReady;
    return !!(window.fmpUser);
  };

  window.fmpIsAdmin = function () {
    const u = window.fmpUser;
    return (
      String((u && u.tier) || "").toLowerCase() === "admin" ||
      String((u && u.role) || "").toLowerCase() === "admin"
    );
  };

  window.fmpRequireAdminPage = async function () {
    await window.fmpAuthReady;
    if (window.fmpIsAdmin()) return true;
    location.replace("/ui/index.html");
    return false;
  };

  window.fmpLogout = async function () {
    try {
      await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" });
    } catch (e) {}
    location.href = "/ui/login.html";
  };

  window.fmpPaintNavAuth = function (slotId) {
    const el = document.getElementById(slotId || "nav-auth");
    if (!el) return;
    const user = window.fmpUser;
    if (user && (user.email || user.clinic_name || user.name)) {
      el.innerHTML =
        '<span class="text-sm text-gray-600 truncate max-w-[12rem]" title="' +
        String(user.email || "").replace(/"/g, "") +
        '">' +
        (user.clinic_name || user.name || user.email) +
        '</span>' +
        '<button type="button" onclick="fmpLogout()" class="text-sm text-gray-500 hover:text-indigo-600">Sign out</button>';
    } else if (window.fmpAuthVia === "api_key" || getKey()) {
      el.innerHTML =
        '<span class="text-sm text-gray-500">API key</span>' +
        '<a href="/ui/login.html" class="text-sm text-indigo-600 hover:underline">Clinic login</a>';
    } else {
      el.innerHTML =
        '<a href="/ui/login.html" class="text-sm font-medium text-indigo-600 hover:underline">Sign in</a>';
    }
  };
})();
