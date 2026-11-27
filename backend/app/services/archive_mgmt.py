"""档案归档领域服务：整理工序完成后联动归档，入库后锁定关联记录。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ArchiveMgmtService(BaseModuleService):
    MODULE = "archive_mgmt"
    ENTITY = "修缮档案"
    OPERATION_TERM = "整理归档"
    LIST_FIELDS = ["档案名称", "档案类别", "形成日期", "保管期限", "密级等级", "归档人", "归档状态", "备注说明"]
    REQUIRED_FIELDS = ["档案名称", "档案类别", "形成日期"]
    STATUS_ORDER = ["待整理", "已整理", "已入库", "已借阅"]
    ACTION_RULES = {"整理归档": "已整理", "归档入库": "已入库", "借阅档案": "已借阅"}
    NEGATIVE_ACTIONS = []

    def cross_module_verify(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if action == "归档入库":
            # 入库前确认关联工序是否全部完成
            arch_name = entry.get("档案名称", "")
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") in arch_name:
                    if ws_row.get("status") != "已完成":
                        return False, f"档案关联工序「{ws_row.get('工序名称')}」尚未完成"
        if action == "借阅档案":
            if entry.get("密级等级", "") in ("机密", "绝密"):
                return False, "高密级档案不支持借阅"
        return True, ""

    def cross_module_effect(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        effects = []
        if action == "归档入库":
            # 入库后标记关联验收记录为已归档
            arch_name = entry.get("档案名称", "")
            for acc in store.rows("acceptance_record"):
                if acc.get("验收工序", "") in arch_name and acc.get("status") == "已签署":
                    acc["已归档"] = True
                    effects.append(dict(acc))
            # 关联修缮日志归档
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") in arch_name and log.get("status") == "已审核":
                    log["status"] = "已归档"
                    log["pending"] = False
                    effects.append(dict(log))
        return effects