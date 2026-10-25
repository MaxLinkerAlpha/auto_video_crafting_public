"""Auto Video Crafting - CLI 入口与主编排模块。

完整流程：
1. 解析 CLI 参数 + 加载 .env 配置
2. 用 pysubs2 解析字幕，补齐时间轴到 target_duration
3. 用 LLM 一次性 batch 处理所有字幕段：翻译为英语 + 抽取视觉关键词
4. 阶段 A（并发）：对每段调用 Pexels+Pixabay 搜索 + 下载最优候选
5. 阶段 B（串行，CPU 密集）：把每个候选转码为统一规格的 clip
6. 用 ffmpeg concat 拼接所有 clip 为 final.mp4
7. 输出 manifest.json 数据契约

调用示例：
    python main.py --srt input.srt --duration 60 --lang auto --output output/
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lib.assembler import Manifest, SegmentManifest, VideoAssembler
from lib.clip_processor import ClipProcessor
from lib.concurrency import run_concurrent
from lib.config import AppConfig, FALLBACK_KEYWORDS, SUPPORTED_LATIN_LANGS, load_config
from lib.downloader import VideoDownloader
from lib.llm_cache import LLMResultCache
from lib.llm_client import LLMClient, LLMResult
from lib.meta_cache import MetaCache
from lib.srt_parser import Segment, detect_language_hint, parse_subtitles
from lib.stock_client import StockClient, VideoCandidate, create_stock_clients, search_all


def parse_args() -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="auto_video_crafting",
        description="通过 AI 自动为带时间戳的字幕文件匹配免费 CC0 视频画面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py --srt input.srt --duration 60\n"
            "  python main.py --srt input.srt --duration 60 --lang de --output out/"
        ),
    )
    parser.add_argument(
        "--srt",
        type=Path,
        required=True,
        help="字幕文件路径（支持 srt/ass/vtt）",
    )
    parser.add_argument(
        "--duration",
        type=float,
        required=True,
        help="目标视频总时长（秒）",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default="auto",
        choices=["auto", *SUPPORTED_LATIN_LANGS.keys()],
        help="字幕源语言代码，默认 auto 自动检测",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出目录，默认 ./output/",
    )
    parser.add_argument(
        "--resolution",
        type=str,
        default="1920x1080",
        help="输出分辨率（宽x高），默认 1920x1080",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="输出帧率，默认 30",
    )
    return parser.parse_args()


def parse_resolution(s: str) -> tuple[int, int]:
    """解析 '1920x1080' 字符串为 (1920, 1080) 元组。"""
    try:
        w_str, h_str = s.lower().split("x", 1)
        return (int(w_str), int(h_str))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"分辨率格式错误，应为 '宽x高' 如 '1920x1080'，实际: {s}") from e


def find_best_candidate(
    candidates: list[VideoCandidate],
    downloader: VideoDownloader,
    target_duration: float,
) -> tuple[Optional[VideoCandidate], Optional[Path]]:
    """从候选列表中依次尝试下载，返回第一个成功下载的。

    排序已在 search_all 中完成（时长够 > 分辨率高）。

    Returns:
        (候选, 本地路径) 元组；全部失败返回 (None, None)
    """
    for cand in candidates:
        local = downloader.download(cand.url)
        if local is not None and local.exists() and local.stat().st_size > 0:
            return cand, local
        print(f"[WARN] 候选下载失败，尝试下一个: {cand.source}#{cand.video_id}")
    return None, None


# 阶段 A 的输入：每个 segment 的处理上下文（不可变，便于并发）
@dataclass(frozen=True)
class SegmentTask:
    """单个 segment 的处理任务（阶段 A 输入）。

    将所有依赖打包为不可变 dataclass，便于在并发环境中安全传递。
    """

    segment: Segment
    index: int
    keywords: list[str]
    en_text: str


# 阶段 A 的输出：搜索+下载完成后的中间结果
@dataclass(frozen=True)
class StageAResult:
    """阶段 A（搜索+下载）的结果。

    attributes:
        index: 对应 SegmentTask.index
        success: 是否成功
        candidate: 选中的候选（失败为 None）
        local_path: 下载到本地的视频路径（失败为 None）
        error_msg: 失败原因（成功为空）
    """

    index: int
    success: bool
    candidate: Optional[VideoCandidate]
    local_path: Optional[Path]
    error_msg: str = ""


def search_and_download_one(
    task: SegmentTask,
    clients: list[StockClient],
    downloader: VideoDownloader,
) -> StageAResult:
    """阶段 A：对单个 segment 执行"搜索 + 选最优候选 + 下载"。

    本函数是 I/O 密集型（HTTP 搜索 + HTTP 下载），适合并发执行。
    失败不抛异常，返回 success=False 的 StageAResult，避免单段失败影响其他段。

    Args:
        task: SegmentTask 上下文
        clients: 素材源客户端列表
        downloader: 视频下载器（线程安全）

    Returns:
        StageAResult
    """
    target_duration = task.segment.duration
    keywords = task.keywords if task.keywords else list(FALLBACK_KEYWORDS)

    print(
        f"  [Seg {task.index}] {task.segment.start:.2f}s-{task.segment.end:.2f}s "
        f"({target_duration:.2f}s) keywords={keywords}"
    )

    # 双源搜索
    candidates = search_all(clients, keywords, target_duration=target_duration)
    if not candidates:
        # fallback：用通用词再搜一次
        print(f"  [Seg {task.index}] 关键词零结果，fallback 到 {FALLBACK_KEYWORDS}")
        candidates = search_all(clients, list(FALLBACK_KEYWORDS), target_duration=target_duration)
    if not candidates:
        return StageAResult(
            index=task.index, success=False, candidate=None, local_path=None,
            error_msg="素材源完全没有结果",
        )

    # 选最优候选 + 下载
    candidate, local_path = find_best_candidate(candidates, downloader, target_duration)
    if candidate is None or local_path is None:
        return StageAResult(
            index=task.index, success=False, candidate=None, local_path=None,
            error_msg="所有候选下载失败",
        )

    return StageAResult(
        index=task.index, success=True, candidate=candidate, local_path=local_path,
    )


def run_pipeline(config: AppConfig, srt_path: Path, target_duration: float, lang: str) -> int:
    """主流程编排。返回退出码（0=成功）。"""
    start_time = time.time()
    print(f"\n=== Auto Video Crafting 启动 ===")
    print(f"字幕: {srt_path}")
    print(f"目标时长: {target_duration}s")
    print(f"规格: {config.width}x{config.height} @ {config.fps}fps")
    print(f"输出: {config.output_dir}\n")

    # 步骤 1：解析字幕
    print("[1/6] 解析字幕...")
    segments = parse_subtitles(srt_path, target_duration=target_duration)
    print(f"  共 {len(segments)} 段（含 gap 段）")
    if not segments:
        print("[ERROR] 字幕解析结果为空")
        return 1

    # 步骤 2：LLM 批量处理
    print("[2/6] LLM 处理字幕（翻译 + 关键词抽取）...")
    # 检测源语言（auto 模式）
    if lang == "auto":
        all_text = " ".join(s.text for s in segments if s.text)
        lang = detect_language_hint(all_text)
        print(f"  检测到源语言: {lang}")
    llm_cache = LLMResultCache(config.cache_dir)
    llm_client = LLMClient(config, cache=llm_cache)
    llm_results = llm_client.process_segments(segments, source_lang=lang)
    print(f"  完成 {len(llm_results)} 段 LLM 处理")
    if len(llm_results) != len(segments):
        print(
            f"[WARN] LLM 结果数 {len(llm_results)} 与字幕段数 {len(segments)} 不一致，"
            f"将以较短者为准"
        )

    # 步骤 3：准备资源
    print("[3/6] 初始化素材源客户端...")
    meta_cache = MetaCache(config.cache_dir)
    clients = create_stock_clients(config, meta_cache=meta_cache)
    downloader = VideoDownloader(config.cache_dir)
    clip_processor = ClipProcessor(config)
    assembler = VideoAssembler(config.output_dir)
    clips_dir = config.output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    print(f"  已启用素材源: {[c.name for c in clients]}")
    meta_stats = meta_cache.stats()
    print(f"  元数据缓存: {meta_stats['entries']} 条 / {meta_stats['total_size_kb']} KB")

    # 准备每段的 keywords/en_text（gap 段继承前段关键词）
    print("[4/6] 阶段 A：并发搜索 + 下载...")
    last_keywords: list[str] = list(FALLBACK_KEYWORDS)
    tasks: list[SegmentTask] = []
    for i, seg in enumerate(segments):
        idx = seg.index
        llm_res = llm_results[i] if i < len(llm_results) else LLMResult(en_text="", keywords=[])
        if seg.is_gap or not llm_res.keywords:
            effective_keywords = last_keywords
            effective_en_text = ""
        else:
            effective_keywords = llm_res.keywords
            effective_en_text = llm_res.en_text
            last_keywords = llm_res.keywords
        tasks.append(SegmentTask(
            segment=seg, index=idx,
            keywords=effective_keywords, en_text=effective_en_text,
        ))

    # 阶段 A：并发执行所有"搜索+下载"
    # 并发数选择依据：
    # - Pexels 200/h：6 并发 × 平均 3s/段 = 720 段/h 上限，远高于实际需求
    # - Pixabay 100/min：6 并发远低于此上限
    # - 双源并行搜索（search_all 内部 ThreadPoolExecutor）+ 段间并发 = 实际并发 API 调用约 12
    # - 搜索有 MetaCache 缓存，重复关键词不消耗 API 配额
    STAGE_A_WORKERS = 6
    stage_a_results: list[StageAResult] = []
    for tr in run_concurrent(
        lambda task: search_and_download_one(task, clients, downloader),
        tasks,
        max_workers=STAGE_A_WORKERS,
        desc="搜索+下载",
    ):
        result = tr.result if tr.success else None
        if result is None:
            # run_concurrent 内部异常（极少见）
            stage_a_results.append(StageAResult(
                index=tr.index, success=False, candidate=None, local_path=None,
                error_msg=f"并发框架异常: {tr.exception}",
            ))
        else:
            stage_a_results.append(result)
    # 按 index 排序，保证与字幕顺序对齐
    stage_a_results.sort(key=lambda r: r.index)

    # 阶段 B：串行转码（CPU 密集，并发会互相抢占 GIL/ffmpeg 进程）
    print(f"[5/6] 阶段 B：串行转码（{sum(1 for r in stage_a_results if r.success)} 个 clip）...")
    clip_paths: list[Path] = []
    seg_manifests: list[SegmentManifest] = []

    for i, task in enumerate(tasks):
        idx = task.index
        seg = task.segment
        effective_keywords = task.keywords
        effective_en_text = task.en_text
        stage_a = stage_a_results[i]

        if not stage_a.success or stage_a.candidate is None or stage_a.local_path is None:
            print(f"  [Seg {idx}] [WARN] 阶段 A 失败: {stage_a.error_msg}，跳过此段")
            seg_m = VideoAssembler.build_segment_manifest(
                index=idx,
                start_ts=seg.format_srt_timestamp("start"),
                end_ts=seg.format_srt_timestamp("end"),
                duration=seg.duration,
                original_text=seg.text,
                en_text=effective_en_text,
                keywords=effective_keywords,
                is_gap=seg.is_gap,
                source="none",
                video_url="",
                video_id="",
                clip_path=clips_dir / f"clip_{idx:03d}_FAILED.mp4",
                clip_result=None,
            )
            seg_manifests.append(seg_m)
            continue

        # 串行转码
        clip_path = clips_dir / f"clip_{idx:03d}.mp4"
        clip_result = clip_processor.process(stage_a.local_path, seg.duration, clip_path)
        if clip_result is None:
            print(f"  [Seg {idx}] [ERROR] clip 处理失败")
            seg_m = VideoAssembler.build_segment_manifest(
                index=idx,
                start_ts=seg.format_srt_timestamp("start"),
                end_ts=seg.format_srt_timestamp("end"),
                duration=seg.duration,
                original_text=seg.text,
                en_text=effective_en_text,
                keywords=effective_keywords,
                is_gap=seg.is_gap,
                source=stage_a.candidate.source,
                video_url=stage_a.candidate.url,
                video_id=stage_a.candidate.video_id,
                clip_path=clips_dir / f"clip_{idx:03d}_FAILED.mp4",
                clip_result=None,
            )
            seg_manifests.append(seg_m)
            continue

        print(
            f"  [Seg {idx}] OK -> {clip_path.name} "
            f"({clip_result.strategy.value}, speed={clip_result.speed:.2f})"
        )
        seg_m = VideoAssembler.build_segment_manifest(
            index=idx,
            start_ts=seg.format_srt_timestamp("start"),
            end_ts=seg.format_srt_timestamp("end"),
            duration=seg.duration,
            original_text=seg.text,
            en_text=effective_en_text,
            keywords=effective_keywords,
            is_gap=seg.is_gap,
            source=stage_a.candidate.source,
            video_url=stage_a.candidate.url,
            video_id=stage_a.candidate.video_id,
            clip_path=clip_path,
            clip_result=clip_result,
        )
        seg_manifests.append(seg_m)
        clip_paths.append(clip_path)

    if not clip_paths:
        print("[ERROR] 没有成功处理的 clip，无法生成最终视频")
        return 1

    # 步骤 5：拼接最终视频
    print(f"[5/6] 拼接 {len(clip_paths)} 个 clip 为最终视频...")
    final_path = config.output_dir / "final.mp4"
    try:
        assembler.concat_clips(clip_paths, final_path)
        print(f"  最终视频: {final_path}")
    except RuntimeError as e:
        print(f"[ERROR] 拼接失败: {e}")
        return 1

    # 步骤 6：写 manifest
    print("[6/6] 写 manifest.json...")
    manifest = Manifest(
        input_srt=str(srt_path),
        target_duration=target_duration,
        resolution=f"{config.width}x{config.height}",
        fps=config.fps,
        segments=seg_manifests,
        final_video=str(final_path),
    )
    manifest_path = assembler.write_manifest(manifest)
    print(f"  manifest: {manifest_path}")

    elapsed = time.time() - start_time
    print(f"\n=== 完成 === 用时 {elapsed:.1f}s")
    print(f"最终视频: {final_path}")
    print(f"匹配清单: {manifest_path}")
    return 0


def main() -> int:
    """CLI 入口。"""
    args = parse_args()

    # 参数校验
    if args.duration <= 0:
        print(f"[ERROR] --duration 必须为正数，实际: {args.duration}")
        return 1
    if not args.srt.exists():
        print(f"[ERROR] 字幕文件不存在: {args.srt}")
        return 1

    try:
        resolution = parse_resolution(args.resolution)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 1

    # 加载配置
    try:
        config = load_config(
            resolution=resolution,
            fps=args.fps,
            output_dir=args.output,
        )
        config.ensure_dirs()
    except RuntimeError as e:
        print(f"[ERROR] 配置加载失败: {e}")
        return 1

    # 执行主流程
    return run_pipeline(config, args.srt, args.duration, args.lang)


if __name__ == "__main__":
    sys.exit(main())
