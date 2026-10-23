"""模板验收:粗筛(零成本)+ GLM 抽帧视觉验收(模板感知 + 画面形式分类)。

"不烧心"的标准从 2026-10-20 用户审核的正/负样本学到:真判据是
剧情/形式骨架,不是台词/标签/BGM 单独出现。
同日用户补充:同一模板下 10 条要"画面多元化"——不同作者搬运同一段
原剧算同一种画面,所以验收时顺便分类画面形式,选择时按形式配额。
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import glm
from .templates import Template

logger = logging.getLogger(__name__)


def prefilter(record: dict, tpl: Template) -> bool:
    """搜索结果粗筛:时长 + 标题信号。跳过时记录明确原因。"""
    dur = (record.get("duration_ms") or 0) // 1000
    lo, hi = tpl.duration
    title = (record.get("title") or "")[:46]
    if lo and dur and not (lo <= dur <= hi):
        logger.info("粗筛跳过(时长 %ds 不在 %d-%ds): %s", dur, lo, hi, title)
        return False
    if not tpl.prefilter_title(record.get("title") or ""):
        logger.info("粗筛跳过(标题不符): %s", title)
        return False
    return True


VERIFY_PROMPT = """你是抖音"模板视频"验收员。两关都过才算通过。

第一关·模板匹配——判断视频是不是【{name}模板】这场戏:
模板定义:{desc}
合格形式:{forms_ok}
【不合格】:{forms_bad}
判定尺度:这场戏的核心桥段(冲突与转折)在画面中成立即算匹配,改编版可省略
原版细节;但剧情完全不同、或只挂相关字眼的不算。

第二关·演绎版硬标准(一票否决,2026-10-21 从人工确认合格样本提炼):
合格 = 对手戏双向互动 + 起承转折果完整弧线 + 动作/道具/表情反应链:
- 有人对另一个人做事、对方有反应(执法→慌张;扑按→反咬"你个骗子";
  骗→怒→捕→哭→哄)——绝不是一个人对镜头单向输出
- 【对手必须在画面内】画外音/旁白配合的单人摆拍不算对手戏——只有
  一个真人出镜、另一方的"指认/台词"全靠画外音,这是摆拍不是演绎
- 弧线完整即可,7-8 秒也合格(合格样本有实测);时长本身不是判据
- 角色/画风可以任意换(宿舍真人、真人+原片合拍、AI动画、其他IP跨界),
  剧本骨架(这场戏的冲突与转折)不变就行
【决定性检验】对比首帧和末帧:如果首尾基本是同一个人在原地说话/做表情/
扭动,没有对手、没有镜头切换、没有动作推进——不通过,这不是演绎。
以下一律不通过(即使有cos装扮、即使喊出模板台词):
- 单人对镜头喊台词/做表情/扭脸扭身体("我先cos上了"式换装展示)
- 分屏/对比式的cos展示
- 变装摆姿势拍照;配音/对口型(含给原片画面重新配音)
- 跟随音乐跳舞/扭动;说唱/BGM/混剪类
- 综艺/晚会/演出片段;纯文字/静态图/录屏

视频信息:标题「{title}」 作者「{author}」 BGM「{music}」 时长 {dur}s
下面是按时间顺序抽取的 {n} 帧画面。可读的字幕请抄录作为剧情证据。

请先用2-3句说明看到的画面内容和情节,再严格回答(共两行):
倒数第二行写画面形式分类。判定标准是【画面的素材来源】——
【形式:素材剪辑】画面主体是原影视/综艺/原剧的画面素材(直接搬运、加字幕、
换配音、混剪、二创配音统统算——只要画面里演的还是原剧演员/原片画面)
【形式:真人翻拍】不是原剧演员,是别人真人出镜重演这场戏
【形式:动画AI】动画/漫画/AI生成画面重演这场戏
【形式:变装cos】真人变装或cosplay装扮演绎
【形式:场景移植】把这场戏搬进宿舍/职场/日常等新场景由素人真人演绎
【形式:其他】以上都不是
最后一行只写【通过】或【不通过】(两关都过才写通过)。"""

FORM_CHOICES = ["素材剪辑", "真人翻拍", "动画AI", "变装cos",
                "场景移植", "其他"]

# 画面形式配额(2026-10-21 终版:模板必须是"大众翻拍热门"——
# 翻拍类形式优先;素材剪辑≤2 永不放开;未分类也按 2 条严控防漏网)
SOURCE_FORM = "素材剪辑"
FORM_CAPS = {SOURCE_FORM: 2, "其他": 2, "未分类": 2}
DEFAULT_FORM_CAP = 4
UNCAPPED_FORMS = {"真人翻拍", "动画AI", "变装cos", "场景移植"}


def verify_video(model: str, mp4: Path, record: dict, tpl: Template,
                 frames_n: int = 6) -> tuple[bool, str, str]:
    """返回 (是否模板视频, 画面形式, 判定说明)。帧取 6 张:演绎版判定
    需要看到情节推进,帧太少会把念台词误判成表演。抽帧写临时目录,
    不污染 data/ 模板文件夹。"""
    import tempfile
    frames = glm.extract_frames(
        mp4, n=frames_n,
        workdir=Path(tempfile.gettempdir()) / "hunter_frames" / mp4.stem)
    if not frames:
        return False, "未分类", "抽帧失败"
    dur = (record.get("duration_ms") or 0) // 1000
    prompt = VERIFY_PROMPT.format(
        name=tpl.name, desc=tpl.desc, forms_ok=tpl.forms_ok,
        forms_bad=tpl.forms_bad,
        title=record.get("title") or "?", author=record.get("author") or "?",
        music=record.get("music") or "?", dur=dur, n=len(frames))
    try:
        answer = glm.ask(model, prompt, frames, max_tokens=4000,
                         thinking_budget=1200)
    except RuntimeError as exc:
        logger.warning("GLM 验收失败(按不通过处理): %s", str(exc)[:200])
        return False, "未分类", f"GLM错误: {str(exc)[:120]}"
    lines = [ln.strip() for ln in answer.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    # 注意:"不通过"包含"通过"子串,必须先判否
    verdict = ("不通过" not in last) and ("通过" in last)
    form = "未分类"
    for ln in lines[-3:]:
        if "形式" in ln:
            for choice in FORM_CHOICES:
                if choice in ln:
                    form = choice
                    break
            break
    return verdict, form, answer.strip()
