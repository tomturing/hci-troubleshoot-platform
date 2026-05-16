"""
将AI回复内容写入Excel G列

逻辑：
1. 从eval_results.json获取问题编号和conversation_id
2. 调用API获取对话消息
3. 提取role='assistant'的消息内容
4. 写入Excel对应行的G列
"""

import json
import httpx
import pandas as pd
from pathlib import Path


def main():
    # 文件路径
    excel_path = Path(__file__).parent.parent.parent / "HCI-内存硬盘非P4-260101-26058.xlsx"
    results_path = Path(__file__).parent / "eval_results.json"
    failed_path = Path(__file__).parent / "failed_records.json"
    output_path = Path(__file__).parent.parent.parent / "HCI-内存硬盘非P4-260101-26058-结果.xlsx"
    api_base_url = "http://172.22.73.249"

    print("=== 从数据库获取AI回复写入Excel ===")

    # 读取Excel
    df = pd.read_excel(excel_path)
    print(f"Excel总行数: {len(df)}")

    # 读取评估结果，获取conversation_id
    with open(results_path, 'r', encoding='utf-8') as f:
        results = json.load(f)

    # 按问题编号建立索引（保留最后一次）
    problem_conv = {}
    for r in results:
        problem_id = r['problem_id']
        if r.get('conversation_id'):
            problem_conv[problem_id] = r['conversation_id']

    # 读取失败记录
    with open(failed_path, 'r', encoding='utf-8') as f:
        failed = json.load(f)
    failed_problems = {f['problem_id'] for f in failed}

    print(f"成功记录: {len(problem_conv)}")
    print(f"失败记录: {len(failed_problems)}")

    # 获取AI回复内容
    ai_responses = []
    with httpx.Client(timeout=10.0) as client:
        for i, row in df.iterrows():
            problem_id = str(row['问题编号'])

            if problem_id in failed_problems:
                ai_responses.append("处理失败")
                continue

            if problem_id not in problem_conv:
                ai_responses.append("")
                continue

            conv_id = problem_conv[problem_id]

            try:
                resp = client.get(f"{api_base_url}/api/conversations/{conv_id}/messages")
                messages = resp.json()

                # 找assistant消息
                assistant_content = ""
                for m in messages:
                    if m['role'] == 'assistant':
                        assistant_content = m['content']
                        break

                ai_responses.append(assistant_content)

            except Exception as e:
                ai_responses.append(f"获取失败: {e}")

            # 每100条显示进度
            if (i + 1) % 100 == 0:
                print(f"  已处理: {i + 1}/{len(df)}")

    # 写入G列
    df['AI回复内容'] = ai_responses

    # 保存
    df.to_excel(output_path, index=False)
    print(f"\n结果已保存: {output_path}")

    # 预览
    print("\n=== 预览前5条 ===")
    for i in range(5):
        print(f"\n{i+1}. {df.iloc[i]['问题编号']}")
        print(f"   问题: {df.iloc[i]['问题描述'][:30]}...")
        print(f"   AI回复: {df.iloc[i]['AI回复内容'][:80]}...")


if __name__ == "__main__":
    main()