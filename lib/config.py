"""配置加载模块（单一数据源）。

集中管理所有运行时配置：API key、目标视频规格、文件路径等。
遵循 SSOT 原则，禁止在其他模块硬编码这些常量。

v1_2_0：支持移动硬盘自动检测。
大容量文件（cache/、output/）优先存到移动硬盘的 AutoVideoCrafting_Workspace 目录，
便于在多台电脑之间协作。移动硬盘拔出时自动回退本地路径。
"""
from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# ===== 路径常量（基于本文件位置，跨平台安全） =====
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR: Path = PROJECT_ROOT / "output"
DEFAULT_CACHE_DIR: Path = PROJECT_ROOT / "cache"
ENV_FILE: Path = PROJECT_ROOT / ".env"

# 移动硬盘工作区目录名（在所有盘符根目录下查找此名称的目录）
EXTERNAL_WORKSPACE_DIRNAME: str = "AutoVideoCrafting_Workspace"


# ===== 默认视频规格 =====
DEFAULT_RESOLUTION: tuple[int, int] = (1920, 1080)  # 宽 x 高
DEFAULT_FPS: int = 30
DEFAULT_CRF: int = 23  # H.264 质量，18~28，越小画质越好
DEFAULT_PRESET: str = "fast"  # 编码速度档：ultrafast/fast/medium/slow


# ===== 业务约束 =====
MAX_SLOWDOWN_FACTOR: float = 2.0  # 最大慢放倍数，超过此值改用循环策略
SEARCH_RESULTS_PER_SOURCE: int = 15  # 每个素材源每次搜索返回的候选数
MAX_RETRIES_LLM: int = 1  # LLM 调用失败重试次数
MAX_RETRIES_HTTP: int = 3  # HTTP 请求失败重试次数
FALLBACK_KEYWORDS: list[str] = ["nature", "landscape", "abstract"]  # 关键词零结果兜底


# ===== 支持的拉丁语种 =====
SUPPORTED_LATIN_LANGS: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
}


