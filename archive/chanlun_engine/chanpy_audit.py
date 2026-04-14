"""chan.py 原码 vs 我们系统 逐一审计对比"""
import sys, requests
sys.path.insert(0, '.')

resp = requests.get('http://localhost:8000/api/klines',
    params={'symbol':'BTC-USDT','interval':'1H','limit':1000,'market':'crypto'}, timeout=20)
candles = resp.json()['candles']
print(f'K线: {len(candles)}')

# === 我们系统 ===
from chanlun_service import analyze
our = analyze(candles)

# === chan.py 原生 ===
from ChanConfig import CChanConfig
from Chan import CChan
from Common.CEnum import KL_TYPE, AUTYPE, BI_DIR
from chanlun_service import _build_kline_units, _klu_to_bar_index
from collections import defaultdict

kline_units = _build_kline_units(candles)
ts_list = [int(c.get('timestamp',0)) for c in candles]

config = CChanConfig({
    'bi_strict':True, 'bi_fx_check':'half', 'divergence_rate':10.0,
    'min_zs_cnt':1, 'zs_combine':True, 'zs_combine_mode':'zs',
    'one_bi_zs':False, 'macd_algo':'area', 'bsp1_only_multibi_zs':True,
    'trigger_step':False
})
lv = KL_TYPE.K_DAY
chan = CChan.__new__(CChan)
chan.code='T'; chan.begin_time=None; chan.end_time=None
chan.autype=AUTYPE.NONE; chan.data_src=None; chan.lv_list=[lv]
chan.conf=config; chan.kl_misalign_cnt=0
chan.kl_inconsistent_detail=defaultdict(list)
chan.g_kl_iter=defaultdict(list)
chan.do_init()
chan.trigger_load({lv: kline_units})
kl = chan[0]

raw_bsp_bi = kl.bs_point_lst.get_latest_bsp(number=0)
raw_bsp_seg = kl.seg_bs_point_lst.get_latest_bsp(number=0)

print()
print('='*80)
print('chan.py vs our system')
print('='*80)

# 1. Bi
print(f'\n--- 1. Bi ---')
print(f'  chan.py: {len(kl.bi_list)}')
print(f'  ours:   {len(our["bi_list"])}')
match_bi = len(kl.bi_list) == len(our["bi_list"])
print(f'  match:  {match_bi}')
diff_bi = 0
for i, bi in enumerate(kl.bi_list):
    bx = _klu_to_bar_index(bi.get_begin_klu(), ts_list)
    ex = _klu_to_bar_index(bi.get_end_klu(), ts_list)
    if i < len(our['bi_list']):
        ob = our['bi_list'][i]
        if ob['begin_x'] != bx or ob['end_x'] != ex:
            print(f'  bi[{i}] DIFF: chan=bar{bx}->{ex} ours=bar{ob["begin_x"]}->{ob["end_x"]}')
            diff_bi += 1
if diff_bi == 0:
    print(f'  all {len(kl.bi_list)} bi positions match')

# 2. Seg
print(f'\n--- 2. Seg ---')
print(f'  chan.py: {len(kl.seg_list)}')
print(f'  ours:   {len(our["seg_list"])}')
match_seg = len(kl.seg_list) == len(our["seg_list"])
print(f'  match:  {match_seg}')
diff_seg = 0
for i, seg in enumerate(kl.seg_list):
    bx = _klu_to_bar_index(seg.start_bi.get_begin_klu(), ts_list)
    ex = _klu_to_bar_index(seg.end_bi.get_end_klu(), ts_list)
    if i < len(our['seg_list']):
        os = our['seg_list'][i]
        if os['begin_x'] != bx or os['end_x'] != ex:
            print(f'  seg[{i}] DIFF: chan=bar{bx}->{ex} ours=bar{os["begin_x"]}->{os["end_x"]}')
            diff_seg += 1
if diff_seg == 0:
    print(f'  all {len(kl.seg_list)} seg positions match')

# 3. ZS
print(f'\n--- 3. ZS ---')
our_bi_zs = [z for z in our['zs_list'] if z['level'] == 'bi']
our_seg_zs = [z for z in our['zs_list'] if z['level'] == 'seg']
print(f'  chan.py: bi_zs={len(kl.zs_list)} seg_zs={len(kl.segzs_list)}')
print(f'  ours:   bi_zs={len(our_bi_zs)} seg_zs={len(our_seg_zs)}')
match_zs = len(kl.zs_list) == len(our_bi_zs) and len(kl.segzs_list) == len(our_seg_zs)
print(f'  match:  {match_zs}')

