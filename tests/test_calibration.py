"""评级校准与复核资格的业务规则测试。"""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from inspect_inputs import load_tables
import calibration

DATA = Path(__file__).resolve().parents[1] / "data"

COLUMNS = {
    "defects": ["defect", "building", "part", "category", "opened", "fixed", "verified_urgent"],
    "ratings": ["report", "defect", "inspector", "date", "evidence", "grade", "reason", "blind"],
    "inspector_roles": ["inspector", "part", "max_grade", "expert", "available_from"],
    "review_jobs": ["job", "defect", "inspector", "state", "opened", "deadline"],
}
REFERENCES = [
    ["ratings", "defect", "defects", "defect"],
    ["ratings", "inspector", "inspector_roles", "inspector"],
    ["review_jobs", "defect", "defects", "defect"],
    ["review_jobs", "inspector", "inspector_roles", "inspector"],
]
POLICY = {
    "等级顺序": ["routine", "soon", "urgent"],
    "原始升级证据": "部位证据 verified_urgent 为独立升级依据",
    "盲评字段": "blind 表示评级时评员不知道原评级；代表性仅由抽核原因与范围决定。",
    "复核资格": "部位与等级都匹配，且复核员不能是所在对象的初评员",
}


def build_dataset(root, rows):
    """在临时目录生成一份可通过 load_tables 校验的最小数据集。"""
    folder = Path(root) / "data"
    folder.mkdir()
    (folder / "schema.json").write_text(
        json.dumps({"tables": COLUMNS, "references": REFERENCES}), encoding="utf-8"
    )
    (folder / "policy.json").write_text(json.dumps(POLICY), encoding="utf-8")
    for name, records in rows.items():
        with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS[name])
            writer.writeheader()
            writer.writerows(records)
    return folder


def role(inspector, part, max_grade="urgent", expert="no", available="2026-01-01"):
    return {
        "inspector": inspector, "part": part, "max_grade": max_grade,
        "expert": expert, "available_from": available,
    }


def rating(report, defect, inspector, evidence, grade, reason, blind="no", date="2026-02-01"):
    return {
        "report": report, "defect": defect, "inspector": inspector, "date": date,
        "evidence": evidence, "grade": grade, "reason": reason, "blind": blind,
    }


def defect(name, part, urgent="no", fixed=""):
    return {
        "defect": name, "building": "B", "part": part, "category": "c",
        "opened": "2026-02-01", "fixed": fixed, "verified_urgent": urgent,
    }


class RealDataCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables(DATA)

    def test_evidence_groups_split_three_kinds(self):
        groups = {g["evidence"]: g for g in calibration.evidence_groups(self.tables)}
        # 6 个共同对象、2 个单评员证据、1 个维修后复评
        kinds = {}
        for group in groups.values():
            kinds.setdefault(group["kind"], []).append(group["evidence"])
        self.assertEqual(
            sorted(kinds["shared"]),
            ["F01", "F04", "F06", "F08", "F11", "F13"],
        )
        self.assertEqual(sorted(kinds["solo"]), ["F10", "F15"])
        self.assertEqual(kinds["followup"], ["F03"])

    def test_followup_is_never_a_shared_object(self):
        # d01 的 F03 是维修后复评，不能因为又多了一条评级就变成共同对象
        groups = [g for g in calibration.evidence_groups(self.tables) if g["defect"] == "d01"]
        self.assertEqual(
            {g["evidence"]: g["kind"] for g in groups},
            {"F01": "shared", "F03": "followup"},
        )

    def test_wall_is_anchored_and_only_adjacent_confusion(self):
        info = calibration.part_calibration(self.tables)["wall"]
        self.assertTrue(info["anchored"])
        self.assertEqual(info["experts"], ["X1", "X2"])
        self.assertEqual(info["pair_stats"]["wide"], 0)
        self.assertGreater(info["pair_stats"]["adjacent"], 0)
        # 所有评员与专家都是直接共评（距离 1），不存在只间接相连的评员
        self.assertTrue(all(
            distance in (0, 1) for distance in info["expert_distance"].values()
        ))

    def test_rater_bias_only_compared_with_expert_anchor(self):
        # F06 上 I2 与专家 X2 同为 urgent，偏级差必须为 0，不被 I3 污染
        bias = calibration.part_calibration(self.tables)["wall"]["rater_bias_vs_expert"]
        self.assertEqual(bias["I2"], 0.0)
        self.assertEqual(bias["I3"], -1.0)

    def test_drain_has_no_anchor_and_stays_uncalibrated(self):
        info = calibration.part_calibration(self.tables)["drain"]
        self.assertFalse(info["anchored"])
        self.assertTrue(all(distance is None for distance in info["expert_distance"].values()))
        for verdict in calibration.defect_verdicts(self.tables):
            if verdict["part"] == "drain":
                self.assertEqual(verdict["calibration"], "uncalibrated")
                self.assertIn("保留原评", verdict["note"])

    def test_wall_calibration_does_not_spill_into_drain(self):
        # 新增/已有专家盲评只覆盖 wall，drain 不得被顺带认定已校准
        verdicts = {v["defect"]: v["calibration"]
                    for v in calibration.defect_verdicts(self.tables)}
        self.assertEqual(verdicts["d05"], "uncalibrated")
        self.assertEqual(verdicts["d08"], "uncalibrated")
        self.assertEqual(verdicts["d03"], "calibrated")

    def test_verified_urgent_escalation_survives_calibration(self):
        audit = {a["defect"]: a for a in calibration.review_audit(self.tables)}["d03"]
        self.assertTrue(any("verified_urgent" in reason for reason in audit["escalation"]))
        # X2 专家盲评 urgent 可作撤争证据；I3 资格不足不能签 urgent 复核
        self.assertIn("X2", " ".join(audit["withdrawal_evidence"]["existing_blind"]))
        self.assertTrue(any(
            "I3" in entry for entry in audit["withdrawal_evidence"]["party_self_ratings"]
        ))

    def test_initial_inspector_cannot_review_own_building(self):
        # d05 初评人是 I3，合格复核员里不得出现 I3
        audit = {a["defect"]: a for a in calibration.review_audit(self.tables)}["d05"]
        self.assertEqual(
            [e["inspector"] for e in audit["eligible_reviewers"]], ["I2"]
        )

    def test_grade_cap_disqualifies_reviewer(self):
        # d03 需 urgent 复核：I3 的 wall max_grade=soon，必须被排除
        audit = {a["defect"]: a for a in calibration.review_audit(self.tables)}["d03"]
        self.assertNotIn("I3", [e["inspector"] for e in audit["eligible_reviewers"]])

    def test_unassigned_job_flagged(self):
        audit = {a["defect"]: a for a in calibration.review_audit(self.tables)}["d05"]
        check = next(check for check in audit["jobs"] if check["job"] == "j3")
        self.assertFalse(check["ok"])
        self.assertIn("未派单", check["issue"])

    def test_sampling_priority_puts_urgent_uncalibrated_first(self):
        gaps = calibration.sampling_gaps(self.tables)
        self.assertEqual([g["defect"] for g in gaps], ["d05", "d08", "d02"])
        # 已校准的 wall 片区不得进入补样清单
        self.assertTrue(all(g["part"] == "drain" for g in gaps))


