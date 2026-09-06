/** Shared clinic sidebar. Include in <head> after auth.js: <script src="/ui/nav.js"></script>
 * White rail, grouped links, clinic name pinned to the bottom.
 * Workspace pages (Guided Fill, Mapping Review) use an icon-only collapsed rail.
 */
(function () {
  if (!document.querySelector('link[rel="icon"]')) {
    const svg = document.createElement("link");
    svg.rel = "icon";
    svg.type = "image/svg+xml";
    svg.href = "/ui/favicon.svg";
    document.head.appendChild(svg);
    const ico = document.createElement("link");
    ico.rel = "icon";
    ico.href = "/ui/favicon.ico";
    ico.sizes = "32x32";
    document.head.appendChild(ico);
  }
  const WORKSPACES = { "form_fill.html": 1, "mapping_review.html": 1 };
  const COLLAPSE_KEY = "fmp_nav_collapsed";
  const pageFile = (location.pathname.split("/").pop() || "index.html").split("?")[0];
  const isWorkspace = !!WORKSPACES[pageFile];

  function storedCollapsed() {
    try {
      const v = localStorage.getItem(COLLAPSE_KEY);
      if (v === "1") return true;
      if (v === "0") return false;
    } catch (e) {}
    return isWorkspace;
  }

  document.documentElement.classList.add("fmp-app");
  if (storedCollapsed()) document.documentElement.classList.add("fmp-nav-collapsed");

  function icon(d) {
    return (
      '<svg class="fmp-nav-ic" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
      '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="' + d + '"/></svg>'
    );
  }

  const I = {
    home: "M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6",
    fill: "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z",
    batch: "M4 6h16M4 10h16M4 14h10M4 18h10",
    templates:
      "M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10",
    profiles: "M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z",
    pdf: "M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4",
    approvals: "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z",
    sign: "M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z",
    jobs: "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2",
    extract: "M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4",
    mapping:
      "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4",
    lightning: "M13 10V3L4 14h7v7l9-11h-7z",
    key: "M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z",
    billing:
      "M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z",
    docs: "M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253",
    menu: "M4 6h16M4 12h16M4 18h16",
    close: "M6 18L18 6M6 6l12 12",
    chevronLeft: "M15 19l-7-7 7-7",
    chevronRight: "M9 5l7 7-7 7",
  };

  const GROUPS = [
    {
      label: null,
      items: [{ href: "/ui/index.html", label: "Dashboard", icon: I.home, files: ["index.html", ""] }],
    },
    {
      label: "Clinic fill",
      items: [
        { href: "/ui/form_fill.html", label: "Guided Fill", icon: I.fill },
        { href: "/ui/guided_batch.html", label: "Guided Batch", icon: I.batch },
        { href: "/ui/templates.html", label: "Templates", icon: I.templates, badgeId: "fmp-nav-tpl-badge" },
        { href: "/ui/profiles.html", label: "Profiles", icon: I.profiles },
      ],
    },
    {
      label: "Tools",
      items: [
        { href: "/ui/pdftools.html", label: "PDF Tools", icon: I.pdf },
        { href: "/ui/make_fillable.html", label: "Make Fillable", icon: I.fill },
        { href: "/ui/approvals.html", label: "Approvals", icon: I.approvals },
        { href: "/ui/sign.html", label: "E-Signature", icon: I.sign, files: ["sign.html", "multisign.html"] },
        { href: "/ui/jobs.html", label: "Fill History", icon: I.jobs },
        { href: "/ui/extract.html", label: "Extract Fields", icon: I.extract },
      ],
    },
    {
      label: "Admin",
      admin: true,
      items: [
        { href: "/ui/mapping_review.html", label: "Mapping Review", icon: I.mapping, badgeId: "fmp-nav-map-badge" },
        { href: "/ui/batch.html", label: "Ad-hoc Batch", icon: I.lightning },
        { href: "/ui/keys.html", label: "API Keys", icon: I.key },
      ],
    },
    {
      label: "Account",
      items: [
        { href: "/ui/billing.html", label: "Billing", icon: I.billing },
        { href: "/docs", label: "API Docs", icon: I.docs, admin: true, id: "fmp-nav-docs" },
      ],
    },
  ];

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function itemFile(href) {
    return (href.split("?")[0].split("/").pop() || "").toLowerCase();
  }

  function isActive(item) {
    const files = (item.files || [itemFile(item.href)]).map(function (f) {
      return String(f).toLowerCase();
    });
    const cur = (pageFile || "index.html").toLowerCase();
    if (files.indexOf(cur) >= 0) return true;
    if ((cur === "" || cur === "ui") && files.indexOf("index.html") >= 0) return true;
    return false;
  }

  const CSS = [
    ":root{--fmp-nav-w:240px;--fmp-nav-bg:#fff;--fmp-nav-bg2:#f9fafb;--fmp-nav-line:#e5e7eb;--fmp-nav-muted:#4b5563;--fmp-nav-dim:#9ca3af;--fmp-nav-text:#1f2937;--fmp-nav-accent:#4f46e5}",
    "html.fmp-nav-collapsed{--fmp-nav-w:56px}",
    "html.fmp-app body{padding-left:var(--fmp-nav-w);font-family:system-ui,-apple-system,sans-serif;overflow-x:hidden}",
    "#fmp-sidebar{position:fixed;inset:0 auto 0 0;width:var(--fmp-nav-w);background:var(--fmp-nav-bg);color:var(--fmp-nav-text);z-index:40;display:flex;flex-direction:column;overflow:visible;border-right:1px solid var(--fmp-nav-line)}",
    "@media(min-width:769px){#fmp-sidebar{transition:width .18s ease}html.fmp-app body{transition:padding-left .18s ease}}",
    ".fmp-nav-collapse{position:absolute;top:18px;right:-11px;width:22px;height:22px;border-radius:99px;background:#fff;border:1px solid #e5e7eb;color:#6b7280;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:42;padding:0}",
    ".fmp-nav-collapse:hover{background:#eef2ff;color:#4f46e5}",
    ".fmp-nav-collapse .fmp-nav-ic{width:14px;height:14px}",
    ".fmp-collapse-out{display:none}",
    "html.fmp-nav-collapsed .fmp-collapse-in{display:none}",
    "html.fmp-nav-collapsed .fmp-collapse-out{display:block}",
    "#fmp-sidebar *{box-sizing:border-box}",
    ".fmp-nav-brand{display:flex;align-items:center;gap:10px;padding:16px 14px 14px;text-decoration:none;color:#111827;flex-shrink:0;border-bottom:1px solid var(--fmp-nav-line)}",
    ".fmp-nav-logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;letter-spacing:-0.04em;flex-shrink:0}",
    ".fmp-nav-word{font-size:15px;font-weight:700;letter-spacing:-0.02em;white-space:nowrap}",
    ".fmp-nav-scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:10px 8px 12px}",
    ".fmp-nav-scroll::-webkit-scrollbar{width:6px}",
    ".fmp-nav-scroll::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:99px}",
    ".fmp-nav-group{margin:12px 0 4px;padding:0 8px;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--fmp-nav-dim);white-space:nowrap}",
    ".fmp-nav-link{display:flex;align-items:center;gap:10px;padding:8px 8px;margin:1px 0;border-radius:8px;color:var(--fmp-nav-muted);text-decoration:none;font-size:13.5px;font-weight:500;line-height:1.25;white-space:nowrap;border-left:2px solid transparent;position:relative}",
    ".fmp-nav-link:hover{background:#f9fafb;color:#111827}",
    ".fmp-nav-link.is-active{background:#eef2ff;color:#3730a3;border-left-color:var(--fmp-nav-accent)}",
    ".fmp-nav-ic{width:18px;height:18px;flex-shrink:0;opacity:.72}",
    ".fmp-nav-link.is-active .fmp-nav-ic,.fmp-nav-link:hover .fmp-nav-ic{opacity:1}",
    ".fmp-nav-label{flex:1;min-width:0}",
    ".fmp-badge{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 6px;margin-left:auto;border-radius:99px;font-size:10px;font-weight:700;letter-spacing:.02em;background:#ffedd5;color:#c2410c;border:1px solid #fdba74;line-height:1;flex-shrink:0}",
    ".fmp-badge.is-off{display:none !important}",
    ".fmp-fast a,.fmp-fast button{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;background:none;border:0;border-bottom:1px solid #f3f4f6;padding:11px 0;margin:0;text-align:left;font-size:13.5px;font-weight:500;color:#1f2937;text-decoration:none;cursor:pointer;font-family:inherit;line-height:1.3}",
    ".fmp-fast a:last-child,.fmp-fast button:last-child{border-bottom:0}",
    ".fmp-fast a:hover,.fmp-fast button:hover{color:#4f46e5}",
    ".fmp-fast button.is-off{color:#9ca3af;cursor:default}",
    ".fmp-fast button.is-off:hover{color:#9ca3af}",
    "html.fmp-nav-collapsed .fmp-nav-link .fmp-badge{position:absolute;top:2px;right:4px;min-width:14px;height:14px;font-size:8px;padding:0 3px;margin:0}",
    ".fmp-nav-footer{flex-shrink:0;border-top:1px solid var(--fmp-nav-line);padding:12px 12px 14px;background:#f9fafb}",
    "#fmp-nav-provider{display:none;font-size:10px;font-weight:600;padding:4px 8px;border-radius:999px;margin-bottom:10px;text-align:center;background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe}",
    "#fmp-nav-provider.is-local{color:#047857;border-color:#a7f3d0;background:#ecfdf5}",
    ".fmp-nav-clinic{font-size:13px;font-weight:600;color:#111827;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".fmp-nav-meta{display:flex;align-items:center;gap:6px;margin-top:2px;min-width:0}",
    ".fmp-nav-email{font-size:11px;color:var(--fmp-nav-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}",
    ".fmp-nav-tier{font-size:9px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:2px 6px;border-radius:999px;background:#eef2ff;color:#4338ca;flex-shrink:0}",
    ".fmp-nav-tier.tier-admin{background:#fee2e2;color:#b91c1c}",
    ".fmp-nav-tier.tier-pro{background:#dbeafe;color:#1d4ed8}",
    ".fmp-nav-tier.tier-business{background:#ede9fe;color:#6d28d9}",
    ".fmp-nav-actions{display:flex;align-items:center;gap:8px;margin-top:10px}",
    ".fmp-nav-actions button,.fmp-nav-actions a{background:none;border:0;padding:0;font-size:12px;color:var(--fmp-nav-muted);cursor:pointer;text-decoration:none;font-family:inherit}",
    ".fmp-nav-actions button:hover,.fmp-nav-actions a:hover{color:#4f46e5}",
    "#fmp-mobile-bar{display:none;position:fixed;top:0;left:0;right:0;height:48px;background:var(--fmp-nav-bg);color:#111827;z-index:45;align-items:center;gap:10px;padding:0 12px;border-bottom:1px solid var(--fmp-nav-line)}",
    "#fmp-mobile-bar .fmp-nav-word{font-size:14px}",
    "#fmp-nav-burger,#fmp-nav-close{background:none;border:0;color:#111827;padding:6px;cursor:pointer;display:flex}",
    "#fmp-nav-close{display:none;position:absolute;top:10px;right:8px;z-index:2}",
    "#fmp-nav-scrim{display:none;position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:35}",
    "html.fmp-nav-open #fmp-nav-scrim{display:block}",
    ".fmp-nav-admin{display:none}",
    "html.fmp-is-admin div.fmp-nav-admin{display:block}",
    "html.fmp-is-admin a.fmp-nav-admin{display:flex}",
    "html.fmp-is-admin .fmp-nav-group.fmp-nav-admin{display:block}",
    "html.fmp-nav-collapsed .fmp-nav-word,html.fmp-nav-collapsed .fmp-nav-group,html.fmp-nav-collapsed .fmp-nav-label,html.fmp-nav-collapsed .fmp-nav-clinic,html.fmp-nav-collapsed .fmp-nav-meta,html.fmp-nav-collapsed .fmp-nav-actions{display:none}",
    "html.fmp-nav-collapsed .fmp-nav-brand{justify-content:center;padding:14px 0}",
    "html.fmp-nav-collapsed .fmp-nav-link{justify-content:center;padding:9px 0;border-left-color:transparent}",
    "html.fmp-nav-collapsed .fmp-nav-link.is-active{background:#eef2ff}",
    ".fmp-how-new{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:16px 18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}",
    ".fmp-how-new h3{margin:0 0 4px;font-size:14px;font-weight:700;color:#111827}",
    ".fmp-how-new .fmp-how-lead{margin:0 0 10px;font-size:13px;color:#4b5563;line-height:1.45}",
    ".fmp-how-new .fmp-how-lead strong{color:#4338ca}",
    ".fmp-how-new ol{margin:0;padding-left:18px;color:#374151;font-size:13px;line-height:1.5}",
    ".fmp-how-new ol li{margin:0 0 6px}",
    ".fmp-how-new ol a{color:#4f46e5;font-weight:600;text-decoration:none}",
    ".fmp-how-new ol a:hover{text-decoration:underline}",
    ".fmp-how-new .fmp-how-admin{display:inline-block;margin-left:6px;font-size:10px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;background:#eef2ff;color:#4338ca;border-radius:999px;padding:2px 7px;vertical-align:middle}",
    ".fmp-nav-avatar{display:none;width:28px;height:28px;border-radius:8px;background:#eef2ff;color:#4338ca;font-size:12px;font-weight:700;align-items:center;justify-content:center}",
    "html.fmp-nav-collapsed .fmp-nav-footer{padding:10px 0;display:flex;justify-content:center}",
    "html.fmp-nav-collapsed #fmp-nav-provider{display:none !important}",
    "html.fmp-nav-collapsed .fmp-nav-avatar{display:flex}",
    "@media(max-width:768px){",
    "html.fmp-app body{padding-left:0;padding-top:48px}",
    "#fmp-mobile-bar{display:flex}",
    ".fmp-nav-collapse{display:none}",
    "#fmp-sidebar{transform:translateX(-105%);transition:transform .18s ease;width:240px !important}",
    "html.fmp-nav-open #fmp-sidebar{transform:translateX(0);z-index:50}",
    "html.fmp-nav-open #fmp-nav-close{display:flex}",
    "html.fmp-nav-collapsed .fmp-nav-word,html.fmp-nav-collapsed .fmp-nav-label{display:inline}",
    "html.fmp-nav-collapsed .fmp-nav-group,html.fmp-nav-collapsed .fmp-nav-clinic{display:block}",
    "html.fmp-nav-collapsed .fmp-nav-meta,html.fmp-nav-collapsed .fmp-nav-actions{display:flex}",
    "html.fmp-nav-collapsed .fmp-nav-brand,html.fmp-nav-collapsed .fmp-nav-link,html.fmp-nav-collapsed .fmp-nav-footer{justify-content:flex-start;padding-left:14px}",
    "html.fmp-nav-collapsed .fmp-nav-link{padding:7px 8px;margin:1px 8px}",
    "html.fmp-nav-collapsed .fmp-nav-avatar{display:none}",
    "html.fmp-nav-open #fmp-nav-provider.is-on{display:block !important}",
    "}",
  ].join("\n");

  function injectCss() {
    if (document.getElementById("fmp-nav-css")) return;
    const s = document.createElement("style");
    s.id = "fmp-nav-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function linkHtml(item) {
    const adminCls = item.admin ? " fmp-nav-admin" : "";
    const active = isActive(item) ? " is-active" : "";
    const id = item.id ? ' id="' + item.id + '"' : "";
    const badge = item.badgeId
      ? '<span class="fmp-badge is-off" id="' + item.badgeId + '"></span>'
      : "";
    return (
      '<a href="' +
      item.href +
      '"' +
      id +
      ' class="fmp-nav-link' +
      active +
      adminCls +
      '" title="' +
      esc(item.label) +
      '">' +
      icon(item.icon) +
      '<span class="fmp-nav-label">' +
      esc(item.label) +
      "</span>" +
      badge +
      "</a>"
    );
  }

  function groupsHtml() {
    return GROUPS.map(function (g) {
      const wrapCls = g.admin ? " fmp-nav-admin" : "";
      const head = g.label ? '<div class="fmp-nav-group' + wrapCls + '">' + esc(g.label) + "</div>" : "";
      const wrapStart = g.admin ? '<div class="fmp-nav-admin">' : "";
      const wrapEnd = g.admin ? "</div>" : "";
      return wrapStart + head + g.items.map(linkHtml).join("") + wrapEnd;
    }).join("");
  }

  function markup() {
    return (
      '<div id="fmp-nav-scrim" aria-hidden="true"></div>' +
      '<header id="fmp-mobile-bar">' +
      '<button type="button" id="fmp-nav-burger" aria-label="Open menu">' +
      icon(I.menu) +
      "</button>" +
      '<span class="fmp-nav-logo">F</span>' +
      '<span class="fmp-nav-word">FillMyPDF</span></header>' +
      '<aside id="fmp-sidebar" aria-label="Main">' +
      '<button type="button" class="fmp-nav-collapse" id="fmp-nav-collapse" aria-label="Collapse sidebar" title="Collapse sidebar">' +
      '<span class="fmp-collapse-in">' + icon(I.chevronLeft) + "</span>" +
      '<span class="fmp-collapse-out">' + icon(I.chevronRight) + "</span>" +
      "</button>" +
      '<button type="button" id="fmp-nav-close" aria-label="Close menu">' +
      icon(I.close) +
      "</button>" +
      '<a class="fmp-nav-brand" href="/ui/index.html" title="Dashboard">' +
      '<span class="fmp-nav-logo">F</span>' +
      '<span class="fmp-nav-word">FillMyPDF</span></a>' +
      '<nav class="fmp-nav-scroll">' +
      groupsHtml() +
      "</nav>" +
      '<div class="fmp-nav-footer">' +
      '<div id="fmp-nav-provider"></div>' +
      '<div class="fmp-nav-avatar" id="fmp-nav-avatar" title="Account">F</div>' +
      '<div class="fmp-nav-clinic" id="fmp-nav-clinic">Not signed in</div>' +
      '<div class="fmp-nav-meta" id="fmp-nav-meta"></div>' +
      '<div class="fmp-nav-actions" id="fmp-nav-actions"></div>' +
      "</div></aside>"
    );
  }

  function setCollapsed(collapsed) {
    document.documentElement.classList.toggle("fmp-nav-collapsed", !!collapsed);
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch (e) {}
    syncCollapseBtn();
  }

  function isCollapsed() {
    return document.documentElement.classList.contains("fmp-nav-collapsed");
  }

  function syncCollapseBtn() {
    const btn = document.getElementById("fmp-nav-collapse");
    if (!btn) return;
    const collapsed = isCollapsed();
    btn.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    btn.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  function setOpen(open) {
    document.documentElement.classList.toggle("fmp-nav-open", !!open);
  }

  function bind() {
    const burger = document.getElementById("fmp-nav-burger");
    const close = document.getElementById("fmp-nav-close");
    const scrim = document.getElementById("fmp-nav-scrim");
    const collapse = document.getElementById("fmp-nav-collapse");
    if (burger) burger.addEventListener("click", function () { setOpen(true); });
    if (close) close.addEventListener("click", function () { setOpen(false); });
    if (scrim) scrim.addEventListener("click", function () { setOpen(false); });
    if (collapse) {
      collapse.addEventListener("click", function (e) {
        e.preventDefault();
        setCollapsed(!isCollapsed());
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setOpen(false);
      if (e.key === "[" && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "")) {
        setCollapsed(!isCollapsed());
      }
    });
    syncCollapseBtn();
    const docs = document.getElementById("fmp-nav-docs");
    if (docs) {
      docs.addEventListener("click", async function (e) {
        e.preventDefault();
        try {
          const r = await fetch("/docs", {
            headers: window.fmpAuthHeaders ? fmpAuthHeaders() : {},
            credentials: "same-origin",
          });
          if (!r.ok) {
            alert("Admin credentials required for API docs.");
            return;
          }
          const html = await r.text();
          const w = window.open("", "_blank");
          if (!w) return;
          w.document.write(html);
          w.document.close();
        } catch (err) {
          alert("Could not open API docs.");
        }
      });
    }
  }

  function paintFooter() {
    const clinicEl = document.getElementById("fmp-nav-clinic");
    const metaEl = document.getElementById("fmp-nav-meta");
    const actionsEl = document.getElementById("fmp-nav-actions");
    const avatarEl = document.getElementById("fmp-nav-avatar");
    if (!clinicEl) return;

    const user = window.fmpUser;
    const admin = window.fmpIsAdmin ? fmpIsAdmin() : false;
    document.documentElement.classList.toggle("fmp-is-admin", admin);
    if (admin) {
      document.querySelectorAll(".admin-only").forEach(function (el) {
        el.classList.remove("hidden");
      });
    }

    if (user && (user.email || user.clinic_name || user.name)) {
      const clinic = user.clinic_name || user.name || user.email || "Clinic";
      const email = user.email || "";
      const tier = String(user.tier || user.role || "pro").toLowerCase();
      clinicEl.textContent = clinic;
      clinicEl.title = clinic;
      if (avatarEl) {
        avatarEl.textContent = (clinic.replace(/[^A-Za-z0-9]/g, "") || "C").charAt(0).toUpperCase();
        avatarEl.title = clinic + (email ? " · " + email : "");
      }
      metaEl.innerHTML =
        '<span class="fmp-nav-email" title="' +
        esc(email) +
        '">' +
        esc(email) +
        '</span><span class="fmp-nav-tier tier-' +
        esc(tier) +
        '">' +
        esc(tier) +
        "</span>";
      actionsEl.innerHTML =
        '<button type="button" onclick="fmpLogout()">Sign out</button>';
    } else if (window.fmpAuthVia === "api_key" || (window.fmpGetKey && fmpGetKey())) {
      clinicEl.textContent = "API key";
      if (avatarEl) {
        avatarEl.textContent = "K";
        avatarEl.title = "Signed in with API key";
      }
      metaEl.innerHTML = '<span class="fmp-nav-email">Integration auth</span>';
      const hasModal = typeof window.showKeyModal === "function";
      actionsEl.innerHTML =
        (hasModal ? '<button type="button" onclick="showKeyModal()">Change key</button>' : "") +
        '<a href="/ui/login.html">Clinic login</a>';
    } else {
      clinicEl.textContent = "Not signed in";
      if (avatarEl) {
        avatarEl.textContent = "?";
        avatarEl.title = "Not signed in";
      }
      metaEl.innerHTML = "";
      actionsEl.innerHTML = '<a href="/ui/login.html">Sign in</a>';
    }
  }

  function setNeedsMappingBadge(n) {
    const count = Math.max(0, Number(n) || 0);
    window.fmpNeedsMapping = count;
    const admin = window.fmpIsAdmin && fmpIsAdmin();
    document.querySelectorAll("[data-fast-badge='needs']").forEach(function (el) {
      el.textContent = String(count);
      el.classList.toggle("is-off", count < 1);
    });
    const map = document.getElementById("fmp-nav-map-badge");
    if (map) {
      map.textContent = String(count);
      map.classList.toggle("is-off", count < 1);
    }
    const tpl = document.getElementById("fmp-nav-tpl-badge");
    if (tpl) {
      tpl.textContent = String(count);
      tpl.classList.toggle("is-off", admin || count < 1);
    }
  }

  // Readiness is the most expensive read in the app (it walks the whole
  // template library), and the sidebar plus the host page both want it. Share
  // one in-flight request so a page load pays for it once.
  const READINESS_TTL_MS = 20000;
  let readinessCache = null;

  function readiness(opts) {
    const now = Date.now();
    if (!(opts && opts.force) && readinessCache && now - readinessCache.at < READINESS_TTL_MS) {
      return readinessCache.promise;
    }
    const promise = fetch("/api/v1/templates/readiness", {
      headers: window.fmpAuthHeaders ? fmpAuthHeaders() : {},
      credentials: "same-origin",
    })
      .then(function (r) {
        if (!r.ok) throw new Error("readiness " + r.status);
        return r.json();
      })
      .catch(function () {
        readinessCache = null;  // never pin a failure — let the next caller retry
        return { items: [], ready_count: 0, total: 0 };
      });
    readinessCache = { at: now, promise: promise };
    return promise;
  }

  async function loadNavBadges() {
    const d = await readiness();
    const ready = d.ready_count || 0;
    const total = d.total || (d.items || []).length;
    if (!total && !ready) return;
    setNeedsMappingBadge(Math.max(0, total - ready));
  }

  async function loadProviderBadge() {
    if (!(window.fmpIsAdmin && fmpIsAdmin())) return;
    const badge = document.getElementById("fmp-nav-provider");
    if (!badge) return;
    try {
      const r = await fetch("/ai-provider");
      if (!r.ok) return;
      const d = await r.json();
      if (d.ai_provider === "local") {
        badge.textContent = d.hipaa_mode ? "Local · HIPAA" : "Local Qwen";
        badge.classList.add("is-local");
        badge.title = d.hipaa_mode
          ? "HIPAA Mode: all LLM calls are on-prem."
          : "Local model: " + (d.local_model || "");
      } else {
        badge.textContent = "Gemini Cloud";
        badge.title = "Cloud mode: mapping may be sent to the configured AI provider.";
      }
      badge.style.display = "block";
      badge.classList.add("is-on");
    } catch (e) {}
  }

  function renderHowNewPdf(host) {
    if (!host || host.dataset.ready) return;
    host.dataset.ready = "1";
    host.classList.add("fmp-how-new");
    host.innerHTML =
      "<h3>How a new PDF becomes Ready</h3>" +
      "<p class=\"fmp-how-lead\">Locked mapping is done by an <strong>admin</strong> — clinics upload, they do not lock.</p>" +
      "<ol>" +
      "<li><a href=\"/ui/make_fillable.html\">Make Fillable</a> if the PDF is a flat scan (no clickable fields).</li>" +
      "<li><a href=\"/ui/templates.html\">Templates → Upload</a> the blank form. It stays private to your clinic. That is the mapping request.</li>" +
      "<li>An admin locks the mapping in Mapping Review <span class=\"fmp-how-admin\">Admin only</span></li>" +
      "<li>The form shows <strong>Ready</strong>. Open <a href=\"/ui/form_fill.html\">Guided Fill</a> — autofill uses the locked map only (no AI on clinical fields).</li>" +
      "</ol>";
  }

  function paintHowNewPdf() {
    document.querySelectorAll("[data-fmp-how-new-pdf]").forEach(renderHowNewPdf);
  }

  function mount() {
    injectCss();
    paintHowNewPdf();
    if (document.getElementById("fmp-sidebar")) return;
    document.body.insertAdjacentHTML("afterbegin", markup());
    bind();
    paintFooter();
    if (window.fmpAuthReady) {
      Promise.resolve(window.fmpAuthReady).then(function () {
        paintFooter();
        loadProviderBadge();
        loadNavBadges();
      });
    }
    document.addEventListener("fmp-auth-ready", function () {
      paintFooter();
      loadProviderBadge();
      loadNavBadges();
    });
  }

  injectCss();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  const prevPaint = window.fmpPaintNavAuth;
  window.fmpPaintNavAuth = function (slotId) {
    if (typeof prevPaint === "function") prevPaint(slotId);
    paintFooter();
  };
  window.fmpPaintSidebar = paintFooter;
  window.fmpToggleNav = function () { setCollapsed(!isCollapsed()); };
  window.fmpSetNeedsMapping = setNeedsMappingBadge;
  window.fmpReadiness = readiness;
  window.fmpReadinessBust = function () { readinessCache = null; };
  window.fmpRenderHowNewPdf = renderHowNewPdf;
})();
