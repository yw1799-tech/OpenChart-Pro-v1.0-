# AI 不可违反的铁律

## 绝对禁止

1. **不要修改 `chanlun_engine/` 下除 `chanlun_service.py` 以外的任何文件** — 那是 chan.py 第三方引擎源码
2. **不要把 API Key、密码、token 写进代码** — 所有敏感信息走 .env 或数据库配置
3. **不要自动部署到服务器** — 所有部署操作等用户明确指示
4. **不要删除用户的数据文件**（data/*.db）— 里面有配置和历史数据
5. **不要在 `_ts_to_ctime` 里用 `utcfromtimestamp`** — 必须用 `fromtimestamp`（本地时间），否则所有缠论指标偏移 8 小时

## 必须遵守

1. 修改 `frontend/js/chart.js` 后，**必须递增** `index.html` 里的 `?v=N` 版本号
2. 修改后端代码后，告诉用户重启服务器
3. 所有解释、代码注释、docstring、commit message 全部用中文
4. 改代码前先说明改什么、为什么、有什么风险
5. 每次改完告诉用户怎么验证

## 已踩过的坑（绝不能再犯）

- `datetime.utcfromtimestamp` vs `datetime.fromtimestamp`：前者导致 CTime 内部时间戳偏移 8 小时，所有缠论笔/线段/买卖点偏移 8 根 K 线
- `_find_extreme_bar` 用 ±15 搜索范围：会把笔端点偏移到错误的 K 线上，应该直接用时间戳匹配
- `divergence_rate=9999`：完全跳过背驰检测，一类买卖点变成假信号
