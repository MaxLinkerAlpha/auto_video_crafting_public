"""LLM 客户端模块：负责字幕翻译 + 视觉关键词抽取。

设计要点：
- 分批调用：将 N 段拆为多批（每批 ≤30 段）调用 LLM，避免输出超长被截断
- 强制 JSON 模式：response_format=json_object 避免解析失败
- 失败降级：重试1次后改用简单分词取名词作为兜底
- 结果缓存：相同字幕+语言+模型时直接读本地缓存，跳过 LLM 调用
- 依赖注入：通过构造函数传入 api_key/base_url/model/cache，便于测试
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from openai import OpenAI

from .config import AppConfig, MAX_RETRIES_LLM
from .llm_cache import LLMResultCache
from .srt_parser import Segment


# 单批最大段数：超过此值会拆分为多批调用
# 经验值：103 段一次性调用 GLM-4-9B 会导致输出截断（>13KB），拆为每批 30 段稳定
LLM_BATCH_SIZE: int = 30


# 简单停用词表（用于降级方案），覆盖主要拉丁语种常见无意义词
_STOPWORDS: set[str] = {
    # English
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "can", "this",
    "that", "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "what", "which", "who", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very", "s",
    "t", "just", "don", "now", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "about", "as", "into", "through", "during", "before",
    "after", "above", "below", "up", "down", "out", "off", "over", "under",
    # German
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer",
    "eines", "einem", "einen", "und", "oder", "aber", "ist", "sind", "war",
    "waren", "sein", "war", "haben", "hat", "hatte", "wird", "werden",
    "ich", "du", "er", "sie", "es", "wir", "ihr", "nicht", "kein", "keine",
    "im", "in", "an", "auf", "mit", "bei", "von", "zu", "zur", "zum",
    # French
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "mais",
    "est", "sont", "était", "être", "avoir", "a", "je", "tu", "il", "nous",
    "vous", "ils", "ne", "pas", "dans", "pour", "par", "sur", "avec", "au",
    # Spanish
    "el", "la", "los", "las", "un", "una", "unos", "unas", "y", "o", "pero",
    "es", "son", "era", "ser", "tener", "tiene", "yo", "tú", "él", "nosotros",
    "vosotros", "ellos", "no", "en", "por", "para", "con", "de", "del",
    # Portuguese
    "o", "a", "os", "as", "um", "uma", "uns", "umas", "e", "ou", "mas",
    "é", "são", "era", "ser", "ter", "tem", "eu", "tu", "ele", "nós",
    "vós", "eles", "não", "em", "por", "para", "com", "de", "do", "da",
    # Italian
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "e", "o", "ma",
    "è", "sono", "era", "essere", "avere", "ha", "io", "tu", "lui", "noi",
    "voi", "loro", "non", "in", "per", "con", "di", "del", "della",
}


@dataclass(frozen=True)
class LLMResult:
    """LLM 对单段字幕的处理结果。

    attributes:
        en_text: 英文翻译（已英语则原样）
        keywords: 2-3 个英文视觉搜索关键词
    """

    en_text: str
    keywords: list[str]


# 系统提示词：明确任务 + 输出格式约束
_SYSTEM_PROMPT = """You are a video B-roll matching assistant.

Given a JSON array of subtitle segments, for each segment you must return:
1. "en_text": English translation. If the original is already English, keep it as-is.
2. "keywords": A list of 2-3 short English keywords that are likely to find matching stock footage on Pexels/Pixabay. Prefer concrete visual nouns (e.g. "coffee cup", "city traffic", "sunset") over abstract concepts.

