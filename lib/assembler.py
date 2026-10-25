"""视频拼接 + manifest 生成模块。

设计要点：
- concat demuxer：所有 clip 已统一规格，可用 -c copy 秒拼（无需重编码）
- manifest.json 作为数据契约：记录每段的源、关键词、处理策略，便于后续手动替换片段
- 单一职责：本模块只负责"把已准备好的 clip 拼成最终视频 + 写 manifest"
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .clip_processor import ClipResult


@dataclass
class SegmentManifest:
    """manifest.json 中单个 segment 的结构。"""

    index: int
    start: str                       # SRT 时间戳格式 HH:MM:SS,mmm
    end: str
    duration: float                  # 秒
    original_text: str
    en_text: str
    keywords: list[str]
    is_gap: bool
    source: str                      # "pexels" / "pixabay" / "none"
    video_url: str
    video_id: str
    clip_path: str
    processing: str                  # cut / slow_down / loop / none
    processing_params: dict = field(default_factory=dict)


@dataclass
class Manifest:
    """manifest.json 顶层结构。"""

    input_srt: str
    target_duration: float
    resolution: str
    fps: int
    segments: list[SegmentManifest] = field(default_factory=list)
    final_video: str = ""


class VideoAssembler:
    """视频拼接器：concat 已处理好的 clip 列表 + 输出 manifest.json。"""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        self._clips_dir = self._output_dir / "clips"
        self._clips_dir.mkdir(parents=True, exist_ok=True)

    def concat_clips(self, clip_paths: list[Path], final_path: Path) -> None:
        """用 ffmpeg concat demuxer 拼接 clip 列表。

        前提：所有 clip 已被 ClipProcessor 统一为相同规格（1920x1080 30fps H.264），
        因此可用 -c copy 秒拼，无需重编码。

        Args:
            clip_paths: 按播放顺序排列的 clip 路径列表
            final_path: 最终视频输出路径（如 output/final.mp4）

        Raises:
            RuntimeError: ffmpeg 失败
            ValueError: clip_paths 为空
        """
        if not clip_paths:
            raise ValueError("clip_paths 不能为空")

        # 生成 concat 列表文件
        # 注意：路径中的单引号需要转义为 '\'' （ffmpeg concat demuxer 的特殊要求）
        list_file = self._output_dir / "_concat_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for p in clip_paths:
                # 用绝对路径，避免工作目录差异
                abs_path = Path(p).resolve().as_posix()
                escaped = abs_path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # concat demuxer + stream copy（不重编码，秒级完成）
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",          # 允许绝对路径
            "-i", str(list_file),
            "-c", "copy",          # 直接复制流，不重编码
            "-movflags", "+faststart",
            str(final_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
        except subprocess.CalledProcessError as e:
            err = e.stderr or "无 stderr"
            raise RuntimeError(
                f"ffmpeg concat 失败\n命令: {' '.join(cmd)}\n错误: {err}"
            ) from e
        finally:
            # 清理临时列表文件
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass

    def write_manifest(
        self,
        manifest: Manifest,
        manifest_path: Optional[Path] = None,
    ) -> Path:
        """把 manifest 写为 JSON 文件。

        Args:
            manifest: Manifest 数据对象
            manifest_path: 输出路径，默认 output/manifest.json

        Returns:
            实际写入的路径
        """
        if manifest_path is None:
            manifest_path = self._output_dir / "manifest.json"

        # dataclass → dict → JSON
        data = {
            "input_srt": manifest.input_srt,
            "target_duration": manifest.target_duration,
            "resolution": manifest.resolution,
            "fps": manifest.fps,
            "segments": [asdict(s) for s in manifest.segments],
            "final_video": manifest.final_video,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return manifest_path

    @staticmethod
    def build_segment_manifest(
        index: int,
        start_ts: str,
        end_ts: str,
        duration: float,
        original_text: str,
        en_text: str,
        keywords: list[str],
        is_gap: bool,
        source: str,
        video_url: str,
        video_id: str,
        clip_path: Path,
        clip_result: Optional[ClipResult],
    ) -> SegmentManifest:
        """工厂方法：把散落的数据组装成 SegmentManifest。"""
        processing = "none"
        params: dict = {}
        if clip_result is not None:
            processing = clip_result.strategy.value
            params = {
                "speed": clip_result.speed,
                "cut_from": clip_result.cut_from,
                "cut_to": clip_result.cut_to,
            }
        return SegmentManifest(
            index=index,
            start=start_ts,
            end=end_ts,
            duration=duration,
            original_text=original_text,
            en_text=en_text,
            keywords=keywords,
            is_gap=is_gap,
            source=source,
            video_url=video_url,
            video_id=video_id,
            clip_path=str(clip_path),
            processing=processing,
            processing_params=params,
        )
