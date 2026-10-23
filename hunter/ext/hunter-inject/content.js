// hunter-inject: 在抖音页面主世界拦截 aweme 接口响应,存入 window.__hunterData
// (与油猴脚本同机制:页面上下文、零额外请求、请求由抖音自己的前端发出)
(function () {
  if (window.__hunterHooked) return;
  window.__hunterHooked = true;
  window.__hunterData = [];

  function push(url, body) {
    try {
      window.__hunterData.push({
        url: String(url),
        t: Date.now(),
        body: String(body).slice(0, 3000000),
      });
      if (window.__hunterData.length > 40) window.__hunterData.shift();
    } catch (e) {}
  }

  // fetch 拦截
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
      if (url.indexOf("/aweme/v1/web/") !== -1) {
        resp.clone().text().then((t) => push(url, t)).catch(() => {});
      }
    } catch (e) {}
    return resp;
  };

  // XHR 拦截
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__hUrl = url;
    return origOpen.apply(this, arguments);
  };
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    const u = this.__hUrl;
    if (u && String(u).indexOf("/aweme/v1/web/") !== -1) {
      this.addEventListener("load", () => {
        try { push(u, this.responseText); } catch (e) {}
      });
    }
    return origSend.apply(this, arguments);
  };
})();
