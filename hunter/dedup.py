"""画面感知去重:ffmpeg 定点抽帧 → dHash 梯度指纹 → 汉明距离。

目的(用户 2026-10-20 要求):同一段素材的"换皮搬运"(加水印/字幕/换
BGM/轻微裁剪)指纹几乎不变,判为近重复;不同人/不同形式的翻拍像素结构
差异大,不受影响。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

SIZE = 32                     # 32x32 灰度 → 每帧 (32*31)=992 bit
ANCHORS = (0.25, 0.50, 0.75)  # 三锚点帧
DUP_THRESHOLD = 70            # 距离 < 此值判为近重复(实测标定)


def _duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _frame_bits(video: Path, t: float) -> list[int] | None:
    """取 t 秒处一帧 32x32 灰度,返回 1024 像素值;失败 None。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-ss", f"{t:.2f}", "-i", str(video),
           "-vf", f"scale={SIZE}:{SIZE}", "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.SubprocessError:
        return None
    if len(out.stdout) < SIZE * SIZE:
        return None
    return list(out.stdout[:SIZE * SIZE])


def _dhash(pixels: list[int]) -> int:
    """水平梯度哈希:每行比较相邻像素,bit=左>右。"""
    bits = 0
    for y in range(SIZE):
        row = pixels[y * SIZE:(y + 1) * SIZE]
        for x in range(SIZE - 1):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def video_hashes(video: Path) -> list[int]:
    """三锚点帧的 dHash 列表;视频太短则退化为可用锚点。"""
    dur = _duration(video)
    if dur <= 0:
        return []
    hashes = []
    for frac in ANCHORS:
        px = _frame_bits(video, dur * frac)
        if px is not None:
            hashes.append(_dhash(px))
    return hashes


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def distance(h1: list[int], h2: list[int]) -> int:
    """两视频指纹距离:所有锚点帧两两组合的最小汉明距离。

    搬运件只要保住任意一锚点处的画面,距离就会很低;而不同制作的
    视频在任何锚点都不同。
    """
    if not h1 or not h2:
        return 9999  # 无法计算时视为不重复(不误杀)
    return min(hamming(a, b) for a in h1 for b in h2)
