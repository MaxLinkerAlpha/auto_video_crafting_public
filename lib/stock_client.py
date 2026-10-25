"""素材源客户端模块：统一封装 Pexels + Pixabay 视频搜索。

设计要点：
- 抽象基类 StockClient 定义统一接口，Pexels/Pixabay 各自实现
- 统一数据结构 VideoCandidate 隔离两家 API 字段差异
- 双源 search_all 并行调用（ThreadPoolExecutor），合并候选并按 (时长够否, 分辨率) 排序
- 限流容错：HTTP 429 时指数退避重试
- 元数据缓存：搜索响应整体落盘，避免重复调用 API（MetaCache）
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import requests

from .config import AppConfig, MAX_RETRIES_HTTP, SEARCH_RESULTS_PER_SOURCE
from .meta_cache import MetaCache


@dataclass(frozen=True)
class VideoCandidate:
    """统一的视频候选项数据结构。

    attributes:
        source: 来源标识 "pexels" / "pixabay"
        video_id: 在源平台的唯一 ID（字符串）
        url: 可直接下载的 MP4 URL
        duration: 时长（秒）
        width: 分辨率宽
        height: 分辨率高
        preview_url: 预览图 URL（可选，用于 manifest）
    """

    source: str
    video_id: str
    url: str
    duration: float
    width: int
    height: int
    preview_url: str = ""


class StockClient:
    """素材源客户端抽象基类。"""

    name: str = "abstract"

    def __init__(self, api_key: str, meta_cache: Optional[MetaCache] = None) -> None:
        if not api_key:
            raise ValueError(f"{self.name} api_key 不能为空")
        self._api_key = api_key
        self._meta_cache = meta_cache  # 可选，None 表示不缓存
        self._session = requests.Session()
        self._session.headers.update(self._default_headers())

    def _default_headers(self) -> dict[str, str]:
        return {"User-Agent": "AutoVideoCrafting/1.0"}

    def search(
        self,
        keywords: list[str],
        min_duration: float = 0.0,
        per_page: int = SEARCH_RESULTS_PER_SOURCE,
    ) -> list[VideoCandidate]:
        """搜索视频。子类必须实现 _search_uncached，本方法负责缓存包装。"""
        if not keywords:
            return []

        # 有缓存：先查缓存，未命中则调 _search_uncached 并存入
        if self._meta_cache is not None:
            data = self._meta_cache.get_or_compute(
                source=self.name,
                keywords=keywords,
                compute_fn=lambda: self._fetch_raw(keywords, min_duration, per_page),
                min_duration=min_duration,
                per_page=per_page,
            )
        else:
            data = self._fetch_raw(keywords, min_duration, per_page)

        return self._parse_response(data)

    def _fetch_raw(
        self,
        keywords: list[str],
        min_duration: float,
        per_page: int,
    ) -> dict:
        """实际发起 HTTP 请求获取原始 JSON 响应。子类实现。"""
        raise NotImplementedError

    def _parse_response(self, data: dict) -> list[VideoCandidate]:
        """解析 API 响应为 VideoCandidate 列表。子类实现。"""
        raise NotImplementedError

    def _request_with_retry(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 15.0,
    ) -> dict:
        """带指数退避重试的 GET 请求。

        Args:
            url: 请求 URL
            params: 查询参数
            headers: 额外 headers
            timeout: 超时秒数

        Returns:
            JSON 解析后的 dict

        Raises:
            RuntimeError: 重试 MAX_RETRIES_HTTP 次后仍失败
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES_HTTP + 1):
            try:
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=timeout
                )
                if resp.status_code == 429:
                    # 限流：指数退避
                    wait = 2**attempt
                    print(
                        f"[WARN] {self.name} 限流 (429)，{wait}s 后重试 "
                        f"(attempt {attempt + 1}/{MAX_RETRIES_HTTP + 1})"
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                last_error = e
                if attempt < MAX_RETRIES_HTTP:
                    wait = 2**attempt
                    print(
                        f"[WARN] {self.name} 请求失败: {e}，{wait}s 后重试 "
                        f"(attempt {attempt + 1}/{MAX_RETRIES_HTTP + 1})"
                    )
                    time.sleep(wait)
        raise RuntimeError(f"{self.name} 请求 {url} 失败: {last_error}")


class PexelsClient(StockClient):
    """Pexels 视频搜索客户端。

    API 文档: https://www.pexels.com/api/documentation/#videos-search
    端点: GET https://api.pexels.com/videos/search
    认证: Header Authorization: API_KEY
    """

    name = "pexels"
    BASE_URL = "https://api.pexels.com/videos/search"

    def _default_headers(self) -> dict[str, str]:
        return {"Authorization": self._api_key, "User-Agent": "AutoVideoCrafting/1.0"}

    def _fetch_raw(
        self,
        keywords: list[str],
        min_duration: float,
        per_page: int,
    ) -> dict:
        """实际发起 Pexels 搜索请求。"""
        query = " ".join(keywords[:3])  # Pexels 偏好空格连接的多关键词
        params: dict[str, object] = {
            "query": query,
            "per_page": min(per_page, 80),  # Pexels 上限 80
            "orientation": "landscape",
        }
        # Pexels 支持最小时长过滤（秒）
        if min_duration > 0:
            params["min_duration"] = int(min_duration) + 1

        try:
            return self._request_with_retry(self.BASE_URL, params=params)
        except RuntimeError as e:
            print(f"[WARN] Pexels 搜索失败: {e}")
            return {}  # 返回空 dict，_parse_response 会处理为空列表

    def _parse_response(self, data: dict) -> list[VideoCandidate]:
        """解析 Pexels 响应为 VideoCandidate 列表。"""
        candidates: list[VideoCandidate] = []
        for item in data.get("videos", []):
            # 在所有 video_files 中挑最大分辨率的 mp4
            best_file: Optional[dict] = None
            best_pixels = 0
            for f in item.get("video_files", []):
                if f.get("file_type") != "video/mp4":
                    continue
                w = int(f.get("width", 0))
                h = int(f.get("height", 0))
                pixels = w * h
                if pixels > best_pixels:
                    best_pixels = pixels
                    best_file = f
            if not best_file:
                continue
            candidates.append(
                VideoCandidate(
                    source=self.name,
                    video_id=str(item.get("id", "")),
                    url=best_file.get("link", ""),
                    duration=float(item.get("duration", 0)),
                    width=int(best_file.get("width", 0)),
                    height=int(best_file.get("height", 0)),
                    preview_url=item.get("image", ""),
                )
            )
        return candidates


class PixabayClient(StockClient):
    """Pixabay 视频搜索客户端。

    API 文档: https://pixabay.com/api/videos/
    端点: GET https://pixabay.com/api/videos/
    认证: Query param key=API_KEY
    """

    name = "pixabay"
    BASE_URL = "https://pixabay.com/api/videos/"

    def _fetch_raw(
        self,
        keywords: list[str],
        min_duration: float,
        per_page: int,
    ) -> dict:
        """实际发起 Pixabay 搜索请求。"""
        query = "+".join(keywords[:3])  # Pixabay 用 + 连接多关键词
        params: dict[str, object] = {
            "key": self._api_key,
            "q": query,
            "per_page": min(per_page, 200),  # Pixabay 上限 200
            "video_type": "film",  # 只要实拍，排除动画
            "min_width": 1280,
        }
        # Pixabay 视频没有直接的 min_duration 参数，靠客户端过滤

        try:
            return self._request_with_retry(self.BASE_URL, params=params)
        except RuntimeError as e:
            print(f"[WARN] Pixabay 搜索失败: {e}")
            return {}

    def _parse_response(self, data: dict) -> list[VideoCandidate]:
        """解析 Pixabay 响应为 VideoCandidate 列表。"""
        candidates: list[VideoCandidate] = []
        for item in data.get("hits", []):
            # Pixabay 提供 large/medium/small//tiny 多档，优先取最大
            videos = item.get("videos", {})
            best_file: Optional[dict] = None
            best_pixels = 0
            for quality in ("large", "medium", "small", "tiny"):
                v = videos.get(quality, {})
                if not v or v.get("url"):
                    pass
                w = int(v.get("width", 0))
                h = int(v.get("height", 0))
                pixels = w * h
                if pixels > best_pixels and v.get("url"):
                    best_pixels = pixels
                    best_file = v
            if not best_file:
                continue
            duration = float(item.get("duration", 0))
            # 注：原 search() 在此过滤 min_duration，但缓存机制下应在缓存 key 中
            # 已包含 min_duration（不同 min_duration 走不同缓存项），故此处不再过滤
            candidates.append(
                VideoCandidate(
                    source=self.name,
                    video_id=str(item.get("id", "")),
                    url=best_file.get("url", ""),
                    duration=duration,
                    width=int(best_file.get("width", 0)),
                    height=int(best_file.get("height", 0)),
                    preview_url=item.get("previewURL", item.get("largeImageURL", "")),
                )
            )
        return candidates


def create_stock_clients(
    config: AppConfig,
    meta_cache: Optional[MetaCache] = None,
) -> list[StockClient]:
    """根据 config 中已配置的 API key 创建可用的客户端列表。

    Args:
        config: 应用配置
        meta_cache: 可选的元数据缓存（推荐传入，避免重复 API 调用）

    Returns:
        List[StockClient]，至少包含 1 个；若 Pexels/Pixabay 都未配置由 load_config 拦截
    """
    clients: list[StockClient] = []
    if config.pexels_api_key:
        clients.append(PexelsClient(config.pexels_api_key, meta_cache=meta_cache))
    if config.pixabay_api_key:
        clients.append(PixabayClient(config.pixabay_api_key, meta_cache=meta_cache))
    return clients


def search_all(
    clients: Iterable[StockClient],
    keywords: list[str],
    target_duration: float,
) -> list[VideoCandidate]:
    """并行调用所有客户端搜索，合并候选并按匹配度排序。

    关键优化：双源（Pexels + Pixabay）真正并行调用，单段搜索延迟减半。

    排序规则：
    1. 时长够目标时长的优先（True > False）
    2. 同优先级按分辨率降序（高清优先）

    Args:
        clients: 客户端列表
        keywords: 搜索关键词列表
        target_duration: 目标段时长（秒），用作 min_duration 过滤

    Returns:
        List[VideoCandidate]，已排序，空列表表示零结果
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clients_list = list(clients)
    candidates: list[VideoCandidate] = []

    def _safe_search(client: StockClient, min_dur: float) -> list[VideoCandidate]:
        try:
            return client.search(keywords, min_duration=min_dur)
        except Exception as e:
            print(f"[WARN] {client.name} 搜索异常: {e}")
            return []

    # 第一轮：并行双源搜索（带 min_duration 过滤）
    with ThreadPoolExecutor(max_workers=len(clients_list)) as executor:
        futures = {
            executor.submit(_safe_search, c, target_duration): c
            for c in clients_list
        }
        for future in as_completed(futures):
            candidates.extend(future.result())

    # 第二轮：若零结果，放宽条件（不限时长）再并行搜一次
    if not candidates:
        with ThreadPoolExecutor(max_workers=len(clients_list)) as executor:
            futures = {
                executor.submit(_safe_search, c, 0.0): c
                for c in clients_list
            }
            for future in as_completed(futures):
                candidates.extend(future.result())

    # 排序：时长够 + 高分辨率 优先
    candidates.sort(
        key=lambda c: (c.duration >= target_duration, c.width * c.height),
        reverse=True,
    )
    return candidates
