"""分析 22 号已入库的 5 个合格演绎版,提炼通用判据(供 skill 固化)。

每个视频抽 6 帧,让 GLM 描述:演了什么、对手互动在哪、情节推进结构、
为什么合格。输出给我做综合。
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from hunter import glm

PROMPT = """这是一个抖音"模板翻拍"视频(模板来源:《疯狂动物城》尼克狐"你被捕了"
名场面),它是经过人工确认的【合格演绎版】。按时间顺序给了 6 帧。

请分析:
1. 演了什么:角色有谁(原版角色还是换了什么角色),发生了什么事
2. 对手互动:谁对谁做了什么,冲突在哪
3. 情节结构:起-承-转折-果 各是什么(哪怕很短)
4. 合格原因:用一句话概括"它和'一个人对镜头念台词'的本质区别"

简洁回答,每点一行。"""


def main() -> None:
    model = glm.probe_model("glm-5.3-flash")
    folder = Path("data/22_尼克狐你被捕了")
    for n in range(1, 6):
        fp = folder / f"{n:02d}.mp4"
        if not fp.exists():
            continue
        frames = glm.extract_frames(fp, n=6, workdir=Path("logs") / f"ex{n:02d}")
        if not frames:
            print(f"--- {n:02d}.mp4 抽帧失败 ---")
            continue
        try:
            out = glm.ask(model, PROMPT, frames, max_tokens=3000,
                          thinking_budget=1000)
        except Exception as exc:
            out = f"分析失败: {exc}"
        print(f"===== {n:02d}.mp4 =====")
        print(out.strip()[:1200])
        print()


if __name__ == "__main__":
    main()
