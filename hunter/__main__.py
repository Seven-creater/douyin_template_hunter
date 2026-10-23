"""hunter CLI。

用法(仓库根目录运行):
  python -m hunter login                          # 只开窗口扫码登录
  python -m hunter hunt "不烧心" [--limit 10]     # 采集一个模板
  python -m hunter batch [--only 名1,名2]         # 批量采集注册表全部模板
  python -m hunter analyze data/01_不烧心         # GLM 分析一个模板文件夹
  python -m hunter models                         # 列出可用模型(校验 key)
  python -m hunter status                         # 查看采集进度
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .browser import DEFAULT_PROFILE_B, BrowserPool, CdpBrowser
from .config import CDP_PORT_DEFAULT, GLM_MODEL_DEFAULT, env
from .templates import Template, all_templates, get as get_template

logger = logging.getLogger(__name__)


def _setup_log() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")


# ---------- 采集核心(供 hunt / batch 复用) ----------
def run_hunt(args, tpl: Template) -> int:
    from . import douyin, glm
    from .fetch import fetch_video
    from .store import TemplateStore
    from .verify import prefilter, verify_video

    store = TemplateStore(args.data)
    folder = store.find(tpl.name) or store.create(tpl.name)
    got = len(list(folder.glob("*.mp4")))
    if got >= args.limit:
        print(f"[{tpl.name}] 已有 {got} 个,跳过")
        return 0
    print(f"\n{'='*56}\n[{tpl.name}] 目标:{folder}(已有 {got},还差 "
          f"{args.limit - got})")

    model = None
    if not args.no_verify:
        model = glm.probe_model(args.model)
        print(f"验收模型:{model}")

    seen = store.known_ids(folder) | store.rejected_ids()  # 含用户否决名单
    # 画面多样性(2026-10-20 终版):按素材来源配额——同源素材(原剧画面
    # 无论怎么剪)最多 2 条且两轮都生效;创作形式(翻拍/动画/变装/移植)
    # 第一轮各限 3 条、第二轮放开;另加像素级查重双保险
    from . import dedup
    from . import semantic_dedup as semantic
    from .verify import (FORM_CAPS, DEFAULT_FORM_CAP, SOURCE_FORM,
                         UNCAPPED_FORMS)
    form_count: dict[str, int] = {}
    existing_sigs: list[tuple[str, list[int]]] = []
    for v in store.load_meta(folder).get("videos", []):
        f = v.get("form")
        if f:
            form_count[f] = form_count.get(f, 0) + 1
        sig = v.get("sig")
        fp = folder / str(v.get("file"))
        if sig:
            existing_sigs.append((v.get("file"), [int(s) for s in sig]))
        elif fp.exists():
            existing_sigs.append((v.get("file"), dedup.video_hashes(fp)))
    # 双进程并行模式(--single):两个槽指向同一浏览器,轮换变空转。
    # --profile b 用于在 19224 端口跑 B 号(必须显式指定 B profile,
    # 否则会用 A 的 profile 再拉一个 Edge,和 A 线锁同一个 profile)
    prof = (DEFAULT_PROFILE_B if getattr(args, "profile", "a") == "b"
            else None)
    if getattr(args, "single", False):
        bpool = BrowserPool(port=args.port, port_b=args.port,
                            profile=prof, profile_b=prof)
    else:
        bpool = BrowserPool(port=args.port, profile=prof)
    bpool.warm()
    b = bpool.current
    # 推荐流模式(关键词搜不到的模板):跨轮共享已爬集合与话题缓存
    browse_visited: set[str] = set()
    browse_tags: dict[str, str] = {}   # hashtag_id -> hashtag_name
    browse_music: dict[str, str] = {}  # 原声id -> 原声名(拍同款收割用)
    with tempfile.TemporaryDirectory() as td:
        # 语义查重代表帧缓存(已入库视频各取中点帧)
        rep_cache: dict[str, Path] = {}
        rep_dir = Path(td) / "reps"
        for name, _ in existing_sigs:
            fp = folder / name
            if fp.exists():
                rp = semantic.rep_frame(fp, rep_dir)
                if rp:
                    rep_cache[name] = rp
        for cap_pass in (True, False):
            if got >= args.limit:
                break
            # 翻拍优先搜索策略(用户 2026-10-21 指定):搜索配额只花在
            # 翻拍词上——裸桥段名搜出来的多是原剧素材,翻拍变体词排最前
            base = tpl.queries[0]
            remake_variants = [f"{base} 翻拍", f"{base} 宿舍翻拍",
                               f"{base} 学校翻拍", f"{base} 模仿",
                               f"{base} 大学生"]
            if tpl.browse_only:
                # 推荐流模式(用户 2026-10-21:关键词生态弱,搜索无效,
                # 只能从种子出发慢慢刷,靠 GLM 两关验收)
                queries = [f"推荐流第{i}轮" for i in range(1, 41)]
            else:
                queries = list(dict.fromkeys(remake_variants + tpl.queries))
            for q in queries:
                if got >= args.limit:
                    break
                if q is not queries[0]:
                    time.sleep(getattr(args, "query_gap", 40))  # 搜索间歇,防限流
                if tpl.browse_only:
                    # 种子=最近入库的视频;先挖模板话题 id,话题页收割
                    seeds = [v.get("aweme_id") for v in
                             store.load_meta(folder).get("videos", [])][-5:]
                    records = []
                    if seeds:
                        if not browse_tags:
                            # 首轮:开种子详情页读它挂的话题,筛出模板相关
                            for aid in seeds:
                                if aid in browse_visited:
                                    continue
                                try:
                                    rec0, _, tags = douyin.detail_record(b, aid)
                                    browse_visited.add(aid)
                                    if rec0 and rec0.get("music_id"):
                                        browse_music[rec0["music_id"]] = (
                                            rec0.get("music") or "")
                                except Exception as exc:
                                    logger.warning("种子 %s 读取失败: %s",
                                                   aid, exc)
                                    continue
                                for hid, hn in tags:
                                    if (any(k in hn for k in tpl.title_must)
                                            and hid not in browse_tags):
                                        browse_tags[hid] = hn
                                        print(f"  话题发现: #{hn} ({hid})",
                                              flush=True)
                        if browse_tags:
                            # 话题排序:含"被捕/逮捕"的精准话题最优先,尼克/
                            # 朱迪其次,泛话题(如"疯狂动物城"大话题,蹭流量
                            # 污染重)垫底,避免它们吃光采集配额
                            def _tag_order(item):
                                _hid, hn = item
                                if "被捕" in hn or "逮捕" in hn:
                                    return 0
                                if "尼克" in hn or "朱迪" in hn:
                                    return 1
                                return 2
                            ordered = [i[0] for i in
                                       sorted(browse_tags.items(),
                                              key=_tag_order)]
                            try:
                                records, related = douyin.tag_harvest(
                                    b, ordered, want=10,
                                    visited_state=browse_visited)
                            except Exception as exc:
                                logger.warning("话题收割失败: %s", exc)
                                records = []
                            # 变体话题(+emoji/后缀)并入话题池,扩大候选面
                            for hid, hn in related.items():
                                if (any(k in hn for k in tpl.title_must)
                                        and hid not in browse_tags
                                        and len(browse_tags) < 12):
                                    browse_tags[hid] = hn
                                    print(f"  话题变体: #{hn} ({hid})",
                                          flush=True)
                        if not records:
                            # 兜底1:推荐流爬行(挑战页空了再走这条)
                            try:
                                records = douyin.walk_candidates(
                                    b, seeds, want=10, max_depth=3,
                                    visited_state=browse_visited)
                            except Exception as exc:
                                logger.warning("推荐流爬行失败: %s", exc)
                        if len(records) < 4:
                            # 兜底2:原声页收割——/music/<id> 列出所有用
                            # 同一原声的"拍同款"(2026-10-22 实测一页 41 卡)
                            for r in records:
                                mid = r.get("music_id")
                                if mid and mid not in browse_music:
                                    browse_music[mid] = r.get("music") or mid
                            if browse_music:
                                try:
                                    extra = douyin.music_harvest(
                                        b, list(browse_music)[:3],
                                        want=10 - len(records),
                                        visited_state=browse_visited)
                                    records.extend(extra)
                                    for r in extra:
                                        mid = r.get("music_id")
                                        if mid and mid not in browse_music:
                                            browse_music[mid] = (
                                                r.get("music") or mid)
                                except Exception as exc:
                                    logger.warning("原声收割失败: %s", exc)
                else:
                    try:
                        records = douyin.search(b, q, want=args.limit)
                    except Exception as exc:  # 查询级容错:崩了换下一个词
                        logger.error("查询 %r 整体失败(继续下一个): %s",
                                     q, exc)
                        records = []
                    if not records:
                        # 双号轮换:切另一个账号重试一次
                        try:
                            b = bpool.rotate()
                            logger.info("切到备用账号重试 %r", q)
                            records = douyin.search(b, q, want=args.limit)
                        except Exception as exc:
                            logger.warning("备用账号搜索失败: %s", exc)
                    if not records:
                        # 搜索被限流 → 推荐流爬行兜底(详情页从未被限)
                        seeds = [v.get("aweme_id") for v in
                                 store.load_meta(folder).get("videos", [])][:3]
                        if seeds:
                            logger.info("搜索空,改用推荐流爬行(种子%d个)",
                                        len(seeds))
                            try:
                                records = douyin.walk_candidates(
                                    b, seeds, want=12,
                                    visited_state=browse_visited)
                            except Exception as exc:
                                logger.warning("爬行失败: %s", exc)
                                records = []
                # ---------- 候选批量:并行直链下载 + 并行 GLM 验收 ----------
                cands = []
                for rec in records:
                    if len(cands) >= 8:
                        break
                    if rec["is_images"] or rec["aweme_id"] in seen:
                        continue
                    if not prefilter(rec, tpl):   # 原因由 prefilter 内部记录
                        continue
                    cands.append(rec)
                if not cands:
                    continue

                def _dl_verify(rec: dict):
                    """线程安全:直链下载+GLM验收(不碰浏览器)。"""
                    from .fetch import _download
                    tmp = Path(td) / f"{rec['aweme_id']}.mp4"
                    tmp.unlink(missing_ok=True)
                    if not rec.get("play_url") or not _download(
                            rec["play_url"], tmp):
                        return rec["aweme_id"], None, None, "直链失败(转嗅探)"
                    if not model:
                        return rec["aweme_id"], tmp, "未分类", ""
                    try:
                        ok, form, why = verify_video(model, tmp, rec, tpl)
                    except Exception as exc:
                        return rec["aweme_id"], None, None, f"验收异常:{exc}"
                    if not ok:
                        tmp.unlink(missing_ok=True)
                        return (rec["aweme_id"], None, None,
                                f"验收不通过:{why.splitlines()[-1][:80]}")
                    return rec["aweme_id"], tmp, form, why

                got = len(list(folder.glob("*.mp4")))
                outcomes: dict[str, tuple] = {}
                try:
                    # z.ai 对同 key 并发连接敏感(>3 路易被重置),限 3 路
                    # 且逐个错峰提交
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        futs = []
                        for c in cands:
                            futs.append(pool.submit(_dl_verify, c))
                            time.sleep(1.5)
                        for f in futs:
                            res = f.result()
                            outcomes[res[0]] = res
                except Exception as exc:
                    logger.warning("并行批次异常(降级串行): %s", exc)
                    for c in cands:
                        outcomes.setdefault(c["aweme_id"],
                                            _dl_verify(c))

                # 语义查重并行预检:通过的候选 vs 已入库代表帧
                sem_hits: dict[str, list[str]] = {}
                if model and rep_cache:
                    verified_ids = [c["aweme_id"] for c in cands
                                    if outcomes.get(c["aweme_id"], (0, None))[1]]

                    def _sem_check(aid: str):
                        rec = next(c for c in cands if c["aweme_id"] == aid)
                        tmp = outcomes[aid][1]
                        cf = semantic.cand_frames(tmp, Path(td))
                        return aid, semantic.find_duplicates(
                            model, cf, list(rep_cache.items()))

                    try:
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            futs = []
                            for aid in verified_ids:
                                futs.append(pool.submit(_sem_check, aid))
                                time.sleep(2)
                            for f in futs:
                                aid, hits = f.result()
                                sem_hits[aid] = hits
                    except Exception as exc:
                        logger.warning("语义预检异常(退回顺序): %s", exc)

                def try_accept(rec: dict, tmp: Path, form: str,
                               why: str, pre_hits: list[str] | None = None) -> bool:
                    """配额+像素查重+语义查重。pre_hits 非空=预检已判重复。"""
                    nonlocal got
                    if got >= args.limit:
                        tmp.unlink(missing_ok=True)
                        return False
                    cap = FORM_CAPS.get(form, DEFAULT_FORM_CAP)
                    if form in UNCAPPED_FORMS and not cap_pass:
                        cap = args.limit
                    if form_count.get(form, 0) >= cap:
                        tmp.unlink(missing_ok=True)
                        logger.info("画面多样性跳过:形式[%s]已有 %d(上限%d)",
                                    form, form_count[form], cap)
                        return False
                    cand_sig = dedup.video_hashes(tmp)
                    for name, sig in existing_sigs:
                        if dedup.distance(cand_sig, sig) < dedup.DUP_THRESHOLD:
                            tmp.unlink(missing_ok=True)
                            logger.info("画面查重跳过:与 %s 近重复", name)
                            return False
                    hits = pre_hits if pre_hits is not None else []
                    if pre_hits is None and model and rep_cache:
                        cframes = semantic.cand_frames(tmp, Path(td))
                        hits = semantic.find_duplicates(
                            model, cframes, list(rep_cache.items()))
                    if hits:
                        tmp.unlink(missing_ok=True)
                        logger.info("语义查重跳过:与 %s 同画面", ",".join(hits))
                        return False
                    rec["form"] = form
                    rec["sig"] = cand_sig
                    index = store.add(folder, tmp, rec, q)
                    seen.add(rec["aweme_id"])
                    form_count[form] = form_count.get(form, 0) + 1
                    final_name = f"{index:02d}.mp4"
                    existing_sigs.append((final_name, cand_sig))
                    rp = semantic.rep_frame(folder / final_name, rep_dir)
                    if rp:
                        rep_cache[final_name] = rp
                    got += 1
                    print(f"  [{got}/{args.limit}] {final_name} "
                          f"[{form}] {(rec.get('title') or '')[:36]}")
                    return True

                for rec in cands:
                    if got >= args.limit:
                        break
                    res = outcomes.get(rec["aweme_id"])
                    if not res:
                        continue
                    _, tmp, form, why = res
                    if tmp is None:
                        # 本轮已判定不过(验收/下载失败)→ 记入 seen,
                        # 后续轮次不再重复下载重复验收(跨轮仍可重判)
                        seen.add(rec["aweme_id"])
                        logger.info("[%s] %s", (rec.get("title") or "")[:36], why)
                        continue
                    if form:
                        print(f"    验收通过[{form}]:"
                              f"{(why or '').splitlines()[0][:56]}")
                    try:
                        try_accept(rec, tmp, form or "未分类", why or "",
                                   pre_hits=sem_hits.get(rec["aweme_id"]))
                    except Exception as exc:
                        logger.warning("%s 入库失败: %s", rec["aweme_id"], exc)
                got = len(list(folder.glob("*.mp4")))
                # ---------- 嗅探兜底:无直链/直链失败的,顺序处理少量 ----------
                sniff_left = 3
                for rec in cands:
                    if got >= args.limit or sniff_left <= 0:
                        break
                    if rec.get("play_url") or rec["aweme_id"] in seen:
                        continue
                    if outcomes.get(rec["aweme_id"], (None, None))[1] is not None:
                        continue
                    tmp = Path(td) / f"{rec['aweme_id']}.mp4"
                    try:
                        if not fetch_video(b, rec, tmp, Path(td)):
                            continue
                        sniff_left -= 1
                        form, why = "未分类", ""
                        if model:
                            ok, form, why = verify_video(model, tmp, rec, tpl)
                            if not ok:
                                tmp.unlink(missing_ok=True)
                                logger.info("验收不通过: %s",
                                            (rec.get("title") or "")[:40])
                                continue
                        try_accept(rec, tmp, form, why)
                    except Exception as exc:
                        logger.warning("%s 嗅探失败: %s", rec["aweme_id"], exc)
                got = len(list(folder.glob("*.mp4")))

    bpool.stop()
    final = len(list(folder.glob("*.mp4")))
    print(f"[{tpl.name}] 完成:{final}/{args.limit}")
    return 0 if final >= args.limit else 1


# ---------- 子命令 ----------
def cmd_login(args) -> int:
    from .browser import DEFAULT_PROFILE_B
    b0 = (CdpBrowser(port=args.port + 1, profile=DEFAULT_PROFILE_B)
          if args.second else CdpBrowser(port=args.port))
    with b0 as b:
        b.navigate("https://www.douyin.com/", settle=6)
        print("=" * 56)
        print("Edge 窗口已打开抖音首页")
        print("请在那个窗口里登录:右上角「登录」→ 手机扫码")
        print("=" * 56)
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            logged = False
            try:
                # document.cookie 看不到 httpOnly 的 sessionid,必须走 CDP
                r = b.call("Network.getCookies",
                           {"urls": ["https://www.douyin.com/"]})
                names = [c.get("name") for c in r.get("cookies", [])]
                logged = "sessionid" in names
            except Exception:
                pass
            if logged:
                print("\n[OK] 已检测到登录态!这个窗口可以留着,也可以关掉")
                return 0
            print(".", end="", flush=True)
            time.sleep(3)
        print("\n[TIMEOUT] 未检测到登录(如实际已登录,直接重跑 hunt 也可以)")
        return 1


def cmd_models(args) -> int:
    from . import glm
    models = glm.list_models()
    flash = [m for m in models if "flash" in m.lower()]
    print(f"可用模型 {len(models)} 个;含 flash 的:")
    for m in flash or models:
        print(" -", m)
    print(f"\n当前 GLM_MODEL={env('GLM_MODEL') or GLM_MODEL_DEFAULT}")
    return 0


def cmd_status(args) -> int:
    from .store import TemplateStore
    store = TemplateStore(args.data)
    dirs = store.templates()
    if not dirs:
        print(f"{args.data}/ 下还没有模板文件夹")
        return 0
    total = 0
    for d in dirs:
        n = len(list(d.glob("*.mp4")))
        total += n
        print(f"{d.name:44s} {n:3d}/10")
    print(f"合计 {len(dirs)} 个模板 {total} 个视频")
    return 0


def cmd_hunt(args) -> int:
    tpl = get_template(args.name)
    if tpl is None:  # 未注册的模板:临时构造
        tpl = Template(
            name=args.name, desc=f"『{args.name}』相关模板视频",
            queries=[args.name, f"{args.name} 仿拍", f"{args.name} 二创"],
            title_must=[args.name],
            forms_ok="与该模板同形式的二创/仿拍",
            forms_bad="无关内容、纯解说科普")
        if args.queries:
            tpl.queries = args.queries.split(",")
    elif args.queries:
        tpl.queries = args.queries.split(",")
        # 用户显式给搜索词时走搜索模式(不进话题收割)
        tpl.browse_only = False
    return run_hunt(args, tpl)


def cmd_batch(args) -> int:
    from .store import TemplateStore
    only = set(args.only.split(",")) if args.only else None
    store = TemplateStore(args.data)
    results: list[tuple[str, int]] = []
    for tpl in all_templates():
        if only and tpl.name not in only:
            continue
        # 满编模板直接跳过,不烧模板间隔(间隔只为真实采集防限流)
        f0 = store.find(tpl.name)
        if f0 and len(list(f0.glob("*.mp4"))) >= args.limit:
            print(f"[{tpl.name}] 已满编,跳过(无间隔)")
            continue
        try:
            rc = run_hunt(args, tpl)
        except (Exception, SystemExit) as exc:
            # SystemExit=GLM 探活彻底失败之类:跳过这个模板换下一个,
            # 不能让整轮批量中断(2026-10-21 事故:z.ai 抖动杀掉全链)
            logger.error("[ %s ] 整轮异常: %s", tpl.name, exc)
            rc = 2
        results.append((tpl.name, rc))
        time.sleep(args.gap)  # 模板间歇一歇,降低风控风险
    print("\n" + "=" * 56 + "\n批量采集汇总:")
    ok = 0
    for name, rc in results:
        mark = "完成" if rc == 0 else f"未满(rc={rc})"
        ok += rc == 0
        print(f"  {name:24s} {mark}")
    print(f"共 {len(results)} 个模板,{ok} 个完成")
    return 0 if ok == len(results) else 1


def cmd_scout(args) -> int:
    from . import scout
    return scout.run_scout(args)


def cmd_analyze(args) -> int:
    from . import analyze, glm
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"找不到文件夹:{folder}")
        return 1
    model = glm.probe_model(args.model)
    print(f"使用模型:{model}")
    report = analyze.analyze_folder(folder, model,
                                    frames_per_video=args.frames)
    print("\n" + "=" * 60 + "\n综合结论:\n" + report)
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_log()
    p = argparse.ArgumentParser(prog="hunter")
    p.add_argument("--data", default="data", help="模板库根目录(默认 data)")
    p.add_argument("--port", type=int, default=CDP_PORT_DEFAULT)
    p.add_argument("--profile", choices=["a", "b"], default="a",
                   help="账号 profile(a=19223 主号,b=19224 备号)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--model", default=env("GLM_MODEL") or "glm-5.3-flash")
        sp.add_argument("--no-verify", action="store_true",
                        help="关闭 GLM 视觉验收(默认开启)")
        sp.add_argument("--limit", type=int, default=10)
        sp.add_argument("--query-gap", type=int, default=40,
                        help="同一模板内搜索词间隔秒数(默认40,防限流)")
        sp.add_argument("--single", action="store_true",
                        help="单账号模式(不轮换;双进程并行时各绑一个账号)")

    sp = sub.add_parser("login", help="打开 Edge 扫码登录抖音")
    sp.add_argument("--second", action="store_true", help="登录第二个账号")
    sp.add_argument("--timeout", type=int, default=600)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("models", help="列出智谱可用模型")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("status", help="查看采集进度")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("hunt", help="采集一个模板的视频")
    sp.add_argument("name", help="模板名(注册表内的名字,或任意新名字)")
    sp.add_argument("--queries", help="自定义逗号分隔搜索词(覆盖默认)")
    add_common(sp)
    sp.set_defaults(func=cmd_hunt)

    sp = sub.add_parser("batch", help="批量采集注册表全部/指定模板")
    sp.add_argument("--only", help="逗号分隔的模板名(默认全部)")
    sp.add_argument("--gap", type=int, default=120,
                    help="模板间隔秒数(默认120,防限流)")
    add_common(sp)
    sp.set_defaults(func=cmd_batch)

    sp = sub.add_parser("analyze", help="GLM 分析一个模板文件夹")
    sp.add_argument("folder")
    sp.add_argument("--model", default=env("GLM_MODEL") or "glm-5.3-flash")
    sp.add_argument("--frames", type=int, default=8)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("scout", help="自主发现新模板候选(热榜→筛→验证)")
    sp.add_argument("--seeds", default="",
                    help="逗号分隔的外部候选词(跳过热榜步,给 AI 会话喂词用)")
    add_common(sp)
    sp.set_defaults(func=cmd_scout)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
