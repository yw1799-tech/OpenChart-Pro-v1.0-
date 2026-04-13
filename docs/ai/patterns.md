# 常用代码模式

> 项目里反复出现的套路，记在这里，下次 AI 能直接参考。

## 模式 1：新增一个后端 API 路由
- **何时用**：需要新增后端接口
- **参考文件**：`backend/main.py` 任意一个 `@xxx_router.get/post` 定义
- **步骤**：
  1. 在 main.py 找到对应的 router（如 chanlun_router）
  2. 加 `@router.get/post("/path")` + async def
  3. 返回 dict 或 JSONResponse
- **注意**：main.py 已经很大了（2767行），未来应拆分

## 模式 2：新增一个前端指标
- **何时用**：需要在K线图上叠加新的可视化
- **参考文件**：`frontend/js/chart.js` 中 CHANLUN 指标的注册方式
- **步骤**：
  1. 用 `chart.registerIndicator({name, draw, calc})` 注册自定义指标
  2. draw 函数里用 canvas ctx 直接画
  3. `chart.createIndicator(name, true, {id: 'candle_pane'})` 添加到主图
- **注意**：改完 chart.js 必须递增 index.html 的 `?v=N`

## 模式 3：缠论参数调整
- **何时用**：缠论买卖点不准、不够、太多
- **参考文件**：`backend/chanlun_engine/chanlun_service.py` 的 CChanConfig
- **步骤**：
  1. 改 config 参数
  2. 用 `chanpy_audit.py` 对比 chan.py 原生输出是否一致
  3. 用 `full_audit.py` 跑全周期验证
  4. 在图表上肉眼检查
- **注意**：不要改 chanlun_engine/ 下除 chanlun_service.py 以外的文件

## 模式 4：前端缓存刷新
- **何时用**：改了 JS 文件但浏览器不生效
- **参考文件**：`frontend/index.html`
- **步骤**：找到 `<script src="js/xxx.js?v=N">`，把 N 加 1
- **注意**：只有 chart.js 改动最频繁，其他 JS 文件少改
