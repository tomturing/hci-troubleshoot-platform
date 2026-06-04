#!/usr/bin/env python3
import os
import shutil
import re
import datetime

# 定义历史归档映射（人工校验确认的四次对话主题与日期）
HISTORICAL_MAP = {
    "b4a93d7d-55de-48e8-b129-1a188614fb7e": {
        "date": "2026-05-29",
        "title": "SOP决策树渲染与详情弹窗优化"
    },
    "2501b76a-5c93-4664-8f0d-863f134bf52e": {
        "date": "2026-05-31",
        "title": "智能体变量池微内核重构与目录文档更新"
    },
    "94030248-50c5-4cd3-b1a5-cb84624043e4": {
        "date": "2026-06-01",
        "title": "KBD双通道数据模型与解耦重构"
    },
    "c09eefb0-3bc7-4ad5-9909-8f00a3856763": {
        "date": "2026-06-04",
        "title": "内置通用Skill机制及磁盘寿命异常SOP重构"
    }
}

BRAIN_DIR = "/home/node/.gemini/antigravity-ide/brain"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def clean_title(title):
    # 去除首部井号与空格
    title = title.strip().lstrip('#').strip()
    # 移除中文和英文括号内容 (例如 (v2), （第二版）)
    title = re.sub(r'[\(\uff08].*?[\)\uff09]', '', title)
    # 移除常见的方案/计划结尾后缀
    for suffix in ["设计方案", "实施计划", "优化方案", "重构方案", "方案", "计划"]:
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()
            break
    # 替换非法字符，仅保留字母、数字、中文、下划线、减号
    title = re.sub(r'[^\w\u4e00-\u9fa5\-]+', '', title)
    return title

def get_file_info(conv_dir, conv_id):
    plan_path = os.path.join(conv_dir, "implementation_plan.md")
    if not os.path.exists(plan_path):
        return None, None
    
    # 1. 优先获取日期
    if conv_id in HISTORICAL_MAP:
        date_str = HISTORICAL_MAP[conv_id]["date"]
    else:
        # 获取文件修改时间作为日期
        mtime = os.path.getmtime(plan_path)
        date_str = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
    
    # 2. 获取标题
    if conv_id in HISTORICAL_MAP:
        title_str = HISTORICAL_MAP[conv_id]["title"]
    else:
        title_str = "未命名方案"
        try:
            with open(plan_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#'):
                        title_str = clean_title(line)
                        break
        except Exception as e:
            print(f"读取标题失败: {e}")
            
    return date_str, title_str

def main():
    if not os.path.exists(BRAIN_DIR):
        print(f"Brain 目录不存在: {BRAIN_DIR}，无需归档。")
        return

    # 创建目标目录
    target_dirs = {
        "plan": os.path.join(REPO_ROOT, "docs/solution/events"),
        "task": os.path.join(REPO_ROOT, "docs/task/events"),
        "verify": os.path.join(REPO_ROOT, "docs/verify/events")
    }
    for d in target_dirs.values():
        os.makedirs(d, exist_ok=True)

    copied_count = 0
    for conv_id in os.listdir(BRAIN_DIR):
        conv_dir = os.path.join(BRAIN_DIR, conv_id)
        if not os.path.isdir(conv_dir):
            continue
            
        date_str, title_str = get_file_info(conv_dir, conv_id)
        if not date_str or not title_str:
            continue
            
        filename = f"{date_str}-{title_str}.md"
        
        # 映射文件
        mapping = {
            "implementation_plan.md": ("plan", filename),
            "task.md": ("task", filename),
            "walkthrough.md": ("verify", filename)
        }
        
        for src_name, (target_key, dest_name) in mapping.items():
            src_path = os.path.join(conv_dir, src_name)
            if os.path.exists(src_path):
                dest_path = os.path.join(target_dirs[target_key], dest_name)
                print(f"正在归档: {src_path} -> {dest_path}")
                shutil.copy2(src_path, dest_path)
                copied_count += 1
                
    print(f"归档完成，共复制/更新了 {copied_count} 个文件。")

if __name__ == "__main__":
    main()
