"""视频下载:搜索接口直链优先,视频页 CDP 嗅探兜底,DASH 音视频 ffmpeg 合并。"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import requests

from .browser import CdpBrowser

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")
HEADERS = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
MIN_BYTES = 60_000          # 60 KB,低于视为错误页
HTTP_TIMEOUT = (15, 120)    # 连接 / 读


class _SlowDownload(Exception):
    """内部信号:下载速度过慢,主动放弃。"""


def is_mp4(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return len(head) >= 8 and head[4:8] == b"ftyp"
    except OSError:
        return False


def _download(url: str, dst: Path, *, hard_cap: float = 300.0,
              slow_after: float = 60.0, slow_min_bytes: int = 1 << 20) -> bool:
    """流式下载 + 慢速熔断:60 秒不足 1 MB 或总时长超 5 分钟即放弃。"""
    start = time.monotonic()
    try:
        with requests.get(url, headers=HEADERS, stream=True,
                          timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
            if r.status_code != 200:
                logger.warning("HTTP %s: %s", r.status_code, url[:100])
                return False
            with open(dst, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                    elapsed = time.monotonic() - start
                    if elapsed > hard_cap or (elapsed > slow_after
                                              and f.tell() < slow_min_bytes):
                        logger.warning("下载过慢放弃(%.1f MB / %.0f s)",
                                       f.tell() / 1e6, elapsed)
                        f.flush()
                        raise _SlowDownload()
    except _SlowDownload:
        dst.unlink(missing_ok=True)
        return False
    except requests.RequestException as exc:
        logger.warning("网络错误: %s", exc)
        return False
    if dst.stat().st_size < MIN_BYTES or not is_mp4(dst):
        logger.warning("文件无效(%d 字节或非 mp4)", dst.stat().st_size)
        dst.unlink(missing_ok=True)
        return False
    return True


def sniff_page_media(b: CdpBrowser, page_url: str, *, window: float = 22.0,
                     audio_grace: float = 8.0) -> tuple[str | None, str | None]:
    """打开视频页,嗅探 CDP 流量里的 DASH 视频/音频 CDN 地址。"""
    b.navigate(page_url, settle=4.0)
    video_url = audio_url = None
    deadline = time.time() + window
    while time.time() < deadline:
        b.pump(1.0)
        for ev in b.take_events():
            try:
                u = ev["params"]["response"]["url"]
            except (KeyError, TypeError):
                continue
            if "douyinvod" not in u and "zjcn" not in u:
                continue
            if "media-video" in u and not video_url:
                video_url = u
            elif "media-audio" in u and not audio_url:
                audio_url = u
            elif (not video_url and u.split("?")[0].endswith(".mp4")
                  and "douyinstatic" not in u):
                video_url = u
        if video_url and (audio_url or deadline - time.time() < audio_grace):
            break
    return video_url, audio_url


def _merge(tmp_v: Path, tmp_a: Path, out: Path) -> bool:
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", str(tmp_v), "-i", str(tmp_a),
           "-c", "copy", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=120)
        return out.exists() and out.stat().st_size > MIN_BYTES
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ffmpeg 合并失败: %s", exc)
        return False


def fetch_video(b: CdpBrowser, rec: dict, out_path: Path,
                tmpdir: Path) -> bool:
    """下载一条记录到 out_path。成功 True。

    顺序:① 搜索接口 play_url 直下 → ② 视频页嗅探(无 DASH 音频则单流)。
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp_v = tmpdir / f"{rec['aweme_id']}_v.mp4"
    tmp_a = tmpdir / f"{rec['aweme_id']}_a.mp4"
    for p in (tmp_v, tmp_a):
        p.unlink(missing_ok=True)

    # ① 直链
    if rec.get("play_url") and _download(rec["play_url"], tmp_v):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_v.replace(out_path)
        logger.info("[直链] %s → %s (%d KB)", rec["aweme_id"], out_path.name,
                    out_path.stat().st_size // 1024)
        return True

    # ② 页面嗅探
    logger.info("[嗅探] %s", rec["page_url"])
    video_url, audio_url = sniff_page_media(b, rec["page_url"])
    if not video_url:
        logger.warning("未嗅探到视频地址: %s", rec["title"][:50])
        return False
    if not _download(video_url, tmp_v):
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if audio_url and _download(audio_url, tmp_a):
        if _merge(tmp_v, tmp_a, out_path):
            tmp_v.unlink(missing_ok=True)
            tmp_a.unlink(missing_ok=True)
            logger.info("[嗅探+合并] %s → %s (%d KB)", rec["aweme_id"],
                        out_path.name, out_path.stat().st_size // 1024)
            return True
        logger.warning("合并失败,退回无声视频")
    tmp_v.replace(out_path)
    tmp_a.unlink(missing_ok=True)
    logger.info("[嗅探] %s → %s (%d KB)", rec["aweme_id"], out_path.name,
                out_path.stat().st_size // 1024)
    return True
