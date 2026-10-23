"""导入用户提供的分享链接(用户亲选 = 预先认可,GLM 只做形式分类)。

用法: python -I -u scripts/import_user_links.py <链接文件> [port]
链接文件格式: 第一行=模板名(须已注册),其后每行一条 v.douyin.com 短链。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from hunter.browser import CdpBrowser
from hunter import douyin as dj, glm
from hunter.fetch import _download
from hunter.verify import verify_video
from hunter.templates import get as get_template
from hunter.store import TemplateStore
from hunter import dedup


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: import_user_links.py <链接文件> [port]")
        return 2
    links_file = Path(sys.argv[1])
    lines = [ln.strip() for ln in
             links_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    tpl_name, links = lines[0], lines[1:]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 19225
    # 独立 profile:可与其他采集线并行,不锁 A/B 号的 profile
    import os
    import tempfile
    prof = os.path.join(tempfile.gettempdir(), "edge_dy_import")
    store = TemplateStore("data")
    folder = store.find(tpl_name) or store.create(tpl_name)
    tpl = get_template(tpl_name)
    model = glm.probe_model("glm-5.3-flash")
    b = CdpBrowser(port=port, profile=prof)
    b.connect()
    ok_n = 0
    for i, link in enumerate(links, 1):
        if len(list(folder.glob("*.mp4"))) >= 10:
            print("  已满 10,停止导入")
            break
        try:
            b.navigate(link, settle=6)
            href = b.eval_js("location.href") or ""
            m = re.search(r"/(?:video|note)/(\d{6,})", href)
            if not m:
                print(f"  {i}. 解析失败: {href[:60]}")
                continue
            aid = m.group(1)
            rec, _, _ = dj.detail_record(b, aid)
            if not rec or not rec.get("play_url"):
                print(f"  {i}. 详情/直链失败 {aid}")
                continue
            if aid in (store.known_ids(folder) | store.rejected_ids()):
                print(f"  {i}. 已在库或黑名单,跳过")
                continue
            tmp = Path(f"logs/user_{aid}.mp4")
            tmp.unlink(missing_ok=True)
            if not _download(rec["play_url"], tmp):
                print(f"  {i}. 下载失败: {(rec.get('title') or '')[:30]}")
                continue
            # GLM 只做形式分类(用户预认可,不否决)
            try:
                _, form, _ = verify_video(model, tmp, rec, tpl)
            except Exception:
                form = "真人翻拍"
            rec["form"] = form
            rec["sig"] = dedup.video_hashes(tmp)
            idx = store.add(folder, tmp, rec, "用户分享链接")
            ok_n += 1
            print(f"  {i}. 已入库 {idx:02d}.mp4 [{form}] "
                  f"{(rec.get('title') or '')[:36]}")
        except Exception as exc:
            print(f"  {i}. 异常: {exc}")
    b.stop()
    n = len(list(folder.glob("*.mp4")))
    print(f"\n完成: 导入 {ok_n} 条,文件夹现有 {n}/10")
    return 0 if n >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
