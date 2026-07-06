import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.neo4j_client import get_driver

_KNOWLEDGE_LABELS = {
    "governance_rule": "GovernanceRule",
    "failure": "Failure",
    "runtime_metric": "RuntimeMetric",
}
_KNOWLEDGE_RELS = {
    "governs": "GOVERNS",
    "caused_by": "CAUSED_BY",
    "measured_by": "MEASURED_BY",
}

def _failure_display_name(failure_category: str | None, root_cause: str | None) -> str:
    has_specific_category = bool(failure_category) and failure_category.strip().upper() != "UNKNOWN"
    if has_specific_category:
        return failure_category
    if root_cause:
        return root_cause[:60]
    return "Unclassified failure"

def _clear_knowledge_nodes_tx(tx, org_login: str) -> None:
    tx.run(
        """
        MATCH (n:GraphNode {org_login: $org})
        WHERE n.node_type IN ['governance_rule', 'failure', 'runtime_metric']
        DETACH DELETE n
        """,
        org=org_login,
    )

def _upsert_knowledge_node_and_edge_tx(
    tx, org_login: str, node_type: str, external_key: str, display_name: str,
    repo_name: str, workflow_file: str, rel_type: str,
) -> None:
    label = _KNOWLEDGE_LABELS[node_type]
    rel = _KNOWLEDGE_RELS[rel_type]
    identity = f"{org_login}::::{external_key}"
    tx.run(
        f"""
        MERGE (n:GraphNode:{label} {{identity_key: $identity}})
        ON CREATE SET n.id = randomUUID(), n.created_at = datetime()
        SET n.org_login = $org, n.node_type = $node_type, n.external_key = $ekey,
            n.display_name = $dname, n.updated_at = datetime()
        WITH n
        MATCH (w:GraphNode:Workflow {{org_login: $org, repo_name: $repo, workflow_file: $wf}})
        MERGE (n)-[e:{rel}]->(w)
        ON CREATE SET e.id = randomUUID(), e.created_at = datetime()
        SET e.confidence = 'certain', e.updated_at = datetime(), e.org_login = $org
        """,
        identity=identity, org=org_login, node_type=node_type, ekey=external_key,
        dname=display_name, repo=repo_name, wf=workflow_file,
    )

def _find_dependency_workflow_node(session: Session, org_login: str, repo_name: str, workflow_file: str) -> uuid.UUID | None:
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

    neo_session = None
    if settings.GRAPH_DUAL_WRITE_NEO4J:
        neo_session = get_driver().session()
        neo_session.execute_write(_clear_knowledge_nodes_tx, org_login)

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
        if neo_session:
            neo_session.execute_write(
                _upsert_knowledge_node_and_edge_tx, org_login, "governance_rule",
                f"governance_rule::{requirement_id}", requirement_id, repo_name, workflow_file, "governs",
            )

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
        display_name = _failure_display_name(failure_category, root_cause)
        failure_node = _upsert_node(
            session, graph_id, "failure", f"failure::{remediation_id}", display_name
        )
        node_count += 1
        workflow_node = _find_dependency_workflow_node(session, org_login, repo_name, workflow_file)
        if workflow_node:
            _add_edge(session, graph_id, failure_node, workflow_node, "caused_by")
            edge_count += 1
        if neo_session:
            neo_session.execute_write(
                _upsert_knowledge_node_and_edge_tx, org_login, "failure",
                f"failure::{remediation_id}", display_name, repo_name, workflow_file, "caused_by",
            )

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
        metric_display_name = f"{rec_type} ({savings}s savings)"
        metric_node = _upsert_node(
            session, graph_id, "runtime_metric", f"runtime_metric::{rec_id}", metric_display_name
        )
        node_count += 1
        workflow_node = _find_dependency_workflow_node(session, org_login, repo_name, workflow_file)
        if workflow_node:
            _add_edge(session, graph_id, metric_node, workflow_node, "measured_by")
            edge_count += 1
        if neo_session:
            neo_session.execute_write(
                _upsert_knowledge_node_and_edge_tx, org_login, "runtime_metric",
                f"runtime_metric::{rec_id}", metric_display_name, repo_name, workflow_file, "measured_by",
            )

    if neo_session:
        neo_session.close()

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
