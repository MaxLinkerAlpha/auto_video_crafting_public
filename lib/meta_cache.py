"""搜索元数据缓存模块。

为什么需要本模块？
- Pexels/Pixabay 搜索响应包含丰富元数据（tags/duration/user/thumbnail/全候选列表）
- 同一关键词+页码重复搜索会浪费 API 配额（Pexels 200/h, Pixabay 100/min）
- 用户后续"手动换片"时需要查阅完整候选池，而非只看最终选中的那一个

设计：
- 缓存 key = sha1(source + keywords + page + min_duration)
- 缓存 value = 原始 API JSON 响应
- 并发安全：多线程同时查同 key 时用 per-key 锁保护"检查+写入"临界区
- 依赖注入 cache_dir，便于测试
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Optional


class MetaCache:
    """搜索元数据本地缓存（线程安全）。

    缓存粒度：每次 search() 调用的完整 JSON 响应。
    缓存命中时直接返回 dict，不命中则由调用方请求 API 后调用 save() 存入。
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # per-key 锁保护，避免多线程同时 miss 时重复请求 API
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def _get_key_lock(self, key: str) -> threading.Lock:
        """获取某 key 专属的锁（避免不同 key 互相阻塞）。"""
        with self._locks_guard:
            if key not in self._key_locks:
                self._key_locks[key] = threading.Lock()
            return self._key_locks[key]

    @staticmethod
    def _make_cache_key(
        source: str,
        keywords: list[str],
        page: int = 1,
        min_duration: float = 0.0,
        per_page: int = 15,
    ) -> str:
        """构造缓存 key：源+关键词+页码+最小时长+每页数 → sha1。

        任意一项变化都视为新查询，避免缓存污染。
        """
        raw = (
            f"{source}|{'|'.join(keywords)}|p{page}|"
            f"d{min_duration:.2f}|n{per_page}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _key_to_path(self, key: str) -> Path:
        return self._cache_dir / f"meta_{key}.json"

    def get(
        self,
        source: str,
        keywords: list[str],
        page: int = 1,
        min_duration: float = 0.0,
        per_page: int = 15,
    ) -> Optional[dict[str, Any]]:
        """查询缓存。

        Returns:
            命中时返回 dict；未命中返回 None（调用方应请求 API 并调 save()）
        """
        key = self._make_cache_key(source, keywords, page, min_duration, per_page)
        path = self._key_to_path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # 缓存损坏：删除后返回 miss
            print(f"[WARN] 元数据缓存损坏 {path.name}: {e}，将重新请求")
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def save(
        self,
        source: str,
        keywords: list[str],
        data: dict[str, Any],
        page: int = 1,
        min_duration: float = 0.0,
        per_page: int = 15,
    ) -> None:
        """写入缓存（原子写：先 .tmp 再 replace）。"""
        key = self._make_cache_key(source, keywords, page, min_duration, per_page)
        path = self._key_to_path(key)
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except OSError as e:
            print(f"[WARN] 元数据缓存写入失败 {path.name}: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def get_or_compute(
        self,
        source: str,
        keywords: list[str],
        compute_fn,
        page: int = 1,
        min_duration: float = 0.0,
        per_page: int = 15,
    ) -> dict[str, Any]:
        """查缓存，未命中则调 compute_fn 请求 API 并存入缓存。

        使用 per-key 锁保证：同 key 多线程并发时只触发一次 API 调用。
        不同 key 之间不互相阻塞。

        Args:
            source: 来源 "pexels" / "pixabay"
            keywords: 关键词列表
            compute_fn: 无参函数，返回 dict（API 响应）
            page/min_duration/per_page: 缓存 key 组成部分

        Returns:
            dict（来自缓存或 API）
        """
        key = self._make_cache_key(source, keywords, page, min_duration, per_page)
        with self._get_key_lock(key):
            # 双重检查：进入锁后再次查缓存
            cached = self.get(source, keywords, page, min_duration, per_page)
            if cached is not None:
                return cached
            # 未命中：请求 API
            data = compute_fn()
            if data:
                self.save(source, keywords, data, page, min_duration, per_page)
            return data

    def stats(self) -> dict[str, int]:
        """返回缓存统计（用于日志）。"""
        files = list(self._cache_dir.glob("meta_*.json"))
        total_size = sum(f.stat().st_size for f in files if f.exists())
        return {
            "entries": len(files),
            "total_size_kb": total_size // 1024,
        }
