"""music_export 单测:假的 fetch/download + tmp_path,全程不联网。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hunter.music_export import collect_music_ids, export
from hunter.store import TemplateStore

BODY = b"ID3 fake-mp3-bytes"


class FakeFetch:
    """fetch_music 假实现;fail_ids 里的抛异常,其余返回正常原声信息。"""

    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def __call__(self, music_id):
        self.calls.append(music_id)
        if music_id in self.fail_ids:
            raise RuntimeError(f"原声页打不开: {music_id}")
        return {"play_url": f"https://cdn.example/{music_id}.mp3",
                "title": f"原声-{music_id}",
                "duration_s": 15.0}


class FakeDownload:
    """download 假实现:把 body 写到 dest_path 并返回 ret。"""

    def __init__(self, body=BODY, ret=True):
        self.body = body
        self.ret = ret
        self.calls = []    # (url, dest_path, max_bytes)

    def __call__(self, url, dest_path, max_bytes):
        self.calls.append((url, Path(dest_path), max_bytes))
        Path(dest_path).write_bytes(self.body)
        return self.ret


def make_folder(tmp_path, videos):
    folder = tmp_path / "01_测试模板"
    folder.mkdir()
    (folder / "metadata.json").write_text(
        json.dumps({"template": "测试模板", "videos": videos},
                   ensure_ascii=False), encoding="utf-8")
    return folder


# ---------- collect_music_ids ----------

def test_collect_music_ids():
    meta = {"videos": [
        {"aweme_id": "111", "music_id": "M1"},
        {"aweme_id": "222", "music_id": "M1"},
        {"aweme_id": "333", "music_id": None},
        {"aweme_id": "444", "music_id": "M2"},
        {"aweme_id": "555"},                       # 无 music_id 键
        {"music_id": "M3"},                        # 无 aweme_id,原声仍在
    ]}
    assert collect_music_ids(meta) == {
        "M1": ["111", "222"], "M2": ["444"], "M3": []}
    assert collect_music_ids({}) == {}
    assert collect_music_ids({"videos": []}) == {}


# ---------- export ----------

def test_export_happy_path(tmp_path):
    folder = make_folder(tmp_path, [
        {"aweme_id": "111", "music_id": "M1"},
        {"aweme_id": "222", "music_id": "M1"},
        {"aweme_id": "333", "music_id": "M2"},
    ])
    fetch, dl = FakeFetch(), FakeDownload()
    entries = export(folder, fetch_music=fetch, download=dl)
    by_id = {e["music_id"]: e for e in entries}
    assert set(by_id) == {"M1", "M2"}

    e1 = by_id["M1"]
    assert e1 == {
        "music_id": "M1",
        "title": "原声-M1",
        "duration_s": 15.0,
        "video_ids": ["111", "222"],
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "bytes": len(BODY),
        "source": "douyin_original_sound",
        "fetched_at": e1["fetched_at"],
    }
    assert e1["fetched_at"]    # 有值即可,具体时刻不断言

    # 音频落盘,manifest 与返回值一致
    assert (folder / "music" / "M1.mp3").read_bytes() == BODY
    assert (folder / "music" / "M2.mp3").exists()
    manifest = json.loads((folder / "music" / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest == entries

    # 注入函数收到约定的参数:下载到 .part,带上 max_bytes 默认值
    url, dest, cap = dl.calls[0]
    assert url == "https://cdn.example/M1.mp3"
    assert dest == folder / "music" / "M1.mp3.part"
    assert cap == 20 * 1024 * 1024


def test_export_idempotent(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    fetch, dl = FakeFetch(), FakeDownload()
    export(folder, fetch_music=fetch, download=dl)
    manifest1 = (folder / "music" / "manifest.json").read_text(encoding="utf-8")
    n_fetch, n_dl = len(fetch.calls), len(dl.calls)

    entries = export(folder, fetch_music=fetch, download=dl)
    assert len(fetch.calls) == n_fetch          # 未再触网
    assert len(dl.calls) == n_dl
    assert (folder / "music" / "manifest.json").read_text(
        encoding="utf-8") == manifest1          # fetched_at 等原样保留
    assert entries == json.loads(manifest1)


def test_export_sha_mismatch_redownloads(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    export(folder, fetch_music=FakeFetch(), download=FakeDownload())
    (folder / "music" / "M1.mp3").write_bytes(b"corrupted")   # sha 不再一致

    dl = FakeDownload(body=b"fresh-audio")
    entries = export(folder, fetch_music=FakeFetch(), download=dl)
    assert len(dl.calls) == 1
    assert (folder / "music" / "M1.mp3").read_bytes() == b"fresh-audio"
    assert entries[0]["sha256"] == hashlib.sha256(b"fresh-audio").hexdigest()


def test_export_single_failure_isolated(tmp_path):
    folder = make_folder(tmp_path, [
        {"aweme_id": "111", "music_id": "M1"},
        {"aweme_id": "222", "music_id": "M2"},
        {"aweme_id": "333", "music_id": "M3"},
    ])
    fetch = FakeFetch(fail_ids={"M2"})
    entries = export(folder, fetch_music=fetch, download=FakeDownload())
    by_id = {e["music_id"]: e for e in entries}

    assert by_id["M1"].get("sha256")            # 其他条不受影响
    assert by_id["M3"].get("sha256")
    err = by_id["M2"]
    assert "M2" in err["error"]
    assert err["video_ids"] == ["222"]

    music = folder / "music"
    assert not (music / "M2.mp3").exists()      # 失败不留半成品
    assert not list(music.glob("*.part"))
    manifest = json.loads((music / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 3                   # 失败也记入 manifest


def test_export_oversize_rejected(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    entries = export(folder, fetch_music=FakeFetch(),
                     download=FakeDownload(body=b"x" * 30), max_bytes=10)
    assert "error" in entries[0]
    assert not (folder / "music" / "M1.mp3").exists()
    assert not (folder / "music" / "M1.mp3.part").exists()


def test_export_exact_max_bytes_ok(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    entries = export(folder, fetch_music=FakeFetch(),
                     download=FakeDownload(body=b"x" * 10), max_bytes=10)
    assert entries[0].get("sha256")             # 等于上限不算超


def test_export_empty_download_rejected(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    entries = export(folder, fetch_music=FakeFetch(),
                     download=FakeDownload(body=b""))
    assert "error" in entries[0]
    assert not (folder / "music" / "M1.mp3").exists()


def test_export_download_false_rejected(tmp_path):
    folder = make_folder(tmp_path, [{"aweme_id": "111", "music_id": "M1"}])
    entries = export(folder, fetch_music=FakeFetch(),
                     download=FakeDownload(body=b"half", ret=False))
    assert "error" in entries[0]
    assert not (folder / "music" / "M1.mp3").exists()
    assert not (folder / "music" / "M1.mp3.part").exists()


def test_export_no_music_ids_is_noop(tmp_path):
    folder = make_folder(tmp_path, [
        {"aweme_id": "111"}, {"aweme_id": "222", "music_id": None}])
    fetch, dl = FakeFetch(), FakeDownload()
    assert export(folder, fetch_music=fetch, download=dl) == []
    assert not fetch.calls and not dl.calls
    assert not (folder / "music").exists()      # 不产生空目录


def test_export_missing_metadata_is_noop(tmp_path):
    folder = tmp_path / "02_空模板"
    folder.mkdir()
    assert export(folder, fetch_music=FakeFetch(),
                  download=FakeDownload()) == []
    assert not (folder / "music").exists()


# ---------- store 落盘 music_id ----------

def test_store_metadata_has_music_id(tmp_path):
    store = TemplateStore(tmp_path)
    folder = store.create("烧心")
    src = tmp_path / "raw.mp4"
    src.write_bytes(b"fake-video")
    rec = {"aweme_id": "7123456789", "title": "标题", "author": "作者",
           "music": "原声名", "music_id": "M42", "form": "真人", "sig": "abc",
           "page_url": "https://www.douyin.com/video/7123456789",
           "likes": 1, "duration_ms": 900}
    idx = store.add(folder, src, rec, "查询词")

    v = store.load_meta(folder)["videos"][0]
    assert v["music_id"] == "M42"
    assert v["index"] == idx == 1
    assert list(v) == ["index", "file", "aweme_id", "title", "author",
                       "music", "music_id", "form", "sig", "page_url",
                       "likes", "duration_ms", "file_size", "added_at"]


def test_store_music_id_defaults_none(tmp_path):
    store = TemplateStore(tmp_path)
    folder = store.create("无原声")
    src = tmp_path / "raw.mp4"
    src.write_bytes(b"fake-video")
    store.add(folder, src, {"aweme_id": "7123456789"}, "查询词")
    assert store.load_meta(folder)["videos"][0]["music_id"] is None


def test_store_then_export_end_to_end(tmp_path):
    store = TemplateStore(tmp_path / "data")
    folder = store.create("端到端")
    for i, mid in enumerate(["M1", "M1", None], 1):
        src = tmp_path / f"raw{i}.mp4"
        src.write_bytes(b"fake-video")
        store.add(folder, src, {"aweme_id": f"700000000{i}", "music_id": mid},
                  "查询词")

    entries = export(folder, fetch_music=FakeFetch(), download=FakeDownload())
    assert [e["music_id"] for e in entries] == ["M1"]
    assert entries[0]["video_ids"] == ["7000000001", "7000000002"]
    assert (folder / "music" / "M1.mp3").exists()
