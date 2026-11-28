# AGENTS.md

## 工程约定

- 不要擅自添加测试用例。
- 不要擅自修改任何 markdown 文档。若需补充，可以在会话中建议。
- 如果需要修改代码，请列出修改文件，主动寻求许可。
- 修改代码时
  - 如果工作区或是暂存区有未提交的更改，则直接增量更新工作区，不要提交 commit。
  - 如果工作区和暂存区都干净，则直接提交 commit。
- 默认直接在当前仓库、当前分支和当前工作区中工作，不要擅自创建或切换 Git Worktree，也不要擅自切换分支，除非用户明确要求。
- 开发过程的关键中间产物，可存放于 `var/output` 目录。
- 开发过程中的非关键中间产物，应存放于 `var/tmp` 目录。

## 格式化

- 首次使用：在仓库根目录运行 `npm install`，并运行 `pre-commit install`。
- 提交前运行 `pre-commit run --all-files`；不要使用 `git commit --no-verify`。
- 前端使用 Prettier（根目录 `.prettierrc.json`），可在仓库根目录运行 `npm run format`。
- 后端 Python 使用 Ruff；ecommerce-service 行宽 100，customer-service 行宽 88。

## 项目约定

- 数据库中有的表同时拥有 `id` 和 `uuid` 字段，`id` 是自增主键，`uuid` 是全局唯一标识符。
  - `id` 字段用于数据库内部的关联和查询，性能更高。
  - `uuid` 字段用于对外暴露，保证全局唯一性。

## 提交 commit 规范

- 如果和上一个未 push 的 commit 相关性很大，可以使用 `--amend` 选项修改上一个 commit，而不是创建新 commit。
- commit message 须包含会话中的提示词。
- 所有包含 AI 辅助的提交，必须在提交信息末尾添加以下 trailer：

  `Assisted-by: <Tool Name> (<Model ID>)`

  例如：

  `Assisted-by: Cursor (GPT-5.6-terra)`

  `Assisted-by: GitHub Copilot (Claude Sonnet 4.5)`

## 关于文档

- 以下文档中的内容与工程无关，避免从中寻找信息。
  - `docs/notes`: 开发笔记

## 日志

- 尽量使用自定义日志模块(LOG)记录日志，而非直接使用 print。
- 日志模版统一使用英文。
- 日志格式化统一使用 f-string。

## 注释

- 解释型注释使用中文，便于开发者理解代码意图。
- 代码逻辑注释，如字段含义、函数功能等使用英文，便于 AI 理解代码逻辑。
