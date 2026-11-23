"""约束求解器：找出每笔票款在两套分组合计约束下仍可行的候选金额。

约束模型
--------
- 每笔票款 i 的取值必须来自其候选集合 domains[i]；
- 每个窗口分组 g 内各笔取值之和必须等于给定合计 totals[g]；
- 每个售票日分组 h 内各笔取值之和必须等于给定合计 totals[h]。

求解方式
--------
单趟带回溯的记忆化搜索（DFS + memo），按笔依次赋值并做前向检查：
不逐一枚举整批填法，而是只记录“某笔取某个值”是否出现在至少一个可行
完整解中，最后返回每笔的可行值集合。剪枝手段：

1. 每个分组剩余笔数的可达金额集合（suffix reachable sets），判断当前
   已用额度 + 剩余笔数能否凑出目标值，不行立即回退；
2. 剩余笔数的最小/最大金额和兜底剪枝；
3. 记忆化状态 (i, 各分组当前已用额度)，合并子搜索中出现过的可行取值。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence


class SolverError(Exception):
    """求解过程中的参数或状态错误。"""


class SearchLimitExceeded(SolverError):
    """搜索状态数超过保护上限，无法在预算内完成判定。"""


def _group_reachable_sets(
    domains: Sequence[tuple[int, ...]],
    groups: Sequence[tuple[int, ...]],
    totals: Sequence[int],
    cap: int = 200_000,
) -> tuple[tuple[dict[int, frozenset[int]], ...], tuple[bool, ...]]:
    """对每个分组预计算“后缀笔数可达金额集合”。

    reach[k] 表示该分组中从第 k 个成员起（含）的笔数能凑出的金额集合。
    集合超过 cap 时停止精确计算，该分组退化为 min/max 兜底剪枝。
    """
    all_sets: list[tuple[dict[int, frozenset[int]], ...]] = []
    exact_flags: list[bool] = []
    for members, target in zip(groups, totals):
        n = len(members)
        reach: list[dict[int, frozenset[int]]] = [None] * (n + 1)  # type: ignore[list-item]
        reach[n] = {0: frozenset((0,))}
        exact = True
        for k in range(n - 1, -1, -1):
            prev = reach[k + 1]
            if not prev:
                # 后缀已无可达金额，本层同样为空，不能回退成 {0}
                reach[k] = {}
                continue
            merged: set[int] = set()
            for s in prev:
                for v in domains[members[k]]:
                    ns = s + v
                    if ns <= target:
                        merged.add(ns)
            if len(merged) > cap:
                exact = False
                reach[k] = {}
                break
            reach[k] = {s: frozenset((s,)) for s in merged}
        if not exact:
            reach = [{} for _ in range(n + 1)]
        all_sets.append(tuple(reach))
        exact_flags.append(exact)
    return tuple(all_sets), tuple(exact_flags)


def compute_feasible(
    domains: Sequence[Iterable[int]],
    group_memberships: Sequence[Sequence[int]],
    totals: Sequence[int],
    node_limit: int = 2_000_000,
) -> tuple[tuple[int, ...], ...]:
    """计算每笔票款的可行候选金额。

    参数
    ----
    domains:
        domains[i] 为第 i 笔票款的候选金额（整数分），去重后处理。
    group_memberships:
        group_memberships[i] 给出第 i 笔所属的各分组 id（本应用固定为
        [窗口分组 id, 售票日分组 id] 两套）。
    totals:
        各分组的纸面合计，下标为分组 id。
    node_limit:
        记忆化搜索访问状态数保护上限，超出抛 SearchLimitExceeded。

    返回
    ----
    与 domains 等长的元组，第 i 项是该笔在至少一个完整可行解中出现过的
    取值集合（升序）。若整批无解，对应可行集合为空元组。
    """
    n = len(domains)
    if n == 0:
        return ()
    d = tuple(tuple(sorted(set(vals))) for vals in domains)
    gm = tuple(tuple(gs) for gs in group_memberships)
    t = tuple(totals)

    if any(not vals for vals in d):
        raise SolverError("存在没有候选金额的票款记录")
    if any(len(g) != len(gm[0]) for g in gm):
        raise SolverError("每笔票款必须归属相同数量的分组")
    if len(gm[0]) == 0:
        raise SolverError("至少需要一套分组合计约束")
    # 分组总数由实际出现的分组 id 决定（本应用为窗口分组数 + 售票日分组数）
    max_group_id = max(max(g) for g in gm)
    if min(min(g) for g in gm) < 0:
        raise SolverError("分组 id 不能为负")
    group_count = max_group_id + 1
    if len(t) != group_count:
        raise SolverError("合计数量与分组数量不一致")
    if any(total < 0 for total in t):
        raise SolverError("分组合计不能为负")

    # 分组 id -> 成员笔号列表
    members: list[list[int]] = [[] for _ in range(group_count)]
    for i, gs in enumerate(gm):
        for g in gs:
            if g < 0 or g >= group_count:
                raise SolverError(f"第 {i + 1} 笔票款引用了不存在的分组 {g}")
            members[g].append(i)
    for g, ids in enumerate(members):
        if not ids:
            raise SolverError(f"分组 {g} 没有任何票款记录")

    # 每个分组在“其成员顺序”上的位置表：
    # position_in_group[g][i] = 笔 i 在组 g 成员序列中的序号（不属于该组为 -1）
    position_in_group: list[list[int]] = [[-1] * n for _ in range(group_count)]
    for g, ids in enumerate(members):
        for pos, i in enumerate(ids):
            position_in_group[g][i] = pos

    # suffix_pos_after[g][i]：全局第 i 笔选值后，组 g 尚未处理的成员在
    # 可达表 reach_sets[g] 中的下标。可达表按组内成员序号索引，而 DFS 按
    # 全局笔号推进：i 属于组 g 时剩余成员数 = 组内序号 + 1 起；i 不属于
    # 组 g 时该组所有成员都未处理，下标为 0。
    suffix_pos_after: list[list[int]] = [[0] * n for _ in range(group_count)]
    for g, ids in enumerate(members):
        for i in range(n):
            p = position_in_group[g][i]
            suffix_pos_after[g][i] = 0 if p == -1 else p + 1

    reach_sets, exact = _group_reachable_sets(d, tuple(tuple(ids) for ids in members), t)

    # min/max 兜底数据：每个分组后缀笔数的金额上下界
    suffix_min: list[list[int]] = []
    suffix_max: list[list[int]] = []
    for g, ids in enumerate(members):
        smin = [0] * (len(ids) + 1)
        smax = [0] * (len(ids) + 1)
        for k in range(len(ids) - 1, -1, -1):
            vals = d[ids[k]]
            smin[k] = smin[k + 1] + vals[0]
            smax[k] = smax[k + 1] + vals[-1]
        suffix_min.append(smin)
        suffix_max.append(smax)

    # dfs 成功时返回“子问题每笔可行取值”（长度 n-i）；无解返回 None
    memo: dict[tuple, tuple[tuple[int, ...], ...] | None] = {}
    nodes = 0

    def dfs(i: int, used: tuple[int, ...]):
        """返回从第 i 笔起的子问题中，每笔可行取值（子问题内下标 0..n-i-1）。"""
        nonlocal nodes
        nodes += 1
        if nodes > node_limit:
            raise SearchLimitExceeded("搜索状态数超过上限，请缩小批次或减少候选")
        if i == n:
            # 终止：所有分组已用额度必须恰好等于纸面合计
            if all(used[g] == t[g] for g in range(group_count)):
                return ()
            return None

        key = (i, used)
        if key in memo:
            return memo[key]

        feasible: list[set[int]] = [set() for _ in range(n - i)]
        for v in d[i]:
            next_used = list(used)
            ok = True
            for g in gm[i]:
                nu = used[g] + v
                pos = suffix_pos_after[g][i]  # 选完本笔后该分组的后缀位置
                target = t[g]
                if nu > target:
                    ok = False
                    break
                remain_min = suffix_min[g][pos]
                remain_max = suffix_max[g][pos]
                if nu + remain_min > target or nu + remain_max < target:
                    ok = False
                    break
                if exact[g] and (target - nu) not in reach_sets[g][pos]:
                    ok = False
                    break
                next_used[g] = nu
            if not ok:
                continue

            sub = dfs(i + 1, tuple(next_used))
            if sub is None:
                continue
            # 该取值出现在至少一个完整可行解中
            feasible[0].add(v)
            for offset, vals in enumerate(sub, start=1):
                feasible[offset].update(vals)

        if not feasible[0]:
            result = None
        else:
            result = tuple(tuple(sorted(vals)) for vals in feasible)
        memo[key] = result
        return result

    root = dfs(0, tuple([0] * group_count))
    if root is None:
        return tuple(tuple() for _ in range(n))
    return root
