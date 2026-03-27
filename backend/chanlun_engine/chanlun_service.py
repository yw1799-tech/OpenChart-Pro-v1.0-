"""
缠论分析服务 - 封装 chan.py 引擎，提供简洁API

接收K线数据列表，返回笔/线段/中枢/买卖点分析结果。
所有坐标使用 (bar_index, price) 格式，方便前端绑定到K线图。
"""
import sys
import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

# 确保 chan.py 库在 sys.path 中
_engine_dir = os.path.dirname(os.path.abspath(__file__))
if _engine_dir not in sys.path:
    sys.path.insert(0, _engine_dir)

from collections import defaultdict

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import DATA_FIELD, KL_TYPE, BI_DIR, BSP_TYPE, AUTYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit
from DataAPI.CommonStockAPI import CCommonStockApi

logger = logging.getLogger(__name__)


class MemoryStockAPI(CCommonStockApi):
    """内存数据源 - 直接从K线列表提供数据给 CChan"""

    _kline_data: List[CKLine_Unit] = []

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=None):
        self.code = code
        self.name = code
        self.is_stock = False
        self.k_type = k_type
        self.begin_date = begin_date
        self.end_date = end_date
        self.autype = autype

    def get_kl_data(self):
        for klu in self.__class__._kline_data:
            yield klu

    def SetBasciInfo(self):
        pass

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    @classmethod
    def set_data(cls, data: List[CKLine_Unit]):
        cls._kline_data = data


def _ts_to_ctime(ts_ms: int) -> CTime:
    """毫秒时间戳转 CTime"""
    dt = datetime.utcfromtimestamp(ts_ms / 1000)
    return CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, auto=False)


def _build_kline_units(candles: List[Dict[str, Any]]) -> List[CKLine_Unit]:
    """将原始K线数据转换为 CKLine_Unit 列表"""
    units = []
    for c in candles:
        ts = c.get("timestamp") or c.get("time") or c.get("t", 0)
        kl_dict = {
            DATA_FIELD.FIELD_TIME: _ts_to_ctime(int(ts)),
            DATA_FIELD.FIELD_OPEN: float(c.get("open") or c.get("o", 0)),
            DATA_FIELD.FIELD_HIGH: float(c.get("high") or c.get("h", 0)),
            DATA_FIELD.FIELD_LOW: float(c.get("low") or c.get("l", 0)),
            DATA_FIELD.FIELD_CLOSE: float(c.get("close") or c.get("c", 0)),
            DATA_FIELD.FIELD_VOLUME: float(c.get("volume") or c.get("v", 0)),
        }
        units.append(CKLine_Unit(kl_dict))
    return units


def _find_bar_index(timestamp_ms: int, ts_list: List[int]) -> int:
    """根据时间戳找到最近的K线索引"""
    # 二分查找
    lo, hi = 0, len(ts_list) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ts_list[mid] == timestamp_ms:
            return mid
        elif ts_list[mid] < timestamp_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    # 返回最近的
    if lo >= len(ts_list):
        return len(ts_list) - 1
    if hi < 0:
        return 0
    if abs(ts_list[lo] - timestamp_ms) <= abs(ts_list[hi] - timestamp_ms):
        return lo
    return hi


def _klu_to_bar_index(klu, ts_list: List[int]) -> int:
    """将 CKLine_Unit 的时间转换为 bar_index"""
    ts_ms = int(klu.time.ts * 1000)
    return _find_bar_index(ts_ms, ts_list)


