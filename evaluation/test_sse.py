"""
测试 SSE 流接收
"""

import asyncio
import json
import httpx


async def test_sse():
    api_base_url = "http://172.22.73.249"
    case_id = "Q2026051144968"
    conversation_id = "8dfc08ba-ab11-4864-80c3-5b0633396d0b"

    print("接收 SSE 流...")
    ai_content = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{api_base_url}/api/conversations/{conversation_id}/message",
            json={
                "case_id": case_id,
                "role": "user",
                "content": "打补丁包的时候提示有亚健康磁盘",
            },
        ) as response:
            print(f"状态码: {response.status_code}")
            async for line in response.aiter_lines():
                print(f"收到: {line[:100]}")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        print("=== 流结束 ===")
                        break
                    try:
                        chunk = json.loads(data)
                        if "content" in chunk:
                            ai_content.append(chunk["content"])
                    except:
                        pass
                elif line.startswith("event:"):
                    print(f"事件: {line}")

    print(f"\nAI 响应内容:\n{''.join(ai_content)}")


asyncio.run(test_sse())