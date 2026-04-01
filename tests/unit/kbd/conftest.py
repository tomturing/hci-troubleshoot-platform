"""
tests/unit/kbd/conftest.py — KBD 管道单元测试公共 fixtures
"""
from __future__ import annotations

import os
import sys

import pytest

# 将 scripts/ 目录加入路径，使 `from kbd.xxx import ...` 可用
_scripts_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
)
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)


# ─── 公共 HTML 测试样本 ───────────────────────────────────────────────────────

# 9 个 Section 的最小化 HTML（基于真实案例 36156 的结构）
MINIMAL_9_SECTION_HTML = """\
<div class="mceNonEditable problem-description-box" contenteditable="false">
  <input class="mceNonEditable firstMceNonEditable" readonly type="text" value="*问题描述" />
  <a id="menu1" data-anchor="catalogue"></a>
  <div class="problem-description" contenteditable="true">网口频繁闪断告警</div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="告警信息" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true"><img src="/_static/202601/img1.png" /></div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="有效排查步骤" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true"><p>登录对应主机查看网口状态</p></div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="根因" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true">网口频繁 down/up，触发告警</div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="*解决方案" />
  <a data-anchor="catalogue"></a>
  <div class="problem-description" contenteditable="true"><p>按优先级依次处理：插拔光模块</p></div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="操作影响范围" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true">无</div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="是否是临时解决方案" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true">否</div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="建议与总结" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true"> </div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="排查内容" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true"> </div>
</div>
"""

MISSING_MANDATORY_HTML = """\
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="*问题描述" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true">有问题描述</div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="有效排查步骤" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true"> </div>
</div>
<div class="mceNonEditable" contenteditable="false">
  <input class="mceNonEditable" readonly type="text" value="*解决方案" />
  <a data-anchor="catalogue"></a>
  <div contenteditable="true">有解决方案</div>
</div>
"""


@pytest.fixture
def minimal_rows() -> dict:
    """最小化 API rows 字典（基于真实案例 36156）"""
    return {
        "id": 36156,
        "name": "【HCI-VN】网口频繁闪断告警。错误码：0x00000814040C000E",
        "content": MINIMAL_9_SECTION_HTML,
        "mainModuleNames": "网络问题",
        "childModuleNames": "实体机网络",
        "suiteVersion": "通用",
        "updateTime": "2026-01-15 11:25:46",
        "createTime": None,
        "createAdminId": "68532",
        "updateAdminId": "14201",
        "productId": 33,
        "productName": "超融合HCI",
        "status": 1,
    }


@pytest.fixture
def missing_mandatory_rows(minimal_rows) -> dict:
    """缺少必填 section（有效排查步骤为空）的 rows"""
    r = dict(minimal_rows)
    r["content"] = MISSING_MANDATORY_HTML
    return r


@pytest.fixture
def api_payload(minimal_rows) -> dict:
    """完整 API 响应（含 code=0 和 rows 字段）"""
    return {"code": 0, "rows": minimal_rows, "msg": "操作成功", "success": True}
