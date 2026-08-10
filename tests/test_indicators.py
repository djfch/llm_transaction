"""技术指标纯函数测试：手算基准 + 数据不足降级 + Decimal 类型约束。

基准值全部手推（见各测试注释）；含 /3 递推的指标（MACD/KDJ）因 Decimal 28 位
舍入路径差异，用 1E-20 容差断言，其余精确断言。
"""

from decimal import Decimal

from src.gateway.base import Candle
from src.market import indicators as ind


def make_candle(t: int, h: str, lo: str, c: str, v: str = "1") -> Candle:
    """构造供指标测试使用的 K 线，并令未参与计算的开盘价等于收盘价。

    参数：
        t: int，K 线时间戳
        h: str，最高价的十进制字符串
        lo: str，最低价的十进制字符串
        c: str，收盘价的十进制字符串
        v: str，成交量的十进制字符串，默认 1

    返回：
        Candle，全部数值字段已转换为 Decimal 的测试 K 线
    """
    return Candle(t=t, o=Decimal(c), h=Decimal(h), l=Decimal(lo), c=Decimal(c), v=Decimal(v))


def test_sma_benchmark():
    """校验 SMA 简单移动平均的手算基准值与返回类型。

    参数：无

    返回：
        None，断言 sma([1,2,3,4,5], 3) 等于手算均值 4，且返回值类型为 Decimal
    """
    # (3+4+5)/3 = 4
    values = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    assert ind.sma(values, 3) == Decimal(4)
    assert isinstance(ind.sma(values, 3), Decimal)


def test_ema_benchmark():
    """校验 EMA 指数移动平均的手算基准值（SMA 播种后按 alpha 递推）。

    参数：无

    返回：
        None，断言 ema([1,2,3,4,5], 3) 等于手算递推结果 4
    """
    # alpha=2/(3+1)=0.5；seed=(1+2+3)/3=2；ema4=0.5*4+0.5*2=3；ema5=0.5*5+0.5*3=4
    values = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    assert ind.ema(values, 3) == Decimal(4)


def test_rsi_benchmark():
    """校验 RSI 相对强弱指标的手算基准值（播种均值加 Wilder 递推）。

    参数：无

    返回：
        None，断言 rsi([1,3,2,4], 2) 等于手算值 600/7
    """
    # period=2：变化量 [2,-1,2]；播种 avg_gain=1, avg_loss=0.5；
    # 递推 avg_gain=(1*1+2)/2=1.5, avg_loss=(0.5*1+0)/2=0.25；RS=6 → RSI=100-100/7=600/7
    closes = [Decimal(i) for i in [1, 3, 2, 4]]
    assert ind.rsi(closes, 2) == Decimal(600) / 7


def test_rsi_all_up_is_100():
    """校验收盘价全程上涨时 RSI 取上极值 100。

    参数：无

    返回：
        None，断言 rsi(16 根单调上涨收盘价, 14) 等于 100（无下跌量时涨幅占满）
    """
    # 全程上涨无下跌量：avg_loss=0 → RSI=100
    closes = [Decimal(i) for i in range(1, 17)]
    assert ind.rsi(closes, 14) == Decimal(100)


def test_rsi_flat_is_50():
    """校验收盘价横盘不动时 RSI 返回中性值 50 而非极端超买。

    参数：无

    返回：
        None，断言 rsi(20 根相同收盘价, 14) 等于 50，不误报超买 100
    """
    # 横盘：收盘价不变，avg_gain=avg_loss=0 → 中性 50（不得报极端超买 100）
    closes = [Decimal(100)] * 20
    assert ind.rsi(closes, 14) == Decimal(50)


def test_rsi_all_down_is_0():
    """校验收盘价全程下跌时 RSI 取下极值 0。

    参数：无

    返回：
        None，断言 rsi(16 根单调下跌收盘价, 14) 等于 0（无上涨量时 RS 为 0）
    """
    # 全程下跌无上涨量：RS=0 → RSI=0
    closes = [Decimal(i) for i in range(20, 4, -1)]
    assert ind.rsi(closes, 14) == Decimal(0)


