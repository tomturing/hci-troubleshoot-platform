"""
KB Pusher - Case 关闭后异步推送摘要到知识库
采用 fire-and-forget 模式，不阻塞工单关闭操作
"""

import asyncio
import json as _json
import urllib.error
import urllib.request

from shared.models.schemas import KBIngestPayload
from shared.observability.logger import get_logger

logger = get_logger("case-service-kb-pusher")

_REQUEST_TIMEOUT = 15.0


def _sync_push(
    url: str,
    payload: dict,
    headers: dict,
    case_id: str,
) -> None:
    """同步推送（在线程池中运行，避免阻塞事件循环）"""
    data = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            status = resp.status
        logger.info(
            event="kb_push_success",
            message=f"Case {case_id} 摘要已推送至 KB",
            case_id=case_id,
            http_status=status,
        )
    except urllib.error.HTTPError as exc:
        logger.warning(
            event="kb_push_http_error",
            message=f"KB 推送返回 HTTP {exc.code}",
            case_id=case_id,
            status_code=exc.code,
        )
    except urllib.error.URLError as exc:
        logger.warning(
            event="kb_push_unavailable",
            message=f"KB Service 不可达，跳过推送: {exc}",
            case_id=case_id,
        )
    except Exception as exc:
        logger.error(
            event="kb_push_unexpected_error",
            message=f"KB 推送意外错误: {exc}",
            case_id=case_id,
        )


async def push_case_summary_to_kb(
    *,
    kb_service_url: str,
    internal_token: str,
    case_id: str,
    title: str,
    description: str,
    resolution_summary: str | None = None,
) -> None:
    """
    将关闭工单的摘要推送至 KB Service 进行知识沉淀。
    文档 source_type = 'realtime'，以 case_id 作为幂等键。

    字段设计：
      - title:   "工单摘要: <title>"
      - content_md: 故障描述 + 解决摘要合并
      - source_type: 'realtime'
      - yaml_meta: {"case_id": case_id}
    """
    content_parts = [f"## 故障描述\n\n{description}"]
    if resolution_summary:
        content_parts.append(f"## 解决方案\n\n{resolution_summary}")

    ingest = KBIngestPayload(
        title=f"工单摘要: {title}",
        content_md="\n\n".join(content_parts),
        source_id=case_id,
        source_type="realtime",
        yaml_meta={"case_id": case_id},
    )
    payload = ingest.model_dump()

    headers = {"Authorization": f"Bearer {internal_token}"}
    url = f"{kb_service_url.rstrip('/')}/api/kb/ingest"

    # 在线程池中执行同步 urllib 调用，避免阻塞事件循环
    await asyncio.to_thread(_sync_push, url, payload, headers, case_id)


def fire_and_forget_push(
    *,
    kb_service_url: str,
    internal_token: str,
    case_id: str,
    title: str,
    description: str,
    resolution_summary: str | None = None,
) -> None:
    """
    异步 fire-and-forget：在当前事件循环中后台提交 KB 推送任务。
    调用方无需 await，不影响主业务响应。
    """
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(
            push_case_summary_to_kb(
                kb_service_url=kb_service_url,
                internal_token=internal_token,
                case_id=case_id,
                title=title,
                description=description,
                resolution_summary=resolution_summary,
            )
        )
    except RuntimeError:
        # 无事件循环场景（测试等）：忽略推送
        logger.warning(event="kb_push_no_loop", message="无事件循环，跳过 KB 推送", case_id=case_id)
