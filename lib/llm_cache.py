"""LLM 结果缓存模块。

为什么需要本模块？
- LLM 调用是最慢且最贵的环节（103 段拉丁语约 30-60 秒）
- 字幕文件不变时，LLM 结果（翻译+关键词）也不变
- 重跑、调试、调参（如调整时长策略）时无需重复调用 LLM

设计：
- 缓存文件单一：cache/llm_cache.json
- 缓存 key = sha1(字幕全文 + 源语言 + 模型名)
- 缓存 value = List[LLMResult 的 dict 形式]
- 并发安全：读写都用全局锁（LLM 调用前已串行，锁开销可忽略）
- 用户可手动删除 cache/llm_cache.json 强制重新调用 LLM
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Optional


class LLMResultCache:
    """LLM 结果缓存（线程安全）。"""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "llm_cache.json"
        self._lock = threading.Lock()

    @staticmethod
    def _make_cache_key(
        subtitle_text: str,
        source_lang: str,
        model: str,
    ) -> str:
        """构造缓存 key：字幕内容哈希 + 语言 + 模型。

        任意一项变化都视为新结果（如换模型、改字幕、改语言检测）。
        """
        raw = f"{model}|{source_lang}|{subtitle_text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def get(
        self,
        subtitle_text: str,
        source_lang: str,
        model: str,
    ) -> Optional[list[dict[str, Any]]]:
        """查询缓存。

        Returns:
            命中时返回 List[dict]（每个 dict 含 en_text/keywords）；未命中返回 None
        """
        key = self._make_cache_key(subtitle_text, source_lang, model)
        with self._lock:
            if not self._cache_file.exists():
                return None
            try:
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    store = json.load(f)
                # store 结构: {"key1": [...], "key2": [...]}
                return store.get(key)
            except (OSError, json.JSONDecodeError) as e:
                print(f"[WARN] LLM 缓存损坏: {e}，将重新调用 LLM")
                try:
                    self._cache_file.unlink()
                except OSError:
                    pass
                return None

    def save(
        self,
        subtitle_text: str,
        source_lang: str,
        model: str,
        results: list[dict[str, Any]],
    ) -> None:
        """写入缓存（读-改-写，全局锁保护）。"""
        key = self._make_cache_key(subtitle_text, source_lang, model)
        with self._lock:
            # 读现有 store（可能不存在）
            store: dict[str, Any] = {}
            if self._cache_file.exists():
                try:
                    with open(self._cache_file, "r", encoding="utf-8") as f:
                        store = json.load(f)
                except (OSError, json.JSONDecodeError):
                    store = {}  # 损坏则重建
            store[key] = results
            # 原子写
            tmp = self._cache_file.with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(store, f, ensure_ascii=False, indent=2)
                tmp.replace(self._cache_file)
            except OSError as e:
                print(f"[WARN] LLM 缓存写入失败: {e}")
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