class SyntheticRuleTests(unittest.TestCase):
    def _load(self, rows):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return load_tables(build_dataset(tmp, rows))

    def test_indirect_network_link_is_not_treated_as_comparable(self):
        # A-X 共评、X-B 共评，但 A、B 从未在同一 evidence 上出现
        rows = {
            "defects": [defect("d1", "wall"), defect("d2", "wall")],
            "inspector_roles": [
                role("A", "wall"), role("B", "wall"), role("X", "wall", expert="yes"),
            ],
            "ratings": [
                rating("r1", "d1", "A", "E1", "routine", "initial"),
                rating("r2", "d1", "X", "E1", "routine", "random", "yes"),
                rating("r3", "d2", "X", "E2", "soon", "initial"),
                rating("r4", "d2", "B", "E2", "soon", "random", "yes"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        info = calibration.part_calibration(tables)["wall"]
        self.assertEqual(info["expert_distance"], {"A": 1, "B": 1, "X": 0})
        self.assertEqual(info["edges"], [("A", "X"), ("B", "X")])
        # A 与 B 没有直接共评边：网络相邻不等于共同对象
        self.assertNotIn(("A", "B"), info["edges"])

    def test_indirect_rater_keeps_original_grade_with_uncertainty(self):
        # 部位虽有专家锚点，但 d3 的共同对象上 B 与 C 都未与专家共评，只能间接相连
        rows = {
            "defects": [
                defect("d1", "wall"), defect("d2", "wall"), defect("d3", "wall"),
            ],
            "inspector_roles": [
                role("A", "wall"), role("B", "wall"),
                role("C", "wall"), role("X", "wall", expert="yes"),
            ],
            "ratings": [
                rating("r1", "d1", "A", "E1", "routine", "initial"),
                rating("r2", "d1", "X", "E1", "routine", "random", "yes"),
                rating("r3", "d2", "A", "E2", "soon", "initial"),
                rating("r4", "d2", "B", "E2", "soon", "random", "yes"),
                rating("r5", "d3", "B", "E3", "urgent", "initial"),
                rating("r6", "d3", "C", "E3", "urgent", "random", "yes"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        verdicts = {v["defect"]: v for v in calibration.defect_verdicts(tables)}
        self.assertEqual(verdicts["d1"]["calibration"], "calibrated")
        self.assertEqual(verdicts["d3"]["calibration"], "indirect_only")
        self.assertEqual(verdicts["d3"]["initial_grade"], "urgent")

    def test_part_specific_stability_does_not_transfer(self):
        # 评员 P 在 wall 上有专家锚定，但 drain 上只有单评员记录
        rows = {
            "defects": [defect("d1", "wall"), defect("d2", "drain")],
            "inspector_roles": [
                role("P", "wall"), role("P", "drain"), role("X", "wall", expert="yes"),
            ],
            "ratings": [
                rating("r1", "d1", "P", "E1", "routine", "initial"),
                rating("r2", "d1", "X", "E1", "routine", "random", "yes"),
                rating("r3", "d2", "P", "E2", "urgent", "initial"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        verdicts = {v["defect"]: v for v in calibration.defect_verdicts(tables)}
        self.assertEqual(verdicts["d1"]["calibration"], "calibrated")
        self.assertEqual(verdicts["d2"]["calibration"], "uncalibrated")

    def test_followup_cannot_withdraw_earlier_dispute(self):
        rows = {
            "defects": [defect("d1", "wall", fixed="2026-02-10")],
            "inspector_roles": [
                role("A", "wall"), role("B", "wall"), role("C", "wall"),
            ],
            "ratings": [
                rating("r1", "d1", "A", "E1", "soon", "initial"),
                rating("r2", "d1", "B", "E1", "urgent", "dispute", "yes"),
                rating("r3", "d1", "C", "E9", "routine", "followup"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        audit = calibration.review_audit(tables)[0]
        self.assertEqual(audit["withdrawal_evidence"]["existing_blind"], [])
        self.assertTrue(audit["withdrawal_evidence"]["followup_notes"])
        # B 是争议方本人，其盲评不能自行撤争
        self.assertTrue(any(
            "B" in entry for entry in audit["withdrawal_evidence"]["party_self_ratings"]
        ))

    def test_third_party_expert_blind_rating_can_withdraw(self):
        rows = {
            "defects": [defect("d1", "wall")],
            "inspector_roles": [
                role("A", "wall"),
                role("B", "wall"),
                role("X", "wall", expert="yes"),
            ],
            "ratings": [
                rating("r1", "d1", "A", "E1", "soon", "initial"),
                rating("r2", "d1", "B", "E1", "urgent", "dispute", "yes"),
                rating("r3", "d1", "X", "E1", "urgent", "random", "yes"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        audit = calibration.review_audit(tables)[0]
        self.assertEqual(len(audit["withdrawal_evidence"]["existing_blind"]), 1)
        self.assertIn("X", audit["withdrawal_evidence"]["existing_blind"][0])
        self.assertTrue(any(
            "B" in entry for entry in audit["withdrawal_evidence"]["party_self_ratings"]
        ))
        self.assertEqual(audit["withdrawal_evidence"]["missing_qualification"], "")

    def test_review_job_by_initial_inspector_is_illegal(self):
        rows = {
            "defects": [defect("d1", "wall")],
            "inspector_roles": [role("A", "wall"), role("B", "wall")],
            "ratings": [rating("r1", "d1", "A", "E1", "soon", "initial")],
            "review_jobs": [{
                "job": "j1", "defect": "d1", "inspector": "A", "state": "assigned",
                "opened": "2026-02-02", "deadline": "2026-02-10",
            }],
        }
        tables = self._load(rows)
        check = calibration.review_audit(tables)[0]["jobs"][0]
        self.assertFalse(check["ok"])
        self.assertIn("初评员", check["issue"])

    def test_same_evidence_repeat_scoring_is_one_object_not_many(self):
        rows = {
            "defects": [defect("d1", "wall")],
            "inspector_roles": [role("A", "wall"), role("B", "wall")],
            "ratings": [
                rating("r1", "d1", "A", "E1", "soon", "initial"),
                rating("r2", "d1", "B", "E1", "urgent", "random", "yes"),
            ],
            "review_jobs": [],
        }
        tables = self._load(rows)
        shared = [g for g in calibration.evidence_groups(tables) if g["kind"] == "shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["evidence"], "E1")


if __name__ == "__main__":
    unittest.main()
