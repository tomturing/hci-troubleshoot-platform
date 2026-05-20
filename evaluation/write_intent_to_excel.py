"""
将意图识别结果写入Excel G列

功能：
1. 从 eval_results.json 读取结果
2. 按问题编号去重，保留最后一次成功的记录
3. 从AI响应中提取意图识别分类编号
4. 按Excel原始顺序写入G列
"""

import json
import re
import pandas as pd
from pathlib import Path
from openpyxl import load_workbook


def extract_intent_result(ai_response: str) -> str:
    """
    从AI响应中提取意图识别结果

    直接返回AI响应中的意图识别部分（包含分类编号和判断依据）
    """
    if not ai_response:
        return ""

    # 清理响应，保留意图识别核心内容
    # 截取到"请回复"之前的部分（意图识别结果）
    if "请回复" in ai_response:
        ai_response = ai_response.split("请回复")[0]

    # 去掉开头的"根据..."等通用描述，保留分类列表
    lines = ai_response.strip().split('\n')
    result_lines = []
    started = False

    for line in lines:
        # 找到分类列表开始（通常以①或分类编号开头）
        if '①' in line or '②' in line or '③' in line or re.match(r'^\s*[①②③]', line):
            started = True
        if started:
            result_lines.append(line.strip())

    if result_lines:
        return '\n'.join(result_lines[:10])  # 最多保留10行

    # 如果没有找到分类列表，返回原始响应的前200字符
    return ai_response[:200].strip()


def main():
    # 文件路径
    excel_path = Path(__file__).parent.parent.parent / "HCI-内存硬盘非P4-260101-26058.xlsx"
    results_path = Path(__file__).parent / "eval_results.json"
    failed_path = Path(__file__).parent / "failed_records.json"
    output_path = Path(__file__).parent.parent.parent / "HCI-内存硬盘非P4-260101-26058-结果.xlsx"

    print("=== 意图识别结果写入Excel ===")
    print(f"Excel: {excel_path}")
    print(f"结果: {results_path}")

    # 读取Excel原始数据
    df = pd.read_excel(excel_path)
    print(f"Excel总行数: {len(df)}")

    # 读取评估结果
    with open(results_path, 'r', encoding='utf-8') as f:
        results = json.load(f)

    # 按问题编号去重，保留最后一次成功的记录
    problem_results = {}
    for r in results:
        problem_id = r['problem_id']
        # 只保留有AI响应的记录
        if r.get('ai_response_preview') or r.get('status') == 'success':
            # 后面的记录覆盖前面的（保留最后一次）
            problem_results[problem_id] = r

    print(f"成功结果数: {len(problem_results)}")

    # 读取失败记录
    with open(failed_path, 'r', encoding='utf-8') as f:
        failed = json.load(f)

    failed_problems = {f['problem_id']: f for f in failed}
    print(f"失败记录数: {len(failed_problems)}")

    # 提取意图识别结果并匹配到Excel行
    intent_results = []
    matched_count = 0
    failed_count = 0
    no_match_count = 0

    for i, row in df.iterrows():
        problem_id = str(row['问题编号'])

        if problem_id in problem_results:
            # 成功匹配
            ai_response = problem_results[problem_id].get('ai_response_preview', '')
            intent = extract_intent_result(ai_response)
            intent_results.append(intent)
            matched_count += 1
        elif problem_id in failed_problems:
            # 失败记录
            intent_results.append("处理失败")
            failed_count += 1
        else:
            # 未匹配（可能是重复测试的记录）
            intent_results.append("")
            no_match_count += 1

    print(f"\n匹配统计:")
    print(f"  成功匹配: {matched_count}")
    print(f"  失败记录: {failed_count}")
    print(f"  未匹配: {no_match_count}")

    # 添加G列
    df['意图识别结果'] = intent_results

    # 保存到新文件
    df.to_excel(output_path, index=False)
    print(f"\n结果已保存: {output_path}")

    # 显示部分结果预览
    print("\n=== 结果预览（前10条） ===")
    preview_df = df[['问题编号', '问题描述', '意图识别结果']].head(10)
    print(preview_df.to_string())


if __name__ == "__main__":
    main()