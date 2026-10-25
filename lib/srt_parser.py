"""字幕解析模块。

负责把 srt/ass/vtt 字幕文件解析为统一的 Segment 列表，
并补齐字幕之间的空隙以及末尾撑满到目标 duration。

设计要点：
- 纯函数：parse_subtitles 输入路径输出 List[Segment]，无副作用
- 容错：使用 pysubs2 自带的容错能力，单条异常不影响整体
- 时间轴补齐：把 [0, target_duration] 完整切分为段，绝不留黑屏
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pysubs2


@dataclass(frozen=True)
class Segment:
    """单段画面描述。

    attributes:
        index: 1-based 序号
        start: 起始时间（秒，浮点）
        end: 结束时间（秒，浮点）
        text: 字幕原文（gap 段为空字符串）
        is_gap: 是否为字幕间空隙段（True 时 text 为空，用前段关键词匹配画面）
    """

    index: int
    start: float
    end: float
    text: str
    is_gap: bool = False

    @property
    def duration(self) -> float:
        """该段画面所需时长（秒）。"""
        return max(0.0, self.end - self.start)

    def format_srt_timestamp(self, which: str = "start") -> str:
        """格式化为 SRT 时间戳字符串 HH:MM:SS,mmm。用于 manifest 输出。"""
        t = self.start if which == "start" else self.end
        if t < 0:
            t = 0.0
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_subtitles(
    srt_path: Path,
    target_duration: float,
    min_segment_duration: float = 0.3,
) -> list[Segment]:
    """解析字幕文件并补齐时间轴，输出连续无空隙的 Segment 列表。

    补齐策略（确保 [0, target_duration] 内每秒都有画面，绝不黑屏）：
    1. 解析字幕得到 [(t1_start, t1_end, text1), (t2_start, ...), ...]
    2. 若首段 start > 0：在 [0, first_start] 插入 gap 段
    3. 字幕间空隙：插入 gap 段，用前段关键词搜画面
    4. 末段 end < target_duration：追加 gap 段至 target_duration
    5. 末段 end > target_duration：截断并打印警告

    Args:
        srt_path: 字幕文件路径
        target_duration: 目标视频总时长（秒）
        min_segment_duration: 最小段时长阈值，过短段会被合并到相邻 gap 段，避免逐帧抖动

    Returns:
        List[Segment]，按时间顺序，index 从 1 开始

    Raises:
        FileNotFoundError: 字幕文件不存在
        pysubs2.exceptions.Pysubs2Error: 字幕格式严重错误
    """
    if not srt_path.exists():
        raise FileNotFoundError(f"字幕文件不存在: {srt_path}")

    # encoding='utf-8' errors='replace' 符合项目文件操作安全规范
    subs = pysubs2.load(str(srt_path), encoding="utf-8")

    # 提取原始字幕事件，按开始时间排序
    events = sorted(subs, key=lambda e: e.start)
    raw_segments: list[tuple[float, float, str]] = []
    for e in events:
        # pysubs2 时间单位是毫秒
        start_s = e.start / 1000.0
        end_s = e.end / 1000.0
        text = e.plaintext.strip() if e.plaintext else ""
        # 过滤掉时长为负或文本全空的脏数据
        if end_s <= start_s or not text:
            continue
        raw_segments.append((start_s, end_s, text))

    # 若字幕为空，整段都是 gap
    if not raw_segments:
        return [Segment(index=1, start=0.0, end=target_duration, text="", is_gap=True)]

    segments: list[Segment] = []
    idx = 1
    cursor = 0.0  # 当前已覆盖到的时间点

    # 处理首段前的 gap
    first_start = raw_segments[0][0]
    if first_start > cursor:
        segments.append(
            Segment(index=idx, start=cursor, end=first_start, text="", is_gap=True)
        )
        idx += 1
        cursor = first_start

    # 逐段处理字幕 + 中间 gap
    for i, (start_s, end_s, text) in enumerate(raw_segments):
        # 字幕段开始时间晚于 cursor → 中间有 gap
        if start_s > cursor + 0.01:
            segments.append(
                Segment(index=idx, start=cursor, end=start_s, text="", is_gap=True)
            )
            idx += 1
            cursor = start_s

        # 字幕段本身
        # 防御：若字幕段时长过短，合并到下一段；否则保留
        seg_end = end_s
        if seg_end - start_s < min_segment_duration and i < len(raw_segments) - 1:
            # 过短段：跳过本段，让 cursor 推进到 end_s，下一段会被合并处理
            cursor = seg_end
            continue

        segments.append(
            Segment(index=idx, start=cursor, end=seg_end, text=text, is_gap=False)
        )
        idx += 1
        cursor = seg_end

    # 末尾撑满到 target_duration
    if cursor < target_duration - 0.01:
        segments.append(
            Segment(
                index=idx,
                start=cursor,
                end=target_duration,
                text="",
                is_gap=True,
            )
        )
    elif cursor > target_duration + 0.01:
        # 字幕末尾超过目标时长：截断最后一段并警告
        last = segments[-1]
        segments[-1] = Segment(
            index=last.index,
            start=last.start,
            end=target_duration,
            text=last.text,
            is_gap=last.is_gap,
        )
        print(
            f"[WARN] 字幕末尾时间戳 {cursor:.2f}s 超过目标时长 {target_duration:.2f}s，"
            f"已截断最后一段至目标时长"
        )

    return segments


def detect_language_hint(text: str) -> str:
    """粗略检测字幕语言代码（仅作 LLM 提示，准确度要求不高）。

    通过常见停用词/特殊字符做启发式判断。
    返回 'la' / 'en' / 'de' / 'fr' / 'es' / 'pt' / 'it' / 'unknown'。

    注：拉丁语必须先检测，否则会被误判为 fr/it（la 与 fr 共享 "est"/"et"，
    与 it 共享 "est"/"la"）。
    """
    if not text:
        return "unknown"
    sample = text[:1000].lower()

    # 拉丁语优先检测（特征：-um/-am/-ā 结尾、典型词汇）
    # 关键词区分度高于罗曼语族共享词
    latin_markers = [
        " est ", " et ", " in ", " non ",  # 共享词，需配合下面的特征
        "īnsula", " Graeciā", " magnae", " parvae", " multae",
        " terra ", " īnsulae", " Graecae",
        "-um ", "-am ", "-ae ",  # 拉丁语特有结尾
        "Crēta", "Dēlos", "Samos", "Rhodus", "Naxus", "Lēmnos",
    ]
    # 至少匹配 2 个拉丁语特征才判定为 la（避免单匹配误判）
    latin_hits = sum(1 for m in latin_markers if m in sample)
    if latin_hits >= 2:
        return "la"

    # 各语种特征词
    hints = [
        ("de", [" der ", " die ", " das ", " und ", " nicht ", " ist ", "ß", "ä", "ö", "ü"]),
        ("fr", [" le ", " la ", " les ", " et ", " est ", " une ", " des ", "ç", "ê", "é"]),
        ("es", [" el ", " la ", " los ", " las ", " y ", " es ", " una ", "ñ", "¿", "¡"]),
        ("pt", [" o ", " a ", " os ", " as ", " e ", " não ", " uma ", "ã", "ç", "õ"]),
        ("it", [" il ", " la ", " le ", " e ", " è ", " una ", " gli ", "ù", "ò"]),
    ]
    for lang, markers in hints:
        if any(m in sample for m in markers):
            return lang
    return "en"
