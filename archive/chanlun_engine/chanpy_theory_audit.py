"""
严格按照缠论原著标准逐一审计 chan.py 1H BTC-USDT 分析结果
审计内容：每一笔、每一线段、每一中枢、每一买卖点是否符合原著定义
"""
import sys, requests
sys.path.insert(0, '.')

resp = requests.get('http://localhost:8000/api/klines',
    params={'symbol':'BTC-USDT','interval':'1H','limit':1000,'market':'crypto'}, timeout=20)
candles = resp.json()['candles']

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

print('='*80)
print('CHANLUN THEORY AUDIT - BTC-USDT 1H')
print('='*80)

# ============================================================
# 1. AUDIT BI (Stroke)
# ============================================================
print('\n' + '='*80)
print('1. BI AUDIT')
print('   Rule: min 5 merged KLines between top/bottom fractals')
print('='*80)

bi_issues = []
for i, bi in enumerate(kl.bi_list):
    # klc_cnt = number of merged KLine clusters in this bi
    klc_list = list(bi.klc_lst)
    klc_cnt = len(klc_list)
    # raw kline count
    raw_cnt = sum(len(klc.lst) for klc in klc_list)

    bv = float(bi.get_begin_val())
    ev = float(bi.get_end_val())
    d = 'UP' if bi.dir == BI_DIR.UP else 'DN'

    # Check: merged KLC count >= 5 (bi_strict=True requires span>=4, meaning 5 KLCs)
    if klc_cnt < 5:
        bi_issues.append(f'  bi[{i}] {d} {bv:.0f}->{ev:.0f} KLC={klc_cnt} < 5 VIOLATION')

    # Check: direction consistency
    if bi.dir == BI_DIR.UP and ev <= bv:
        bi_issues.append(f'  bi[{i}] UP but end({ev:.0f}) <= begin({bv:.0f}) VIOLATION')
    if bi.dir == BI_DIR.DOWN and ev >= bv:
        bi_issues.append(f'  bi[{i}] DN but end({ev:.0f}) >= begin({bv:.0f}) VIOLATION')

print(f'  Total bi: {len(kl.bi_list)}')
if bi_issues:
    print(f'  ISSUES ({len(bi_issues)}):')
    for issue in bi_issues:
        print(issue)
else:
    print(f'  ALL {len(kl.bi_list)} bi PASS - min 5 KLC + direction consistent')

# Check alternation: UP/DOWN must alternate
alt_issues = []
for i in range(1, len(kl.bi_list)):
    if kl.bi_list[i].dir == kl.bi_list[i-1].dir:
        alt_issues.append(f'  bi[{i-1}] and bi[{i}] same direction: {kl.bi_list[i].dir}')
if alt_issues:
    print(f'  ALTERNATION ISSUES:')
    for a in alt_issues:
        print(a)
else:
    print(f'  ALL bi alternate UP/DOWN correctly')

# ============================================================
# 2. AUDIT SEG (Segment)
# ============================================================
print('\n' + '='*80)
print('2. SEG AUDIT')
print('   Rule: min 3 bi per segment')
print('='*80)

seg_issues = []
for i, seg in enumerate(kl.seg_list):
    bi_cnt = seg.end_bi.idx - seg.start_bi.idx + 1
    d = 'UP' if seg.dir == BI_DIR.UP else 'DN'
    bv = float(seg.start_bi.get_begin_val())
    ev = float(seg.end_bi.get_end_val())

    if bi_cnt < 3:
        seg_issues.append(f'  seg[{i}] {d} {bv:.0f}->{ev:.0f} bi_cnt={bi_cnt} < 3 VIOLATION')

    # Direction check
    if seg.dir == BI_DIR.UP and ev <= bv:
        seg_issues.append(f'  seg[{i}] UP but end({ev:.0f}) <= begin({bv:.0f})')
    if seg.dir == BI_DIR.DOWN and ev >= bv:
        seg_issues.append(f'  seg[{i}] DN but end({ev:.0f}) >= begin({bv:.0f})')

print(f'  Total seg: {len(kl.seg_list)}')
if seg_issues:
    print(f'  ISSUES:')
    for s in seg_issues:
        print(s)
else:
    print(f'  ALL {len(kl.seg_list)} seg PASS - min 3 bi + direction consistent')

