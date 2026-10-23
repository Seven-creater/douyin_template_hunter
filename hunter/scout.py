"""模板自主发现(2026-10-22 用户需求:模板不要人给,GLM 自己找)。

流程:抖音热榜抓词 → GLM 按 skill 规则 1 筛"像模板的"(具体桥段/句式、
非泛话题) → 抖音侧证据验证(多作者同剧本?) → 写 data/scout_backlog.json。
证据验证用一次搜索(受限接口,词间隔 ≥60s)+ 详情页,候选多时较慢;
AI 会话模式下可先用外部搜索喂词(--seeds 参数跳过热榜步)。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import glm
from .browser import CdpBrowser

logger = logging.getLogger(__name__)

FILTER_PROMPT = """下面是抖音热榜词条。判断哪些像【模板视频】的种子——
模板 = 一个固定剧本/句式被很多不同人翻拍演绎(A 族影视名场面型:某部戏
的某个具体桥段,如"华强买瓜";B 族原生句式型:某句流行语/文学梗被接力,
如"多吃方便面方便多见面")。
合格信号:含具体台词/名场面名/句式;有"翻拍/同款/模仿/挑战/接龙"生态。
不合格:泛热点事件、明星八卦、体育赛果、政策新闻、纯音乐/影视上映、
泛话题(如"XX名场面"没指明哪个场面)。

词条列表:
{words}

只输出合格的,每行一个:{{'名称': '热榜原词', '模板句式': '猜它的固定台词/剧本',
 '族': 'A或B', '理由': '一句话'}}
JSON 数组格式,没有合格的输出 []。"""

EVIDENCE_PROMPT = """判断这组抖音视频是否构成一个【模板生态】:多个【不同作者】
各自演绎【同一套固定剧本/句式】(台词链或结构相同,人/场景/画风不同)。

先过前置门槛(不满足直接判不是):
- 必须是【演绎性内容】:有剧情表演/角色扮演/句式文学演绎,观众看视频
  本身能收获它表达的内容
- 教程/资讯/科普/带货/软件教学类即使多作者同结构也算【不是】(2026-10-22
  实锤误判:手机号查抖音号教程被判成模板——信息类内容不是模板)
- 营销号批量搬运/同一素材多号分发不算

候选视频(标题|作者|时长s):
{items}

严格判断:
- 同一作者多发不算;搬运/混剪同一素材不算;只是都提到同一话题但内容
  各异不算
