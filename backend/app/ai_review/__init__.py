"""AI Reviewer（代码评审）模块。

- ``platform_client``：GitHub / GitLab REST 封装（拉 PR/MR 列表与详情）。
- ``runner``：克隆仓库并调用 ocr CLI（alibaba/open-code-review）执行评审。
- ``jobs``：rq 队列入口与降级入队（与 knowledge.ingest_jobs 同一套模板）。
"""
