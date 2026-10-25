"""并发工具模块：封装 ThreadPoolExecutor 的常用并发模式。

设计要点：
- 单一职责：本模块只提供"并发执行函数列表"的通用工具
- 线程安全：所有函数都是无状态的，调用方需自行管理共享状态（如加锁）
- 容错：单任务失败不影响其他任务，返回 (result, exception) 元组
- 依赖注入：max_workers 由调用方传入，便于按 API 限流策略调整

为什么用线程而非进程？
- I/O 密集任务（HTTP 下载/搜索）—— GIL 在 I/O wait 时释放，线程足够
- 进程池开销大且共享状态复杂
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")  # 输入类型
R = TypeVar("R")  # 输出类型


@dataclass(frozen=True)
class TaskResult(Generic[R]):
    """单个并发任务的执行结果。

    attributes:
        index: 任务在原输入列表中的下标（0-based），便于结果对齐
        success: 是否成功
        result: 成功时的返回值（失败时为 None）
        exception: 失败时的异常（成功时为 None）
        elapsed: 耗时（秒）
    """

    index: int
    success: bool
    result: R | None
    exception: Exception | None
    elapsed: float


def run_concurrent(
    func: Callable[[T], R],
    items: Iterable[T],
    max_workers: int = 5,
    desc: str = "",
) -> list[TaskResult[R]]:
    """并发执行 func 对每个 item 的调用，返回与输入等长的 TaskResult 列表。

    Args:
        func: 单任务处理函数（接受 T 返回 R）
        items: 输入项列表
        max_workers: 最大并发数（按 API 限流策略选择，保守取 5）
        desc: 日志描述（如 "搜索素材"），用于进度提示

    Returns:
        List[TaskResult]，按 index 升序排列（与输入顺序对齐）
    """
    items_list = list(items)
    n = len(items_list)
    if n == 0:
        return []

    results: list[TaskResult[R] | None] = [None] * n
    actual_max_workers = min(max_workers, n)  # 避免 ThreadPoolExecutor 空转

    print(f"[CONCURRENT] {desc}: {n} 个任务，{actual_max_workers} 并发")

    with ThreadPoolExecutor(max_workers=actual_max_workers) as executor:
        # 提交所有任务，记录 future → index 映射
        future_to_index = {
            executor.submit(_run_single, func, i, item): i
            for i, item in enumerate(items_list)
        }

        completed = 0
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                result_data = future.result()
                results[idx] = result_data
            except Exception as e:
                # _run_single 内部已捕获，理论上不会到这；防御性兜底
                results[idx] = TaskResult(
                    index=idx,
                    success=False,
                    result=None,
                    exception=e,
                    elapsed=0.0,
                )
            completed += 1
            # 进度提示：每 10 个或最后一批输出一次
            if completed % 10 == 0 or completed == n:
                print(f"  [{desc}] 进度 {completed}/{n}")

    # 类型安全：所有位置已填充
    return [r if r is not None else TaskResult(
        index=0, success=False, result=None,
        exception=RuntimeError("未填充"), elapsed=0.0
    ) for r in results]


def _run_single(func: Callable[[T], R], index: int, item: T) -> TaskResult[R]:
    """单个任务的执行包装：捕获异常 + 计时。"""
    start = time.perf_counter()
    try:
        result = func(item)
        elapsed = time.perf_counter() - start
        return TaskResult(
            index=index,
            success=True,
            result=result,
            exception=None,
            elapsed=elapsed,
        )
    except Exception as e:
        elapsed = time.perf_counter() - start
        return TaskResult(
            index=index,
            success=False,
            result=None,
            exception=e,
            elapsed=elapsed,
        )
