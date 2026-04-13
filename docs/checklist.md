# 提交前检查清单

每次 git commit 之前对照检查一遍。

## 功能层面
- [ ] 代码能跑起来（python run.py 不报错）
- [ ] 新改的功能实际测试过
- [ ] 没有破坏已有功能

## 代码层面
- [ ] 保存时自动格式化过（或手动跑 `ruff format backend/ run.py`）
- [ ] 没有 console.log / print / debugger 调试残留
- [ ] 没有硬编码的密码、key、token
- [ ] 没有 TODO 但不加说明的地方
- [ ] 改了 frontend/js/chart.js 的话，index.html 的 `?v=N` 已递增

## 配置层面
- [ ] 新增的环境变量加进 .env.example
- [ ] 新增的依赖加进 requirements.txt
- [ ] .gitignore 不需要更新

## 文档层面
- [ ] README 是否需要更新
- [ ] CHANGELOG 加一条
- [ ] 修了 bug：docs/ai/lessons.md 加一条
- [ ] 做了重要决策：docs/decisions.md 加一条

## Git 层面
- [ ] commit 信息写清楚（做了什么、为什么），用中文
- [ ] 一次 commit 只做一件事（不要混功能+格式化+重构）
