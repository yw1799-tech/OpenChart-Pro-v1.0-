"""
chan.py 内置绘图对比脚本
从本地API获取BTC-USDT 1H数据，运行CChan分析，生成matplotlib图表
"""
import sys
import os
import json
from datetime import datetime

# 设置工作目录和路径
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '.')

import matplotlib
matplotlib.use('Agg')  # 非交互式后端，用于保存文件
import matplotlib.pyplot as plt

import requests

from Common.CEnum import DATA_FIELD, KL_TYPE, AUTYPE, DATA_SRC
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit
from ChanConfig import CChanConfig
from Chan import CChan
from Plot.PlotDriver import CPlotDriver


def fetch_klines():
    """从本地API获取K线数据"""
    url = "http://localhost:8000/api/klines"
    params = {
        "symbol": "BTC-USDT",
        "interval": "1H",
        "limit": 1000,
        "market": "crypto"
    }
    print(f"正在从 {url} 获取数据...")
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    candles = data["candles"]
    print(f"获取到 {len(candles)} 根K线")
    return candles


def candles_to_klu_iter(candles):
    """将API返回的candle数据转换为CKLine_Unit迭代器"""
    for idx, c in enumerate(candles):
        # candle格式: {timestamp, open, high, low, close, volume, turnover}
        ts = c["timestamp"]  # 毫秒时间戳
        dt = datetime.utcfromtimestamp(ts / 1000.0)

        time_obj = CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, auto=False)
        item_dict = {
            DATA_FIELD.FIELD_TIME: time_obj,
            DATA_FIELD.FIELD_OPEN: float(c["open"]),
            DATA_FIELD.FIELD_HIGH: float(c["high"]),
            DATA_FIELD.FIELD_LOW: float(c["low"]),
            DATA_FIELD.FIELD_CLOSE: float(c["close"]),
            DATA_FIELD.FIELD_VOLUME: float(c["volume"]),
        }
        klu = CKLine_Unit(item_dict)
        klu.set_idx(idx)
        klu.kl_type = KL_TYPE.K_60M
        yield klu


def main():
    # 1. 获取数据
    candles = fetch_klines()

    # 2. 配置CChan - 与OpenChart Pro一致的参数
    config = CChanConfig({
        "bi_strict": True,
        "bi_fx_check": "half",
        "divergence_rate": 10.0,
        "min_zs_cnt": 1,
        "zs_combine": True,
        "zs_combine_mode": "zs",
        "one_bi_zs": False,
        "macd_algo": "area",
        "bsp1_only_multibi_zs": True,
        "trigger_step": False,
    })

    # 3. 创建CChan实例并手动注入数据
    #    使用trigger_step=True先创建空实例，然后手动加载数据
    config_for_init = CChanConfig({
        "bi_strict": True,
        "bi_fx_check": "half",
        "divergence_rate": 10.0,
        "min_zs_cnt": 1,
        "zs_combine": True,
        "zs_combine_mode": "zs",
        "one_bi_zs": False,
        "macd_algo": "area",
        "bsp1_only_multibi_zs": True,
        "trigger_step": True,  # 先用step模式创建空对象
    })

    lv_list = [KL_TYPE.K_60M]
    chan = CChan(
        code="BTC-USDT",
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.BAO_STOCK,  # 不会实际使用
        lv_list=lv_list,
        config=config_for_init,
        autype=AUTYPE.QFQ,
    )

    # 手动注入数据迭代器
    chan.add_lv_iter(0, candles_to_klu_iter(candles))

    # 关闭trigger_step，让load一次性算完
    chan.conf.trigger_step = False

    # 初始化缓存
    chan.klu_cache = [None for _ in chan.lv_list]
    chan.klu_last_t = [CTime(1980, 1, 1, 0, 0) for _ in chan.lv_list]

    # 加载并计算
    print("正在运行缠论分析...")
    for _ in chan.load_iterator(lv_idx=0, parent_klu=None, step=False):
        pass

    # 计算线段和中枢
    for lv in chan.lv_list:
        chan.kl_datas[lv].cal_seg_and_zs()

    print("分析完成!")

    # 4. 统计结果
    kl_data = chan[KL_TYPE.K_60M]

    bi_count = len(kl_data.bi_list)
    seg_count = len(kl_data.seg_list)
    zs_count = len(kl_data.zs_list)
    bsp_count = len(list(kl_data.bs_point_lst.bsp_iter()))
    seg_bsp_count = len(list(kl_data.seg_bs_point_lst.bsp_iter()))

    print(f"\n========== 分析结果统计 ==========")
    print(f"笔(Bi)数量:     {bi_count}")
    print(f"线段(Seg)数量:   {seg_count}")
    print(f"中枢(ZS)数量:    {zs_count}")
    print(f"笔买卖点(BSP):   {bsp_count}")
    print(f"段买卖点(SegBSP): {seg_bsp_count}")

    # 5. 列出所有BSP详情
    print(f"\n========== 笔级别买卖点详情 ==========")
    print(f"{'类型':<12} {'买/卖':<6} {'价格':<12} {'K线位置(idx)':<12} {'时间'}")
    print("-" * 70)
    for bsp in kl_data.bs_point_lst.bsp_iter():
        bsp_type = bsp.type2str()
        direction = "买" if bsp.is_buy else "卖"
        price = bsp.klu.low if bsp.is_buy else bsp.klu.high
        idx = bsp.klu.idx
        time_str = str(bsp.klu.time)
        print(f"{bsp_type:<12} {direction:<6} {price:<12.2f} {idx:<12} {time_str}")

    if seg_bsp_count > 0:
        print(f"\n========== 段级别买卖点详情 ==========")
        print(f"{'类型':<12} {'买/卖':<6} {'价格':<12} {'K线位置(idx)':<12} {'时间'}")
        print("-" * 70)
        for bsp in kl_data.seg_bs_point_lst.bsp_iter():
            bsp_type = bsp.type2str()
            direction = "买" if bsp.is_buy else "卖"
            price = bsp.klu.low if bsp.is_buy else bsp.klu.high
            idx = bsp.klu.idx
            time_str = str(bsp.klu.time)
            print(f"{bsp_type:<12} {direction:<6} {price:<12.2f} {idx:<12} {time_str}")

    # 6. 生成图表
    print(f"\n正在生成图表...")

    plot_config = {
        "plot_kline": True,
        "plot_kline_combine": True,
        "plot_bi": True,
        "plot_seg": True,
        "plot_zs": True,
        "plot_bsp": True,
        "plot_segbsp": True,
        "plot_macd": True,
    }

    plot_para = {
        "figure": {
            "w": 36,
            "h": 12,
            "x_tick_num": 20,
        },
    }

    driver = CPlotDriver(chan, plot_config=plot_config, plot_para=plot_para)

    output_path = "d:/OpenChart Pro/chanlun_comparison.png"
    driver.save2img(output_path)
    print(f"图表已保存到: {output_path}")

    # 关闭matplotlib图形释放内存
    plt.close('all')

    print("\n完成!")


if __name__ == "__main__":
    main()
