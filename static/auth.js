// Shared login guard. Include in <head> with: <script src="/static/auth.js" data-role="owner"></script>
(function () {
  const allowed = (document.currentScript.dataset.role || "").split(",");
  const token = sessionStorage.getItem("munshi_token");
  const role = sessionStorage.getItem("munshi_role");
  if (!token || !(allowed.includes(role) || role === "admin")) {
    location.replace("/static/login.html?next=" + encodeURIComponent(location.pathname));
    throw new Error("login required");
  }
  // send the login token with every API call
  const realFetch = window.fetch.bind(window);
  window.fetch = async (url, opts = {}) => {
    if (String(url).startsWith("/api")) opts = { ...opts, headers: { ...(opts.headers || {}), "X-Token": token } };
    const r = await realFetch(url, opts);
    if (r.status === 401) { sessionStorage.clear(); location.replace("/static/login.html"); }
    return r;
  };
  window.MUNSHI = {
    token, role,
    // wss:// on an https site (live hosting), ws:// on localhost
    ws: channel => new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${channel}?token=${encodeURIComponent(token)}`),
    logout: async () => {
      await fetch("/api/logout", { method: "POST" }).catch(() => {});
      sessionStorage.clear(); location.replace("/static/login.html");
    },
  };
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.style.cssText = "position:absolute;top:8px;right:12px;font:12px sans-serif;z-index:50";
    bar.innerHTML = `<span style="background:#e2e8f0;color:#334155;padding:3px 8px;border-radius:999px">${role}</span>
      <a href="#" style="margin-left:6px;color:#64748b">Log out</a>`;
    bar.querySelector("a").onclick = e => { e.preventDefault(); MUNSHI.logout(); };
    document.body.appendChild(bar);
  });
})();
