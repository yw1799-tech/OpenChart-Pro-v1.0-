"""全周期买卖点审计 — 1m/5m/15m/30m/1H/4H/1D/1W"""
import sys, requests
sys.path.insert(0, '.')
from chanlun_service import analyze

timeframes = [
    ('1m', 500), ('5m', 500), ('15m', 500), ('30m', 500),
    ('1H', 1000), ('4H', 500), ('1D', 500), ('1W', 500),
]

for tf, limit in timeframes:
    try:
        resp = requests.get('http://localhost:8000/api/klines',
            params={'symbol':'BTC-USDT','interval':tf,'limit':limit,'market':'crypto'}, timeout=30)
        candles = resp.json()['candles']
        data = analyze(candles)
        buys = [b for b in data['bsp_list'] if b['is_buy']]
        sells = [b for b in data['bsp_list'] if not b['is_buy']]
        bi = len(data['bi_list'])
        seg = len(data['seg_list'])
        zs = len(data['zs_list'])

        # 检查异常
        issues = []
        if len(buys) == 0 and len(sells) > 0:
            issues.append('NO BUY POINTS')
        if len(sells) == 0 and len(buys) > 0:
            issues.append('NO SELL POINTS')
        if bi < 3:
            issues.append(f'TOO FEW BI ({bi})')
        if zs == 0 and bi >= 10:
            issues.append('NO ZHONGSHU')

        # 检查买卖点价格合理性
        for b in buys:
            if b['y'] <= 0:
                issues.append(f'BUY price <= 0: {b["y"]}')
        for s in sells:
            if s['y'] <= 0:
                issues.append(f'SELL price <= 0: {s["y"]}')

        # 检查买点是否都在低位、卖点在高位（基本合理性）
        all_prices = [b['y'] for b in buys] + [s['y'] for s in sells]
        if all_prices:
            mid = (max(all_prices) + min(all_prices)) / 2
            high_buys = [b for b in buys if b['y'] > mid * 1.3]
            low_sells = [s for s in sells if s['y'] < mid * 0.7]
            # 不标为issue，但记录异常分布

        status = 'PASS' if not issues else 'ISSUES'
        print(f'{tf:>3} | K={len(candles):>4} | bi={bi:>2} seg={seg:>2} zs={zs:>2} | buys={len(buys):>2} sells={len(sells):>2} | {status}')
        if issues:
            for iss in issues:
                print(f'      !!! {iss}')

        # 列出买卖点
        for b in sorted(buys, key=lambda x: x['x']):
            seg_flag = '(seg)' if b['type'].startswith('S') else ''
            print(f'      BUY [{b["type"]:>6}]{seg_flag} bar={b["x"]:>4} price={b["y"]:>10.1f}')
        for s in sorted(sells, key=lambda x: x['x']):
            seg_flag = '(seg)' if s['type'].startswith('S') else ''
            print(f'      SELL [{s["type"]:>6}]{seg_flag} bar={s["x"]:>4} price={s["y"]:>10.1f}')
        print()

    except Exception as e:
        print(f'{tf:>3} | ERROR: {e}')
        print()
