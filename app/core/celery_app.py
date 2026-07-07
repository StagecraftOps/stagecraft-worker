from celery import Celery

app = Celery(
    "remediation-worker",
    include=[
        "app.tasks.remediation",
        "app.tasks.dependency_graph",
        "app.tasks.job_timing",
        "app.tasks.standardization",
        "app.tasks.drift_detection",
        "app.tasks.vulnerability",
        "app.tasks.vulnerability_remediation",
        "app.tasks.pr_review",
        "app.tasks.governance",
        "app.tasks.optimization",
        "app.tasks.knowledge_graph",
    ],
)
app.config_from_object("app.core.celery_config")
