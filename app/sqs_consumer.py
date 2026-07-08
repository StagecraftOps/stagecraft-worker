import json
import logging
import time

import boto3

from app.core.config import settings
from app.core.health import mark_ready, start_health_server
from app.tasks.dependency_graph import build_dependency_graph_task
from app.tasks.remediation import (
    backfill_org_runs_task,
    process_failed_workflow,
    register_app_installation_task,
    upsert_workflow_run_task,
)
from app.tasks.governance import extract_governance_requirements_task, run_governance_analysis_task
from app.tasks.knowledge_graph import build_knowledge_graph_task
from app.tasks.optimization import run_optimization_analysis_task
from app.tasks.pr_review import process_pull_request
from app.tasks.standardization import run_pattern_frequency_task, run_template_diff_task
from app.tasks.drift_detection import run_drift_detection_task
from app.tasks.vulnerability import backfill_finding_package_names_task, run_vulnerability_remediation_task
from app.tasks.vulnerability_remediation import (
    publish_vulnerability_agent_task,
    run_agentic_remediation_task,
    run_copilot_remediation_task,
    run_vulnerability_dependency_fix_task,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_POLL_WAIT_SECONDS = 20
_VISIBILITY_TIMEOUT = 60

def _dispatch(message: dict) -> None:
    event_type = message.get("event_type")

    if event_type == "workflow_run":
        if message.get("requires_analysis"):
            process_failed_workflow.delay(message)
        else:
            upsert_workflow_run_task.delay(message)
        return

    if event_type == "backfill_org":
        backfill_org_runs_task.delay(message["org_login"])
        return

    if event_type == "app_installation":
        register_app_installation_task.delay(message)
        return

    if event_type == "build_dependency_graph":
        build_dependency_graph_task.delay(message)
        return

    if event_type == "run_template_diff":
        run_template_diff_task.delay(message)
        return

    if event_type == "run_pattern_frequency":
        run_pattern_frequency_task.delay(message)
        return

    if event_type == "run_drift_detection":
        run_drift_detection_task.delay(message)
        return

    if event_type == "security_alert":
        run_vulnerability_remediation_task.delay(message)
        return

    if event_type == "run_vulnerability_dependency_fix":
        run_vulnerability_dependency_fix_task.delay(message)
        return

    if event_type == "publish_vulnerability_agent":
        publish_vulnerability_agent_task.delay(message)
        return

    if event_type == "run_agentic_remediation":
        run_agentic_remediation_task.delay(message)
        return

    if event_type == "run_copilot_remediation":
        run_copilot_remediation_task.delay(message)
        return

    if event_type == "backfill_finding_package_names":
        backfill_finding_package_names_task.delay(message)
        return

    if event_type == "pull_request":
        process_pull_request.delay(message)
        return

    if event_type == "extract_governance_requirements":
        extract_governance_requirements_task.delay(message)
        return

    if event_type == "run_governance_analysis":
        run_governance_analysis_task.delay(message)
        return

    if event_type == "run_optimization_analysis":
        run_optimization_analysis_task.delay(message)
        return

    if event_type == "build_knowledge_graph":
        build_knowledge_graph_task.delay(message)
        return

    logger.warning("Unknown event_type in SQS message, dropping: %r", event_type)

def run() -> None:
    start_health_server(port=8080)

    client = boto3.client("sqs", region_name=settings.AWS_REGION)
    queue_url = settings.SQS_QUEUE_URL
    logger.info("Starting SQS consumer, polling %s", queue_url)

    mark_ready()

    while True:
        try:
            response = client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=_POLL_WAIT_SECONDS,
                VisibilityTimeout=_VISIBILITY_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("SQS receive_message failed (no AWS creds?): %s", exc)
            time.sleep(30)
            continue

        for sqs_message in response.get("Messages", []):
            receipt_handle = sqs_message["ReceiptHandle"]
            try:
                body = json.loads(sqs_message["Body"])
                _dispatch(body)
            except Exception as exc:
                logger.exception("Failed to process SQS message, dropping: %s", exc)
            finally:
                client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)

if __name__ == "__main__":
    run()
