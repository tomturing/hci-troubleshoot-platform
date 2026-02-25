import httpx
import asyncio
import json

BASE_URL = "http://localhost:8000"

async def run_e2e():
    print("--- 启动 E2E 验证 ---")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # 1. 创建工单
        print("\\n1. [Gateway -> Case Service] 创建工单")
        case_data = {
            "client_id": "demo-client-1",
            "title": "系统无法启动",
            "description": "出现内核报错信息",
            "source": "api"
        }
        resp = await client.post("/api/cases/", json=case_data)
        assert resp.status_code == 201, f"Failed: {resp.text}"
        case = resp.json()
        case_id = case["case_id"]
        print(f"✅ 工单创建成功: {case_id}")

        # 2. 查询工单详情
        print("\\n2. [Gateway -> Case Service] 查询工单")
        resp = await client.get(f"/api/cases/{case_id}")
        assert resp.status_code == 200, f"Failed: {resp.text}"
        print(f"✅ 工单详情验证通过: {resp.json().get('title')}")

        # 3. 创建对话
        print("\\n3. [Gateway -> Conversation Service] 为工单打开排查对话")
        conv_resp = await client.post(f"/api/conversations/?case_id={case_id}")
        assert conv_resp.status_code == 201, f"Failed: {conv_resp.text}"
        conv_id = conv_resp.json()["conversation_id"]
        print(f"✅ 对话创建成功: {conv_id}")

        # 4. 发送消息，期待有流式响应返回
        print("\\n4. [Gateway -> Conversation Service -> OpenClaw] 发送消息并获取智能体的排查回复")
        msg_payload = {"case_id": case_id, "role": "user", "content": "我看见屏幕上输出了 OOM 错误。"}
        async with client.stream("POST", f"/api/conversations/{conv_id}/message", json=msg_payload) as stream_resp:
            assert stream_resp.status_code == 200, f"Streaming endpoint failed: {stream_resp.status_code}"
            
            full_reply = []
            async for chunk in stream_resp.aiter_text():
                # print(chunk, end="")
                full_reply.append(chunk)
            
            reply_text = "".join(full_reply)
            assert "data:" in reply_text or len(reply_text) > 0, "No data streamed back"
            print(f"✅ 收到了 SSE 消息！总计长度：{len(reply_text)}")

        # 因为后台存库可能有一瞬间的异步延迟，这里略等
        await asyncio.sleep(1)

        # 5. 获取对话消息树
        print("\\n5. [Gateway -> Conversation Service] 验证消息入库情况")
        msgs_resp = await client.get(f"/api/conversations/{conv_id}/messages")
        assert msgs_resp.status_code == 200, f"Failed: {msgs_resp.text}"
        messages = msgs_resp.json()
        print(f"✅ 成功提取到本轮谈话历史！总计记录：{len(messages)} 条。")
        for m in messages:
            print(f"   [{m['role']}] {m['content'][:30]}...")
            
    print("\\n🎉 全链路串联测试大圆满通过！🎉")

if __name__ == "__main__":
    asyncio.run(run_e2e())
