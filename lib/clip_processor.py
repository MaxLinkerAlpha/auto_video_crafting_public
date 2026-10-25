"""视频片段处理模块：核心时长撑满算法。

设计要点：
- 三策略择优：截取 > 变速慢放(≤2x) > 循环+截取
- 绝对禁止黑屏：任何路径下输出时长都 == target_duration
- 统一规格：所有输出 clip 强制 1920x1080 30fps H.264 无音频
  原因：不同源的分辨率/帧率/编码不一致，concat demuxer 要求统一才能用 -c copy 秒拼
- 依赖注入：通过构造函数接收 AppConfig，便于测试
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import AppConfig, MAX_SLOWDOWN_FACTOR


class ProcessingStrategy(str, Enum):
    """时长撑满策略标识，用于 manifest 记录。"""

    CUT = "cut"                # 截取：v_duration >= target_duration
    SLOW_DOWN = "slow_down"    # 变速慢放：v_duration < target_duration, speed >= 0.5
    LOOP = "loop"              # 循环+截取：speed < 0.5（即需超过2倍慢放）


@dataclass(frozen=True)
class ClipResult:
    """单段处理结果。

    attributes:
        clip_path: 输出 clip 文件路径
        strategy: 实际使用的策略
        speed: 实际播放速度（1.0=原速，0.5=慢放2倍，2.0=加速2倍）
        cut_from: 截取起始秒
        cut_to: 截取结束秒
    """

    clip_path: Path
    strategy: ProcessingStrategy
    speed: float
    cut_from: float
    cut_to: float


class ClipProcessor:
    """视频片段处理器：负责把任意源视频处理为统一规格的目标 clip。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._verify_ffmpeg_available()

    def _verify_ffmpeg_available(self) -> None:
        """尽早失败：启动时检查 ffmpeg 是否在 PATH。"""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "未找到 ffmpeg 可执行文件，请先安装并加入 PATH\n"
                "  Windows: https://ffmpeg.org/download.html\n"
                "  macOS:   brew install ffmpeg\n"
                "  Linux:   sudo apt install ffmpeg"
            )
        if shutil.which("ffprobe") is None:
            raise RuntimeError("未找到 ffprobe 可执行文件（通常与 ffmpeg 一起安装）")

    def _ffprobe_duration(self, video_path: Path) -> float:
        """用 ffprobe 获取视频时长（秒）。

        Args:
            video_path: 视频文件路径

        Returns:
            时长秒数；获取失败返回 0.0
        """
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0.0))
        except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as e:
            print(f"[WARN] ffprobe 读取时长失败 {video_path}: {e}")
            return 0.0

    def _build_vf_filter(self) -> str:
        """构造统一规格的 -vf 滤镜链。

        1. scale: 等比缩放到目标分辨率，保持原始宽高比
        2. pad: 不足部分用黑边填充（防止画面变形）
        3. fps: 强制帧率统一
        """
        w, h = self._config.resolution
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:-1:-1:color=black,"
            f"fps={self._config.fps}"
        )

    def _run_ffmpeg(self, args: list[str], desc: str = "") -> None:
        """执行 ffmpeg 命令，失败抛异常。

        Args:
            args: ffmpeg 参数列表（不含 "ffmpeg" 前缀）
            desc: 失败时的描述信息

        Raises:
            RuntimeError: ffmpeg 返回非零退出码
        """
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
        except subprocess.CalledProcessError as e:
            err = e.stderr or "无 stderr 输出"
            raise RuntimeError(f"ffmpeg 执行失败 {desc}\n命令: {' '.join(cmd)}\n错误: {err}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"ffmpeg 执行超时 {desc}") from e

    def process(
        self,
        video_path: Path,
        target_duration: float,
        output_path: Path,
    ) -> Optional[ClipResult]:
        """处理单个视频，输出统一规格且时长精确等于 target_duration 的 clip。

        策略选择：
        1. v_duration >= target_duration → CUT 截取前 target_duration 秒
        2. v_duration < target_duration 且 v_duration/target_duration >= 1/MAX → SLOW_DOWN 慢放
        3. 否则 → LOOP 循环+截取

        Args:
            video_path: 源视频文件路径
            target_duration: 目标时长（秒）
            output_path: 输出 clip 路径

        Returns:
            ClipResult；失败返回 None
        """
        if target_duration <= 0:
            raise ValueError(f"target_duration 必须为正数，实际: {target_duration}")
        if not video_path.exists():
            raise FileNotFoundError(f"源视频不存在: {video_path}")

        v_duration = self._ffprobe_duration(video_path)
        if v_duration <= 0:
            print(f"[WARN] 无法读取视频时长，按 1 秒兜底: {video_path}")
            v_duration = 1.0

        vf = self._build_vf_filter()
        # 编码参数：统一 H.264 + 无音频（最终视频无需音轨）
        enc_args = [
            "-c:v", "libx264",
            "-crf", str(self._config.crf),
            "-preset", self._config.preset,
            "-an",  # 丢弃音频
            "-movflags", "+faststart",  # 元数据前置，便于后续 concat
        ]

        # 策略 1：截取
        if v_duration >= target_duration:
            args = [
                "-ss", "0",
                "-t", f"{target_duration:.3f}",
                "-i", str(video_path),
                "-vf", vf,
                *enc_args,
                str(output_path),
            ]
            try:
                self._run_ffmpeg(args, desc=f"CUT {video_path.name}")
                return ClipResult(
                    clip_path=output_path,
                    strategy=ProcessingStrategy.CUT,
                    speed=1.0,
                    cut_from=0.0,
                    cut_to=target_duration,
                )
            except RuntimeError as e:
                print(f"[ERROR] CUT 失败: {e}")
                return None

        # 计算所需变速倍率
        speed = v_duration / target_duration  # < 1.0，越小越慢
        min_speed = 1.0 / MAX_SLOWDOWN_FACTOR  # = 0.5

        # 策略 2：变速慢放
        if speed >= min_speed:
            # setpts factor = 1/speed（speed<1 时 factor>1，即拉伸时间轴 = 慢放）
            pts_factor = 1.0 / speed
            filter_chain = f"setpts={pts_factor:.4f}*PTS,{vf}"
            args = [
                "-i", str(video_path),
                "-vf", filter_chain,
                "-t", f"{target_duration:.3f}",  # 强制截断到目标时长，避免精度误差
                *enc_args,
                str(output_path),
            ]
            try:
                self._run_ffmpeg(args, desc=f"SLOW_DOWN {video_path.name} speed={speed:.3f}")
                return ClipResult(
                    clip_path=output_path,
                    strategy=ProcessingStrategy.SLOW_DOWN,
                    speed=speed,
                    cut_from=0.0,
                    cut_to=v_duration,
                )
            except RuntimeError as e:
                print(f"[ERROR] SLOW_DOWN 失败: {e}")
                return None

        # 策略 3：循环+截取
        # stream_loop=-1 表示无限循环；-t 强制截断到 target_duration
        args = [
            "-stream_loop", "-1",
            "-i", str(video_path),
            "-t", f"{target_duration:.3f}",
            "-vf", vf,
            *enc_args,
            str(output_path),
        ]
        try:
            self._run_ffmpeg(args, desc=f"LOOP {video_path.name}")
            return ClipResult(
                clip_path=output_path,
                strategy=ProcessingStrategy.LOOP,
                speed=1.0,
                cut_from=0.0,
                cut_to=target_duration,
            )
        except RuntimeError as e:
            print(f"[ERROR] LOOP 失败: {e}")
            return None
