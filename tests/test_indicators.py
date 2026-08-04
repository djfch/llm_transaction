"""技术指标纯函数测试：手算基准 + 数据不足降级 + Decimal 类型约束。

基准值全部手推（见各测试注释）；含 /3 递推的指标（MACD/KDJ）因 Decimal 28 位
舍入路径差异，用 1E-20 容差断言，其余精确断言。
"""

from decimal import Decimal

from src.gateway.base import Candle
from src.market import indicators as ind


def make_candle(t: int, h: str, lo: str, c: str, v: str = "1") -> Candle:
    """测试 K 线（o 指标不参与，取与 c 相同值）。"""
    return Candle(t=t, o=Decimal(c), h=Decimal(h), l=Decimal(lo), c=Decimal(c), v=Decimal(v))


def test_sma_benchmark():
    # (3+4+5)/3 = 4
    values = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    assert ind.sma(values, 3) == Decimal(4)
    assert isinstance(ind.sma(values, 3), Decimal)


def test_ema_benchmark():
    # alpha=2/(3+1)=0.5；seed=(1+2+3)/3=2；ema4=0.5*4+0.5*2=3；ema5=0.5*5+0.5*3=4
    values = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    assert ind.ema(values, 3) == Decimal(4)


def test_rsi_benchmark():
    # period=2：变化量 [2,-1,2]；播种 avg_gain=1, avg_loss=0.5；
    # 递推 avg_gain=(1*1+2)/2=1.5, avg_loss=(0.5*1+0)/2=0.25；RS=6 → RSI=100-100/7=600/7
    closes = [Decimal(i) for i in [1, 3, 2, 4]]
    assert ind.rsi(closes, 2) == Decimal(600) / 7


def test_rsi_all_up_is_100():
    # 全程上涨无下跌量：avg_loss=0 → RSI=100
    closes = [Decimal(i) for i in range(1, 17)]
    assert ind.rsi(closes, 14) == Decimal(100)


def test_rsi_flat_is_50():
    # 横盘：收盘价不变，avg_gain=avg_loss=0 → 中性 50（不得报极端超买 100）
    closes = [Decimal(100)] * 20
    assert ind.rsi(closes, 14) == Decimal(50)


def test_rsi_all_down_is_0():
    # 全程下跌无上涨量：RS=0 → RSI=0
    closes = [Decimal(i) for i in range(20, 4, -1)]
    assert ind.rsi(closes, 14) == Decimal(0)


def test_atr_benchmark():
    # TR=[10-8, max(3,|12-9|,|9-9|)=3, max(3,|13-11|,|10-11|)=3]；
    # 播种 (2+3)/2=2.5；Wilder 递推 (2.5*1+3)/2=2.75
    candles = [
        make_candle(1, "10", "8", "9"),
        make_candle(2, "12", "9", "11"),
        make_candle(3, "13", "10", "12"),
    ]
    assert ind.atr(candles, 2) == Decimal("2.75")


def test_macd_benchmark():
    # fast=2/slow=3/signal=2 手推：dif=[·,·,0.5,0.5,0.5]，dea=[·,·,·,0.5,0.5]，hist=0
    closes = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    result = ind.macd(closes, fast=2, slow=3, signal=2)
    assert result is not None
    dif, dea, hist = result
    tol = Decimal("1E-20")
    assert abs(dif - Decimal("0.5")) < tol
    assert abs(dea - Decimal("0.5")) < tol
    assert abs(hist) < tol
    assert dif - dea == hist


def test_kdj_benchmark():
    # period=2：HHV=12/LLV=8，RSV=(11-8)/4*100=75；
    # K=(2*50+75)/3=175/3；D=(2*50+K)/3=475/9；J=3K-2D=625/9
    candles = [make_candle(1, "10", "8", "9"), make_candle(2, "12", "9", "11")]
    result = ind.kdj(candles, 2)
    assert result is not None
    k, d, j = result
    tol = Decimal("1E-20")
    assert abs(k - Decimal(175) / 3) < tol
    assert abs(d - Decimal(475) / 9) < tol
    assert abs(j - Decimal(625) / 9) < tol


def test_bollinger_benchmark():
    # mid=(1+2+3+4+5)/5=3；总体方差=(4+1+0+1+4)/5=2；带宽=2*sqrt(2)
    closes = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    result = ind.bollinger(closes, 5, Decimal(2))
    assert result is not None
    upper, mid, lower = result
    assert mid == Decimal(3)
    assert upper == Decimal(3) + 2 * Decimal(2).sqrt()
    assert lower == Decimal(3) - 2 * Decimal(2).sqrt()


def test_roc_benchmark():
    # (110/100-1)*100=10
    closes = [Decimal(i) for i in [100, 105, 110]]
    assert ind.roc(closes, 2) == Decimal(10)


def test_vol_ratio_benchmark():
    # 最新量 30 / 近3根均量 (10+20+30)/3=20 → 1.5
    candles = [
        make_candle(1, "10", "9", "9.5", "10"),
        make_candle(2, "10", "9", "9.5", "20"),
        make_candle(3, "10", "9", "9.5", "30"),
    ]
    assert ind.vol_ratio(candles, 3) == Decimal("1.5")


def test_obv_benchmark():
    # 收盘 10→11(+5)→10.5(-3)→10.5(平) → 累计 2
    candles = [
        make_candle(1, "10", "9", "10", "1"),
        make_candle(2, "11", "10", "11", "5"),
        make_candle(3, "11", "10", "10.5", "3"),
        make_candle(4, "11", "10", "10.5", "7"),
    ]
    assert ind.obv(candles) == Decimal(2)


def test_insufficient_data_returns_none():
    closes = [Decimal(i) for i in [1, 2, 3]]
    candles = [make_candle(i, "10", "8", "9") for i in range(1, 4)]
    assert ind.sma(closes, 5) is None
    assert ind.ema(closes, 5) is None
    assert ind.rsi(closes, 14) is None
    assert ind.macd(closes) is None
    assert ind.kdj(candles, 9) is None
    assert ind.roc(closes, 10) is None
    assert ind.atr(candles, 14) is None
    assert ind.bollinger(closes, 20) is None
    assert ind.vol_ratio(candles, 20) is None
    assert ind.obv([]) is None


def test_series_aligned_with_input():
    # 序列与输入等长，前段数据不足处为 None（服务层逐根对齐依赖该形状）
    values = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    seq = ind.ema_series(values, 3)
    assert len(seq) == 5
    assert seq[:2] == [None, None]
    assert seq[2] == Decimal(2)  # SMA 播种
    obv_seq = ind.obv_series([make_candle(1, "10", "9", "10"), make_candle(2, "11", "10", "11")])
    assert len(obv_seq) == 2
    assert all(isinstance(v, Decimal) for v in obv_seq)


def test_return_types_are_decimal():
    values = [Decimal(i) for i in range(1, 61)]
    candles = [make_candle(i, str(101 + i), str(99 + i), str(100 + i), "10") for i in range(1, 61)]
    assert isinstance(ind.rsi(values), Decimal)
    assert isinstance(ind.atr(candles), Decimal)
    assert isinstance(ind.roc(values), Decimal)
    assert isinstance(ind.vol_ratio(candles), Decimal)
    assert isinstance(ind.obv(candles), Decimal)
    for item in (ind.macd(values), ind.kdj(candles), ind.bollinger(values)):
        assert item is not None
        assert all(isinstance(v, Decimal) for v in item)
