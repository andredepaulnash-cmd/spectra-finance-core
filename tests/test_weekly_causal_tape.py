import numpy as np
import pytest

from research.weekly_reversal.causal_tape import CausalTape, load_admitted_tape


def tape():
    data = np.zeros((48, 2, 6), dtype=np.float64)
    data[:] = [10, 12, 8, 11, 5, 3]
    return CausalTape(data, data_start=0, warmup_start=0, start=7200, end=14400,
                      roster=("BTC", "ETH"), synthetic=True)


def test_boundary_exposes_prior_bar_and_current_open_only():
    t = tape()
    t.array[24, 0] = [100, 900, 1, 800, 9999999, 99999]
    e = t.boundary(7200)
    assert e.completed[0].close == 11
    assert e.completed[0].completed_at == 7200
    assert e.observed_opens[0] == 100
    assert e.completed_hours[0].high == 12


def test_future_range_and_volume_do_not_veto_current_open():
    t = tape()
    t.array[24, 0, 1:] = np.nan
    assert t.boundary(7200).observed_opens[0] == 10
    assert t.boundary(7500).completed[0] is None


def test_missing_constituent_never_fills_hour():
    t = tape()
    t.array[15, 0] = np.nan
    e = t.boundary(7200)
    assert e.completed_hours[0] is None
    assert e.completed_hours[1].constituents == 12
    assert e.completed_hours[1].volume == 60
    assert e.completed_hours[1].trades == 36


def test_final_boundary_has_marks_no_future_execution_price():
    t = tape()
    e = t.boundary(14400)
    assert e.evaluation_end
    assert e.observed_opens == (None, None)
    assert e.completed[0].completed_at == 14400
    assert len(list(t.boundaries())) == 25


@pytest.mark.parametrize("at", [-300, 6900, 14700, 7201, True])
def test_outside_scope_rejected(at):
    with pytest.raises(ValueError):
        tape().boundary(at)


def test_warmup_stops_before_evaluation_prices():
    t = tape()
    t.array[24:] = [100, 110, 90, 100, 1, 1]
    bars = list(t.warmup_bars())
    hours = list(t.warmup_hours())
    assert len(bars) == 24 and bars[-1][0] == 7200
    assert len(hours) == 2 and hours[-1][1][0].close == 11
    assert all(item.close == 11 for _, row in bars for item in row)


def test_historical_loader_fails_before_any_file_access_without_admission():
    with pytest.raises(ValueError, match="admission"):
        load_admitted_tape("/does/not/exist", expected_sha256="x", admission={})


def test_malformed_observation_is_missing_not_fabricated():
    t = tape()
    t.array[23, 0, 2] = 20  # low above close
    t.array[23, 1, 5] = 2.5
    e = t.boundary(7200)
    assert e.completed == (None, None)
    assert e.completed_hours == (None, None)


def test_sealed_tape_is_immutable_and_detached_from_fixture_source():
    t=tape();original=t.array
    identity=t.seal()
    original[24,:,0]=99
    assert t.boundary(7200).observed_opens==(10,10)
    assert t.seal()==identity
    with pytest.raises(ValueError):t.array[24,0,0]=99
    with pytest.raises(ValueError):t.array.setflags(write=True)


def test_seal_binds_every_in_scope_price_and_only_frozen_scope():
    first=tape();second=tape();second.array[24,0,0]=99
    assert first.seal()['bounded_sha256']!=second.seal()['bounded_sha256']
    data=np.zeros((72,2,6),dtype=np.float64);data[:]=[10,12,8,11,5,3]
    t=CausalTape(data,data_start=0,warmup_start=3600,start=7200,end=14400,roster=('BTC','ETH'),synthetic=True)
    result=t.seal()
    assert t.array.shape==(36,2,6)
    assert result['source_data_start']==0 and result['bounds']==[3600,7200,14400]
    assert t.data_start==3600
    assert t.boundary(7200).completed[0].close==11
    assert result['bounded_bytes']==36*2*6*8
