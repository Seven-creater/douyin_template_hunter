"""持久化 CDP Edge 会话。

与 share_link.py 的区别:
- 可见窗口(首次扫码登录后,登录态存独立 profile 长期复用)
- 独立 user-data-dir,不碰用户日常 Edge;退出只杀自己拉起的进程
- 一条 websocket 长连接,支持导航、JS 执行、网络事件缓冲、响应体抓取
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from collections import deque
from pathlib import Path

import websocket

logger = logging.getLogger(__name__)

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DEFAULT_PROFILE = os.path.join(os.environ.get("TEMP") or "/tmp", "edge_dy_hunter")


class CdpError(RuntimeError):
    pass


class CdpBrowser:
    def __init__(self, port: int = 19223, headless: bool = False,
                 profile: str | None = None):
        self.port = port
        self.headless = headless
        self.profile = profile or DEFAULT_PROFILE
        self._proc: subprocess.Popen | None = None
        self._ws: websocket.WebSocket | None = None
        self._id = 0
        self._events: deque[dict] = deque()

    # ---------- 生命周期 ----------
    def _http(self, path: str, method: str = "GET"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def alive(self) -> bool:
        try:
            self._http("/json/version")
            return True
        except Exception:
            return False

    def start(self) -> None:
        """启动(或复用)带调试端口的 Edge。"""
        if self.alive():
            logger.info("复用运行中的调试 Edge (port %d)", self.port)
            return
        edge = next((p for p in EDGE_PATHS if Path(p).exists()), None) \
            or shutil.which("msedge")
        if not edge:
            raise CdpError("找不到 msedge.exe")
        args = [edge,
                f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check",
                "--disable-gpu", "--disable-features=msEdgeOOBE",
                f"--user-data-dir={self.profile}"]
        # 内置内容脚本扩展(等价油猴:页面上下文拦截接口数据)
        ext = Path(__file__).resolve().parent / "ext" / "hunter-inject"
        if (ext / "manifest.json").exists():
            args.append(f"--load-extension={ext}")
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        logger.info("启动 Edge: %s (profile=%s, headless=%s)",
                    edge, self.profile, self.headless)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        for _ in range(40):
            if self.alive():
                return
            time.sleep(0.5)
        raise CdpError(f"Edge 调试端口 {self.port} 未就绪")

    def _page_ws_url(self) -> str:
        tabs = [t for t in self._http("/json/list") if t.get("type") == "page"]
        if not tabs:
            tabs = [self._http("/json/new?about:blank", method="PUT")]
        return tabs[0]["webSocketDebuggerUrl"]

    def connect(self) -> None:
        if self._ws:
            return
        self.start()
        url = self._page_ws_url()
        self._ws = websocket.create_connection(url, timeout=5,
                                               suppress_origin=True)
        self.call("Network.enable")
        self.call("Page.enable")
        logger.info("CDP 已连接")

    def stop(self) -> None:
        """关 websocket;只终止自己拉起的 Edge(复用的不动)。"""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def __enter__(self) -> "CdpBrowser":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- CDP 命令 ----------
    def call(self, method: str, params: dict | None = None,
             timeout: float = 20.0, _retry: bool = True) -> dict:
        if not self._ws:
            self.connect()
        self._id += 1
        mid = self._id
        try:
            self._ws.send(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
        except (ConnectionError, OSError,
                websocket.WebSocketConnectionClosedException):
            if not _retry:
                raise CdpError(f"{method}: 连接已断开")
            return self._reconnect_and_retry(method, params, timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (ConnectionError, OSError,
                    websocket.WebSocketConnectionClosedException):
                if not _retry:
                    raise CdpError(f"{method}: 连接中断")
                return self._reconnect_and_retry(method, params, timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._events.append(msg)
        raise CdpError(f"{method} 超时")

    def _reconnect_and_retry(self, method: str, params: dict | None,
                             timeout: float) -> dict:
        logger.warning("CDP 连接断开,尝试重连...")
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._events.clear()
        time.sleep(2)
        self.connect()  # 会自动拉起/复用 Edge
        return self.call(method, params, timeout, _retry=False)

    def pump(self, duration: float) -> None:
        """阻塞读取 duration 秒的网络事件进缓冲区。"""
        if not self._ws:
            raise CdpError("未连接")
        deadline = time.time() + duration
        self._ws.settimeout(0.5)
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            try:
                self._events.append(json.loads(raw))
            except ValueError:
                pass

    def take_events(self) -> list[dict]:
        ev, self._events = list(self._events), deque()
        return ev

    def clear_events(self) -> None:
        self._events.clear()

    # ---------- 页面操作 ----------
    def navigate(self, url: str, settle: float = 4.0) -> None:
        self.clear_events()
        self.call("Page.navigate", {"url": url})
        time.sleep(settle)

    def eval_js(self, expr: str, await_promise: bool = False):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout=60)
        if r.get("exceptionDetails"):
            raise CdpError(str(r.get("exceptionDetails"))[:300])
        return r.get("result", {}).get("value")

    def scroll(self, times: int = 4, pause: float = 1.2) -> None:
        for _ in range(times):
            try:
                self.eval_js("window.scrollTo(0, document.body.scrollHeight)")
            except CdpError:
                pass
            time.sleep(pause)

    def page_text(self) -> str:
        try:
            return self.eval_js("document.body ? document.body.innerText : ''") or ""
        except CdpError:
            return ""

    # ---------- 响应体 ----------
    def body(self, request_id: str, timeout: float = 4.0) -> bytes | None:
        try:
            r = self.call("Network.getResponseBody",
                          {"requestId": request_id}, timeout=timeout)
        except CdpError:
            return None
        if r.get("base64Encoded"):
            return base64.b64decode(r.get("body", ""))
        return str(r.get("body", "")).encode("utf-8")


DEFAULT_PROFILE_B = os.path.join(os.environ.get("TEMP") or "/tmp",
                                 "edge_dy_hunter_b")


class BrowserPool:
    """双账号浏览器池:A 号搜索被限流时自动切 B 号,交替使用。

    两个实例各自独立 profile/端口,互不影响登录态;下载与爬行
    用当前活跃实例(它们不受搜索限流影响)。
    """

    def __init__(self, port: int = 19223, port_b: int | None = None,
                 profile: str | None = None, profile_b: str | None = None):
        port_b = port_b or port + 1
        self._slots = [
            CdpBrowser(port=port, profile=profile),
            CdpBrowser(port=port_b, profile=profile_b or DEFAULT_PROFILE_B),
        ]
        self._started: list[CdpBrowser] = []
        self._i = 0

    @property
    def current(self) -> CdpBrowser:
        return self._slots[self._i]

    def warm(self) -> CdpBrowser:
        b = self._slots[self._i]
        b.connect()
        if b not in self._started:
            self._started.append(b)
        return b

    def rotate(self) -> CdpBrowser:
        """切换到另一个账号实例(未启动则拉起)。"""
        self._i = (self._i + 1) % len(self._slots)
        return self.warm()

    def stop(self) -> None:
        for b in self._started:
            try:
                b.stop()
            except Exception:
                pass