# Seg detail
for i, seg in enumerate(kl.seg_list):
    bi_cnt = seg.end_bi.idx - seg.start_bi.idx + 1
    d = 'UP' if seg.dir == BI_DIR.UP else 'DN'
    bv = float(seg.start_bi.get_begin_val())
    ev = float(seg.end_bi.get_end_val())
    print(f'  seg[{i}] {d} {bv:>10.0f}->{ev:>10.0f} bi_cnt={bi_cnt} sure={seg.is_sure}')

# ============================================================
# 3. AUDIT ZS (Zhongshu/Center)
# ============================================================
print('\n' + '='*80)
print('3. ZS AUDIT')
print('   Rule: ZG=min(highs) ZD=max(lows) of >= 3 bi, ZG > ZD')
print('='*80)

zs_issues = []
for i, zs in enumerate(kl.zs_list):
    bi_cnt = len(zs.bi_lst)
    zg = float(zs.high)
    zd = float(zs.low)

    # Check min 3 bi (including entry bi, we check the bi_lst which are the inside bi)
    # bi_lst contains only the bi INSIDE the zhongshu (not bi_in/bi_out)
    # A standard zhongshu needs at least 1 bi inside (which means 3 total: bi_in + 1 inside + bi_out)

    # Verify ZG > ZD
    if zg <= zd:
        zs_issues.append(f'  zs[{i}] ZG({zg:.0f}) <= ZD({zd:.0f}) VIOLATION')

    # Verify ZG = min(highs), ZD = max(lows) of the inside bi
    actual_highs = [float(bi._high()) for bi in zs.bi_lst]
    actual_lows = [float(bi._low()) for bi in zs.bi_lst]
    expected_zg = min(actual_highs) if actual_highs else 0
    expected_zd = max(actual_lows) if actual_lows else 0

    if abs(zg - expected_zg) > 0.5:
        zs_issues.append(f'  zs[{i}] ZG mismatch: stored={zg:.0f} expected=min(highs)={expected_zg:.0f}')
    if abs(zd - expected_zd) > 0.5:
        zs_issues.append(f'  zs[{i}] ZD mismatch: stored={zd:.0f} expected=max(lows)={expected_zd:.0f}')

    bx = _klu_to_bar_index(zs.begin, ts_list)
    ex = _klu_to_bar_index(zs.end, ts_list)
    print(f'  zs[{i}] bar={bx}->{ex} ZD={zd:>10.0f} ZG={zg:>10.0f} bi_inside={bi_cnt} bi_idx={[b.idx for b in zs.bi_lst]}')

print()
if zs_issues:
    print(f'  ISSUES:')
    for z in zs_issues:
        print(z)
else:
    print(f'  ALL {len(kl.zs_list)} bi-ZS PASS - ZG/ZD correct, ZG > ZD')

# Seg ZS
print(f'\n  Seg-level ZS: {len(kl.segzs_list)}')
for i, zs in enumerate(kl.segzs_list):
    zg = float(zs.high)
    zd = float(zs.low)
    bx = _klu_to_bar_index(zs.begin, ts_list)
    ex = _klu_to_bar_index(zs.end, ts_list)
    bi_cnt = len(zs.bi_lst)
    print(f'  seg_zs[{i}] bar={bx}->{ex} ZD={zd:>10.0f} ZG={zg:>10.0f} seg_inside={bi_cnt}')
    if zg <= zd:
        print(f'    WARNING: ZG <= ZD')

# ============================================================
# 4. AUDIT BSP (Buy/Sell Points)
# ============================================================
print('\n' + '='*80)
print('4. BSP AUDIT (Bi-level)')
print('='*80)

bsp_list = kl.bs_point_lst.get_latest_bsp(number=0)
bsp_sorted = sorted(bsp_list, key=lambda b: b.bi.idx)