@dataclass(frozen=True)
class AppConfig:
    """应用配置（不可变，避免运行时被意外修改）。

    通过 load_config() 工厂函数构造，禁止直接实例化以保证 .env 已加载。
    """

    # 素材源 API key
    pexels_api_key: str
    pixabay_api_key: str

    # LLM 配置
    llm_api_key: str
    llm_base_url: str
    llm_model: str

    # 视频输出规格
    resolution: tuple[int, int] = DEFAULT_RESOLUTION
    fps: int = DEFAULT_FPS
    crf: int = DEFAULT_CRF
    preset: str = DEFAULT_PRESET

    # 工作目录
    output_dir: Path = DEFAULT_OUTPUT_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR

    @property
    def width(self) -> int:
        return self.resolution[0]

    @property
    def height(self) -> int:
        return self.resolution[1]

    def ensure_dirs(self) -> None:
        """确保输出/缓存目录存在。在 main.py 启动时调用一次。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "clips").mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


def _get_env(name: str, default: str = "") -> str:
    """读取环境变量，去除首尾空白。"""
    return os.getenv(name, default).strip()


def auto_detect_workspace() -> Optional[Path]:
    """自动检测移动硬盘工作区。

    扫描所有盘符根目录，查找名为 AutoVideoCrafting_Workspace 的目录。
    适用于多台电脑协作场景：用户只需在每台电脑上插入移动硬盘，程序自动识别。

    检测顺序：
    1. 优先扫描非系统盘（D: 之后的所有盘符）
    2. 系统盘 C: 不扫描（避免误命中本地目录）

    Returns:
        工作区 Path（如 E:\\AutoVideoCrafting_Workspace）；未找到返回 None
    """
    # string.ascii_uppercase = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for letter in string.ascii_uppercase:
        if letter == "C":
            # 跳过系统盘，避免误命中本地目录
            continue
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        workspace = drive / EXTERNAL_WORKSPACE_DIRNAME
        if workspace.is_dir():
            return workspace
    return None


def resolve_storage_paths(
    explicit_output_dir: Optional[Path] = None,
    explicit_cache_dir: Optional[Path] = None,
) -> tuple[Path, Path, Optional[Path]]:
    """解析实际使用的存储路径（移动硬盘优先 → 本地回退）。

    优先级（从高到低）：
    1. 命令行参数 --output 显式指定
    2. .env 中 CACHE_DIR / OUTPUT_DIR 环境变量
    3. 移动硬盘自动检测（E:\\AutoVideoCrafting_Workspace 等）
    4. 本地项目目录（cache/、output/）

    Args:
        explicit_output_dir: 命令行 --output 参数
        explicit_cache_dir: 命令行 --cache 参数（目前未启用，预留）

    Returns:
        (output_dir, cache_dir, workspace) 三元组
        - workspace 非 None 表示命中移动硬盘，None 表示使用本地
    """
    # 1. 命令行参数优先
    if explicit_output_dir:
        cache_dir = explicit_cache_dir or explicit_output_dir.parent / "cache"
        return explicit_output_dir, cache_dir, None

    # 2. .env 环境变量
    env_output = _get_env("OUTPUT_DIR")
    env_cache = _get_env("CACHE_DIR")
    if env_output:
        out = Path(env_output)
        cache = Path(env_cache) if env_cache else out.parent / "cache"
        return out, cache, None

    # 3. 移动硬盘自动检测
    workspace = auto_detect_workspace()
    if workspace is not None:
        out = workspace / "output"
        cache = workspace / "cache"
        return out, cache, workspace

    # 4. 本地回退
    return DEFAULT_OUTPUT_DIR, DEFAULT_CACHE_DIR, None


def load_config(
    resolution: Optional[tuple[int, int]] = None,
    fps: Optional[int] = None,
    output_dir: Optional[Path] = None,
) -> AppConfig:
    """加载配置：先加载 .env，再读取环境变量构造 AppConfig。

    路径解析顺序（详见 resolve_storage_paths）：
    命令行 --output > .env OUTPUT_DIR > 移动硬盘自动检测 > 本地默认

    Args:
        resolution: 可选，覆盖默认分辨率 (width, height)
        fps: 可选，覆盖默认帧率
        output_dir: 可选，覆盖默认输出目录（来自 --output 命令行）

    Returns:
        AppConfig 不可变实例

    Raises:
        RuntimeError: 若 .env 文件不存在或关键 key 缺失（尽早失败）
    """
    if not ENV_FILE.exists():
        raise RuntimeError(
            f"未找到 .env 文件: {ENV_FILE}\n"
            f"请复制 .env.example 为 .env 并填入 API key"
        )

    load_dotenv(ENV_FILE)

    pexels_key = _get_env("PEXELS_API_KEY")
    pixabay_key = _get_env("PIXABAY_API_KEY")
    llm_key = _get_env("LLM_API_KEY")

    # 尽早失败：至少需要一个素材源 + LLM
    if not pexels_key and not pixabay_key:
        raise RuntimeError(
            "PEXELS_API_KEY 和 PIXABAY_API_KEY 至少需要配置一个"
        )
    if not llm_key:
        raise RuntimeError("LLM_API_KEY 必须配置")

    # 解析存储路径（移动硬盘优先）
    actual_output_dir, actual_cache_dir, workspace = resolve_storage_paths(
        explicit_output_dir=output_dir,
    )

    # 提示当前使用的存储位置
    if workspace is not None:
        print(f"[STORAGE] 检测到移动硬盘工作区: {workspace}")
        print(f"[STORAGE]   cache -> {actual_cache_dir}")
        print(f"[STORAGE]   output -> {actual_output_dir}")
    else:
        print(f"[STORAGE] 未检测到移动硬盘，使用本地存储")
        print(f"[STORAGE]   cache -> {actual_cache_dir}")
        print(f"[STORAGE]   output -> {actual_output_dir}")

    return AppConfig(
        pexels_api_key=pexels_key,
        pixabay_api_key=pixabay_key,
        llm_api_key=llm_key,
        llm_base_url=_get_env(
            "LLM_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        llm_model=_get_env("LLM_MODEL", "qwen2.5-7b-instruct"),
        resolution=resolution or DEFAULT_RESOLUTION,
        fps=fps or DEFAULT_FPS,
        output_dir=actual_output_dir,
        cache_dir=actual_cache_dir,
    )
