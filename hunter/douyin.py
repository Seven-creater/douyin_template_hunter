"""抖音网页版搜索:驱动 CDP 打开搜索页,截获搜索接口 JSON。

两级策略:
1. 首选:截获 /aweme/v1/web/search/item/ 响应体(结构化:标题/作者/BGM/直链)
2. 兜底:解析 DOM 里的 /video/<id> 链接(接口路径变化时仍可用)
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse

from .browser import CdpBrowser

logger = logging.getLogger(__name__)

SEARCH_API = "/aweme/v1/web/search/item/"


def search_url(query: str) -> str:
    kw = urllib.parse.quote(query)
    return (f"https://www.douyin.com/search/{kw}"
            "?type=video&publish_time=0&sort_type=0&source=normal")


# ---------- 接口 JSON 解析 ----------
def _walk_awemes(node, out: list) -> None:
    if isinstance(node, dict):
        if "aweme_id" in node and ("desc" in node or "video" in node):
            out.append(node)
            return
        for v in node.values():
            _walk_awemes(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_awemes(v, out)


def _to_record(a: dict) -> dict | None:
    aweme_id = str(a.get("aweme_id") or "")
    if not aweme_id.isdigit() or len(aweme_id) < 10:
        return None
    video = a.get("video") or {}
    play = None
    for u in (video.get("play_addr") or {}).get("url_list") or []:
        if u:
            play = u
            break
    music = a.get("music") or {}
    stats = a.get("statistics") or {}
    author = a.get("author") or {}
    return {
        "aweme_id": aweme_id,
        "title": (a.get("desc") or "").strip(),
        "author": author.get("nickname"),
        "music": (music.get("title") or "").strip() or None,
        "music_id": (str(music.get("id")) if music.get("id") else None),
        "duration_ms": a.get("duration") or video.get("duration"),
        "likes": stats.get("digg_count"),
        "is_images": bool(a.get("images")),
        "play_url": play,
        "page_url": f"https://www.douyin.com/video/{aweme_id}",
    }


def _parse_search_events(b: CdpBrowser, max_bodies: int = 6) -> list[dict]:
    """从已缓冲的网络事件里提取搜索接口响应 → 记录列表。

    只抓最近 max_bodies 个搜索响应体(后面的批次数据最全),
    防止大量失效响应体逐个超时把整个解析拖死。
    """
    request_ids: list[str] = []
    n_api = 0
    for ev in b.take_events():
        try:
            url = ev["params"]["response"]["url"]
        except (KeyError, TypeError):
            continue
        if SEARCH_API in url:
            n_api += 1
            request_ids.append(ev["params"]["requestId"])
    logger.info("缓冲事件解析:搜索接口命中 %d 次", n_api)
    awemes: list[dict] = []
    for rid in request_ids[-max_bodies:]:
        body = b.body(rid)
        if not body:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        _walk_awemes(payload, awemes)
    records: list[dict] = []
    seen: set[str] = set()
    for a in awemes:
        if not isinstance(a, dict):
            continue
        rec = _to_record(a)
        if rec and rec["aweme_id"] not in seen:
            seen.add(rec["aweme_id"])
            records.append(rec)
    return records


# ---------- DOM 兜底 ----------
def _dom_items(b: CdpBrowser) -> list[dict]:
    expr = r"""
    (() => {
      const out = [];
      document.querySelectorAll('a[href*="/video/"]').forEach(a => {
        const m = a.href.match(/\/video\/(\d{6,})/);
        if (!m) return;
        const card = a.closest('li') || a;
        out.push({id: m[1], text: (card.innerText || '').slice(0, 120)});
      });
      return JSON.stringify(out);
    })()
    """
    try:
        raw = b.eval_js(expr) or "[]"
    except Exception:
        return []
    records, seen = [], set()
    for it in json.loads(raw):
        if not isinstance(it, dict):
            continue
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        text = (it.get("text") or "").replace("\n", " ").strip()
        records.append({
            "aweme_id": it["id"],
            "title": text[:80],
            "author": None, "music": None, "duration_ms": None, "likes": None,
            "is_images": False, "play_url": None,
            "page_url": f"https://www.douyin.com/video/{it['id']}",
        })
    return records


def _login_wall(b: CdpBrowser) -> bool:
    text = b.page_text()
    return ("扫码登录" in text) or ("登录后即可" in text)


# ---------- SPA 式搜索(真人式:输入框+回车,前端路由,不发整页导航) ----------
_INPUT_JS = """
(() => {
  const input = document.querySelector('input[data-e2e="searchbar-input"]')
         || document.querySelector('#douyin-header input[type="text"]')
         || document.querySelector('header input');
  if (!input) return 'NO_INPUT';
  input.focus();
  const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, %s);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  return 'OK';
})()
"""

_ENTER_JS = """
(() => {
  const btn = document.querySelector('button[data-e2e="searchbar-button"]');
  if (btn) { btn.click(); return 'OK'; }
  const input = document.querySelector('input[data-e2e="searchbar-input"]')
         || document.querySelector('header input');
  if (!input) return 'NO_INPUT';
  input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return 'OK_FALLBACK_ENTER';
})()
"""


def spa_search(b: CdpBrowser, query: str, settle: float = 7.0) -> list[dict] | None:
    """SPA 搜索:填输入框+回车(前端路由跳转),读 hunter-inject
    扩展拦到的接口数据。返回记录列表;输入框找不到返回 None(回退导航式)。
    """
    try:
        url = b.eval_js("location.href") or ""
    except Exception:
        url = ""
    if "douyin.com" not in url:
        b.navigate("https://www.douyin.com/", settle=7)
    r = b.eval_js(_INPUT_JS % json.dumps(query))
    if r != "OK":
        logger.info("[SPA] 找不到搜索输入框,回退导航式")
        return None
    b.eval_js("window.__hunterData = []")
    r2 = b.eval_js(_ENTER_JS)
    if r2 != "OK":
        return None
    time.sleep(3)
    # 快速判定:路由是否已切到 /search/;没切说明点击没生效,立刻回退
    try:
        href = b.eval_js("location.href") or ""
    except Exception:
        href = ""
    if "/search/" not in href:
        logger.info("[SPA] 点击后路由未变化,回退导航式")
        return None
    time.sleep(settle - 3 if settle > 3 else 2)
    b.scroll(times=2, pause=1.5)
    b.pump(1.5)
    try:
        raw = b.eval_js("JSON.stringify(window.__hunterData || [])") or "[]"
    except Exception:
        raw = "[]"
    awemes: list[dict] = []
    n_api = 0
    for rec in json.loads(raw):
        if SEARCH_API not in rec.get("url", ""):
            continue
        n_api += 1
        try:
            _walk_awemes(json.loads(rec["body"]), awemes)
        except ValueError:
            continue
    logger.info("[SPA] %r:接口响应 %d 份,aweme %d 条", query, n_api, len(awemes))
    records, seen = [], set()
    for a in awemes:
        rec = _to_record(a)
        if rec and rec["aweme_id"] not in seen:
            seen.add(rec["aweme_id"])
            records.append(rec)
    return records


def search(b: CdpBrowser, query: str, *, want: int = 15,
           login_wait: float = 900.0) -> list[dict]:
    """搜索一个关键词,返回去重后的记录列表(按搜索排名序)。

    空结果(未登录/限流)时指数退避:连续空结果说明被风控,
    快速重试只会加重,退避并限制尝试次数。
    优先 SPA 式(输入框+回车,零整页导航);失败回退导航式。
    """
    # ① SPA 式优先
    try:
        spa = spa_search(b, query)
        if spa:
            logger.info("搜索 %r: %d 条结果(SPA)", query, len(spa))
            return spa[:max(want * 2, 30)]
    except Exception as exc:
        logger.warning("SPA 搜索异常(%s),回退导航式", str(exc)[:120])
    url = search_url(query)
    deadline = time.time() + login_wait
    warned = False
    empty_streak = 0
    while time.time() < deadline:
        logger.info("[%s] 导航搜索页(连续空结果 %d)...", query, empty_streak)
        b.navigate(url, settle=6)
        b.scroll(times=2, pause=1.5)   # 克制滚动,降低风控概率
        b.pump(2.0)
        records = _parse_search_events(b)
        if not records:
            records = _dom_items(b)
        if records:
            logger.info("搜索 %r: %d 条结果", query, len(records))
            return records[:max(want * 2, 30)]
        empty_streak += 1
        if not warned:
            warned = True
            logger.warning(
                ">>> 搜索无结果(可能限流或需登录):请看一眼 Edge 窗口,"
                "如需登录/验证请在窗口里完成 <<<")
        if empty_streak >= 6:
            logger.error("搜索 %r 连续 %d 次空结果,放弃该词", query,
                         empty_streak)
            break
        backoff = min(20 * empty_streak, 180)
        logger.warning("空结果退避 %ds(疑似限流,慢一点更稳)", backoff)
        time.sleep(backoff)
    return []


def detail_record(b: CdpBrowser, aweme_id: str) -> tuple[dict | None, list[str],
                                                        list[tuple[str, str]]]:
    """访问视频详情页:截获 detail 接口 → 记录;同时抓 DOM 推荐视频 id
    和该视频携带的话题 (hashtag_id, hashtag_name) 列表。

    返回 (record_or_None, 推荐的 aweme_id 列表, 话题列表)。
    """
    b.clear_events()
    b.navigate(f"https://www.douyin.com/video/{aweme_id}", settle=6)
    b.pump(2.0)
    record = None
    tags: list[tuple[str, str]] = []
    for ev in b.take_events():
        try:
            url = ev["params"]["response"]["url"]
        except (KeyError, TypeError):
            continue
        if "/aweme/v1/web/" not in url or "detail" not in url:
            continue
        body = b.body(ev["params"]["requestId"])
        if not body:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        awemes: list[dict] = []
        _walk_awemes(payload, awemes)
        for a in awemes:
            if str(a.get("aweme_id")) == str(aweme_id):
                record = _to_record(a)
                for te in (a.get("text_extra") or []):
                    hn, hid = te.get("hashtag_name"), te.get("hashtag_id")
                    if hn and hid and (str(hid), hn) not in tags:
                        tags.append((str(hid), hn))
                break
        if record:
            break
    rec_ids = b.eval_js(
        "JSON.stringify([...new Set([...document.querySelectorAll("
        "'a[href*=\\\"/video/\\\"]')].map(a => (a.href.match("
        "/video\/(\d{6,})/) || [])[1]).filter(Boolean))])") or "[]"
    try:
        rec_ids = json.loads(rec_ids)
    except ValueError:
        rec_ids = []
    return record, rec_ids, tags


def walk_candidates(b: CdpBrowser, seed_ids: list[str],
                    want: int = 12, max_depth: int = 2,
                    visited_state: set[str] | None = None) -> list[dict]:
    """推荐流爬行:从种子视频出发,经详情页推荐链收集候选记录。

    不碰搜索接口;详情页访问从未被限流。visited_state 传入时跨轮共享,
    避免每轮重爬同样的种子(推荐流模式会多轮调用)。
    """
    out: dict[str, dict] = {}
    visited = visited_state if visited_state is not None else set()
    queue = [i for i in seed_ids if i and i not in visited]
    depth = {i: 0 for i in queue}
    while queue and len(out) < want:
        aid = queue.pop(0)
        if aid in visited:
            continue
        visited.add(aid)
        try:
            rec, rec_ids, _tags = detail_record(b, aid)
        except Exception as exc:
            logger.warning("爬行 %s 失败: %s", aid, exc)
            continue
        if rec and rec["aweme_id"] not in out:
            out[rec["aweme_id"]] = rec
            logger.info("[爬行] + %s (%d/%d)", (rec.get("title") or "")[:30],
                        len(out), want)
        d = depth.get(aid, 0)
        if d < max_depth:
            for rid in rec_ids:
                if rid not in visited and rid not in depth:
                    depth[rid] = d + 1
                    queue.append(rid)
    return list(out.values())[:want]


_HOT_CLICK_JS = """(() => {
  const c = [...document.querySelectorAll('a,span,div')].filter(
      e => ((e.innerText||'').trim() === '最热') && e.children.length <= 1);
  if (c.length) { c[c.length-1].click(); return 'OK'; }
  return 'NOFIND';
})()"""

# 热门页视频卡片专用:只抓 /video/ 链接(图文帖挂 /note/,自动排除)
DOM_VIDEO_IDS_JS = r"""JSON.stringify([...new Set(
    [...document.querySelectorAll('a[href*="/video/"]')]
    .map(a => (a.href.match(/video\/(\d{6,})/) || [])[1])
    .filter(Boolean))])"""


def _collect_tags(node, out: dict) -> None:
    """从 challenge/search 响应里挖同名变体话题 cha_id→cha_name。"""
    if isinstance(node, dict):
        nm = node.get("cha_name") or node.get("challenge_name")
        cid = node.get("cha_id") or node.get("cid")
        if nm and cid and str(cid) not in out:
            out[str(cid)] = str(nm)
        for v in node.values():
            _collect_tags(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_tags(v, out)


def tag_harvest(b: CdpBrowser, tag_ids: list[str],
                want: int = 12, visited_state: set[str] | None = None,
                hot: bool = True) -> tuple[list[dict], dict[str, str]]:
    """话题页收割(2026-10-21 用户指定路径):话题 → 筛选"最热" → 视频池。

    网页话题接口的 payload 不带可下载视频直链(play_addr 是音频轨)且混着
    图文帖,所以只把话题最热榜当【候选 id 池】,逐个开 /video/ 详情页取
    权威记录——图文帖会重定向到 /note/ 取不到记录,自动淘汰。
    顺带从 challenge/search 响应收集同名变体话题(+emoji/后缀)扩大池子。
    返回 (records, 变体话题表)。
    """
    visited = visited_state if visited_state is not None else set()
    out: list[dict] = []
    related: dict[str, str] = {}
    for tid in tag_ids:
        if len(out) >= want:
            break
        b.clear_events()
        b.navigate(f"https://www.douyin.com/hashtag/{tid}", settle=8)
        if hot:
            b.eval_js(_HOT_CLICK_JS)
            time.sleep(3.5)
        b.pump(1.5)
        # 滚两屏让热门页多渲染些卡片
        b.scroll(times=2, pause=1.5)
        b.pump(1.0)
        # DOM 直取 /video/ 链接(2026-10-21 用户指定:只要视频不要图文——
        # 热门页视频卡片挂 /video/,图文帖挂 /note/,天然分流)
        dom_ids: list[str] = []
        raw = b.eval_js(DOM_VIDEO_IDS_JS) or "[]"
        try:
            dom_ids = [i for i in json.loads(raw) if i]
        except ValueError:
            pass
        cand_ids: list[str] = list(dom_ids)
        seen_here: set[str] = set(dom_ids)
        for ev in b.take_events():
            try:
                u = ev["params"]["response"]["url"]
            except (KeyError, TypeError):
                continue
            if "challenge/aweme" not in u:
                if "challenge/search" in u:
                    body = b.body(ev["params"]["requestId"])
                    if body:
                        try:
                            _collect_tags(json.loads(body), related)
                        except ValueError:
                            pass
                continue
            body = b.body(ev["params"]["requestId"])
            if not body:
                continue
            try:
                awemes: list[dict] = []
                _walk_awemes(json.loads(body), awemes)
            except ValueError:
                continue
            for a in awemes:
                aid = str(a.get("aweme_id"))
                if aid and aid not in seen_here:
                    seen_here.add(aid)
                    cand_ids.append(aid)   # API 兜底(DOM 没渲染到的)
        logger.info("[话题 %s 最热] 候选 %d 个(DOM视频 %d)",
                    tid, len(cand_ids), len(dom_ids))
        for aid in cand_ids:
            if len(out) >= want:
                break
            if aid in visited:
                continue
            visited.add(aid)
            try:
                rec, _, _ = detail_record(b, aid)
            except Exception as exc:
                logger.warning("话题详情 %s 失败: %s", aid, exc)
                continue
            if rec and rec.get("play_url") and not rec["is_images"]:
                out.append(rec)
                logger.info("[话题] + %s (%d/%d)",
                            (rec.get("title") or "")[:28], len(out), want)
    return out, related


def music_harvest(b: CdpBrowser, music_ids: list[str],
                  want: int = 12,
                  visited_state: set[str] | None = None) -> list[dict]:
    """原声页收割(2026-10-22):/music/<id> 页列着所有用这条原声的视频——
    "拍同款"传播链的聚宝盆(实测一页 41 张视频卡片),全详情页路线不受限。
    原声 id 从已入库视频的 detail payload(music.id)取得,_to_record 已带
    music_id 字段。
    """
    visited = visited_state if visited_state is not None else set()
    out: list[dict] = []
    for mid in music_ids:
        if len(out) >= want:
            break
        b.clear_events()
        b.navigate(f"https://www.douyin.com/music/{mid}", settle=8)
        b.scroll(times=2, pause=1.5)
        b.pump(1.0)
        raw = b.eval_js(DOM_VIDEO_IDS_JS) or "[]"
        try:
            ids = [i for i in json.loads(raw) if i]
        except ValueError:
            ids = []
        logger.info("[原声 %s] 页面视频 %d 个", mid, len(ids))
        for aid in ids:
            if len(out) >= want:
                break
            if aid in visited:
                continue
            visited.add(aid)
            try:
                rec, _, _ = detail_record(b, aid)
            except Exception as exc:
                logger.warning("原声详情 %s 失败: %s", aid, exc)
                continue
            if rec and rec.get("play_url") and not rec["is_images"]:
                out.append(rec)
                logger.info("[原声] + %s (%d/%d)",
                            (rec.get("title") or "")[:28], len(out), want)
    return out
