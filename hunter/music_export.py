"""原声导出:把模板的干净 BGM 落到 data/<NN_模板>/music/。

抖音"原声页"(/music/<id>)的音频就是模板的原始 BGM,比从视频里做
声道分离可靠。metadata.json 每条视频已带 music_id(store.add 落盘),
本模块按 music_id 聚合,经注入的 fetch_music / download 保存
<music_id>.mp3 并写 manifest.json:sha256 与旧 manifest 一致则跳过重下
(幂等,video_ids 仍按本次 metadata 刷新),超过 max_bytes 或下载为空视为
失败并删除半成品,单条失败只记 "error" 不拖垮整批。联网实现由调用方注入,
本模块导入时不触网。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from .store import TemplateStore

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024    # 单条原声 20 MB 上限
SOURCE = "douyin_original_sound"


class _ExportFail(Exception):
    """内部信号:单条原声导出失败(消息直接进 manifest 的 error 字段)。"""


def collect_music_ids(meta: dict) -> dict[str, list[str]]:
    """从模板 metadata.json 内容聚合 music_id → 共用它的 aweme_id 列表。

    没有原声(music_id 缺失或 None)的视频不计入。
    """
    ids: dict[str, list[str]] = {}
    for v in meta.get("videos", []):
        mid = v.get("music_id")
        if not mid:
            continue
        vids = ids.setdefault(mid, [])
        vid = v.get("aweme_id")
        if vid and vid not in vids:
            vids.append(vid)
    return ids


def export(folder: str | Path, *, fetch_music, download,
           max_bytes: int = MAX_BYTES) -> list[dict]:
    """导出模板全部原声到 <folder>/music/,返回 manifest 条目(含失败条目)。

    注入接口(真实联网实现不在本模块):
      fetch_music(music_id) -> {"play_url": str, "title": str, "duration_s": float}
      download(url, dest_path, max_bytes) -> bool
    下载先落 <music_id>.mp3.part,校验大小后原子改名为 <music_id>.mp3,
    失败时半成品删除、已入库的旧文件不动。
    """
    folder = Path(folder)
    ids = collect_music_ids(TemplateStore(folder.parent).load_meta(folder))
    if not ids:
        return []
    music_dir = folder / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = music_dir / "manifest.json"
    old_by_id = {e.get("music_id"): e for e in _read_manifest(manifest_path)
                 if e.get("music_id")}

    entries: list[dict] = []
    for mid, vids in ids.items():
        dest = music_dir / f"{mid}.mp3"
        part = music_dir / f"{mid}.mp3.part"
        old = old_by_id.get(mid) or {}
        info: dict = {}
        try:
            if old.get("sha256") and dest.exists() \
                    and _sha256(dest) == old["sha256"]:
                # 幂等:不重下,只刷新 video_ids(本次 metadata 可能新增共用
                # 视频),其余字段(含 fetched_at)原样保留。
                entries.append({**old, "video_ids": vids})
                logger.info("[原声] sha 一致,跳过: %s", mid)
                continue
            part.unlink(missing_ok=True)
            info = fetch_music(mid) or {}
            url = info.get("play_url")
            if not url:
                raise _ExportFail("fetch_music 未返回 play_url")
            if not download(url, part, max_bytes):
                raise _ExportFail("download 返回失败")
            size = part.stat().st_size
            if size == 0:
                raise _ExportFail("下载为空")
            if size > max_bytes:
                raise _ExportFail(f"{size} 字节超过 max_bytes={max_bytes}")
            sha = _sha256(part)
            part.replace(dest)
        except Exception as exc:         # 单条失败不拖垮整批
            part.unlink(missing_ok=True)
            logger.warning("[原声] %s 失败: %s", mid, exc)
            entries.append({
                "music_id": mid,
                "title": info.get("title"),
                "duration_s": info.get("duration_s"),
                "video_ids": vids,
                "error": str(exc),
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue
        entries.append({
            "music_id": mid,
            "title": info.get("title"),
            "duration_s": info.get("duration_s"),
            "video_ids": vids,
            "sha256": sha,
            "bytes": size,
            "source": SOURCE,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        logger.info("[原声] %s → %s (%d KB,%d 个视频共用)",
                    mid, dest.name, size // 1024, len(vids))

    failed = sum(1 for e in entries if e.get("error"))
    logger.info("[原声] %s:导出 %d 条,失败 %d 条",
                folder.name, len(entries) - failed, failed)
    manifest_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest(path: Path) -> list[dict]:
    """读旧 manifest,缺失或损坏当空表(下次全量重下,天然自愈)。"""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        else:
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)]
    return []
