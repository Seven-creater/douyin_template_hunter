"""语义级画面查重(用户 2026-10-20 指定方案):

"截取第一个视频五张图和第二个视频五张图——人物一样、场景一样就是重复,
删掉,找不一样的,但内容要一样。"

实现:候选视频 3 帧 + 已入库视频代表帧,一次 GLM 调用判断候选与哪些
已入库视频是【同一素材/同一演员同一场景】,命中即拒收/删除。
像素哈希只抓同文件搬运;语义比对抓"同素材不同片段/同演员同布景"。
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import glm

logger = logging.getLogger(__name__)

COMPARE_PROMPT = """你是视频画面查重员。第一组图(编号 C1~C3)是候选视频按时间顺序的帧。
后面的图(编号 R1、R2…)是已入库视频各自的代表帧(视频中点)。

背景:这些视频全部是【同一个模板的不同人翻拍】——剧情相同、台词相同、
穿同款或相似 cos 服装(狐耳/警服等)都非常正常,这不是重复的证据。

判断候选与每个 R 是否来自【同一段实拍素材】,唯一标准:
- 出镜者是【同一个人】:五官长相肉眼可辨是同一张脸(不是相似化妆,
  是同一人)。仅此一条。
- 剧情/构图/机位/服装/道具/布景相同,只要脸不同,就一定不是重复。
- 脸看不清、画质模糊、角度无法比对 → 判该 R 不重复(宁漏勿杀)。

先逐个 R 写一行判断(如「R1:不同人」「R2:看不清→不重复」),
最后严格写一行:【重复:R1,R3】(只列五官明确相同的编号);
全无则最后一行只写:【无重复】。"""


def _mid_frame(video: Path, out: Path) -> Path | None:
    """取视频中点一帧(480 宽),复用 hunter.glm 的 ffmpeg 管线。"""
    dur = glm.probe_duration(video)
    if dur <= 0:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    t = dur / 2
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
           "-vf", "scale=480:-2", "-frames:v", "1", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except subprocess.SubprocessError:
        return None
    return out if out.exists() else None


def rep_frame(video: Path, workdir: Path) -> Path | None:
    return _mid_frame(video, workdir / f"{video.stem}_rep.jpg")


def cand_frames(video: Path, workdir: Path, n: int = 3) -> list[Path]:
    frames = glm.extract_frames(video, n=n, workdir=workdir / "cand")
    return frames


def find_duplicates(model: str, cand: list[Path],
                    reps: list[tuple[str, Path]]) -> list[str]:
    """返回与候选画面同源的已入库视频文件名列表。"""
    if not cand or not reps:
        return []
    text = COMPARE_PROMPT + "\n\n候选帧 C1~C%d 依次在后;然后是代表帧:" % len(cand)
    for i, (name, _) in enumerate(reps, 1):
        text += f" R{i}={name}"
    images = list(cand) + [p for _, p in reps]
    try:
        answer = glm.ask(model, text, images, max_tokens=2000,
                         thinking_budget=400)
    except RuntimeError as exc:
        logger.warning("语义查重调用失败(放行,交由像素查重兜底): %s",
                       str(exc)[:150])
        return []
    last = answer.strip().splitlines()[-1]
    if "无重复" in last:
        return []
    hits = []
    import re
    for m in re.finditer(r"R(\d+)", last):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(reps):
            hits.append(reps[idx][0])
    return hits
