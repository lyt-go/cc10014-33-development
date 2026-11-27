"""竣工总结免验确认链路测试。

覆盖：
- 免验确认权限（只能由负责该工序的验收人确认）与回执留痕（工程范围+原因）
- 免验结论同步到报告待审核清单
- 同一项目两个修缮阶段：第一阶段凭免验回执提交、第二阶段无任何签署被拦
- 补登记验收后报告恢复资格、断线恢复后可重试（幂等）
- 同一工序免验回执与正式签署资格互斥
"""
from __future__ import annotations

import pytest

from app.store import store
from app.services.work_sequence import WorkSequenceService
from app.services.quality_inspect import QualityInspectService
from app.services.acceptance_record import AcceptanceRecordService
from app.services.safety_briefing import SafetyBriefingService
from app.services.completion_summary import CompletionSummaryService


@pytest.fixture(autouse=True)
def reset_store():
    """每个测试前重置 store 中的 non-seed 状态。"""
    store._idempotent_log.clear()
    store._resource_locks.clear()
    from app.seed import SEED_ROWS
    for name in list(store._tables.keys()):
        if name in SEED_ROWS:
            store._tables[name] = [dict(row) for row in SEED_ROWS[name]]
        else:
            store._tables[name] = []
    yield


work_service = WorkSequenceService()
quality_service = QualityInspectService()
acceptance_service = AcceptanceRecordService()
safety_service = SafetyBriefingService()
completion_service = CompletionSummaryService()

PROJECT = "观音阁修缮"
PHASE1 = "观音阁修缮-第一阶段"
PHASE2 = "观音阁修缮-第二阶段"
INSPECTOR = "监理王五"


# ── 辅助函数 ──
def ensure_entry(service, values):
    entry, missing = service.create_entry(values)
    assert missing == [], f"创建失败缺少字段: {missing}"
    return entry


def sign_all_materials():
    """让材料签认前置检查通过（项目级条件，与本次场景无关）。"""
    for row in store.rows("material_verify"):
        row["status"] = "已签认"
        row["pending"] = False


def make_completed_process(name, scope):
    """走完整流程创建一道已完成工序：安全交底签认 -> 发布 -> 施工 -> 完成。"""
    safety = ensure_entry(safety_service, {
        "交底工序": name, "危险源": "高空坠落", "防护措施": "安全带+安全网",
    })
    safety_service.run_action(safety["id"], "执行交底")
    safety_service.run_action(safety["id"], "签认确认")
    ws = ensure_entry(work_service, {
        "工序名称": name, "前置工序": "无", "所需工匠": "木工",
        "施工区域": "一进院", "工程范围": scope,
    })
    for action in ("发布工序", "开始施工", "完成工序"):
        entry, msg = work_service.run_action(ws["id"], action)
        assert entry is not None, f"{action} 失败: {msg}"
    return ws


def make_report(scope, project=PROJECT):
    return ensure_entry(completion_service, {
        "修缮项目": project,
        "开工日期": "2026-03-01",
        "竣工日期": "2026-10-30",
        "编制人": "项目负责人甲",
        "工程范围": scope,
    })


def make_acceptance(process, scope, inspector=INSPECTOR):
    return ensure_entry(acceptance_service, {
        "验收工序": process,
        "验收标准": "GB50203",
        "验收结论": "合格",
        "验收人": inspector,
        "工程范围": scope,
    })


def confirm_exemption(entry_id, operator=INSPECTOR, reason="隐蔽部位施工全程旁站，现场获准免验"):
    return acceptance_service.run_action(
        entry_id, "确认免验", context={"operator": operator, "reason": reason}
    )


def sign_acceptance_via_late_registration(process):
    """补登记验收：质量检查出具报告 -> 自动登记验收记录 -> 开始验收 -> 签署。"""
    qi = ensure_entry(quality_service, {
        "检查工序": process, "检查项目": "实体质量", "技术标准": "GB50203",
    })
    entry, msg = quality_service.run_action(qi["id"], "开始检查")
    assert entry is not None, msg
    entry, msg = quality_service.run_action(qi["id"], "出具报告")
    assert entry is not None, msg
    records = store.query_by_field("acceptance_record", "验收工序", process)
    assert records, "质量检查通过后应自动补登记验收记录"
    acc_id = records[-1]["id"]
    entry, msg = acceptance_service.run_action(acc_id, "开始验收")
    assert entry is not None, msg
    return acceptance_service.run_action(acc_id, "签署验收")


# ═══════════════════════════════════════════════════════════════════
# 免验确认：权限与回执留痕
# ═══════════════════════════════════════════════════════════════════