def analyze(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    缠论完整分析

    参数:
        candles: K线数据列表，每项包含 {timestamp, open, high, low, close, volume}
                 timestamp 为毫秒时间戳

    返回:
        {
            bi_list: [{begin_x, begin_y, end_x, end_y, dir, is_sure}],
            seg_list: [{begin_x, begin_y, end_x, end_y, dir, is_sure}],
            zs_list: [{begin_x, end_x, zg, zd, dir, level}],
            bsp_list: [{x, y, type, is_buy}],
        }
    """
    if not candles or len(candles) < 10:
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": []}

    # 构建时间戳索引
    ts_list = [int(c.get("timestamp") or c.get("time") or c.get("t", 0)) for c in candles]

    # 构建 CKLine_Unit 列表
    kline_units = _build_kline_units(candles)

    # 配置：使用trigger_load模式直接灌入数据
    config = CChanConfig({
        "bi_strict": True,
        "divergence_rate": 9999,
        "min_zs_cnt": 1,
        "zs_combine": True,
        "trigger_step": False,
    })

    try:
        lv = KL_TYPE.K_DAY
        # 创建CChan但不触发load（用trigger_load模式）
        chan = CChan.__new__(CChan)
        chan.code = "MEMORY"
        chan.begin_time = None
        chan.end_time = None
        chan.autype = AUTYPE.NONE
        chan.data_src = None
        chan.lv_list = [lv]
        chan.conf = config
        chan.kl_misalign_cnt = 0
        chan.kl_inconsistent_detail = defaultdict(list)
        chan.g_kl_iter = defaultdict(list)
        chan.do_init()

        # 直接用trigger_load灌入所有K线数据
        chan.trigger_load({lv: kline_units})
    except Exception as e:
        logger.error(f"缠论分析失败: {e}", exc_info=True)
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": []}

    # 提取结果
    kl_data = chan[0]  # 最高级别

    # 1. 提取笔
    bi_list = []
    for bi in kl_data.bi_list:
        try:
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            begin_x = _klu_to_bar_index(begin_klu, ts_list)
            end_x = _klu_to_bar_index(end_klu, ts_list)
            bi_list.append({
                "begin_x": begin_x,
                "begin_y": float(bi.get_begin_val()),
                "end_x": end_x,
                "end_y": float(bi.get_end_val()),
                "begin_ts": ts_list[begin_x] if 0 <= begin_x < len(ts_list) else 0,
                "end_ts": ts_list[end_x] if 0 <= end_x < len(ts_list) else 0,
                "dir": 1 if bi.dir == BI_DIR.UP else -1,
                "is_sure": bi.is_sure,
            })
        except Exception as e:
            logger.debug(f"提取笔失败: {e}")

    # 2. 提取线段
    seg_list = []
    for seg in kl_data.seg_list:
        try:
            begin_klu = seg.start_bi.get_begin_klu()
            end_klu = seg.end_bi.get_end_klu()
            begin_x = _klu_to_bar_index(begin_klu, ts_list)
            end_x = _klu_to_bar_index(end_klu, ts_list)
            seg_list.append({
                "begin_x": begin_x,
                "begin_y": float(seg.start_bi.get_begin_val()),
                "end_x": end_x,
                "end_y": float(seg.end_bi.get_end_val()),
                "begin_ts": ts_list[begin_x] if 0 <= begin_x < len(ts_list) else 0,
                "end_ts": ts_list[end_x] if 0 <= end_x < len(ts_list) else 0,
                "dir": 1 if seg.dir == BI_DIR.UP else -1,
                "is_sure": seg.is_sure,
            })
        except Exception as e:
            logger.debug(f"提取线段失败: {e}")

    # 3. 提取中枢（笔中枢）
    zs_list = []
    for zs in kl_data.zs_list:
        try:
            begin_x = _klu_to_bar_index(zs.begin, ts_list)
            end_x = _klu_to_bar_index(zs.end, ts_list)
            # 中枢方向：根据进入笔的方向判断
            zs_dir = 0
            if zs.bi_in is not None:
                zs_dir = 1 if zs.bi_in.dir == BI_DIR.UP else -1
            zs_list.append({
                "begin_x": begin_x,
                "end_x": end_x,
                "begin_ts": ts_list[begin_x] if 0 <= begin_x < len(ts_list) else 0,
                "end_ts": ts_list[end_x] if 0 <= end_x < len(ts_list) else 0,
                "zg": float(zs.high),
                "zd": float(zs.low),
                "dir": zs_dir,
                "level": "bi",
            })
        except Exception as e:
            logger.debug(f"提取中枢失败: {e}")

    # 线段中枢
    for zs in kl_data.segzs_list:
        try:
            begin_x = _klu_to_bar_index(zs.begin, ts_list)
            end_x = _klu_to_bar_index(zs.end, ts_list)
            zs_dir = 0
            if zs.bi_in is not None:
                zs_dir = 1 if zs.bi_in.dir == BI_DIR.UP else -1
            zs_list.append({
                "begin_x": begin_x,
                "end_x": end_x,
                "begin_ts": ts_list[begin_x] if 0 <= begin_x < len(ts_list) else 0,
                "end_ts": ts_list[end_x] if 0 <= end_x < len(ts_list) else 0,
                "zg": float(zs.high),
                "zd": float(zs.low),
                "dir": zs_dir,
                "level": "seg",
            })
        except Exception as e:
            logger.debug(f"提取线段中枢失败: {e}")

    # 4. 提取买卖点
    bsp_list = []
    try:
        all_bsp = kl_data.bs_point_lst.get_latest_bsp(number=0)
        for bsp in all_bsp:
            try:
                klu = bsp.klu
                x = _klu_to_bar_index(klu, ts_list)
                y = float(klu.low if bsp.is_buy else klu.high)
                bsp_list.append({
                    "x": x,
                    "y": y,
                    "ts": ts_list[x] if 0 <= x < len(ts_list) else 0,
                    "type": bsp.type2str(),
                    "is_buy": bsp.is_buy,
                })
            except Exception as e:
                logger.debug(f"提取买卖点失败: {e}")
    except Exception as e:
        logger.debug(f"获取买卖点列表失败: {e}")

    # 线段买卖点
    try:
        seg_bsp = kl_data.seg_bs_point_lst.get_latest_bsp(number=0)
        for bsp in seg_bsp:
            try:
                klu = bsp.klu
                x = _klu_to_bar_index(klu, ts_list)
                y = float(klu.low if bsp.is_buy else klu.high)
                bsp_list.append({
                    "x": x,
                    "y": y,
                    "ts": ts_list[x] if 0 <= x < len(ts_list) else 0,
                    "type": "S" + bsp.type2str(),
                    "is_buy": bsp.is_buy,
                })
            except Exception as e:
                logger.debug(f"提取线段买卖点失败: {e}")
    except Exception as e:
        logger.debug(f"获取线段买卖点列表失败: {e}")

    return {
        "bi_list": bi_list,
        "seg_list": seg_list,
        "zs_list": zs_list,
        "bsp_list": bsp_list,
    }
