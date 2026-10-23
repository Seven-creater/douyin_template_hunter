"""模板库目录管理:data/<NN>_<模板名>/01.mp4..NN.mp4 + metadata.json。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return (name.strip("_") or "template")[:40]


class TemplateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def templates(self) -> list[Path]:
        return sorted(d for d in self.root.iterdir()
                      if d.is_dir() and re.match(r"^\d{2}_", d.name))

    def create(self, name: str) -> Path:
        """新建模板文件夹,自动分配下一个编号。"""
        idx = max((int(d.name[:2]) for d in self.templates()), default=0) + 1
        d = self.root / f"{idx:02d}_{sanitize(name)}"
        d.mkdir(exist_ok=True)
        return d

    def find(self, name: str) -> Path | None:
        target = sanitize(name)
        for d in self.templates():
            if d.name[3:] == target:
                return d
        return None

    # ---------- metadata.json ----------
    @staticmethod
    def _meta_path(folder: Path) -> Path:
        return folder / "metadata.json"

    def load_meta(self, folder: Path) -> dict:
        p = self._meta_path(folder)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"template": folder.name[3:], "videos": []}

    def save_meta(self, folder: Path, meta: dict) -> None:
        self._meta_path(folder).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def known_ids(self, folder: Path) -> set[str]:
        return {v.get("aweme_id") for v in self.load_meta(folder).get("videos", [])
                if v.get("aweme_id")}

    # ---------- 用户否决名单(data/rejected_ids.json) ----------
    # 用户看过并明确判垃圾的视频 ID,永久拉黑,任何模板都不再收录
    def rejected_ids(self) -> set[str]:
        p = self.root / "rejected_ids.json"
        if p.exists():
            try:
                return set(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                pass
        return set()

    def reject_id(self, aid: str) -> None:
        p = self.root / "rejected_ids.json"
        ids = self.rejected_ids()
        ids.add(aid)
        p.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=1),
                     encoding="utf-8")

    def add(self, folder: Path, src: Path, record: dict,
            query: str) -> int:
        """把下载好的视频编号新入库,返回编号(1 起)。"""
        meta = self.load_meta(folder)
        used = {v["index"] for v in meta.get("videos", [])}
        index = next(i for i in range(1, 100) if i not in used)
        dst = folder / f"{index:02d}.mp4"
        src.replace(dst)
        meta.setdefault("queries", [])
        if query not in meta["queries"]:
            meta["queries"].append(query)
        meta["videos"].append({
            "index": index,
            "file": dst.name,
            "aweme_id": record.get("aweme_id"),
            "title": record.get("title"),
            "author": record.get("author"),
            "music": record.get("music"),
            "form": record.get("form"),
            "sig": record.get("sig"),
            "page_url": record.get("page_url"),
            "likes": record.get("likes"),
            "duration_ms": record.get("duration_ms"),
            "file_size": dst.stat().st_size,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save_meta(folder, meta)
        return index

    def remove_videos(self, folder: Path, files: list[str]) -> None:
        """删除指定视频并把剩余的重排成连续 01..NN。

        幸存视频按原编号升序拿到新编号 i,恒有 i <= 原编号(删除只腾位),
        所以先删后改名的顺序不会发生文件名冲突。
        """
        if not files:
            return
        meta = self.load_meta(folder)
        keep = [v for v in meta.get("videos", [])
                if v.get("file") not in files]
        for f in files:
            (folder / f).unlink(missing_ok=True)
        keep.sort(key=lambda v: v.get("index", 0))
        for i, v in enumerate(keep, 1):
            new = f"{i:02d}.mp4"
            if new != v["file"]:
                (folder / v["file"]).replace(folder / new)
            v["index"] = i
            v["file"] = new
        meta["videos"] = keep
        self.save_meta(folder, meta)
