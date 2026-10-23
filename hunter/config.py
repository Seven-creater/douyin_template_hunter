"""hunter 配置:环境变量 + 极简 .env 加载(零第三方依赖)。"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> None:
    """加载 .env 到环境变量(不覆盖已有值)。搜索顺序:cwd → 仓库根。"""
    if path is None:
        for cand in [Path.cwd() / ".env",
                     Path(__file__).resolve().parent.parent / ".env"]:
            if cand.exists():
                path = cand
                break
    if path is None or not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    load_dotenv()
    return os.environ.get(name, default)


# 智谱 BigModel(OpenAI 兼容协议);国内 open.bigmodel.cn,国际 api.z.ai
ZHIPU_BASE_URL = env("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
GLM_MODEL_DEFAULT = env("GLM_MODEL", "glm-5.3-flash")

# CDP 调试端口(避开 share_link.py 的 19222)
CDP_PORT_DEFAULT = 19223