Output strictly as JSON: {"results": [{"index": 1, "en_text": "...", "keywords": ["...", "..."]}, ...]}
- Do not include any prose outside the JSON.
- Keep keywords lowercase.
- For empty/gap segments, use generic keywords like ["nature", "landscape"].
"""


class LLMClient:
    """LLM 客户端：负责翻译 + 关键词抽取。

    通过依赖注入接收 AppConfig 和可选的 LLMResultCache，便于单元测试时替换 mock client。
    """

    def __init__(
        self,
        config: AppConfig,
        cache: Optional[LLMResultCache] = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._client = OpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
        )

    def process_segments(
        self,
        segments: list[Segment],
        source_lang: str = "auto",
    ) -> list[LLMResult]:
        """批量处理所有字幕段，返回与输入等长的 LLMResult 列表。

        优化点：
        1. 先查缓存，命中则直接返回（跳过 LLM 调用）
        2. 未命中时分批调用 LLM（每批 LLM_BATCH_SIZE 段），避免输出超长截断
        3. 全部成功后写入缓存

        Args:
            segments: 已解析的字幕段列表（含 gap 段）
            source_lang: 源语言代码（auto/en/de/fr/...），仅作 LLM 提示

        Returns:
            List[LLMResult]，长度与输入一致，顺序对应
        """
        if not segments:
            return []

        # 构造缓存 key 的输入：所有字幕文本拼接
        subtitle_text = "\n".join(s.text for s in segments)

        # 1. 查缓存
        if self._cache is not None:
            cached = self._cache.get(subtitle_text, source_lang, self._config.llm_model)
            if cached is not None:
                print(f"[LLM] 缓存命中，跳过 LLM 调用（{len(cached)} 段）")
                return [LLMResult(
                    en_text=item.get("en_text", ""),
                    keywords=item.get("keywords", ["nature", "landscape"]),
                ) for item in cached]

        # 2. 分批调用 LLM
        all_results: list[LLMResult] = []
        total_batches = (len(segments) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE

        for batch_idx in range(total_batches):
            start = batch_idx * LLM_BATCH_SIZE
            end = min(start + LLM_BATCH_SIZE, len(segments))
            batch_segments = segments[start:end]
            print(
                f"[LLM] 处理批次 {batch_idx + 1}/{total_batches} "
                f"(段 {start + 1}-{end}, 共 {len(batch_segments)} 段)"
            )
            batch_results = self._call_llm_batch(batch_segments, source_lang)
            all_results.extend(batch_results)

        # 3. 写入缓存
        if self._cache is not None and all_results:
            self._cache.save(
                subtitle_text, source_lang, self._config.llm_model,
                [{"en_text": r.en_text, "keywords": r.keywords} for r in all_results],
            )
            print(f"[LLM] 结果已写入缓存（{len(all_results)} 段）")

        return all_results

    def _call_llm_batch(
        self,
        segments: list[Segment],
        source_lang: str,
    ) -> list[LLMResult]:
        """调用 LLM 处理单批字幕段（≤ LLM_BATCH_SIZE）。"""
        # 构造输入 payload
        payload = [
            {"index": i + 1, "text": seg.text if seg.text else "[no speech / gap]"}
            for i, seg in enumerate(segments)
        ]
        user_prompt = (
            f"Source language hint: {source_lang}\n"
            f"Process these {len(payload)} subtitle segments:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES_LLM + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self._config.llm_model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    # 强制 JSON 输出，避免 LLM 写散文
                    response_format={"type": "json_object"},
                    temperature=0.3,  # 关键词抽取需要一定稳定性
                )
                return self._parse_llm_response(
                    resp.choices[0].message.content or "", len(segments)
                )
            except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
                # 解析失败：可重试
                last_error = e
                print(
                    f"[WARN] LLM 响应解析失败 (attempt {attempt + 1}/{MAX_RETRIES_LLM + 1}): {e}"
                )
                continue
            except Exception as e:
                # API 调用失败：不重试，直接降级
                print(f"[WARN] LLM API 调用失败，降级使用简单分词: {e}")
                return self._fallback_degrade(segments)

        # 重试用尽：降级
        print(f"[WARN] LLM 重试 {MAX_RETRIES_LLM + 1} 次仍失败，降级使用简单分词: {last_error}")
        return self._fallback_degrade(segments)

    @staticmethod
    def _parse_llm_response(content: str, expected_len: int) -> list[LLMResult]:
        """解析 LLM 返回的 JSON 字符串为 LLMResult 列表。

        兼容两种格式：
        - {"results": [...]} （system prompt 要求格式）
        - [...] （直接数组，部分模型会忽略 json_object 约束）
        """
        data: Any = json.loads(content)
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            raise ValueError(f"LLM 返回结构非预期: {type(data)}")

        # 按 index 对齐到输入顺序
        results_map: dict[int, LLMResult] = {}
        for item in data:
            idx = int(item.get("index", 0))
            en_text = str(item.get("en_text", "")).strip()
            keywords_raw = item.get("keywords", [])
            if not isinstance(keywords_raw, list):
                keywords_raw = [keywords_raw]
            keywords = [str(k).strip().lower() for k in keywords_raw if str(k).strip()]
            # 关键词零个或太多都规范化到 2-3 个
            if not keywords:
                keywords = ["nature", "landscape"]
            elif len(keywords) > 3:
                keywords = keywords[:3]
            results_map[idx] = LLMResult(en_text=en_text, keywords=keywords)

        # 按输入顺序输出，缺失项用降级填充
        output: list[LLMResult] = []
        for i in range(1, expected_len + 1):
            if i in results_map:
                output.append(results_map[i])
            else:
                output.append(LLMResult(en_text="", keywords=["nature", "landscape"]))
        return output

    @staticmethod
    def _fallback_degrade(segments: list[Segment]) -> list[LLMResult]:
        """LLM 完全失败时的降级方案：用简单停用词过滤 + 取前 2-3 个长词。

        精度差但能保证流水线不中断。
        """
        output: list[LLMResult] = []
        for seg in segments:
            if not seg.text or seg.is_gap:
                output.append(LLMResult(en_text="", keywords=["nature", "landscape"]))
                continue
            # 简单分词：非字母数字字符作为分隔符
            tokens = re.findall(r"[A-Za-zÀ-ÿ]{4,}", seg.text.lower())
            keywords = [t for t in tokens if t not in _STOPWORDS]
            # 去重保序
            seen: set[str] = set()
            unique_kw: list[str] = []
            for k in keywords:
                if k not in seen:
                    seen.add(k)
                    unique_kw.append(k)
                if len(unique_kw) >= 3:
                    break
            if not unique_kw:
                unique_kw = ["nature", "landscape"]
            output.append(LLMResult(en_text=seg.text, keywords=unique_kw))
        return output
