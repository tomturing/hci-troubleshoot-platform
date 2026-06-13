#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://hci_admin:dev_password_123@localhost:5432/hci_troubleshoot"
)

NEW_VARIABLE_SCHEMA = [
  {
    "name": "hci_version",
    "display_name": "hci_version",
    "description": "超融合版本信息",
    "type": "string",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "env:hci_version",
    "required": True,
    "depends_on": []
  },
  {
    "name": "alert_type",
    "display_name": "alert_type",
    "description": "告警类型",
    "type": "string",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "env:alert_type",
    "required": True,
    "depends_on": []
  },
  {
    "name": "node_ip",
    "display_name": "node_ip",
    "description": "告警硬盘所在主机",
    "type": "ip",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "skill:alert-parsing",
    "required": True,
    "depends_on": []
  },
  {
    "name": "is_sys_disk",
    "display_name": "is_sys_disk",
    "description": "是否是系统盘",
    "type": "boolean",
    "acquisition_strategy": "skill_call",
    "acquisition_tool": "is_sys_disk",
    "required": True,
    "depends_on": ["alert_type"]
  },
  {
    "name": "asan_disks",
    "display_name": "asan_disks",
    "description": "aSAN硬盘信息",
    "type": "json",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_storage_disk_list",
    "required": True,
    "depends_on": []
  },
  {
    "name": "disk_sn",
    "display_name": "disk_sn",
    "description": "告警硬盘的标识",
    "type": "string",
    "acquisition_strategy": "llm_inference",
    "acquisition_tool": None,
    "required": True,
    "depends_on": ["asan_disks"]
  },
  {
    "name": "disk_dev",
    "display_name": "disk_dev",
    "description": "asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值",
    "type": "string",
    "acquisition_strategy": "llm_inference",
    "acquisition_tool": None,
    "required": True,
    "depends_on": ["disk_sn", "asan_disks"]
  },
  {
    "name": "smart_info",
    "display_name": "smart_info",
    "description": "执行硬盘SMART原始回显信息",
    "type": "string",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_system_smartctl",
    "required": True,
    "depends_on": ["disk_dev", "node_ip"]
  },
  {
    "name": "check_meth",
    "display_name": "check_meth",
    "description": "磁盘厂商寿命检测结果（正常 / 返修）",
    "type": "string",
    "acquisition_strategy": "skill_call",
    "acquisition_tool": "disk_vendor_lifetime",
    "required": True,
    "depends_on": ["smart_info"]
  }
]

async def main():
    print(f"Connecting to database at {DATABASE_URL}...")
    engine = create_async_engine(DATABASE_URL, echo=False)
    
    async with engine.begin() as conn:
        # 1. 查找 SOP 2 的数据
        result = await conn.execute(
            text("SELECT content_md, tree_json FROM sop_document WHERE id = 2")
        )
        row = result.fetchone()
        if not row:
            print("ERROR: SOP document with ID=2 not found in database!")
            await engine.dispose()
            sys.exit(1)
            
        content_md, tree_json = row
        print("SOP 2 found. Inspecting variable declaration section...")
        
        # 2. 检查并替换 content_md 中的变量声明表
        # 寻找 Markdown 中的变量表格声明区段
        # 如果已经存在 alert_type，则无需重复插入
        if "alert_type" in content_md:
            print("alert_type already declared in Markdown.")
        else:
            print("Injecting alert_type and updating is_sys_disk, smart_info in Markdown...")
            # 找到 is_sys_disk 这一行，定位变量表格并修改
            # 这是一个典型的 Markdown 替换操作。
            # 原始变量声明表：
            # | hci_version   | string  | env:hci_version             | 超融合版本信息                                 |
            # | node_ip       | ip      | skill:alert-parsing         | 告警硬盘所在主机                               |
            # | is_sys_disk   | boolean | llm_inference               | 是否是系统盘                                   |
            # | asan_disks    | json    | tool:acli_storage_disk_list | aSAN硬盘信息                                   |
            # | disk_sn       | string  | llm_inference               | 告警硬盘的标识                                 |
            # | disk_dev      | string  | llm_inference               | asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值 |
            # | smart_info    | string  | llm_inference               | 执行硬盘SMART原始回显信息                      |
            # | check_meth    | string  | skill:disk_vendor_lifetime  | 磁盘厂商寿命检测结果（正常 / 返修）            |
            
            old_table_section = (
                "| hci_version   | string  | env:hci_version             | 超融合版本信息                                 |\n"
                "| node_ip       | ip      | skill:alert-parsing         | 告警硬盘所在主机                               |\n"
                "| is_sys_disk   | boolean | llm_inference               | 是否是系统盘                                   |\n"
                "| asan_disks    | json    | tool:acli_storage_disk_list | aSAN硬盘信息                                   |\n"
                "| disk_sn       | string  | llm_inference               | 告警硬盘的标识                                 |\n"
                "| disk_dev      | string  | llm_inference               | asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值 |\n"
                "| smart_info    | string  | llm_inference               | 执行硬盘SMART原始回显信息                      |\n"
                "| check_meth    | string  | skill:disk_vendor_lifetime  | 磁盘厂商寿命检测结果（正常 / 返修）            |"
            )
            
            new_table_section = (
                "| hci_version   | string  | env:hci_version             | 超融合版本信息                                 |\n"
                "| alert_type    | string  | env:alert_type              | 告警类型                                       |\n"
                "| node_ip       | ip      | skill:alert-parsing         | 告警硬盘所在主机                               |\n"
                "| is_sys_disk   | boolean | skill:is_sys_disk           | 是否是系统盘                                   |\n"
                "| asan_disks    | json    | tool:acli_storage_disk_list | aSAN硬盘信息                                   |\n"
                "| disk_sn       | string  | llm_inference               | 告警硬盘的标识                                 |\n"
                "| disk_dev      | string  | llm_inference               | asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值 |\n"
                "| smart_info    | string  | tool:acli_system_smartctl   | 执行硬盘SMART原始回显信息                      |\n"
                "| check_meth    | string  | skill:disk_vendor_lifetime  | 磁盘厂商寿命检测结果（正常 / 返修）            |"
            )
            
            if old_table_section in content_md:
                content_md = content_md.replace(old_table_section, new_table_section)
                print("Markdown content variables table replaced successfully.")
            else:
                # 模糊替换（以防有细微的换行符或空格差异）
                print("WARNING: Table layout doesn't match perfectly. Trying fuzzy line-by-line replacement...")
                content_md = content_md.replace("is_sys_disk   | boolean | llm_inference", "is_sys_disk   | boolean | skill:is_sys_disk")
                content_md = content_md.replace("smart_info    | string  | llm_inference", "smart_info    | string  | tool:acli_system_smartctl")
                if "alert_type" not in content_md:
                    lines = content_md.splitlines()
                    for idx, line in enumerate(lines):
                        if "hci_version" in line:
                            lines.insert(idx + 1, "| alert_type    | string  | env:alert_type              | 告警类型                                       |")
                            break
                    content_md = "\n".join(lines)
                    print("Fuzzy replacement completed.")
                    
        # 3. 更新 content_md 和 variable_schema 到数据库
        print("Updating variable_schema in database...")
        schema_json = json.dumps(NEW_VARIABLE_SCHEMA, ensure_ascii=False)
        await conn.execute(
            text(
                "UPDATE sop_document "
                "SET content_md = :content_md, variable_schema = :variable_schema "
                "WHERE id = 2"
            ),
            {"content_md": content_md, "variable_schema": schema_json}
        )
        print("Database update successful!")
        
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
