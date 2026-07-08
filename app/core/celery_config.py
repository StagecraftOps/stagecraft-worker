import ssl

from app.core.config import settings

broker_url = settings.REDIS_URL
result_backend = settings.REDIS_URL

if broker_url.startswith("rediss://"):
    broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE}
    redis_backend_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE}

task_serializer = "json"
accept_content = ["json"]
result_serializer = "json"

timezone = "UTC"
enable_utc = True

task_routes = {
    "app.tasks.remediation.process_failed_workflow": {"queue": "remediation"},
    "app.tasks.remediation.upsert_workflow_run_task": {"queue": "remediation"},
    "app.tasks.remediation.backfill_org_runs_task": {"queue": "remediation"},
    "app.tasks.remediation.register_app_installation_task": {"queue": "remediation"},
    "app.tasks.remediation.backfill_embeddings_task": {"queue": "remediation"},
    "app.tasks.remediation.run_code_level_fix_task": {"queue": "remediation"},
    "app.tasks.dependency_graph.build_dependency_graph_task": {"queue": "remediation"},
    "app.tasks.job_timing.sync_job_timings_task": {"queue": "remediation"},
    "app.tasks.standardization.run_template_diff_task": {"queue": "remediation"},
    "app.tasks.standardization.run_pattern_frequency_task": {"queue": "remediation"},
    "app.tasks.pr_review.process_pull_request": {"queue": "remediation"},
    "app.tasks.governance.extract_governance_requirements_task": {"queue": "remediation"},
    "app.tasks.governance.run_governance_analysis_task": {"queue": "remediation"},
    "app.tasks.optimization.run_optimization_analysis_task": {"queue": "remediation"},
    "app.tasks.knowledge_graph.build_knowledge_graph_task": {"queue": "remediation"},
    "app.tasks.drift_detection.run_drift_detection_task": {"queue": "remediation"},
    "app.tasks.vulnerability.run_vulnerability_remediation_task": {"queue": "remediation"},
    "app.tasks.vulnerability_remediation.run_vulnerability_dependency_fix_task": {"queue": "remediation"},
    "app.tasks.vulnerability_remediation.publish_vulnerability_agent_task": {"queue": "remediation"},
    "app.tasks.vulnerability_remediation.run_agentic_remediation_task": {"queue": "remediation"},
    "app.tasks.vulnerability_remediation.run_copilot_remediation_task": {"queue": "remediation"},
    "app.tasks.vulnerability_remediation.poll_copilot_task_result": {"queue": "remediation"},
    "app.tasks.vulnerability.backfill_finding_package_names_task": {"queue": "remediation"},
}

task_acks_late = True
task_reject_on_worker_lost = True
worker_prefetch_multiplier = 1

broker_transport_options = {"visibility_timeout": 5400}

result_expires = 86400
