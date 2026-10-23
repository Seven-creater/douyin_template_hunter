"""模板分析:对单个模板文件夹里的 N 个视频做 GLM 多模态分析。

两步:
1. 逐视频:抽帧 → GLM 描述(场景/人物/字幕台词/剧情节点/风格)
2. 综合:把 10 份描述 + 元数据交给 GLM,提炼模板的共同结构
   (固定元素/转折点/BGM/变化维度),并给出"如何找更多同类模板"的建议
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import glm

logger = logging.getLogger(__name__)

DESC_PROMPT = """你是短视频内容分析专家。这是抖音同一条视频按时间顺序均匀抽取的{n}帧画面。
视频标题:{title}
作者:{author}
BGM/原声:{music}
点赞数:{likes}

请客观描述:
1. 场景与人物(真人/动漫/AI 生成?几人?外观特征)
2. 每个画面上的字幕/台词(原样抄录中文)
3. 剧情推进:按帧顺序概括发生了什么,如有转折点明确指出在第几帧
4. 拍摄手法(固定机位?运镜?分屏?特效?)
5. 你推断的 BGM 类型/节奏(结合画面情绪)
只描述看到的,不要臆测看不到的内容。"""

SYNTH_PROMPT = """你是一位短视频爆款研究员。下面是抖音同一个模板"{name}"下的 {k} 个不同视频的逐帧分析:

{digest}

请回答:
一、这个模板的共性公式是什么?
1. 固定不变的元素(台词/字幕句式/BGM/剧情骨架/转折点/时长结构)
2. 变化的维度(人物/画风/场景/局部改编)
3. 估计的传播形态(原声跟拍?拍同款?二创改编?)
二、逐条判断:哪些视频确实是该模板的合格样本,哪些可能混入了不相关内容(给视频编号和理由)。
三、如果想找到更多同模板视频,应该用什么抖音搜索词/策略?给出 5 个以上具体查询词。
四、从这批样本出发,总结"如何发现其他热门模板"的可复用规律(3 条以上)。"""


MERGE_PROMPT = """你是短视频爆款研究员。下面是对"{name}"模板视频分批分析得到的 {k} 份阶段性结论:

{partials}

请合并成一份最终报告:合并重复的结论、保留每份独有的发现,按
「模板共性公式(固定元素/变化维度)/逐条合格性判断/扩展搜索词/可复用的模板发现规律」
四部分组织。直接输出报告正文。"""


def _synthesize(model: str, name: str, digests: list[str]) -> str:
    """综合归纳。

    实测(z.ai Anthropic 端点):budget_tokens 是软约束,该模型对此类
    提示的思考量约 2k+;总 max_tokens 必须 ≥16k 才能装下思考+正文。
    单次失败则回退到 3 条一组的 map-reduce。
    """
    def one(ds: list[str], mt: int = 16000) -> str:
        return glm.ask(model, SYNTH_PROMPT.format(
            name=name, k=len(ds), digest="\n\n".join(d[:900] for d in ds)),
            max_tokens=mt, thinking_budget=2000)

    try:
        return one(digests)
    except RuntimeError:
        logger.warning("全量综合失败,回退 map-reduce")
    chunks = [digests[i:i + 3] for i in range(0, len(digests), 3)]
    partials = [one(ch, mt=8000) for ch in chunks]
    return glm.ask(model, MERGE_PROMPT.format(
        name=name, k=len(partials), partials="\n\n".join(partials)),
        max_tokens=8000, thinking_budget=2000)


def analyze_folder(folder: Path, model: str, frames_per_video: int = 8,
                   max_videos: int = 10) -> str:
    import json
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8")) \
        if (folder / "metadata.json").exists() else {"videos": []}
    by_file = {v["file"]: v for v in meta.get("videos", [])}

    videos = sorted(folder.glob("*.mp4"))[:max_videos]
    if not videos:
        raise SystemExit(f"{folder} 里没有 mp4")

    # 摘要缓存:digests.json 已有的文件不重算
    cache_path = folder / "digests.json"
    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}

    digests: list[str] = []
    for vp in videos:
        info = by_file.get(vp.name, {})
        if vp.name in cache and cache[vp.name]:
            digests.append(cache[vp.name])
            logger.info("使用缓存摘要: %s", vp.name)
            continue
        frames = glm.extract_frames(vp, n=frames_per_video)
        if not frames:
            logger.warning("跳过 %s(抽帧失败)", vp.name)
            continue
        prompt = DESC_PROMPT.format(
            n=len(frames),
            title=info.get("title") or "(未知)",
            author=info.get("author") or "(未知)",
            music=info.get("music") or "(未知)",
            likes=info.get("likes") if info.get("likes") is not None else "(未知)")
        logger.info("分析 %s(%d 帧)...", vp.name, len(frames))
        try:
            desc = glm.ask(model, prompt, frames, max_tokens=4000)
        except RuntimeError as exc:
            logger.warning("%s 分析失败,跳过: %s", vp.name, str(exc)[:150])
            desc = f"(该视频分析失败:{str(exc)[:100]})"
        entry = (f"### 视频 {vp.name}\n元数据: 标题={info.get('title')} "
                 f"作者={info.get('author')} BGM={info.get('music')}\n{desc}")
        cache[vp.name] = entry
        digests.append(entry)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    # 综合:map-reduce,失败也不致命
    report = "(综合步骤失败:GLM 未产出结论,分视频摘要见上)"
    try:
        report = _synthesize(model, meta.get("template", folder.name), digests)
    except RuntimeError as exc:
        logger.error("综合失败(报告仍会保存分视频摘要): %s", str(exc)[:200])

    out = folder / "analysis.md"
    out.write_text(
        f"# 模板分析:{meta.get('template', folder.name)}\n\n"
        f"- 模型:{model}\n- 视频数:{len(digests)}\n- 每视频帧数:{frames_per_video}\n\n"
        + "\n\n---\n\n".join(digests)
        + "\n\n---\n\n# 综合结论\n\n" + report,
        encoding="utf-8")
    logger.info("报告已写入 %s", out)
    return report