- 需要至少 3 个不同作者、骨架(台词链/结构)明显同源
输出一行:【是模板】或【不是】,后跟一句话依据。"""


def hot_words(b: CdpBrowser, limit: int = 40) -> list[str]:
    """从抖音任意页的 hot/search/list 接口抓热搜词。"""
    b.clear_events()
    b.navigate("https://www.douyin.com/", settle=8)
    b.scroll(times=1, pause=1.5)
    b.pump(2)
    words: list[str] = []
    for ev in b.take_events():
        try:
            u = ev["params"]["response"]["url"]
        except (KeyError, TypeError):
            continue
        if "hot/search" not in u:
            continue
        body = b.body(ev["params"]["requestId"])
        if not body:
            continue
        try:
            p = json.loads(body)
        except ValueError:
            continue
        stack = [p]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                w = o.get("word") or o.get("sentence_tag")
                if isinstance(w, str) and w.strip() and w not in words:
                    words.append(w.strip())
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return words[:limit]


def filter_candidates(model: str, words: list[str]) -> list[dict]:
    """分批筛(每批 ≤25 词,输出短不易被思考预算截断),逐批重试。"""
    out: list[dict] = []
    for k in range(0, len(words), 25):
        batch = words[k:k + 25]
        prompt = FILTER_PROMPT.format(
            words="\n".join(f"{i}. {w}" for i, w in enumerate(batch, 1)))
        for _ in range(3):
            try:
                # 筛选是长思考任务:实测 4000 配额思考吃光正文,
                # 必须 16000 总量 + 2000 思考预算才稳定
                ans = glm.ask(model, prompt, max_tokens=16000,
                              thinking_budget=2000)
                txt = ans.strip()
                i, j = txt.find("["), txt.rfind("]")
                if i >= 0 and j > i:
                    data = json.loads(txt[i:j + 1])
                    out.extend(d for d in data
                               if isinstance(d, dict) and d.get("名称"))
                    break
            except Exception as exc:
                logger.warning("候选筛选失败: %s", str(exc)[:120])
                time.sleep(5)
    return out


def evidence_check(b: CdpBrowser, model: str, phrase: str) -> dict:
    """一次搜索取证:多作者同剧本?A 族候选首查多是原片搬运时,
    自动带"模仿/翻拍"变体词重试一次再判死(2026-10-23:王传君拜佛
    原句查出来全是同源搬运,变体词才翻出演绎生态)。"""
    from . import douyin
    for k, q in enumerate(dict.fromkeys([phrase, f"{phrase} 模仿",
                                         f"{phrase} 翻拍"])):
        if k:
            time.sleep(15)     # 变体词之间也留间隔,防限流
        try:
            records = douyin.search(b, q, want=12)
        except Exception as exc:
            return {"ok": False, "why": f"搜索失败: {str(exc)[:80]}"}
        verdict = _judge_items(model, records)
        if verdict is not None:
            return verdict
        # 该词结果判不出(素材同源/太杂)→ 换下一个变体词
    return {"ok": False,
            "why": "原句/模仿/翻拍三个词均未翻出多人演绎生态",
            "samples": _fmt(records)[:10]}


def _fmt(records: list[dict]) -> list[str]:
    return [f"{(r.get('title') or '')[:30]} | {r.get('author') or '?'} | "
            f"{(r.get('duration_ms') or 0)//1000}s" for r in records]


def _judge_items(model: str, records: list[dict]) -> dict | None:
    """GLM 判定;返回 None 表示本批判不出(等价失败,让上层换词)。"""
    items = _fmt(records)
    if len(items) < 3:
        return None
    prompt = EVIDENCE_PROMPT.format(items="\n".join(items[:10]))
    try:
        # 判定同样要大配额(小配额思考吃光正文,2026-10-22 实测)
        verdict = glm.ask(model, prompt, max_tokens=8000,
                          thinking_budget=1500).strip()
    except Exception as exc:
        return {"ok": False, "why": f"GLM失败: {str(exc)[:80]}",
                "samples": items}
    if verdict.startswith("【是模板】"):
        return {"ok": True, "verdict": verdict, "samples": items,
                "n": len(items)}
    if verdict.startswith("【不是】"):
        return None     # 该词不行,换变体词再试
    return None


def run_scout(args) -> int:
    from .store import TemplateStore
    store = TemplateStore(args.data)
    model = glm.probe_model(args.model)
    b = CdpBrowser(port=args.port)
    b.connect()

    seeds = [s.strip() for s in (args.seeds or "").split(",") if s.strip()]
    if seeds:
        cands = [{"名称": s, "模板句式": "", "族": "", "理由": "外部搜索喂词"}
                 for s in seeds]
    else:
        words = hot_words(b, limit=50)
        print(f"热榜抓到 {len(words)} 词", flush=True)
        cands = filter_candidates(model, words)
        print(f"GLM 筛出 {len(cands)} 个疑似模板", flush=True)
    for c in cands[:8]:
        print(f"  · {c.get('名称')} [{c.get('族','?')}] "
              f"{(c.get('模板句式') or '')[:20]} {(c.get('理由') or '')[:30]}",
              flush=True)

    backlog_p = Path(args.data) / "scout_backlog.json"
    backlog = json.loads(backlog_p.read_text(encoding="utf-8")) \
        if backlog_p.exists() else []
    known = {d.name[3:] for d in store.templates()} | \
        {x.get("name") for x in backlog}
    new_ok = 0
    for c in cands[:8]:
        name = (c.get("名称") or "").strip()
        if not name or name in known:
            continue
        print(f"\n验证: {name} ...", flush=True)
        ev = evidence_check(b, model, c.get("模板句式") or name)
        mark = "✓" if ev.get("ok") else "✗"
        print(f"  {mark} {(ev.get('verdict') or ev.get('why') or '')[:70]}",
              flush=True)
        backlog.append({"name": name, "phrase": c.get("模板句式") or name,
                        "family": c.get("族"), "evidence": ev, "at":
                        time.strftime("%Y-%m-%d %H:%M")})
        new_ok += bool(ev.get("ok"))
        time.sleep(60)      # 取证搜索走受限接口,间隔拉满
    backlog_p.write_text(json.dumps(backlog, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    b.stop()
    print(f"\n完成:新增候选 {len([x for x in backlog if x['name'] not in known])} "
          f"(证据充分 {new_ok}),入库 {backlog_p}", flush=True)
    return 0