diff_zs = 0
for i, zs in enumerate(kl.zs_list):
    bx = _klu_to_bar_index(zs.begin, ts_list)
    ex = _klu_to_bar_index(zs.end, ts_list)
    if i < len(our_bi_zs):
        oz = our_bi_zs[i]
        zg_ok = abs(oz['zg'] - float(zs.high)) < 0.5
        zd_ok = abs(oz['zd'] - float(zs.low)) < 0.5
        pos_ok = oz['begin_x'] == bx and oz['end_x'] == ex
        if not (zg_ok and zd_ok and pos_ok):
            print(f'  zs[{i}] DIFF: chan=bar{bx}->{ex} ZD={float(zs.low):.0f} ZG={float(zs.high):.0f}')
            print(f'              ours=bar{oz["begin_x"]}->{oz["end_x"]} ZD={oz["zd"]:.0f} ZG={oz["zg"]:.0f}')
            diff_zs += 1
if diff_zs == 0:
    print(f'  all ZS values match')

# 4. BSP bi-level
print(f'\n--- 4. BSP (bi-level) ---')
our_bi_bsp = [b for b in our['bsp_list'] if not b['type'].startswith('S')]
print(f'  chan.py: {len(raw_bsp_bi)}')
print(f'  ours:   {len(our_bi_bsp)}')
match_bsp = len(raw_bsp_bi) == len(our_bi_bsp)
print(f'  match:  {match_bsp}')

raw_sorted = sorted(raw_bsp_bi, key=lambda b: b.bi.idx)
our_sorted = sorted(our_bi_bsp, key=lambda b: b['x'])
for i, bsp in enumerate(raw_sorted):
    x = _klu_to_bar_index(bsp.klu, ts_list)
    price = float(bsp.klu.low if bsp.is_buy else bsp.klu.high)
    act = 'B' if bsp.is_buy else 'S'
    tp = bsp.type2str()
    if i < len(our_sorted):
        ob = our_sorted[i]
        o_act = 'B' if ob['is_buy'] else 'S'
        ok = abs(ob['y'] - price) < 1.0 and ob['type'] == tp and o_act == act
        if not ok:
            print(f'  [{i}] DIFF: chan=[{tp}]{act} bar={x} p={price:.0f} | ours=[{ob["type"]}]{o_act} bar={ob["x"]} p={ob["y"]:.0f}')
    else:
        print(f'  [{i}] MISSING in ours: [{tp}]{act} bar={x} p={price:.0f}')

if len(our_sorted) > len(raw_sorted):
    for i in range(len(raw_sorted), len(our_sorted)):
        ob = our_sorted[i]
        print(f'  [{i}] EXTRA in ours: [{ob["type"]}] bar={ob["x"]} p={ob["y"]:.0f}')

# 5. BSP seg-level
print(f'\n--- 5. BSP (seg-level) ---')
our_seg_bsp = [b for b in our['bsp_list'] if b['type'].startswith('S')]
print(f'  chan.py: {len(raw_bsp_seg)}')
print(f'  ours:   {len(our_seg_bsp)}')
match_sbsp = len(raw_bsp_seg) == len(our_seg_bsp)
print(f'  match:  {match_sbsp}')

raw_s_sorted = sorted(raw_bsp_seg, key=lambda b: b.bi.idx)
our_s_sorted = sorted(our_seg_bsp, key=lambda b: b['x'])
for i, bsp in enumerate(raw_s_sorted):
    x = _klu_to_bar_index(bsp.klu, ts_list)
    price = float(bsp.klu.low if bsp.is_buy else bsp.klu.high)
    act = 'B' if bsp.is_buy else 'S'
    tp = 'S' + bsp.type2str()
    if i < len(our_s_sorted):
        ob = our_s_sorted[i]
        o_act = 'B' if ob['is_buy'] else 'S'
        ok = abs(ob['y'] - price) < 1.0 and ob['type'] == tp and o_act == act
        if not ok:
            print(f'  [{i}] DIFF: chan=[{tp}]{act} bar={x} p={price:.0f} | ours=[{ob["type"]}]{o_act} bar={ob["x"]} p={ob["y"]:.0f}')

# Summary
print()
print('='*80)
all_ok = match_bi and match_seg and match_zs and match_bsp and match_sbsp and diff_bi==0 and diff_seg==0 and diff_zs==0
if all_ok:
    print('RESULT: FULLY CONSISTENT - chan.py == our system')
else:
    print('RESULT: DIFFERENCES FOUND')
print('='*80)
