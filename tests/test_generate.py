"""Phase 1 acceptance: reproducible generation, defect-class floor, pure-integer
money columns, and a guard against reading holdout ground truth outside eval/.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from ledgerloop.config import load_config
from ledgerloop.generate.defects import ALL_DEFECT_CLASSES
from ledgerloop.generate.generator import Generator, write_dataset


@pytest.fixture(scope="module")
def config():
    return load_config()


def test_generation_is_deterministic(tmp_path, config):
    seed = config["generate"]["dev_seed"]
    ds1 = Generator(seed, config).generate()
    ds2 = Generator(seed, config).generate()

    out1, out2 = tmp_path / "a", tmp_path / "b"
    write_dataset(ds1, out1, seed=seed, config=config)
    write_dataset(ds2, out2, seed=seed, config=config)

    names = [
        "gateway_transactions.csv",
        "settlement_report.csv",
        "bank_statement.csv",
        "erp_ledger.csv",
        "answer_key.json",
        "manifest.json",
    ]
    for name in names:
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), name


def test_every_defect_class_meets_floor_in_dev(config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    floor = config["generate"]["min_instances_per_defect"]
    for defect in ALL_DEFECT_CLASSES:
        assert ds.defect_counts.get(defect.value, 0) >= floor, (
            f"{defect.value} appeared {ds.defect_counts.get(defect.value, 0)} times, need >= {floor}"
        )


def test_every_defect_class_meets_floor_in_holdout(config):
    seed = config["generate"]["holdout_seed"]
    ds = Generator(seed, config).generate()
    floor = config["generate"]["min_instances_per_defect"]
    for defect in ALL_DEFECT_CLASSES:
        assert ds.defect_counts.get(defect.value, 0) >= floor, defect.value


def test_gateway_transaction_volume_matches_config(config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    target = config["generate"]["n_gateway_transactions"]
    # DUPLICATE decoys land on top of the target; allow modest slack for those.
    assert target <= len(ds.gateway_transactions) <= target + 200


AMOUNT_COLUMNS = {
    "gross_amount_paise",
    "fee_paise",
    "gst_on_fee_paise",
    "tds_paise",
    "refund_paise",
    "chargeback_paise",
    "net_paise",
    "credit_amount_paise",
    "expected_amount_paise",
}


def test_csv_amount_columns_are_pure_integers(tmp_path, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    out = tmp_path / "dev"
    write_dataset(ds, out, seed=seed, config=config)

    checked_any = False
    for csv_path in out.glob("*.csv"):
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for col, value in row.items():
                    if col in AMOUNT_COLUMNS:
                        checked_any = True
                        assert re.fullmatch(r"-?\d+", value), (
                            f"{csv_path.name}:{col}={value!r} is not a pure integer"
                        )
    assert checked_any, "expected at least one amount column to be checked"


NON_HOLDOUT_EXEMPT_DIRS = {"eval", "generate"}


def test_holdout_answer_key_not_referenced_outside_eval():
    """Static guard for the honesty rule in data/README.md: only eval/harness.py may
    read holdout ground truth. generate/ is exempt -- it writes both profiles
    symmetrically and never inspects answer_key content.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "ledgerloop"
    offenders = []
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(src_root)
        if rel.parts and rel.parts[0] in NON_HOLDOUT_EXEMPT_DIRS:
            continue
        text = path.read_text(encoding="utf-8")
        if "holdout" in text and "answer_key" in text:
            offenders.append(str(rel))
    assert not offenders, f"holdout ground truth referenced outside eval/: {offenders}"
