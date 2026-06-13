-- 1. 更新 Markdown 表格中的 is_sys_disk 来源为 llm_inference
UPDATE sop_document
SET content_md = replace(
    content_md,
    '| is_sys_disk | boolean | skill:is_sys_disk | 是否是系统盘 |',
    '| is_sys_disk | boolean | llm_inference | 是否是系统盘 |'
)
WHERE id = 2;

-- 2. 在 variable_schema 中将 is_sys_disk 的策略由 skill_call 改为 llm_inference，且 depends_on 仍保留 ["alert_type"]
UPDATE sop_document
SET variable_schema = '[
  {
    "name": "hci_version",
    "display_name": "hci_version",
    "description": "超融合版本信息",
    "type": "string",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "env:hci_version",
    "required": true,
    "depends_on": []
  },
  {
    "name": "alert_type",
    "display_name": "alert_type",
    "description": "告警类型",
    "type": "string",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "env:alert_type",
    "required": true,
    "depends_on": []
  },
  {
    "name": "node_ip",
    "display_name": "node_ip",
    "description": "告警硬盘所在主机",
    "type": "ip",
    "acquisition_strategy": "env_injection",
    "acquisition_tool": "skill:alert-parsing",
    "required": true,
    "depends_on": []
  },
  {
    "name": "is_sys_disk",
    "display_name": "is_sys_disk",
    "description": "是否是系统盘",
    "type": "boolean",
    "acquisition_strategy": "llm_inference",
    "acquisition_tool": null,
    "required": true,
    "depends_on": ["alert_type"]
  },
  {
    "name": "asan_disks",
    "display_name": "asan_disks",
    "description": "aSAN硬盘信息",
    "type": "json",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_storage_disk_list",
    "required": true,
    "depends_on": []
  },
  {
    "name": "disk_sn",
    "display_name": "disk_sn",
    "description": "告警硬盘的标识",
    "type": "string",
    "acquisition_strategy": "llm_inference",
    "acquisition_tool": null,
    "required": true,
    "depends_on": ["asan_disks"]
  },
  {
    "name": "disk_dev",
    "display_name": "disk_dev",
    "description": "asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值",
    "type": "string",
    "acquisition_strategy": "llm_inference",
    "acquisition_tool": null,
    "required": true,
    "depends_on": ["disk_sn", "asan_disks"]
  },
  {
    "name": "smart_info",
    "display_name": "smart_info",
    "description": "执行硬盘SMART原始回显信息",
    "type": "string",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_system_smartctl",
    "required": true,
    "depends_on": ["disk_dev", "node_ip"]
  },
  {
    "name": "check_meth",
    "display_name": "check_meth",
    "description": "磁盘厂商寿命检测结果（正常 / 返修）",
    "type": "string",
    "acquisition_strategy": "skill_call",
    "acquisition_tool": "disk_vendor_lifetime",
    "required": true,
    "depends_on": ["smart_info"]
  }
]'::jsonb
WHERE id = 2;