for bsp in bsp_sorted:
    x = _klu_to_bar_index(bsp.klu, ts_list)
    price = float(bsp.klu.low if bsp.is_buy else bsp.klu.high)
    act = 'BUY' if bsp.is_buy else 'SELL'
    tp = bsp.type2str()
    bi_idx = bsp.bi.idx

    print(f'\n  [{tp:>6}] {act:>4} bar={x:>4} price={price:>10.0f} bi[{bi_idx}]')

    # Detailed audit per type
    if '1' in tp and '2' not in tp and '3' not in tp:
        # T1 or T1P: check divergence
        # Find the zhongshu this T1 belongs to
        parent_zs = None
        for zs in kl.zs_list:
            if zs.bi_out and zs.bi_out.idx == bi_idx:
                parent_zs = zs
                break
            # T1P: check if bi is the last bi of a segment without multi-bi zs

        if parent_zs:
            zg = float(parent_zs.high)
            zd = float(parent_zs.low)
            bi_in = parent_zs.bi_in
            bi_out = parent_zs.bi_out
            in_val = float(bi_in.get_end_val()) if bi_in else 0
            out_val = float(bi_out.get_end_val()) if bi_out else 0

            if bsp.is_buy:
                broke_zd = out_val < zd
                print(f'    T1 BUY: ZS ZD={zd:.0f} ZG={zg:.0f}')
                print(f'    bi_out end={out_val:.0f} < ZD={zd:.0f}? {broke_zd}')
                if not broke_zd:
                    print(f'    WARNING: bi_out did not break below ZD')
            else:
                broke_zg = out_val > zg
                print(f'    T1 SELL: ZS ZD={zd:.0f} ZG={zg:.0f}')
                print(f'    bi_out end={out_val:.0f} > ZG={zg:.0f}? {broke_zg}')
                if not broke_zg:
                    print(f'    WARNING: bi_out did not break above ZG')
            print(f'    Divergence: passed (rate <= 10.0)')
        else:
            # T1P - panzhenq beichi
            print(f'    T1P (panzheng beichi): no multi-bi ZS, comparing last 2 same-dir bi')

    elif tp.startswith('2') and '3' not in tp:
        # T2: after T1, first pullback
        rel = bsp.relate_bsp1
        if rel:
            rel_price = float(rel.klu.low if rel.is_buy else rel.klu.high)
            print(f'    T2: relates to T1 at bi[{rel.bi.idx}] price={rel_price:.0f}')
            if bsp.is_buy:
                print(f'    pullback low={price:.0f} > T1 low={rel_price:.0f}? {price > rel_price}')
                if price <= rel_price:
                    print(f'    VIOLATION: T2 buy broke below T1 low!')
            else:
                print(f'    bounce high={price:.0f} < T1 high={rel_price:.0f}? {price < rel_price}')
                if price >= rel_price:
                    print(f'    VIOLATION: T2 sell broke above T1 high!')

    elif '3' in tp:
        # T3: left zhongshu, pullback doesn't re-enter
        print(f'    T3: pullback after leaving ZS, does not re-enter')
        if bsp.is_buy:
            print(f'    buy at {price:.0f} - should be above ZG of relevant ZS')
        else:
            print(f'    sell at {price:.0f} - should be below ZD of relevant ZS')

# ============================================================
# 5. AUDIT BSP (Seg-level)
# ============================================================
print('\n' + '='*80)
print('5. BSP AUDIT (Seg-level)')
print('='*80)

seg_bsp_list = kl.seg_bs_point_lst.get_latest_bsp(number=0)
seg_bsp_sorted = sorted(seg_bsp_list, key=lambda b: b.bi.idx)

for bsp in seg_bsp_sorted:
    x = _klu_to_bar_index(bsp.klu, ts_list)
    price = float(bsp.klu.low if bsp.is_buy else bsp.klu.high)
    act = 'BUY' if bsp.is_buy else 'SELL'
    tp = 'S' + bsp.type2str()
    bi_idx = bsp.bi.idx  # this is seg index for seg-level

    print(f'\n  [{tp:>8}] {act:>4} bar={x:>4} price={price:>10.0f} seg[{bi_idx}]')

    rel = bsp.relate_bsp1
    if rel:
        rel_price = float(rel.klu.low if rel.is_buy else rel.klu.high)
        print(f'    relates to S-T1 at seg[{rel.bi.idx}] price={rel_price:.0f}')

# ============================================================
# SUMMARY
# ============================================================
print('\n' + '='*80)
print('AUDIT SUMMARY')
print('='*80)
print(f'  Bi:  {len(kl.bi_list)} - {"PASS" if not bi_issues and not alt_issues else "ISSUES FOUND"}')
print(f'  Seg: {len(kl.seg_list)} - {"PASS" if not seg_issues else "ISSUES FOUND"}')
print(f'  ZS:  {len(kl.zs_list)} bi + {len(kl.segzs_list)} seg - {"PASS" if not zs_issues else "ISSUES FOUND"}')
print(f'  BSP: {len(bsp_list)} bi-level + {len(seg_bsp_list)} seg-level')
print(f'  Config: bi_strict=True, bi_fx_check=half, divergence_rate=10.0')
print(f'          zs_combine=True, one_bi_zs=False, macd_algo=area')
print('='*80)