def test_exemption_only_confirmable_by_responsible_inspector():
    """只能由有权限负责这道工序的验收人确认免验。"""
    acc = make_acceptance("木构架检修", PHASE1)

    # 未填操作人
    entry, msg = acceptance_service.run_action(
        acc["id"], "确认免验", context={"reason": "x"})
    assert entry is None and "验收人" in msg

    # 非负责验收人
    entry, msg = confirm_exemption(acc["id"], operator="无关人员张三")
    assert entry is None
    assert "只能由" in msg and INSPECTOR in msg and "无权" in msg

    # 负责验收人但缺免验原因
    entry, msg = acceptance_service.run_action(
        acc["id"], "确认免验", context={"operator": INSPECTOR})
    assert entry is None and "免验原因" in msg

    # 负责验收人 + 原因 -> 确认成功
    entry, msg = confirm_exemption(acc["id"])
    assert entry is not None, msg
    assert entry["status"] == "已免验"


def test_exemption_receipt_keeps_scope_and_reason():
    """免验回执应保留工程范围和原因。"""
    acc = make_acceptance("木构架检修", PHASE1)
    reason = "屋面隐蔽部位已全程旁站记录"
    entry, msg = confirm_exemption(acc["id"], reason=reason)
    assert entry is not None, msg
    row = store.find("acceptance_record", acc["id"])
    assert row["工程范围"] == PHASE1
    assert row["免验原因"] == reason
    assert row["免验确认人"] == INSPECTOR
    assert row["免验日期"]


def test_exemption_requires_scope_on_record():
    """验收记录未登记工程范围时不得确认免验。"""
    acc = ensure_entry(acceptance_service, {
        "验收工序": "木构架检修", "验收标准": "GB50203",
        "验收结论": "合格", "验收人": INSPECTOR,
    })
    entry, msg = confirm_exemption(acc["id"])
    assert entry is None and "工程范围" in msg


def test_exemption_rejected_when_no_inspector_assigned():
    """验收记录未指定验收人时，无人有权确认免验。"""
    acc = ensure_entry(acceptance_service, {
        "验收工序": "木构架检修", "验收标准": "GB50203",
        "验收结论": "合格", "验收人": "", "工程范围": PHASE1,
    })
    entry, msg = confirm_exemption(acc["id"])
    assert entry is None and "未指定验收人" in msg


# ═══════════════════════════════════════════════════════════════════
# 免验结论同步到报告待审核清单
# ═══════════════════════════════════════════════════════════════════

def test_exemption_conclusion_syncs_to_report_checklist():
    """免验结论同步到同工程范围、编制中的报告待审核清单；其他范围不同步。"""
    report = make_report(PHASE1)
    other_report = make_report(PHASE2)
    acc = make_acceptance("木构架检修", PHASE1)

    entry, msg = confirm_exemption(acc["id"], reason="隐蔽部位已旁站")
    assert entry is not None, msg

    row = store.find("completion_summary", report["id"])
    checklist = row.get("待审核清单", "")
    assert "木构架检修" in checklist
    assert "免验回执" in checklist
    assert "隐蔽部位已旁站" in checklist
    assert INSPECTOR in checklist
    # 不同工程范围的报告不受影响
    other = store.find("completion_summary", other_report["id"])
    assert not other.get("待审核清单")


def test_checklist_backfilled_when_report_created_after_exemption():
    """先确认免验、后登记报告时，待审核清单同样回填免验结论。"""
    make_completed_process("木构架检修", PHASE1)
    acc = make_acceptance("木构架检修", PHASE1)
    entry, msg = confirm_exemption(acc["id"], reason="隐蔽部位已旁站")
    assert entry is not None, msg

    report = make_report(PHASE1)
    checklist = store.find("completion_summary", report["id"]).get("待审核清单", "")
    assert "木构架检修" in checklist and "免验回执" in checklist


# ═══════════════════════════════════════════════════════════════════
# 第一阶段：确有免验回执 -> 报告可提交，证据说明为免验回执
# ═══════════════════════════════════════════════════════════════════

def test_phase1_report_submits_with_exemption_receipt():
    sign_all_materials()
    make_completed_process("木构架检修", PHASE1)
    acc = make_acceptance("木构架检修", PHASE1)
    entry, msg = confirm_exemption(acc["id"], reason="隐蔽部位已旁站")
    assert entry is not None, msg

    report = make_report(PHASE1)
    entry, msg = completion_service.run_action(report["id"], "提交报告")
    assert entry is not None, f"第一阶段有免验回执，提交应成功: {msg}"
    assert entry["status"] == "已提交"

    row = store.find("completion_summary", report["id"])
    # 报告说明用的是哪种证据：全部免验回执、无正式签署验收行
    assert "免验回执" in row["证据说明"]
    assert "无正式签署验收行" in row["证据说明"]
    assert "免验回执" in row["待审核清单"]

    # 断线恢复后重复提交：幂等返回，不产生重复副作用
    again, msg2 = completion_service.run_action(report["id"], "提交报告")
    assert again is not None and "已执行过" in msg2
    assert store.find("completion_summary", report["id"])["status"] == "已提交"


