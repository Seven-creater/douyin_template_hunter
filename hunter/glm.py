"""GLM 多模态客户端(走 Anthropic Messages 协议端点,z.ai)。

"看视频"的实现:ffmpeg 均匀抽帧 → 帧图 base64 走 messages 视觉输入。
协议:POST {base}/v1/messages,鉴权 x-api-key + Bearer 双保险。
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from pathlib import Path

import requests

from .config import env

logger = logging.getLogger(__name__)

# Anthropic 兼容端点(z.ai;即用户 Claude Code 所用通道)
ANTHROPIC_BASE = env("GLM_ANTHROPIC_BASE", "https://api.z.ai/api/anthropic")
GLM_MODEL_DEFAULT = env("GLM_MODEL", "glm-5.3-flash")
ANTHROPIC_VERSION = "2023-06-01"


def api_key() -> str:
    return env("ZHIPU_API_KEY").strip()


def _headers() -> dict:
    key = api_key()
    if not key:
        raise SystemExit("缺少 ZHIPU_API_KEY:请把 key 写入 .env "
                         "(ZHIPU_API_KEY=xxx) 后重试")
    return {"x-api-key": key,
            "Authorization": f"Bearer {key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json"}


def list_models() -> list[str]:
    r = requests.get(f"{ANTHROPIC_BASE}/v1/models", headers=_headers(),
                     timeout=20)
    r.raise_for_status()
    return sorted(m.get("id", "") for m in r.json().get("data", []))


def probe_model(model: str) -> str:
    """发一条最小请求,验证模型可用。返回实际可用模型名。

    注意 glm-5.3-flash 是思考型模型,max_tokens 必须留足思考预算。
    """
    for cand in dict.fromkeys([model, GLM_MODEL_DEFAULT, "glm-5.3-flash"]):
        if not cand:
            continue
        try:
            ask(cand, "回复:OK", max_tokens=1000)
            return cand
        except RuntimeError as exc:
            logger.warning("模型 %s 不可用: %s", cand, str(exc)[:150])
    raise SystemExit("没有可用的 GLM 模型,运行 python -m hunter models 查看")


# ---------- 抽帧 ----------
def probe_duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError):
        return 0.0


def extract_frames(video: Path, n: int = 8, workdir: Path | None = None,
                   width: int = 480) -> list[Path]:
    """均匀抽 n 帧(缩放到 width 宽,控制 token)。"""
    workdir = workdir or video.parent / f"{video.stem}_frames"
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("f_*.jpg"):
        old.unlink()
    duration = probe_duration(video)
    if duration <= 0:
        return []
    fps = max(n / duration, 0.05)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video),
           "-vf", f"fps={fps:.4f},scale={width}:-2", "-frames:v", str(n),
           str(workdir / "f_%02d.jpg")]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("抽帧失败 %s: %s", video.name, exc)
        return []
    return sorted(workdir.glob("f_*.jpg"))


# ---------- 对话 ----------
def _image_block(path: Path) -> dict:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": b64}}


def ask(model: str, text: str, frames: list[Path] | None = None,
        max_tokens: int = 4000, temperature: float = 0.3,
        max_attempts: int = 4, thinking_budget: int | None = None) -> str:
    """发一条多模态消息(文本 + 可选帧图),返回模型文本。"""
    blocks: list[dict] = [{"type": "text", "text": text}]
    for f in (frames or []):
        blocks.append(_image_block(f))
    # glm-5.3-flash 是思考型模型:不限制思考预算时,思考可能吃光
    # max_tokens 导致正文为空。实测 budget_tokens 可有效封顶。
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "thinking": {"type": "enabled",
                     "budget_tokens": thinking_budget
                     or min(1200, max(400, max_tokens // 2))},
        "messages": [{"role": "user", "content": blocks}],
    }
    last_err = ""
    empty_retried = False
    attempt = 0
    # 长输出(16k token)生成耗时可能超过 3 分钟,超时随规模伸缩
    http_timeout = max(180, max_tokens // 40)
    while attempt < max_attempts:
        attempt += 1
        try:
            r = requests.post(f"{ANTHROPIC_BASE}/v1/messages",
                              headers=_headers(), json=body,
                              timeout=http_timeout)
        except requests.RequestException as exc:
            wait = 5 * attempt
            last_err = f"网络异常: {exc}"
            logger.warning("GLM 网络异常(%s),%ds 后第 %d 次重试",
                           str(exc)[:120], wait, attempt)
            time.sleep(wait)
            continue
        # 400 里 z.ai 偶发 1210(Invalid API parameter)实测是接口抖动,
        # 同请求重试就过——归入可重试;其余 4xx 是真参数错,立刻抛
        if (r.status_code == 429 or r.status_code >= 500
                or (r.status_code == 400 and "1210" in r.text[:500])):
            wait = 5 * attempt
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.warning("GLM 暂时不可用(%s),%ds 后第 %d 次重试",
                           last_err, wait, attempt)
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"GLM HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        texts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        out = "\n".join(t for t in texts if t).strip()
        if not out:
            # 正文为空:思考失控吃光了预算,压缩思考再来一次
            if not empty_retried:
                empty_retried = True
                body["thinking"]["budget_tokens"] = 200
                logger.warning("GLM 空正文,压缩思考预算至 200 重试")
                attempt -= 1  # 不占用重试次数
                continue
            raise RuntimeError(
                f"GLM 返回空内容(stop_reason={data.get('stop_reason')}): "
                f"{json.dumps(data)[:300]}")
        return out
    raise RuntimeError(f"GLM 重试耗尽: {last_err}")
