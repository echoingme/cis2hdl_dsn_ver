"""EXP 辅助键名对齐模块测试（L1 键名对齐 / L2 补键修正 / L3 U7 球标）。"""
import pytest

from cis2hdl.core.parser.dsn.exp.exp_dir_assist import (
    EXP_TYPE_PART_PIN,
    load_partandpin,
)

from tests.support import data_path, design_path

EXP_PATH = design_path("EXP")
DSN_PATH = design_path("DSN")


@pytest.mark.parametrize("ref,pin,net", [
    ("R190", "A", "DVDD3V3"),
    ("U7F", "VSS1", "GND"),
    ("JS701", "P1", "TIP"),
    ("J4", "A1", "UART1_RXD"),
])
def test_exp_load_pins(ref, pin, net):
    """EXP 真值加载（part+pin 型核心断言）。"""
    exp, nums, *_ = load_partandpin(EXP_PATH)
    assert exp[ref][pin] == net


def test_align_100_percent():
    """EXP 对齐后 EXP 网名全量对拍 100%（mismatch=0, missing=0）。"""
    from pathlib import Path

    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    ir = DSNParser().parse(Path(DSN_PATH), exp_path=Path(EXP_PATH))
    assert ir.metadata["exp_source"] == EXP_TYPE_PART_PIN
    st = ir.metadata["exp_align"]
    assert st["l1_instances"] > 0
    assert st["l2a_filled"] > 0

    exp, *_ = load_partandpin(EXP_PATH)
    conns = {}
    for pg in ir.pages:
        for inst in pg.instances:
            conns.setdefault(inst.refdes, {}).update(inst.pin_connections)
    u7_sec = {"U7A", "U7B", "U7C", "U7D", "U7E", "U7F"}
    ok = mm = ms = 0
    for ref, pins in exp.items():
        # 2026-08-21：U7A~U7F 六段独立芯片，IR refdes 已按 EXP Part
        # Reference 覆盖 → 直接按 ref 查
        iref = ref
        conn = conns.get(iref, {})
        for pin, net in pins.items():
            if not net:
                ok += 1
                continue
            if pin in conn:
                if conn[pin] == net:
                    ok += 1
                else:
                    mm += 1
            else:
                ms += 1
    assert mm == 0, f"EXP 对齐后仍 {mm} mismatch"
    assert ms == 0, f"EXP 对齐后仍 {ms} missing"


def test_align_js701_mech_pin_removed():
    """L2c：JS701 M1/M2 机械引脚（EXP 空网）误连被删除。"""
    from pathlib import Path

    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    ir = DSNParser().parse(Path(DSN_PATH), exp_path=Path(EXP_PATH))
    js701 = None
    for pg in ir.pages:
        for inst in pg.instances:
            if inst.refdes == "JS701":
                js701 = inst
                break
    assert js701 is not None
    assert "M1" not in js701.pin_connections
    assert "M2" not in js701.pin_connections
    assert js701.pin_connections.get("P1") == "TIP"
    assert js701.pin_connections.get("P2") == "RING"


def test_align_u7_ball_409():
    """L3：U7 pin_labels 球标覆盖 409（C8/F19/T12 补齐）。"""
    from pathlib import Path

    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    ir = DSNParser().parse(Path(DSN_PATH), exp_path=Path(EXP_PATH))
    # 2026-08-21：U7A~U7F 六段独立芯片——球标全量 = 六段并集
    u7s = [inst for pg in ir.pages for inst in pg.instances
           if inst.refdes in ("U7A", "U7B", "U7C", "U7D", "U7E", "U7F")]
    assert len(u7s) == 6
    balls = set()
    for u7 in u7s:
        balls |= set(u7.pin_labels.keys()) | set(u7.pin_labels.values())
    assert "C8" in balls and "F19" in balls and "T12" in balls
    assert len(balls) >= 409