# ═══════════════════════════════════════════════════════════════════
# 第二阶段：没有任何签署 -> 拦截并留在待处理；补登记验收后恢复资格
# ═══════════════════════════════════════════════════════════════════

def test_phase2_blocked_without_signature_then_eligible_after_late_acceptance():
    sign_all_materials()
    ws = make_completed_process("屋面揭瓦", PHASE2)
    assert store.find("work_sequence", ws["id"])["pending"] is False

    report = make_report(PHASE2)
    # 没有任何签署（也无免验回执）-> 提交被拦，原因列出工序
    entry, msg = completion_service.run_action(report["id"], "提交报告")
    assert entry is None
    assert "屋面揭瓦" in msg
    assert "免验回执" in msg and "待处理" in msg
    # 缺失回执的工序留在待处理，报告仍在编制中
    assert store.find("work_sequence", ws["id"])["pending"] is True
    assert store.find("completion_summary", report["id"])["status"] == "编制中"

    # 现场补登记验收（迟到的正式验收）并签署
    signed, sign_msg = sign_acceptance_via_late_registration("屋面揭瓦")
    assert signed is not None, sign_msg
    assert store.find("work_sequence", ws["id"])["pending"] is False

    # 断线恢复后重试提交：此前失败未记幂等，可重试且成功
    entry, msg = completion_service.run_action(report["id"], "提交报告")
    assert entry is not None, f"补登记验收后报告应恢复资格: {msg}"
    assert entry["status"] == "已提交"

    row = store.find("completion_summary", report["id"])
    # 报告说明用的是正式签署；已签署记录保留在待审核清单供复核
    assert "正式签署" in row["证据说明"]
    assert "正式签署" in row["待审核清单"] and "可复核" in row["待审核清单"]


# ═══════════════════════════════════════════════════════════════════
# 混合证据：报告说明每道工序各用哪种证据
# ═══════════════════════════════════════════════════════════════════

def test_report_states_evidence_type_per_process():
    sign_all_materials()
    scope = "观音阁修缮-油漆阶段"
    make_completed_process("门窗油饰", scope)
    make_completed_process("地仗处理", scope)
    # 一道正式签署
    signed, msg = sign_acceptance_via_late_registration("门窗油饰")
    assert signed is not None, msg
    # 一道确认免验
    acc = make_acceptance("地仗处理", scope)
    entry, msg = confirm_exemption(acc["id"], reason="材料小样已送检备案")
    assert entry is not None, msg

    report = make_report(scope)
    entry, msg = completion_service.run_action(report["id"], "提交报告")
    assert entry is not None, msg
    note = store.find("completion_summary", report["id"])["证据说明"]
    assert "1道以正式签署验收说明" in note
    assert "1道以免验回执说明" in note


# ═══════════════════════════════════════════════════════════════════
# 互斥：同一工序免验回执与正式签署资格互斥
# ═══════════════════════════════════════════════════════════════════

def test_signature_rejected_after_exemption_confirmed():
    """已确认免验的工序，迟到的正式签署不得再生效。"""
    sign_all_materials()
    make_completed_process("木构架检修", PHASE1)
    acc = make_acceptance("木构架检修", PHASE1)
    entry, msg = confirm_exemption(acc["id"])
    assert entry is not None, msg

    # 现场补登记一条验收记录，试图迟到签署
    late = make_acceptance("木构架检修", PHASE1)
    acceptance_service.run_action(late["id"], "开始验收")
    entry, msg = acceptance_service.run_action(late["id"], "签署验收")
    assert entry is None and "互斥" in msg

    # 已免验的记录本身也不能再走签署
    entry, msg = acceptance_service.run_action(acc["id"], "签署验收")
    assert entry is None and "互斥" in msg


def test_exemption_rejected_after_signature_done():
    """已正式签署的工序，不得再确认免验。"""
    sign_all_materials()
    make_completed_process("屋面揭瓦", PHASE2)
    signed, msg = sign_acceptance_via_late_registration("屋面揭瓦")
    assert signed is not None, msg

    # 已签署的记录不能改为免验
    entry, msg = confirm_exemption(signed["id"])
    assert entry is None and "互斥" in msg

    # 同一工序另起一条记录确认免验同样被拦
    another = make_acceptance("屋面揭瓦", PHASE2)
    entry, msg = confirm_exemption(another["id"])
    assert entry is None and "互斥" in msg
