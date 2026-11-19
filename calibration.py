"""评级校准与限期复核分析。

只使用 Python 标准库。输入表由 ``inspect_inputs.load_tables`` 校验后载入。

分析口径（与 data/policy.json 及交接说明一致）：

* 同一 evidence 上、两名及以上评员的非 followup 评级才是“共同对象”，可用于
  识别评员差异；维修后 followup 评级看到的是维修后的状态，不是共同对象。
* 同一 evidence 只有一名评员的记录只能说明该对象当时的状态，不能当作校准证据；
  同一张照片的重复评分也不会变成多栋楼的证据（分组键是 evidence）。
* 校准按部位（part）分别进行：某部位有专家在共同对象上出现才算有专家锚点；
  评级网络中只间接相连的评员不自动可比，用到专家的最短网络距离标注。
* 无法校准的部位/对象保留原评并附不确定说明，不做全体平均映射。
* 复核资格要求部位与等级都匹配，且复核员不能是该对象的初评员。
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

# 隔离模式（python -I）下脚本目录不会自动进入 sys.path，显式补上以便同库导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_inputs import load_tables

FOLLOWUP = "followup"
DATA_FOLDER = Path(__file__).resolve().parent / "data"


# ---------------------------------------------------------------- 基础工具
def grade_order(folder=DATA_FOLDER):
    """返回 policy.json 中的等级顺序及等级到序号的映射。"""
    policy = json.loads((folder / "policy.json").read_text(encoding="utf-8"))
    order = policy["等级顺序"]
    return order, {grade: index for index, grade in enumerate(order)}


def _grade_index(tables):
    # main() 会把自定义数据目录的等级映射挂到表上；未挂载时用仓库默认配置
    return tables.get("_grade_index", grade_order()[1])


def role_map(tables):
    """(inspector, part) -> 角色记录；布尔字段转成 bool。"""
    roles = {}
    for row in tables["inspector_roles"]:
        roles[(row["inspector"], row["part"])] = {
            "max_grade": row["max_grade"],
            "expert": row["expert"] == "yes",
            "available_from": row["available_from"],
        }
    return roles


def defect_index(tables):
    return {row["defect"]: row for row in tables["defects"]}


# ------------------------------------------------ 证据分组：三类记录分清
def evidence_groups(tables):
    """按 evidence 分组评级记录，并判定每组性质。

    返回列表，每组含：
      kind: "shared"  共同对象（同 evidence 多名评员、非 followup）
            "solo"    单评员证据，只能说明该对象当前状态
            "followup" 维修后复评，不能与维修前评级当作同对象比较
      defect/part: 该证据所属对象与部位
      inspectors/grades/rows: 组内明细
    """
    defects = defect_index(tables)
    grouped = defaultdict(list)
    for row in tables["ratings"]:
        grouped[row["evidence"]].append(row)

    result = []
    for evidence, rows in sorted(grouped.items()):
        defect = rows[0]["defect"]
        non_followup = [row for row in rows if row["reason"] != FOLLOWUP]
        # 保留评员在该证据上的出现顺序（数据按日期排列），用于识别原始争议双方
        ordered_inspectors = list(dict.fromkeys(row["inspector"] for row in non_followup))
        if not non_followup:
            kind = FOLLOWUP
        elif len(ordered_inspectors) >= 2:
            kind = "shared"
        else:
            kind = "solo"
        result.append(
            {
                "evidence": evidence,
                "kind": kind,
                "defect": defect,
                "part": defects[defect]["part"],
                "inspectors": ordered_inspectors,
                "grades": sorted({row["grade"] for row in non_followup}),
                "reasons": sorted({row["reason"] for row in non_followup}),
                "rows": rows,
            }
        )
    return result


# ------------------------------------------------ 部位级网络与一致性
def _bfs_distance(adjacency, experts):
    """网络中每个评员到最近专家的最短距离；不可达为 None。"""
    distances = {}
    frontier = []
    for expert in experts:
        if expert not in distances:
            distances[expert] = 0
            frontier.append(expert)
    while frontier:
        current = frontier.pop(0)
        for neighbor in adjacency.get(current, ()):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                frontier.append(neighbor)
    return distances


def part_calibration(tables):
    """按部位汇总校准信息：网络连通、专家锚点、相邻混淆、评员倾向。"""
    index = _grade_index(tables)
    roles = role_map(tables)
    groups = evidence_groups(tables)

    parts = defaultdict(lambda: {
        "shared_groups": [],
        "solo_evidence": [],
        "followup_evidence": [],
        "edges": set(),
        "raters": set(),
    })
    for group in groups:
        bucket = parts[group["part"]]
        if group["kind"] == "shared":
            bucket["shared_groups"].append(group)
        elif group["kind"] == "solo":
            bucket["solo_evidence"].append(group["evidence"])
        else:
            bucket["followup_evidence"].append(group["evidence"])
        for row in group["rows"]:
            if row["reason"] != FOLLOWUP:
                bucket["raters"].add(row["inspector"])
        if group["kind"] == "shared":
            inspectors = group["inspectors"]
            for i, left in enumerate(inspectors):
                for right in inspectors[i + 1:]:
                    bucket["edges"].add(frozenset((left, right)))

    calibration = {}
    for part, bucket in sorted(parts.items()):
        adjacency = defaultdict(set)
        for edge in bucket["edges"]:
            left, right = tuple(edge)
            adjacency[left].add(right)
            adjacency[right].add(left)
        for rater in bucket["raters"]:
            adjacency.setdefault(rater, set())

        experts = sorted(
            rater
            for rater in bucket["raters"]
            if roles.get((rater, part), {}).get("expert")
        )
        distance = _bfs_distance(adjacency, experts)

        # 共同对象上的成对一致性：完全一致 / 相邻等级混淆 / 跨级混淆
        pair_stats = {"agree": 0, "adjacent": 0, "wide": 0}
        confusion = defaultdict(int)
        rater_bias = defaultdict(list)
        reasons = defaultdict(int)
        for group in bucket["shared_groups"]:
            rows = [row for row in group["rows"] if row["reason"] != FOLLOWUP]
            for reason in group["reasons"]:
                reasons[reason] += 1
            for i, left in enumerate(rows):
                for right in rows[i + 1:]:
                    gap = abs(index[left["grade"]] - index[right["grade"]])
                    if gap == 0:
                        pair_stats["agree"] += 1
                    elif gap == 1:
                        pair_stats["adjacent"] += 1
                    else:
                        pair_stats["wide"] += 1
                    if gap:
                        key = tuple(sorted((left["grade"], right["grade"])))
                        confusion[key] += 1
            # 以同组专家评级为锚点，记录非专家相对专家的带符号级差
            expert_rows = [row for row in rows if roles.get(
                (row["inspector"], part), {}).get("expert")]
            for row in rows:
                if roles.get((row["inspector"], part), {}).get("expert"):
                    continue
                for anchor in expert_rows:
                    rater_bias[row["inspector"]].append(
                        index[row["grade"]] - index[anchor["grade"]]
                    )
        anchored = bool(experts) and bool(bucket["shared_groups"])
        calibration[part] = {
            "anchored": anchored,
            "experts": experts,
            "raters": sorted(bucket["raters"]),
            "edges": sorted(tuple(sorted(edge)) for edge in bucket["edges"]),
            "expert_distance": {rater: distance.get(rater) for rater in sorted(bucket["raters"])},
            "shared_evidence": [g["evidence"] for g in bucket["shared_groups"]],
            "solo_evidence": bucket["solo_evidence"],
            "followup_evidence": bucket["followup_evidence"],
            "shared_reasons": dict(sorted(reasons.items())),
            "pair_stats": pair_stats,
            "adjacent_confusion": {
                "+".join(pair): count for pair, count in sorted(confusion.items())
            },
            "rater_bias_vs_expert": {
                rater: round(sum(values) / len(values), 3)
                for rater, values in sorted(rater_bias.items())
            },
        }
    return calibration


# ------------------------------------------------ 单栋缺陷的校准结论
def initial_rating(tables, defect):
    for row in tables["ratings"]:
        if row["defect"] == defect and row["reason"] == "initial":
            return row
    return None


def defect_verdicts(tables):
    """每个缺陷保留原评，并标注可否校准、混淆范围与独立升级证据。"""
    index = _grade_index(tables)
    calibration = part_calibration(tables)
    groups = {group["evidence"]: group for group in evidence_groups(tables)}

    verdicts = []
    for defect_row in tables["defects"]:
        defect = defect_row["defect"]
        part = defect_row["part"]
        initial = initial_rating(tables, defect)
        part_info = calibration[part]

        related = [group for group in groups.values() if group["defect"] == defect]
        shared = [group for group in related if group["kind"] == "shared"]
        solo = [group for group in related if group["kind"] == "solo"]
        followup = [group for group in related if group["kind"] == "followup"]

        if not part_info["anchored"] or not shared:
            if not shared:
                status = "uncalibrated"
                note = "该对象无共同样本，仅有单评员证据，保留原评并附不确定说明"
            else:
                status = "uncalibrated"
                note = (
                    "部位无专家锚点，保留原评并附不确定说明，不做平均映射"
                )
        else:
            distances = part_info["expert_distance"]
            direct = all(
                distances.get(rater) == 1
                for group in shared
                for rater in group["inspectors"]
                if distances.get(rater) != 0
            ) and all(
                distances.get(rater) is not None
                for group in shared
                for rater in group["inspectors"]
            )
            grades = sorted({grade for group in shared for grade in group["grades"]},
                            key=lambda grade: index[grade])
            if direct:
                status = "calibrated"
                note = (
                    "部位有专家锚点且评员与专家直接共评；"
                    + ("共同对象存在相邻等级混淆，区间 " + "~".join(grades)
                       if len(grades) > 1 else "共同对象评级一致")
                )
            else:
                status = "indirect_only"
                note = ("共同对象上的评员仅经网络间接连到专家，可比性不足，"
                        "保留原评并标注不确定")

        verdicts.append({
            "defect": defect,
            "building": defect_row["building"],
            "part": part,
            "category": defect_row["category"],
            "initial_grade": initial["grade"] if initial else None,
            "initial_inspector": initial["inspector"] if initial else None,
            "verified_urgent": defect_row["verified_urgent"] == "yes",
            "fixed": defect_row["fixed"] or None,
            "shared_evidence": [g["evidence"] for g in shared],
            "solo_evidence": [g["evidence"] for g in solo],
            "followup_evidence": [g["evidence"] for g in followup],
            "calibration": status,
            "grade_span": sorted(
                {grade for group in shared for grade in group["grades"]},
                key=lambda grade: index[grade],
            ),
            "note": note,
        })
    return verdicts


# ------------------------------------------------ 复核资格与工单审查
def eligible_reviewers(tables, defect, required_grade):
    """返回对某缺陷、某等级有复核资格的评员（部位+等级匹配、非初评员）。"""
    index = _grade_index(tables)
    roles = role_map(tables)
    part = defect_index(tables)[defect]["part"]
    initial = initial_rating(tables, defect)
    initial_inspector = initial["inspector"] if initial else None
    parties = {
        row["inspector"]
        for row in tables["ratings"]
        if row["defect"] == defect and row["reason"] != FOLLOWUP
    }

    eligible = []
    for (inspector, role_part), role in sorted(roles.items()):
        if role_part != part:
            continue
        if inspector == initial_inspector:
            continue
        if index[role["max_grade"]] < index[required_grade]:
            continue
        eligible.append({
            "inspector": inspector,
            "expert": role["expert"],
            "max_grade": role["max_grade"],
            "available_from": role["available_from"],
            "rated_this_defect": inspector in parties,
        })
    return eligible


def review_audit(tables):
    """待复核件清单：升级原因、撤争证据、资格缺口、工单合规性。"""
    index = _grade_index(tables)
    verdicts = {item["defect"]: item for item in defect_verdicts(tables)}
    jobs_by_defect = defaultdict(list)
    for job in tables["review_jobs"]:
        jobs_by_defect[job["defect"]].append(job)

    audits = []
    for defect, verdict in verdicts.items():
        initial = initial_rating(tables, defect)
        required = "urgent" if verdict["verified_urgent"] or (
            initial and initial["grade"] == "urgent") else initial["grade"]
        eligible = eligible_reviewers(tables, defect, required)

        # 升级原因：独立部位证据优先，其次原评/争议
        reasons = []
        if verdict["verified_urgent"]:
            reasons.append("verified_urgent 独立部位证据升级，校准结果不能撤回")
        if initial and initial["grade"] == "urgent":
            reasons.append("初评 urgent")
        disputes = [
            row for row in tables["ratings"]
            if row["defect"] == defect and row["reason"] == "dispute"
        ]
        if disputes:
            reasons.append(
                "存在争议盲评："
                + "；".join(f"{row['evidence']} {row['inspector']}={row['grade']}"
                           for row in disputes)
            )

        # 现有工单的合规检查
        job_checks = []
        for job in jobs_by_defect.get(defect, []):
            if not job["inspector"]:
                job_checks.append({
                    "job": job["job"], "state": job["state"],
                    "issue": "未派单", "ok": False,
                })
                continue
            issues, warnings = [], []
            role = role_map(tables).get((job["inspector"], verdict["part"]))
            if role is None:
                issues.append("复核员无该部位角色")
            else:
                if index[role["max_grade"]] < index[required]:
                    issues.append(f"等级资格不足（需 {required}）")
                if role["available_from"] > job["deadline"]:
                    issues.append(
                        f"可用日期 {role['available_from']} 晚于工单截止 {job['deadline']}"
                    )
                elif role["available_from"] > job["opened"]:
                    warnings.append(
                        f"派单时（{job['opened']}）复核员尚未可用"
                        f"（{role['available_from']}），窗口紧但不违规"
                    )
            if job["inspector"] == verdict["initial_inspector"]:
                issues.append("复核员即初评员，禁止同栋签复评")
            text = "；".join(issues) if issues else "资格与回避均满足"
            if warnings:
                text += "；提示：" + "；".join(warnings)
            job_checks.append({
                "job": job["job"], "state": job["state"],
                "inspector": job["inspector"],
                "issue": text, "ok": not issues,
            })

        # 什么证据可以撤回争议：争议双方之外、合格、独立第三人的盲评
        withdraw = _withdrawal_options(tables, defect, required, eligible, verdict)

        pending = bool(jobs_by_defect.get(defect)) or bool(reasons)
        audits.append({
            "defect": defect,
            "building": verdict["building"],
            "part": verdict["part"],
            "required_grade": required,
            "pending": pending,
            "escalation": reasons,
            "calibration": verdict["calibration"],
            "calibration_note": verdict["note"],
            "jobs": job_checks,
            "eligible_reviewers": eligible,
            "independent_experts": [
                item["inspector"] for item in eligible
                if item["expert"] and not item["rated_this_defect"]
            ],
            "withdrawal_evidence": withdraw,
        })
    return audits


def _withdrawal_options(tables, defect, required_grade, eligible, verdict):
    """判断现有证据能否撤回争议，以及还缺什么资格的复核。"""
    part = verdict["part"]
    roles = role_map(tables)
    groups = {group["evidence"]: group for group in evidence_groups(tables)}
    initial = initial_rating(tables, defect)
    qualified = {item["inspector"] for item in eligible}

    usable, party_self_ratings = [], []
    for row in tables["ratings"]:
        if row["defect"] != defect or row["reason"] == FOLLOWUP:
            continue
        if row["blind"] != "yes" or row["inspector"] == initial["inspector"]:
            continue
        group = groups[row["evidence"]]
        # 原始争议双方 = 该证据上最早出现的两名评员；其后加入的才是独立第三人
        # （reason=dispute 表示因争议加查，不代表加查人是争议方）
        original_parties = set(group["inspectors"][:2])
        tag = "专家" if roles[(row["inspector"], part)]["expert"] else "非专家，部位无锚点时不能单独定案"
        entry = f"{row['evidence']}：{row['inspector']} 盲评 {row['grade']}（{tag}）"
        if row["inspector"] not in qualified:
            party_self_ratings.append(entry + "，等级资格不足，不能签此复核")
        elif row["inspector"] in original_parties:
            party_self_ratings.append(entry + "，属原始争议方评级，不能自行撤回争议")
        else:
            usable.append(entry)

    followup_notes = [
        f"{row['evidence']} 为维修后复评（{row['grade']}），"
        "只记录维修后状态，不能撤回维修前评级争议"
        for row in tables["ratings"]
        if row["defect"] == defect and row["reason"] == FOLLOWUP
    ]

    if not eligible:
        missing = f"名册中无同时满足 {part} 部位、可签 {required_grade} 且回避初评员的复核员"
    elif not usable:
        missing = f"缺 {part} 部位可签 {required_grade} 的独立第三人盲评"
        if not any(item["expert"] for item in eligible):
            missing += "；现有合格人选均非专家，结论无专家锚点"
    else:
        missing = ""
    return {
        "existing_blind": usable,
        "party_self_ratings": party_self_ratings,
        "followup_notes": followup_notes,
        "missing_qualification": missing,
    }


# ------------------------------------------------ 补做共同样本的优先级
def sampling_gaps(tables):
    """优先填补会改变实际处置的可比缺口，而非复核最多的片区。"""
    audits = {item["defect"]: item for item in review_audit(tables)}
    verdicts = {item["defect"]: item for item in defect_verdicts(tables)}
    gaps = []
    for defect, item in audits.items():
        if item["calibration"] not in ("uncalibrated", "indirect_only"):
            continue
        score = 0
        drivers = []
        if item["required_grade"] == "urgent":
            score += 3
            drivers.append("涉及 urgent 处置")
        if verdicts[defect]["verified_urgent"]:
            score += 2
            drivers.append("verified_urgent 独立证据在身")
        if any(not check["ok"] for check in item["jobs"]):
            score += 3
            drivers.append("工单被资格/派单问题阻断")
        elif item["jobs"]:
            score += 1
            drivers.append("已有工单但结论无锚点")
        if any("争议" in reason for reason in item["escalation"]):
            score += 2
            drivers.append("争议未决")
        if not item["independent_experts"]:
            score += 2
            drivers.append(f"{item['part']} 部位名册无可用专家锚点")
        gaps.append({
            "defect": defect,
            "part": item["part"],
            "priority_score": score,
            "drivers": drivers,
            "needed": (
                f"在 {item['part']} 部位补专家盲评共同样本（同 evidence、"
                f"等级覆盖到 {item['required_grade']}）"
            ),
        })
    gaps.sort(key=lambda item: (-item["priority_score"], item["defect"]))
    return gaps


# ---------------------------------------------------------------- CLI
def _print_report(tables):
    groups = evidence_groups(tables)
    print("== 1. 记录分类（共同对象 / 单评员状态记录 / 维修后复评） ==")
    for kind, title in (
        ("shared", "共同对象（可识别评员差异）"),
        ("solo", "单评员证据（仅说明对象当前状态）"),
        ("followup", "维修后复评（非共同对象）"),
    ):
        picked = [group for group in groups if group["kind"] == kind]
        print(f"[{title}] {len(picked)} 组")
        for group in picked:
            detail = ", ".join(
                f"{row['inspector']}={row['grade']}({row['reason']})"
                for row in group["rows"]
            )
            print(f"  {group['evidence']} -> {group['defect']}/{group['part']}: {detail}")

    print("\n== 2. 部位级校准 ==")
    for part, info in part_calibration(tables).items():
        print(f"[{part}] anchored={info['anchored']} experts={info['experts']}")
        print(f"  评员到专家最短距离: {info['expert_distance']}")
        print(f"  共同对象: {info['shared_evidence']} 抽核原因构成: {info['shared_reasons']}")
        print(f"  成对一致/相邻混淆/跨级混淆: {info['pair_stats']}")
        print(f"  相邻等级混淆对: {info['adjacent_confusion']}")
        print(f"  非专家相对专家平均级差: {info['rater_bias_vs_expert']}")
        print(f"  单评员证据: {info['solo_evidence']}；维修后复评: {info['followup_evidence']}")

    print("\n== 3. 单栋缺陷结论（保留原评） ==")
    for verdict in defect_verdicts(tables):
        print(
            f"{verdict['defect']}({verdict['building']}/{verdict['part']}) "
            f"原评={verdict['initial_grade']} verified_urgent={verdict['verified_urgent']} "
            f"校准={verdict['calibration']} 区间={verdict['grade_span'] or '-'} :: {verdict['note']}"
        )

    print("\n== 4. 待复核件审查 ==")
    for item in review_audit(tables):
        if not item["pending"]:
            continue
        print(
            f"{item['defect']}({item['building']}/{item['part']}) 需处置等级={item['required_grade']} "
            f"校准={item['calibration']}"
        )
        for reason in item["escalation"]:
            print(f"  升级原因: {reason}")
        for check in item["jobs"]:
            flag = "OK" if check["ok"] else "问题"
            extra = f" 复核员={check.get('inspector', '-')}" if "inspector" in check else ""
            print(f"  工单 {check['job']}({check['state']}){extra} [{flag}] {check['issue']}")
        print(
            "  合格复核员: "
            + ", ".join(
                f"{e['inspector']}{'(专家)' if e['expert'] else ''}"
                f"{'(已评过本对象)' if e['rated_this_defect'] else ''}"
                for e in item["eligible_reviewers"]
            )
        )
        print(f"  独立专家: {item['independent_experts'] or '无'}")
        for evidence in item["withdrawal_evidence"]["existing_blind"]:
            print(f"  可用于撤争的现有盲评: {evidence}")
        for entry in item["withdrawal_evidence"]["party_self_ratings"]:
            print(f"  不能撤争的盲评: {entry}")
        if item["withdrawal_evidence"]["missing_qualification"]:
            print(f"  资格缺口: {item['withdrawal_evidence']['missing_qualification']}")
        for note in item["withdrawal_evidence"]["followup_notes"]:
            print(f"  {note}")

    print("\n== 5. 补做共同样本优先级（只列会改变处置的缺口） ==")
    for gap in sampling_gaps(tables):
        print(f"{gap['defect']}({gap['part']}) score={gap['priority_score']} "
              f"{'; '.join(gap['drivers'])} -> {gap['needed']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, nargs="?",
                        default=Path(__file__).parent / "data")
    args = parser.parse_args()
    tables = load_tables(args.folder)
    tables["_grade_index"] = grade_order(args.folder)[1]
    _print_report(tables)


if __name__ == "__main__":
    main()
