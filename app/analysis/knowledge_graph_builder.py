"""FR-11: builds the org-wide knowledge graph by cross-linking governance
findings, remediation failures, and optimization recommendations into
graph_type='knowledge' rows that reference graph_type='dependency' node ids
directly — same graph_nodes/graph_edges tables from FR-1, no separate store.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session


def _find_dependency_workflow_node(session: Session, org_login: str, repo_name: str, workflow_file: str) -> uuid.UUID | None:
    """Look up the workflow node from the latest completed dependency graph for this repo."""
    row = session.execute(
        text(
            """
            SELECT gn.id FROM graph_nodes gn
            JOIN graphs g ON g.id = gn.graph_id
            WHERE g.org_login = :org AND g.repo_name = :repo AND g.graph_type = 'dependency'
              AND g.status = 'completed' AND gn.node_type = 'workflow' AND gn.workflow_file = :wf
            ORDER BY g.built_at DESC LIMIT 1
            """
        ),
        {"org": org_login, "repo": repo_name, "wf": workflow_file},
    ).fetchone()
    return row[0] if row else None


def _upsert_node(session: Session, graph_id: uuid.UUID, node_type: str, external_key: str, display_name: str) -> uuid.UUID:
    existing = session.execute(
        text("SELECT id FROM graph_nodes WHERE graph_id = :gid AND node_type = :ntype AND external_key = :key"),
        {"gid": str(graph_id), "ntype": node_type, "key": external_key},
    ).fetchone()
    if existing:
        return existing[0]

    node_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO graph_nodes (id, graph_id, node_type, external_key, display_name, created_at)
            VALUES (:id, :gid, :ntype, :key, :name, :now)
            """
        ),
        {
            "id": str(node_id), "gid": str(graph_id), "ntype": node_type,
            "key": external_key, "name": display_name, "now": datetime.now(timezone.utc),
        },
    )
    return node_id


def _add_edge(session: Session, graph_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID, edge_type: str) -> None:
    session.execute(
        text(
            """
            INSERT INTO graph_edges (id, graph_id, source_node_id, target_node_id, edge_type, confidence, created_at)
            VALUES (:id, :gid, :src, :tgt, :etype, 'certain', :now)
            """
        ),
        {
            "id": str(uuid.uuid4()), "gid": str(graph_id), "src": str(source_id), "tgt": str(target_id),
            "etype": edge_type, "now": datetime.now(timezone.utc),
        },
    )


def build_knowledge_graph(session: Session, org_login: str) -> tuple[uuid.UUID, int, int]:
    """Build/refresh the org's knowledge graph. Returns (graph_id, node_count, edge_count)."""
    now = datetime.now(timezone.utc)
    graph_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO graphs (id, org_login, repo_name, graph_type, status, node_count, edge_count, built_at, created_at, updated_at)
            VALUES (:id, :org, NULL, 'knowledge', 'building', 0, 0, :now, :now, :now)
            """
        ),
        {"id": str(graph_id), "org": org_login, "now": now},
    )

    node_count = 0
    edge_count = 0

    # governance_rule nodes <-governs- compliance findings, linked to the dependency graph's workflow node
    findings = session.execute(
        text(
            """
            SELECT repo_name, workflow_file, requirement_id, status, severity
            FROM compliance_findings WHERE org_login = :org
            """
        ),
        {"org": org_login},
    ).fetchall()
    for repo_name, workflow_file, requirement_id, status, severity in findings:
        rule_node = _upsert_node(session, graph_id, "governance_rule", f"governance_rule::{requirement_id}", requirement_id)
        node_count += 1
        workflow_node = _find_dependency_workflow_node(session, org_login, repo_name, workflow_file)
        if workflow_node:
            _add_edge(session, graph_id, rule_node, workflow_node, "governs")
            edge_count += 1

    # failure nodes <-caused_by- remediations, linked to the dependency graph's workflow node
    remediations = session.execute(
        text(
            """
            SELECT id, repo_name, workflow_file, failure_category, root_cause
            FROM remediations WHERE org_login = :org AND failure_category IS NOT NULL
            """
        ),
        {"org": org_login},
    ).fetchall()
    for remediation_id, repo_name, workflow_file, failure_category, root_cause in remediations:
        display_name = failure_category or (root_cause[:60] if root_cause else "Unclassified failure")
        failure_node = _upsert_node(
            session, graph_id, "failure", f"failure::{remediation_id}", display_name
        )
        node_count += 1
        workflow_node = _find_dependency_workflow_node(session, org_login, repo_name, workflow_file)
        if workflow_node:
            _add_edge(session, graph_id, failure_node, workflow_node, "caused_by")
            edge_count += 1

    # runtime_metric nodes <-measured_by- optimization recommendations, linked to the workflow node
    recommendations = session.execute(
        text(
            """
            SELECT id, repo_name, workflow_file, recommendation_type, estimated_time_savings_seconds
            FROM optimization_recommendations WHERE org_login = :org
            """
        ),
        {"org": org_login},
    ).fetchall()
    for rec_id, repo_name, workflow_file, rec_type, savings in recommendations:
        metric_node = _upsert_node(
            session, graph_id, "runtime_metric", f"runtime_metric::{rec_id}", f"{rec_type} ({savings}s savings)"
        )
        node_count += 1
        workflow_node = _find_dependency_workflow_node(session, org_login, repo_name, workflow_file)
        if workflow_node:
            _add_edge(session, graph_id, metric_node, workflow_node, "measured_by")
            edge_count += 1

    session.execute(
        text(
            """
            UPDATE graphs SET status = 'completed', node_count = :nc, edge_count = :ec, built_at = :now, updated_at = :now
            WHERE id = :id
            """
        ),
        {"id": str(graph_id), "nc": node_count, "ec": edge_count, "now": now},
    )
    session.commit()

    return graph_id, node_count, edge_count
