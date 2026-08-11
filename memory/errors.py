"""稳定的存储层业务异常，供 API 做类型化错误映射。"""
from __future__ import annotations


class ResourceConflictError(RuntimeError):
    """请求与当前资源状态冲突，应映射为 HTTP 409。"""


class DocumentWriteBusyError(ResourceConflictError):
    """同一文档由另一个进程持有可恢复写入意图。"""


class StorageRecoveryPendingError(ResourceConflictError):
    """写入已登记但暂未能完成，后续 MemoryStore 操作会继续恢复。"""
