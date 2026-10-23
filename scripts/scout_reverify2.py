"""三变体重验:对指定候选用 原词/模仿/翻拍 三连查重新取证。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from hunter.browser import CdpBrowser
from hunter import glm
from hunter.scout import evidence_check

NAMES = ["王传君拜佛", "甄嬛传滴血验亲", "头文字D 上车",
         "张万森下雪了", "华妃贱人就是矫情"]
BACKLOG = Path("data/scout_backlog.json")


def main() -> None:
    model = glm.probe_model("glm-5.3-flash")
    backlog = json.loads(BACKLOG.read_text(encoding="utf-8"))
    idx = {b.get("name"): b for b in backlog}
    b = CdpBrowser(port=19223)
    b.connect()
    for name in NAMES:
        print(f"重验(三变体): {name} ...", flush=True)
        ev = evidence_check(b, model, name)
        mark = "✓" if ev.get("ok") else "✗"
        print(f"  {mark} {(ev.get('verdict') or ev.get('why') or '')[:75]}",
              flush=True)
        if name in idx:
            idx[name]["evidence"] = ev
        else:
            backlog.append({"name": name, "phrase": name, "family": "A",
                            "evidence": ev,
                            "at": time.strftime("%Y-%m-%d %H:%M")})
        time.sleep(45)
    BACKLOG.write_text(json.dumps(backlog, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    b.stop()
    ok = [b_["name"] for b_ in backlog if (b_.get("evidence") or {}).get("ok")]
    print("\n重验完成;当前证据充分的候选:", ok or "无", flush=True)


if __name__ == "__main__":
    main()