def test_atr_benchmark():
    """校验 ATR 平均真实波幅的手算基准值（TR 播种加 Wilder 递推）。

    参数：无

    返回：
        None，断言 atr(3 根手算 K 线, 2) 等于 2.75
    """
    # TR=[10-8, max(3,|12-9|,|9-9|)=3, max(3,|13-11|,|10-11|)=3]；
    # 播种 (2+3)/2=2.5；Wilder 递推 (2.5*1+3)/2=2.75
    candles = [
        make_candle(1, "10", "8", "9"),
        make_candle(2, "12", "9", "11"),
        make_candle(3, "13", "10", "12"),
    ]
    assert ind.atr(candles, 2) == Decimal("2.75")


def test_macd_benchmark():
    """校验 MACD 的手算基准值及 dif/dea/hist 三者的内在一致性。

    参数：无

    返回：
        None，断言 macd([1,2,3,4,5], 2/3/2) 的 dif、dea 与 0.5 的偏差、hist 与 0
        的偏差均在 1E-20 容差内，且满足 dif - dea == hist
    """
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
    """校验 KDJ 随机指标的手算基准值（RSV 经 /3 递推得 K/D/J）。

    参数：无

    返回：
        None，断言 kdj(2 根手算 K 线, 2) 的 K=175/3、D=475/9、J=625/9（1E-20 容差）
    """
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
    """校验布林带的手算基准值（中轨为均值，上下轨按总体标准差展开）。

    参数：无

    返回：
        None，断言 bollinger([1,2,3,4,5], 5, 2) 的中轨为 3、上下轨为 3±2*sqrt(2)
    """
    # mid=(1+2+3+4+5)/5=3；总体方差=(4+1+0+1+4)/5=2；带宽=2*sqrt(2)
    closes = [Decimal(i) for i in [1, 2, 3, 4, 5]]
    result = ind.bollinger(closes, 5, Decimal(2))
    assert result is not None
    upper, mid, lower = result
    assert mid == Decimal(3)
    assert upper == Decimal(3) + 2 * Decimal(2).sqrt()
    assert lower == Decimal(3) - 2 * Decimal(2).sqrt()


def test_roc_benchmark():
    """校验 ROC 变动率指标的手算基准值。

    参数：无

    返回：
        None，断言 roc([100,105,110], 2) 等于 10（区间涨幅 10%）
    """
    # (110/100-1)*100=10
    closes = [Decimal(i) for i in [100, 105, 110]]
    assert ind.roc(closes, 2) == Decimal(10)


def test_vol_ratio_benchmark():
    """校验量比指标的手算基准值（最新成交量除以近 N 根均量）。

    参数：无

    返回：
        None，断言 vol_ratio(成交量 10/20/30 的 3 根 K 线, 3) 等于 1.5
    """
    # 最新量 30 / 近3根均量 (10+20+30)/3=20 → 1.5
    candles = [
        make_candle(1, "10", "9", "9.5", "10"),
        make_candle(2, "10", "9", "9.5", "20"),
        make_candle(3, "10", "9", "9.5", "30"),
    ]
    assert ind.vol_ratio(candles, 3) == Decimal("1.5")


def test_obv_benchmark():
    """校验 OBV 能量潮的手算基准值（涨加量、跌减量、平盘不计）。

    参数：无

    返回：
        None，断言 obv(4 根手算 K 线) 等于累计值 2（+5 后 -3，平盘那根不计）
    """
    # 收盘 10→11(+5)→10.5(-3)→10.5(平) → 累计 2
    candles = [
        make_candle(1, "10", "9", "10", "1"),
        make_candle(2, "11", "10", "11", "5"),
        make_candle(3, "11", "10", "10.5", "3"),
        make_candle(4, "11", "10", "10.5", "7"),
    ]
    assert ind.obv(candles) == Decimal(2)


def test_insufficient_data_returns_none():
    """校验样本数不足周期时各指标统一降级返回 None 而不是抛错或硬算。

    参数：无

    返回：
        None，断言 sma/ema/rsi/macd/kdj/roc/atr/bollinger/vol_ratio 在样本数
        小于各自周期时返回 None，空 K 线输入的 obv 同样返回 None
    """
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
    """校验序列类指标输出与输入等长，前段数据不足处以 None 占位。

    参数：无

    返回：
        None，断言 ema_series 长度等于输入长度、前两位为 None、第三位为 SMA
        播种值 2；obv_series 长度对齐输入且每个元素都是 Decimal
    """
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
    """校验各指标返回值全部为 Decimal，防止 float 混入金额计算链路。

    参数：无

    返回：
        None，断言 rsi/atr/roc/vol_ratio/obv 直接返回 Decimal，macd/kdj/bollinger
        三元组的每个分量均为 Decimal
    """
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
