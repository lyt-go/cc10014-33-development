"""验收记录领域服务：签署验收后联动工序完成状态、修缮日志、归档和质量评定。

新增「确认免验」：现场获准免验的工序，由负责该工序的验收人确认后生成
免验回执（保留工程范围与免验原因），并把免验结论同步到同工程范围、
仍在编制的竣工报告待审核清单。同一工序的免验回执与正式签署资格互斥。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AcceptanceRecordService(BaseModuleService):
    MODULE = "acceptance_record"
    ENTITY = "验收报告"
    OPERATION_TERM = "开始验收"
    LIST_FIELDS = ["验收工序", "验收标准", "验收结论", "遗留问题", "整改期限", "验收人", "验收日期", "工程范围", "备注说明"]
    REQUIRED_FIELDS = ["验收工序", "验收标准", "验收结论"]
    STATUS_ORDER = ["待验收", "验收中", "已签署", "待整改", "已免验"]
    ACTION_RULES = {"开始验收": "验收中", "签署验收": "已签署", "退回整改": "待整改", "确认免验": "已免验"}
    NEGATIVE_ACTIONS = []

    # ── 内部辅助 ──
    @staticmethod
    def _records_for(process: str) -> list[dict[str, Any]]:
        return [r for r in store.rows("acceptance_record") if r.get("验收工序", "") == process]

    @staticmethod
    def _sync_checklist(scope: str, process: str, conclusion: str, effects: list[dict]) -> None:
        """把验收/免验结论同步到同工程范围、仍在编制的竣工报告待审核清单。

        已签署记录同样保留条目，作为报告复核入口；同一工序只保留最新结论。
        """
        if not scope:
            return
        for summary in store.rows("completion_summary"):
            if summary.get("工程范围", "") != scope:
                continue
            if summary.get("status") not in ("编制中", "待补充"):
                continue
            items = [i for i in str(summary.get("待审核清单", "")).split("；") if i]
            prefix = f"工序「{process}」"
            items = [i for i in items if not i.startswith(prefix)]
            items.append(conclusion)
            summary["待审核清单"] = "；".join(items)
            effects.append(dict(summary))

    def cross_module_verify(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        ctx = context or {}
        process = entry.get("验收工序", "")
        if action == "签署验收":
            # 0. 互斥：同一工序已存在免验回执，不得再正式签署
            if any(r.get("status") == "已免验" for r in self._records_for(process)):
                return False, f"工序「{process}」已确认免验，免验回执与正式签署资格互斥，不能再签署验收"
            # 1. 必须有对应的质量检查已通过
            quality_records = store.query_by_field("quality_inspect", "检查工序", process)
            if not quality_records:
                return False, f"工序「{process}」缺少质量检查记录"
            if not any(r.get("status") == "已通过" for r in quality_records):
                return False, "关联质量检查未通过，无法签署验收"
            # 2. 关联排期中工期是否合理
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            for s in schedules:
                if s.get("status") == "已延期":
                    return False, "关联排期已延期，需先获取变更签证再验收"
        if action == "确认免验":
            # 1. 互斥：同一工序已有正式签署，不得再确认免验
            if entry.get("status") == "已签署":
                return False, "该验收记录已正式签署，免验回执与正式签署资格互斥，不能改为免验"
            if any(
                r.get("status") == "已签署" and int(r.get("id", 0)) != entry_id
                for r in self._records_for(process)
            ):
                return False, f"工序「{process}」已有正式签署验收，免验回执与正式签署资格互斥"
            # 2. 权限：只能由负责这道工序的验收人确认免验
            operator = str(ctx.get("operator") or "").strip()
            owner = str(entry.get("验收人") or "").strip()
            if not operator:
                return False, "确认免验必须填写操作的验收人"
            if not owner:
                return False, f"工序「{process}」的验收记录未指定验收人，无人有权确认免验"
            if operator != owner:
                return False, f"只能由负责工序「{process}」的验收人「{owner}」确认免验，当前操作人「{operator}」无权确认"
            # 3. 免验必须保留工程范围和原因
            scope = str(entry.get("工程范围") or ctx.get("scope") or "").strip()
            if not scope:
                return False, "确认免验必须保留工程范围，请先在验收记录中登记工程范围"
            reason = str(ctx.get("reason") or "").strip()
            if not reason:
                return False, "确认免验必须填写免验原因"
        return True, ""

    def cross_module_effect(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        effects = []
        ctx = context or {}
        if action == "签署验收":
            process = entry.get("验收工序", "")
            # 1. 联动工序完成状态
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["status"] = "已完成"
                    ws_row["pending"] = False
                    effects.append(dict(ws_row))

            # 2. 联动修缮日志归档
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process:
                    log["status"] = "已归档"
                    log["pending"] = False
                    effects.append(dict(log))

            # 3. 联动施工排期里程碑更新
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"{process}-验收已签署"
                    effects.append(dict(sched))

            # 4. 触发档案归档（自动创建归档条目）
            arch_rows = store.rows("archive_mgmt")
            new_arch = {
                "id": max((int(r.get("id", 0)) for r in arch_rows), default=0) + 1,
                "档案名称": f"{process}-验收报告",
                "档案类别": "验收档案",
                "形成日期": entry.get("验收日期", ""),
                "保管期限": "永久",
                "密级等级": "内部",
                "归档人": entry.get("验收人", ""),
                "归档状态": "待整理",
                "备注说明": f"验收签署#{entry_id}自动触发",
                "status": "待整理",
                "pending": True,
                "abnormal": False,
            }
            arch_rows.append(new_arch)
            effects.append(new_arch)

            # 5. 更新竣工总结质量评定
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    if summary.get("质量评定", "") == "":
                        summary["质量评定"] = f"工序{process}已验收"
                    effects.append(dict(summary))

            # 6. 已签署记录同步到报告待审核清单，保留复核入口
            self._sync_checklist(
                entry.get("工程范围", ""),
                process,
                f"工序「{process}」正式签署（验收人：{entry.get('验收人', '')}），可复核",
                effects,
            )

        elif action == "确认免验":
            process = entry.get("验收工序", "")
            reason = str(ctx.get("reason") or "").strip()
            operator = str(ctx.get("operator") or "").strip()
            scope = str(entry.get("工程范围") or ctx.get("scope") or "").strip()
            # 1. 生成免验回执：保留工程范围和原因
            entry["工程范围"] = scope
            entry["免验原因"] = reason
            entry["免验确认人"] = operator
            entry["免验日期"] = date.today().isoformat()
            if not entry.get("验收结论"):
                entry["验收结论"] = "现场获准免验"
            effects.append(dict(entry))

            # 2. 证据已补齐：已完成的工序不再留在待处理
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process and ws_row.get("status") == "已完成":
                    ws_row["pending"] = False
                    effects.append(dict(ws_row))

            # 3. 免验结论同步到报告待审核清单
            self._sync_checklist(
                scope,
                process,
                f"工序「{process}」免验回执：{reason}（确认人：{operator}）",
                effects,
            )

        elif action == "退回整改":
            process = entry.get("验收工序", "")
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
        return effects
