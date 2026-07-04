from celery import Celery

# Every task module must be listed here so the Celery worker process registers
# them — the worker entrypoint (app/main.py) only imports remediation directly,
# so without this the others (e.g. job_timing) raise KeyError when a task tries
# to .delay() them. (The SQS consumer imports them separately, in its own process.)
app = Celery(
    "remediation-worker",
    include=[
        "app.tasks.remediation",
        "app.tasks.dependency_graph",
        "app.tasks.job_timing",
        "app.tasks.standardization",
        "app.tasks.pr_review",
        "app.tasks.governance",
        "app.tasks.optimization",
        "app.tasks.knowledge_graph",
    ],
)
app.config_from_object("app.core.celery_config")
