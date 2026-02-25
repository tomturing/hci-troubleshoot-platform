#!/bin/bash
echo "=== 1. 创建工单 ==="
CASE_RESP=$(curl -s -X POST "http://localhost:8000/api/cases/" \
  -H "Content-Type: application/json" \
  -H "x-trace-id: trace-manual-001" \
  -d '{"client_id": "manual-test-user", "title": "节点 NotReady", "description": "我的 K8s 工作节点突然变成 NotReady 状态", "source": "terminal"}')

echo $CASE_RESP | jq . || echo $CASE_RESP

CASE_ID=$(echo $CASE_RESP | grep -o '"case_id":"[^"]*' | cut -d'"' -f4)
echo -e "\n=> 获取到的 Case ID: $CASE_ID\n"

echo "=== 2. 查看工单详情 ==="
curl -s -X GET "http://localhost:8000/api/cases/$CASE_ID" | jq .

echo -e "\n=== 3. 为该工单创建对话 ==="
CONV_RESP=$(curl -s -X POST "http://localhost:8000/api/conversations/?case_id=$CASE_ID" \
  -H "Content-Type: application/json" )

echo $CONV_RESP | jq . || echo $CONV_RESP

CONV_ID=$(echo $CONV_RESP | grep -o '"conversation_id":"[^"]*' | cut -d'"' -f4)
echo -e "\n=> 获取到的 Conversation ID: $CONV_ID\n"

echo "=== 4. 发送诊断消息 (SSE 流式输出) ==="
curl -N -X POST "http://localhost:8000/api/conversations/$CONV_ID/message" \
  -H "Content-Type: application/json" \
  -d '{"case_id": "'"$CASE_ID"'", "role": "user", "content": "你好，请帮我排查一下节点 NotReady 的问题。"}'

echo -e "\n\n=== 5. 等待 2 秒后查看对话历史记录 ==="
sleep 2
curl -s -X GET "http://localhost:8000/api/conversations/$CONV_ID/messages" | jq .
