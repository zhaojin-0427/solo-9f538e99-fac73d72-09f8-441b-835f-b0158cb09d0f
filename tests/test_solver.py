import pytest

from app.solver import SolverError, compute_feasible


def test_single_solution_exact_domains():
    # 每个分组只有一种凑法时，所有可行域都收缩到唯一值
    # 唯一完整解：(100, 400, 600, 150)
    domains = [
        (100, 150),
        (350, 400),
        (550, 600),
        (100, 150),
    ]
    # 四个分组：g0={0,1}=500, g1={2,3}=750, g2={0,2}=700, g3={1,3}=550
    memberships = [(0, 2), (0, 3), (1, 2), (1, 3)]
    totals = (500, 750, 700, 550)
    result = compute_feasible(domains, memberships, totals)
    assert result == ((100,), (400,), (600,), (150,))


def test_multiple_solutions_keeps_values_appearing_in_any_solution():
    # 4 笔四个分组；两套合计
    # g0: x0+x1=10, g1: x2+x3=20；g2: x0+x2=15, g3: x1+x3=15
    domains = [(5, 8), (2, 5), (7, 10), (10, 13)]
    memberships = [(0, 2), (0, 3), (1, 2), (1, 3)]
    result = compute_feasible(domains, memberships, (10, 20, 15, 15))
    # 可行完整解：(5,5,10,10) 与 (8,2,7,13)
    assert result == (
        (5, 8),
        (2, 5),
        (7, 10),
        (10, 13),
    )


def test_infeasible_returns_empty_domains():
    domains = [(1, 2), (3, 4)]
    memberships = [(0, 1), (0, 1)]
    # 唯一分组合计 100，无法凑出
    result = compute_feasible(domains, memberships, (100, 100))
    assert result == (tuple(), tuple())


def test_fixed_confirmation_shrinks_other_domains():
    # 把某笔域收缩成单值，模拟复核员确认后的重算
    memberships = [(0, 2), (0, 3), (1, 2), (1, 3)]
    result = compute_feasible(
        [(8,), (2, 5), (7, 10), (10, 13)],
        memberships,
        (10, 20, 15, 15),
    )
    # 确认 x0=8 后只剩 (8,2,7,13)
    assert result == ((8,), (2,), (7,), (13,))


def test_conflicting_confirmation_yields_empty():
    domains = [(8,), (2, 5), (7, 10), (10, 13)]
    memberships = [(0, 2), (0, 3), (1, 2), (1, 3)]
    result = compute_feasible(domains, memberships, (9, 20, 15, 15))
    assert any(len(v) == 0 for v in result)


def test_amount_bounds_large_values_are_exact():
    # 金额上限 1_000_000，求解结果必须精确
    big = 1_000_000
    domains = [
        (big - 1, big),
        (1, 2),
        (big - 3, big - 2),
        (3, 4),
    ]
    memberships = [(0, 2), (0, 3), (1, 2), (1, 3)]
    totals = (big + 1, big, big + big - 3, 4)
    result = compute_feasible(domains, memberships, totals)
    # g0: x0+x1=1000001 -> x0=1000000,x1=1；g3: x1+x3=4 -> x3=3
    # g2: x0+x2=1999997 -> x2=999997；g1: x2+x3=1000000 校验通过
    assert result == ((big,), (1,), (big - 3,), (3,))


def test_validation_errors():
    with pytest.raises(SolverError):
        compute_feasible([()], [(0,)], (1,))
    with pytest.raises(SolverError):
        compute_feasible([(1,)], [()], ())
    with pytest.raises(SolverError):
        compute_feasible([(1,)], [(0, 1)], (1,))  # totals 数量不符
