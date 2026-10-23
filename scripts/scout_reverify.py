"""重验 scout backlog 里 GLM 失败/判错的候选(2026-10-22)。

用修好的大配额 + 新证据 prompt(带演绎性前置门槛)重新搜索取证,
更新 backlog 的 evidence 字段。
"""
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

BACKLOG = Path("data/scout_backlog.json")
# 用户已确认/已有的模板,重验时跳过
SKIP = {"见面", "尼克狐你被捕了"}


def main() -> None:
    model = glm.probe_model("glm-5.3-flash")
    backlog = json.loads(BACKLOG.read_text(encoding="utf-8"))
    b = CdpBrowser(port=19223)
    b.connect()
    n = 0
    for item in backlog:
        name = item.get("name", "")
        ev = item.get("evidence") or {}
        need = (ev.get("why", "").startswith("GLM") or ev.get("ok"))
        if not need or any(k in name for k in SKIP):
            continue
        n += 1
        print(f"重验: {name} ...", flush=True)
        item["evidence"] = evidence_check(b, model, item.get("phrase") or name)
        mark = "✓" if item["evidence"].get("ok") else "✗"
        print(f"  {mark} {(item['evidence'].get('verdict')
                          or item['evidence'].get('why') or '')[:70]}",
              flush=True)
        time.sleep(60)
        if n >= 7:
            break
    BACKLOG.write_text(json.dumps(backlog, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    b.stop()
    print(f"\n重验完成 {n} 个", flush=True)


if __name__ == "__main__":
    main()
