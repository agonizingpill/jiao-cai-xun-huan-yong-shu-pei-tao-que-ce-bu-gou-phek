"""整班可配齐口径：共需筛选与主管确认共用同一套库存计算。

只承认主管已批准的版次兼容（Compatibility.state='approved'），不做传递推断。
库存按单本实时状态计算：condition='usable' 且 state='available' 才算可占。
"""
from sqlalchemy import select, func
from .models import Edition, Compatibility, Copy, PackLine, Assignment, Classroom


def approved_compat_map(s):
    """target_edition_id -> 已批准可代用的 edition_id 列表（不含自身）。"""
    out = {}
    for target_id, allowed_id in s.execute(
        select(Compatibility.target_id, Compatibility.allowed_id).where(
            Compatibility.state == 'approved')
    ):
        out.setdefault(target_id, []).append(allowed_id)
    return out


def compatible_editions(s, target_id, compat=None):
    """按已批准兼容关系，能满足某课程版次需求的全部版次（自身在前）。"""
    compat = compat if compat is not None else approved_compat_map(s)
    return [target_id] + compat.get(target_id, [])


def available_counts(s, edition_ids):
    """每个版次当前可占用（usable + available）的单本数。"""
    counts = dict(s.execute(
        select(Copy.edition_id, func.count(Copy.id)).where(
            Copy.edition_id.in_(edition_ids),
            Copy.condition == 'usable',
            Copy.state == 'available',
        ).group_by(Copy.edition_id)
    ).all())
    return {eid: counts.get(eid, 0) for eid in edition_ids}


def line_capacity(s, edition_id, compat=None, counts=None):
    """某必修课程版次当前最多能配到的册数（自身 + 已批准兼容的可用单本）。"""
    ids = compatible_editions(s, edition_id, compat)
    if counts is None:
        counts = available_counts(s, ids)
    return sum(counts.get(eid, 0) for eid in ids), ids, counts


def pack_required_lines(s, pack_id):
    return list(s.scalars(select(PackLine).where(
        PackLine.pack_id == pack_id).order_by(PackLine.id)))


def pack_feasibility(s, pack_id):
    """按当前库存复算每个班级必修课程行的整班可配情况。

    返回每行的需求册数、当前可配上限与是否凑得齐整班；任一行凑不齐即整班不可配。
    已占给本配套的册数（reserved/issued 的 Assignment）仍然计入“已配”，
    确认时只对缺口再占书，所以可配上限对照的是整行需求而非剩余缺口。
    """
    compat = approved_compat_map(s)
    lines = pack_required_lines(s, pack_id)
    rows = []
    completable = True
    for line in lines:
        capacity, ids, counts = line_capacity(s, line.edition_id, compat)
        assigned = s.scalar(select(func.count()).select_from(Assignment).where(
            Assignment.line_id == line.id,
            Assignment.replacement_for_id.is_(None)))
        # 整班能否凑齐 = 本配套已占 + 当前兼容可用池；新配套 assigned=0，两口径等价
        ok = assigned + capacity >= line.quantity
        completable = completable and ok
        rows.append({
            'line_id': line.id,
            'edition_id': line.edition_id,
            'required_quantity': line.quantity,
            'assigned_quantity': assigned,
            'available_capacity': capacity,
            'short_quantity': max(0, line.quantity - assigned - capacity),
            'compatible_edition_ids': ids,
            'completable': ok,
        })
    return {'pack_id': pack_id, 'completable': completable, 'lines': rows}


def subject_editions(s, subject):
    return list(s.scalars(select(Edition).where(Edition.subject == subject).order_by(Edition.id)))


def annotate_suggestions(s, pack, suggestions):
    """给 Apriori 共需建议加上“能否配齐全班、且不挤占必修可配量”的判定。

    - 建议科目与必修同科目时不会出现在 suggestions 中（recommend 已排除当前科目）；
    - 不同科目的单本库天然分属不同课程，互不挤占必修可配册数；
    - 仍显式复核：选配以班级人数为需求时，每个必修行的可配上限不减少。
    只把能整班配齐的建议放进 feasible_suggestions；被筛掉的建议保留并给出原因。
    """
    room = s.get(Classroom, pack.classroom_id)
    size = room.size if room else 0
    compat = approved_compat_map(s)
    baseline = pack_feasibility(s, pack.id)
    baseline_cap = {r['line_id']: r['available_capacity'] for r in baseline['lines']}
    annotated = []
    feasible = []
    for rule in suggestions:
        subject = rule['then_subject']
        editions = subject_editions(s, subject)
        if not editions:
            entry = {**rule, 'feasible': False,
                     'reason': '教材目录中没有该课程版次，无法选配',
                     'candidate_edition_id': None, 'available_capacity': 0,
                     'classroom_size': size}
        else:
            # 以兼容池可用册数最多的版次作为拟配版次
            pools = []
            all_ids = set()
            for e in editions:
                ids = compatible_editions(s, e.id, compat)
                all_ids.update(ids)
                pools.append((e.id, ids))
            counts = available_counts(s, sorted(all_ids))
            scored = sorted(
                ((sum(counts.get(i, 0) for i in ids), eid) for eid, ids in pools),
                key=lambda x: (-x[0], x[1]))
            capacity, candidate_id = scored[0]
            enough = capacity >= size
            # 复核必修可配量不被减少（按全库实时计数重算，不使用建议科目局部计数）
            protected = all(
                line_capacity(s, ln.edition_id, compat)[0] >= baseline_cap[ln.id]
                for ln in pack_required_lines(s, pack.id))
            entry = {**rule, 'feasible': enough and protected,
                     'candidate_edition_id': candidate_id,
                     'available_capacity': capacity,
                     'classroom_size': size,
                     'reason': '' if enough and protected
                     else ('按已批准兼容关系仅可配 %d 册，少于全班 %d 人，凑不齐整班' % (capacity, size)
                           if not enough else '选配会挤占必修课程可配册数，不予建议')}
        annotated.append(entry)
        if entry['feasible']:
            feasible.append(entry)
    return annotated, feasible
