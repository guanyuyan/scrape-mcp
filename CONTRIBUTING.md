# Contributing

感谢你对 scrape-mcp 的关注！以下是参与本项目的约定。

## 环境准备

```bash
cd scrape-mcp
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"          # 含 pytest / ruff / pre-commit
pre-commit install               # 安装 git 钩子
```

## 开发流程

1. 从 `main` 切新分支：`git checkout -b feat/your-feature`
2. 修改代码，风格遵循 ruff（`ruff check .`、`ruff format .` 零告警）
3. 补对应测试到 `tests/`，`pytest -q` 全绿
4. 更新 `CHANGELOG.md`（`Unreleased` 段）
5. 提交前确保 `pre-commit` 通过，push 后 CI 需绿

## 代码风格

- 遵循 ruff 默认规则，行宽 100，双引号
- 类型注解完整（项目目标 Python 3.10+）
- 纯函数优先，副作用集中在调用入口

## 提交信息

建议遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：
`feat:`、`fix:`、`docs:`、`chore:`、`refactor:` 等前缀。

## 发版流程

项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，避免长期停留在一个版本，
保持稳定的小步快跑节奏（如每月一次 v0.x）：

1. 合并目标改动到 `main`，确保 `pytest -q` 与 `ruff check .` 全绿
2. `CHANGELOG.md` 从 `Unreleased` 摘要生成对应版本的 `Added/Fixed/Changed` 段
3. 打 tag：`git tag v0.x.x && git push --tags`
4. 在 GitHub 用该 tag 创建 Release，把该版 CHANGELOG 段复制进 release notes

## 反爬与合规

本项目用于"让 AI 读取网页正文改善效率"的正当目的。请勿利用其绕开付费墙或授权控制。
参见 [SECURITY.md](SECURITY.md)。