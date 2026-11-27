"""竣工总结领域服务：汇总工序后编制竣工报告。

提交报告时按「工程范围」核对每道工序的验收证据：正式签署或免验回执，
两者都没有的工序留在待处理并阻止提交；提交成功后在报告上写明证据
说明（哪些工序用签署、哪些用免验回执），并重建待审核清单供复核。
未填写工程范围的报告保持旧行为：按全部工序核对。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CompletionSummaryService(BaseModuleService):
    MODULE = "completion_summary"
    ENTITY = "竣工报告"
    OPERATION_TERM = "提交报告"
    LIST_FIELDS = ["修缮项目", "开工日期", "竣工日期", "实际工期", "质量评定", "编制人", "报告版本", "工程范围", "备注说明"]
    REQUIRED_FIELDS = ["修缮项目", "开工日期", "竣工日期"]
    STATUS_ORDER = ["编制中", "已提交", "已备案", "待补充"]
    ACTION_RULES = {"提交报告": "已提交", "执行备案": "已备案", "退回补充": "待补充"}
    NEGATIVE_ACTIONS = []

    # ── 工程范围与证据 ──
    @staticmethod
    def _scope_of(entry: dict[str, Any]) -> str:
        return str(entry.get("工程范围") or "").strip()

    def _scoped_procedures(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """报告工程范围内的工序；未填写工程范围时按全部工序（旧行为）。"""
        scope = self._scope_of(entry)
        rows = store.rows("work_sequence")
        if not scope:
            return list(rows)
        return [r for r in rows if str(r.get("工程范围") or "") == scope]

    @staticmethod
    def _evidence_for(process: str, scope: str) -> tuple[dict | None, dict | None]:
        """返回工序的 (正式签署记录, 免验回执)；两种证据资格互斥，最多各一条。"""
        signed = exempt = None
        for row in store.rows("acceptance_record"):
            if row.get("验收工序", "") != process:
                continue
            if scope and str(row.get("工程范围") or "") != scope:
                continue
            if row.get("status") == "已签署" and signed is None:
                signed = row
            elif row.get("status") == "已免验" and exempt is None:
                exempt = row
        return signed, exempt

    def _evidence_breakdown(
        self, entry: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        """按工程范围汇总每道工序的证据：签署名单、免验名单、待审核清单条目。"""
        scope = self._scope_of(entry)
        signed_names: list[str] = []
        exempt_names: list[str] = []
        checklist: list[str] = []
        for proc in self._scoped_procedures(entry):
            name = proc.get("工序名称", "")
            signed, exempt = self._evidence_for(name, scope)
            if signed is not None:
                signed_names.append(name)
                checklist.append(
                    f"工序「{name}」正式签署（验收人：{signed.get('验收人', '')}），可复核"
                )
            elif exempt is not None:
                exempt_names.append(name)
                checklist.append(
                    f"工序「{name}」免验回执：{exempt.get('免验原因', '')}"
                    f"（确认人：{exempt.get('免验确认人', '')}）"
                )
        return signed_names, exempt_names, checklist

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 回填同工程范围已有的验收/免验结论，待审核清单与登记先后无关
            _, _, checklist = self._evidence_breakdown(entry)
            if checklist:
                entry["待审核清单"] = "；".join(checklist)
        return entry, missing

    def cross_module_verify(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if action == "提交报告":
            scope = self._scope_of(entry)
            procedures = self._scoped_procedures(entry)
            if scope and not procedures:
                return False, f"工程范围「{scope}」内没有工序，无法编制报告"
            # 1. 范围内工序必须完成
            incomplete = [r for r in procedures if r.get("status") != "已完成"]
            if incomplete:
                names = [r.get("工序名称", "") for r in incomplete[:3]]
                return False, f"尚有{len(incomplete)}道工序未完成，如：{'、'.join(names)}"
            # 2. 所有材料必须签认
            unsigned_materials = [
                r for r in store.rows("material_verify") if r.get("status") not in ("已签认",)
            ]
            if unsigned_materials:
                return False, f"尚有{len(unsigned_materials)}组材料未签认"
            # 3. 范围内每道工序必须有验收证据：正式签署或免验回执
            missing = []
            for proc in procedures:
                name = proc.get("工序名称", "")
                signed, exempt = self._evidence_for(name, scope)
                if signed is None and exempt is None:
                    missing.append(name)
                    # 缺失回执的工序留在待处理
                    proc["pending"] = True
            if missing:
                return False, (
                    f"工程范围「{scope or '全部'}」内{len(missing)}道工序既无签署验收也无免验回执："
                    f"{'、'.join(missing[:3])}，已保留在待处理"
                )
        if action == "执行备案":
            # 备案前所有档案必须入库
            unarchived = [
                r for r in store.rows("archive_mgmt") if r.get("status") != "已入库"
            ]
            if unarchived and len(unarchived) > len(store.rows("archive_mgmt")) * 0.3:
                return False, f"尚有{len(unarchived)}份档案未入库"
        return True, ""

    def cross_module_effect(
        self,
        entry_id: int,
        action: str,
        entry: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        effects = []
        if action == "提交报告":
            scope = self._scope_of(entry)
            procedures = self._scoped_procedures(entry)
            signed_names, exempt_names, checklist = self._evidence_breakdown(entry)
            # 证据说明：报告用的是哪种证据
            total = len(procedures)
            label = scope or "全部"
            if exempt_names and not signed_names:
                note = f"工程范围「{label}」{total}道工序全部以免验回执说明，无正式签署验收行"
            elif signed_names and not exempt_names:
                note = f"工程范围「{label}」{total}道工序全部以正式签署验收说明"
            else:
                note = (
                    f"工程范围「{label}」{total}道工序："
                    f"{len(signed_names)}道以正式签署验收说明，{len(exempt_names)}道以免验回执说明"
                )
            entry["证据说明"] = note
            entry["待审核清单"] = "；".join(checklist)
            effects.append(dict(entry))
        if action == "执行备案":
            # 备案后锁定所有关联记录
            for ws_row in store.rows("work_sequence"):
                ws_row["已备案锁定"] = True
                effects.append(dict(ws_row))
            for acc in store.rows("acceptance_record"):
                acc["已备案锁定"] = True
                effects.append(dict(acc))
            for log in store.rows("restoration_log"):
                log["已备案锁定"] = True
                effects.append(dict(log))
        return effects
