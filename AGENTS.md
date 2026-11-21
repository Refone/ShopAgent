# AGENTS.md

## 工程约定

- 不要擅自添加测试用例。
- 不要擅自修改任何 markdown 文档。若需补充，可以在会话中建议。
- 如果需要修改代码，请列出修改文件，主动寻求许可。
- 修改代码时
  - 如果工作区或是暂存区有未提交的更改，则直接增量更新工作区，不要提交 commit。
  - 如果工作区和暂存区都干净，则直接提交 commit。

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
