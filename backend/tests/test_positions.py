from positions import STEP, evenly_spaced, position_at


def test_empty_list():
    assert position_at([], 0) == STEP


def test_insert_at_start_middle_and_end():
    ps = [1024.0, 2048.0, 3072.0]
    assert position_at(ps, 0) == 512.0
    assert position_at(ps, 1) == 1536.0
    assert position_at(ps, 3) == 3072.0 + STEP


def test_index_is_clamped():
    assert position_at([1024.0], 99) == 2048.0


def test_signals_rebalance_when_gap_is_exhausted():
    assert position_at([1.0, 1.0 + 1e-7], 1) is None
    assert position_at([1e-7], 0) is None


def test_repeated_inserts_eventually_require_rebalance():
    low, high = 1024.0, 2048.0
    for _ in range(200):
        mid = position_at([low, high], 1)
        if mid is None:
            break
        high = mid
    else:
        raise AssertionError("le rééquilibrage aurait dû être demandé")


def test_evenly_spaced():
    assert evenly_spaced(3) == [1024.0, 2048.0, 3072.0]
