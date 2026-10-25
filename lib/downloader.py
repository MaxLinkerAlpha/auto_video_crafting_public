"""视频下载模块：带 URL-hash 缓存，避免重复下载。

设计要点：
- 缓存 key 为 URL 的 sha1，缓存命中直接返回本地路径
- 流式下载（stream=True），避免大视频占满内存
- 并发安全：download_many 用锁保护"检查缓存 + 写入"临界区，避免竞态
- 依赖注入 cache_dir，便于测试
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Optional

import requests

from .concurrency import run_concurrent
from .config import MAX_RETRIES_HTTP


class VideoDownloader:
    """视频下载器：带本地文件缓存，支持单下载与批量并发下载。"""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "AutoVideoCrafting/1.0"})
        # 关键：保护"检查缓存是否存在 + 写入"的临界区
        # 避免多线程并发下载同一 URL 时产生竞态（双方都判定未命中→都下载→互相覆盖）
        # 锁是 per-URL 的（通过 dict + 全局锁实现），避免不同 URL 互相阻塞
        self._url_locks_guard = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}

    def _get_url_lock(self, url: str) -> threading.Lock:
        """获取某 URL 专属的锁（双重检查避免重复创建）。"""
        with self._url_locks_guard:
            if url not in self._url_locks:
                self._url_locks[url] = threading.Lock()
            return self._url_locks[url]

    @staticmethod
    def _url_to_cache_path(url: str, cache_dir: Path) -> Path:
        """URL → 缓存文件路径（sha1.mp4）。

        使用 sha1 而非原文件名的原因：
        - 不同素材源 URL 结构差异大，原文件名可能冲突或非法
        - sha1 保证一致性，同 URL 永远映射到同一路径
        """
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return cache_dir / f"{url_hash}.mp4"

    def download(self, url: str) -> Optional[Path]:
        """下载视频到缓存，返回本地路径。

        并发安全：通过 per-URL 锁保证同一 URL 不会被多线程同时下载。
        缓存命中直接返回；下载失败返回 None（不抛异常，由调用方处理候选列表回退）。

        Args:
            url: 视频 URL

        Returns:
            本地文件 Path；失败返回 None
        """
        if not url:
            return None

        # per-URL 锁：同一 URL 串行，不同 URL 并行
        with self._get_url_lock(url):
            cache_path = self._url_to_cache_path(url, self._cache_dir)
            # 双重检查：进入锁后再次确认（可能其他线程刚下完）
            if cache_path.exists() and cache_path.stat().st_size > 0:
                return cache_path

            last_error: Exception | None = None
            for attempt in range(MAX_RETRIES_HTTP + 1):
                try:
                    # stream=True 流式下载，避免大文件占内存
                    with self._session.get(url, stream=True, timeout=60) as resp:
                        resp.raise_for_status()
                        # 先写临时文件，下载成功后再原子重命名，避免半截文件污染缓存
                        tmp_path = cache_path.with_suffix(".mp4.tmp")
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)
                        tmp_path.replace(cache_path)
                    return cache_path
                except (requests.RequestException, OSError) as e:
                    last_error = e
                    # 清理可能的残留临时文件
                    tmp_path = cache_path.with_suffix(".mp4.tmp")
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                    if attempt < MAX_RETRIES_HTTP:
                        print(
                            f"[WARN] 下载失败 {url}: {e}，重试 "
                            f"(attempt {attempt + 1}/{MAX_RETRIES_HTTP + 1})"
                        )
                    continue
            print(f"[ERROR] 下载最终失败 {url}: {last_error}")
            return None

    def download_many(
        self,
        urls: list[str],
        max_workers: int = 8,
    ) -> list[Optional[Path]]:
        """并发下载多个 URL，返回与输入等长的本地路径列表。

        Args:
            urls: URL 列表
            max_workers: 最大并发数（默认 8，受网络带宽约束而非 API 限流）
                         视频下载与搜索 API 不同源，不受 Pexels/Pixabay 的 429 约束

        Returns:
            List[Optional[Path]]，每个元素为本地路径或 None（失败）
        """
        results = run_concurrent(self.download, urls, max_workers=max_workers, desc="下载视频")
        return [r.result if r.success else None for r in results]

    def cache_path_for(self, url: str) -> Path:
        """查询某 URL 对应的缓存路径（不触发下载，用于检查是否已缓存）。"""
        return self._url_to_cache_path(url, self._cache_dir)
