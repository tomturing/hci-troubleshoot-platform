"""受控 PNG 派生（隔离进程执行，设计文档 §6.2）。

控制台截图可能来自不受信来源；解码器缺陷、超大尺寸或解码挂起都不能影响
平台主进程。因此 PPM→PNG 派生在独立子进程（spawn 上下文）中执行，并施加
像素/边长上限与总超时；超限或超时即终止子进程并 fail-closed。
"""

from __future__ import annotations

import contextlib
import io
import multiprocessing as mp

# 派生资源上限（与在线/离线 Worker 共享）。
MAX_PNG_PIXELS = 4096 * 4096
MAX_PNG_SIDE = 8192
DEFAULT_DERIVE_TIMEOUT_SECONDS = 30.0


def _derive_worker(conn, ppm_bytes: bytes, max_pixels: int, max_side: int) -> None:
    """子进程工作函数：解码 PPM、校验尺寸、编码 PNG；结果经管道回传。"""

    try:
        from PIL import Image

        image = Image.open(io.BytesIO(ppm_bytes))
        width, height = image.size
        if width > max_side or height > max_side or width * height > max_pixels:
            conn.send(("error", f"截图尺寸超过派生上限: {width}x{height}"))
            return
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        conn.send(("ok", buffer.getvalue(), width, height))
    except Exception as exc:  # 子进程内任何异常都转为结构化错误，不抛出到管道之外
        with contextlib.suppress(Exception):
            conn.send(("error", f"PPM 派生失败: {exc}"))
    finally:
        conn.close()


def derive_png_isolated(
    ppm_bytes: bytes,
    *,
    timeout_seconds: float = DEFAULT_DERIVE_TIMEOUT_SECONDS,
    max_pixels: int = MAX_PNG_PIXELS,
    max_side: int = MAX_PNG_SIDE,
) -> tuple[bytes, int, int]:
    """在隔离子进程中把 PPM 派生为 PNG，返回 ``(png_bytes, width, height)``。

    Raises:
        ValueError: 图片非法或超过尺寸上限。
        TimeoutError: 派生超时（子进程已被终止）。
    """

    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_derive_worker,
        args=(child_conn, ppm_bytes, max_pixels, max_side),
        daemon=True,
    )
    proc.start()
    child_conn.close()

    try:
        if not parent_conn.poll(timeout_seconds):
            proc.terminate()
            proc.join(5)
            raise TimeoutError(f"PNG 派生超时（{timeout_seconds}s），子进程已终止")
        message = parent_conn.recv()
    finally:
        parent_conn.close()
        proc.join(5)

    if message[0] == "ok":
        return message[1], message[2], message[3]
    raise ValueError(message[1])
