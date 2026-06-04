import pytest
from app.skills.registry import disk_vendor_lifetime


def test_disk_vendor_lifetime_kioxia():
    # Kioxia / Toshiba: SMART 173 < 100 -> 返修
    smart_info_fail = """
Device Model:     Kioxia KCD61LUL960G
173 Unknown_Attribute       0x0032   099   099   000    Old_age   Always       -       123
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_fail}) == "返修"

    smart_info_ok = """
Device Model:     Kioxia KCD61LUL960G
173 Unknown_Attribute       0x0032   100   100   000    Old_age   Always       -       123
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_ok}) == "正常"


def test_disk_vendor_lifetime_intel():
    # Intel: SMART 233 <= 10 -> 返修
    smart_info_fail = """
Model Number:     INTEL SSDSC2KB240G8
233 Media_Wearout_Indicator 0x0032   010   010   000    Old_age   Always       -       10
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_fail}) == "返修"

    smart_info_ok = """
Model Number:     INTEL SSDSC2KB240G8
233 Media_Wearout_Indicator 0x0032   011   011   000    Old_age   Always       -       11
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_ok}) == "正常"


def test_disk_vendor_lifetime_samsung():
    # Samsung: SMART 177 <= 10 -> 返修
    smart_info_fail = """
Device Model:     SAMSUNG MZ7LM240HCGR-00003
177 Wear_Leveling_Count     0x0013   008   008   000    Old_age   Always       -       8
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_fail}) == "返修"

    smart_info_ok = """
Device Model:     SAMSUNG MZ7LM240HCGR-00003
177 Wear_Leveling_Count     0x0013   011   011   000    Old_age   Always       -       11
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_ok}) == "正常"


def test_disk_vendor_lifetime_liteon():
    # Liteon: SMART 202 <= 10 -> 返修; otherwise 177 <= 10 -> 返修
    smart_info_202_fail = """
Device Model:     LITEON CV3-8D128
202 Unknown_Attribute       0x0032   010   010   000    Old_age   Always       -       10
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_202_fail}) == "返修"

    smart_info_177_fail = """
Device Model:     LITEON CV3-8D128
177 Wear_Leveling_Count     0x0013   009   009   000    Old_age   Always       -       9
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_177_fail}) == "返修"

    smart_info_ok = """
Device Model:     LITEON CV3-8D128
202 Unknown_Attribute       0x0032   100   100   000    Old_age   Always       -       0
177 Wear_Leveling_Count     0x0013   100   100   000    Old_age   Always       -       0
"""
    assert disk_vendor_lifetime({"smart_info": smart_info_ok}) == "正常"


def test_disk_vendor_lifetime_unrecognized():
    smart_info = """
Device Model:     MyCustomUnrecognizedSSD 123
177 Wear_Leveling_Count     0x0013   001   001   000    Old_age   Always       -       1
"""
    # Unrecognized model should fallback to returning "正常"
    assert disk_vendor_lifetime({"smart_info": smart_info}) == "正常"


def test_disk_vendor_lifetime_missing_smart_info():
    with pytest.raises(ValueError, match="缺少或空的 'smart_info' 变量"):
        disk_vendor_lifetime({})


from unittest.mock import AsyncMock, MagicMock

from app.memory.variable_pool.engine import sop_request_variable


@pytest.mark.asyncio
async def test_sop_request_variable_skill_call():
    # Mock KBClient
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {"name": "check_meth", "acquisition_strategy": "skill_call", "acquisition_tool": "disk_vendor_lifetime"}
            ],
        }
    )

    # Mock ConversationSopClient
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "smart_info": "Device Model: SAMSUNG MZ7LM240\n177 Wear_Leveling_Count 0x0013 008 008 000 Old_age Always - 8"
            },
            "pending_variable_name": None,
        }
    )

    # Execute
    res = await sop_request_variable(
        variable_name="check_meth",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    # Validate
    assert isinstance(res, dict)
    assert res.get("ok") is True
    assert res.get("value") == "返修"
    assert res.get("source") == "skill_call"
