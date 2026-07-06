import json
import logging
import uuid
from datetime import datetime, timezone

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.analysis.composite_action_resolver import resolve_runtime
from app.analysis.dispatch_detector import find_dispatch_edges
from app.analysis.orchestrator_parser import parse_orchestrator
from app.analysis.workflow_parser import parse_workflow
from app.core.config import settings
from app.services.github_client import GitHubRemediationClient
from app.services.neo4j_client import get_driver

logger = logging.getLogger(__name__)

_REPO_SCOPED_TYPES = {"workflow", "job", "composite_action"}

_ORG_WIDE_DECLARABLE_TYPES = {"service", "external_repo"}

_NODE_LABELS = {
    "workflow": "Workflow",
    "job": "Job",
    "reusable_workflow": "ReusableWorkflow",
    "composite_action": "CompositeAction",
    "service": "Service",
    "external_repo": "ExternalRepo",
}

_REL_TYPES = {
    "needs": "NEEDS",
    "needs_output": "NEEDS_OUTPUT",
    "matrix_fanout": "MATRIX_FANOUT",
    "uses_reusable": "USES_REUSABLE",
    "uses_composite": "USES_COMPOSITE",
    "orchestrator_service_dep": "ORCHESTRATOR_SERVICE_DEP",
    "repository_dispatch": "REPOSITORY_DISPATCH",
    "workflow_run_trigger": "WORKFLOW_RUN_TRIGGER",
}

def _identity_key(org_login: str, node_type: str, external_key: str, repo_name: str) -> str:
    scope = repo_name if node_type in _REPO_SCOPED_TYPES else ""
    return f"{org_login}::{scope}::{external_key}"

def _list_workflow_files(tree: list[dict]) -> list[str]:
    return [
        entry["path"]
        for entry in tree
        if entry.get("type") == "blob"
        and entry.get("path", "").startswith(".github/workflows/")
        and entry["path"].endswith((".yml", ".yaml"))
    ]

def _find_repo_file(tree: list[dict], name: str) -> str | None:
    for entry in tree:
        if entry.get("type") == "blob" and entry.get("path") == name:
            return name
    return None

def _workflow_display_name(content: str, fallback: str) -> str:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return fallback
    if isinstance(doc, dict) and isinstance(doc.get("name"), str):
        return doc["name"]
    return fallback

def _workflow_run_trigger_names(content: str) -> list[str]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    on_block = doc.get("on") or doc.get(True) or {}
    if not isinstance(on_block, dict):
        return []
    workflow_run = on_block.get("workflow_run")
    if not isinstance(workflow_run, dict):
        return []
    names = workflow_run.get("workflows")
    if isinstance(names, str):
        return [names]
    if isinstance(names, list):
        return [n for n in names if isinstance(n, str)]
    return []

def build_graph_data(
    github: GitHubRemediationClient, owner: str, repo: str, ref: str
) -> tuple[list[dict], list[dict]]:
    tree = github.get_repo_tree(owner, repo, ref)
    tree_paths = {e["path"] for e in tree if e.get("type") == "blob"}

    all_nodes: list[dict] = []
    all_edges: list[dict] = []

    workflow_files = _list_workflow_files(tree)
    file_contents: dict[str, str] = {}
    name_to_file: dict[str, str] = {}

    for path in workflow_files:
        content = github.get_file_content(owner, repo, path, ref)
        if content is None:
            continue
        file_contents[path] = content
        name_to_file[_workflow_display_name(content, path)] = path

        nodes, edges = parse_workflow(path, content)
        all_nodes.extend(nodes)
        all_edges.extend(edges)

        dispatch_nodes, dispatch_edges = find_dispatch_edges(path, content)
        all_nodes.extend(dispatch_nodes)
        all_edges.extend(dispatch_edges)

    for path, content in file_contents.items():
        for trigger_name in _workflow_run_trigger_names(content):
            source_file = name_to_file.get(trigger_name)
            if source_file:
                all_edges.append({
                    "source_key": f"workflow::{source_file}",
                    "target_key": f"workflow::{path}",
                    "edge_type": "workflow_run_trigger",
                    "confidence": "certain",
                    "metadata": {"matched_by": "name", "workflow_name": trigger_name},
                })

    orchestrator_path = _find_repo_file(tree, "orchestrator.yaml")
    if orchestrator_path:
        content = github.get_file_content(owner, repo, orchestrator_path, ref)
        if content:
            nodes, edges = parse_orchestrator(content)
            all_nodes.extend(nodes)
            all_edges.extend(edges)

    service_config: dict = {}
    config_path = _find_repo_file(tree, "service-config.json")
    if config_path:
        raw = github.get_file_content(owner, repo, config_path, ref)
        if raw:
            try:
                service_config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                service_config = {}

    for edge in all_edges:
        if edge["edge_type"] != "uses_composite" or edge["confidence"] != "ambiguous":
            continue
        step_with = (edge.get("metadata") or {}).get("with") or {}
        service_path = step_with.get("path") if isinstance(step_with, dict) else None
        runtime, confidence = resolve_runtime(service_path, service_config, tree_paths)
        if runtime:
            edge["confidence"] = confidence
            edge["metadata"] = {**(edge.get("metadata") or {}), "resolved_runtime": runtime}

    deduped: dict[str, dict] = {}
    for node in all_nodes:
        key = node["external_key"]
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = node
            continue
        existing_is_placeholder = bool((existing.get("metadata") or {}).get("placeholder_reusable_ref"))
        node_is_placeholder = bool((node.get("metadata") or {}).get("placeholder_reusable_ref"))
        if existing_is_placeholder and not node_is_placeholder:
            deduped[key] = node

    return list(deduped.values()), all_edges

def _write_dependency_subgraph_tx(tx, org_login: str, repo_name: str, nodes: list[dict], edges: list[dict]) -> None:
    tx.run(
        """
        MATCH (n:GraphNode {org_login: $org, repo_name: $repo})
        WHERE n.node_type IN ['workflow', 'job', 'reusable_workflow', 'composite_action']
        DETACH DELETE n
        """,
        org=org_login, repo=repo_name,
    )

    key_to_identity: dict[str, str] = {}
    for node in nodes:
        node_type = node["node_type"]
        if node_type not in _NODE_LABELS:
            raise ValueError(f"Unknown node_type for Neo4j write: {node_type!r}")
        label = _NODE_LABELS[node_type]
        identity = _identity_key(org_login, node_type, node["external_key"], repo_name)
        key_to_identity[node["external_key"]] = identity

        node_repo_name = repo_name if node_type in _REPO_SCOPED_TYPES else None
        if node_type in _ORG_WIDE_DECLARABLE_TYPES:

            tx.run(
                f"""
                MERGE (n:GraphNode:{label} {{identity_key: $identity}})
                ON CREATE SET n.id = randomUUID(), n.created_at = datetime(), n.declared_by_repos = []
                SET n.org_login = $org, n.repo_name = $repo, n.node_type = $node_type,
                    n.external_key = $ekey, n.display_name = $dname,
                    n.workflow_file = $wf, n.job_id = $jid, n.metadata_json = $meta,
                    n.updated_at = datetime(),
                    n.declared_by_repos = CASE
                        WHEN $repo_name IN coalesce(n.declared_by_repos, []) THEN coalesce(n.declared_by_repos, [])
                        ELSE coalesce(n.declared_by_repos, []) + $repo_name
                    END
                """,
                identity=identity, org=org_login, repo=node_repo_name, node_type=node_type,
                ekey=node["external_key"], dname=node["display_name"],
                wf=node.get("workflow_file"), jid=node.get("job_id"),
                meta=json.dumps(node["metadata"]) if node.get("metadata") is not None else None,
                repo_name=repo_name,
            )
        else:
            tx.run(
                f"""
                MERGE (n:GraphNode:{label} {{identity_key: $identity}})
                ON CREATE SET n.id = randomUUID(), n.created_at = datetime()
                SET n.org_login = $org, n.repo_name = $repo, n.node_type = $node_type,
                    n.external_key = $ekey, n.display_name = $dname,
                    n.workflow_file = $wf, n.job_id = $jid, n.metadata_json = $meta,
                    n.updated_at = datetime()
                """,
                identity=identity, org=org_login, repo=node_repo_name, node_type=node_type,
                ekey=node["external_key"], dname=node["display_name"],
                wf=node.get("workflow_file"), jid=node.get("job_id"),
                meta=json.dumps(node["metadata"]) if node.get("metadata") is not None else None,
            )

    for edge in edges:
        source_identity = key_to_identity.get(edge["source_key"])
        target_identity = key_to_identity.get(edge["target_key"])
        if not source_identity or not target_identity:
            continue
        edge_type = edge["edge_type"]
        if edge_type not in _REL_TYPES:
            raise ValueError(f"Unknown edge_type for Neo4j write: {edge_type!r}")
        rel = _REL_TYPES[edge_type]
        tx.run(
            f"""
            MATCH (s:GraphNode {{identity_key: $src}}), (t:GraphNode {{identity_key: $tgt}})
            MERGE (s)-[e:{rel}]->(t)
            ON CREATE SET e.id = randomUUID(), e.created_at = datetime()
            SET e.confidence = $conf, e.metadata_json = $meta, e.updated_at = datetime(),
                e.org_login = $org, e.repo_name = $repo
            """,
            src=source_identity, tgt=target_identity, org=org_login, repo=repo_name,
            conf=edge.get("confidence", "certain"),
            meta=json.dumps(edge["metadata"]) if edge.get("metadata") is not None else None,
        )

def persist_graph(
    session: Session, graph_id: uuid.UUID, org_login: str, repo_name: str, nodes: list[dict], edges: list[dict]
) -> None:
    if settings.GRAPH_DUAL_WRITE_NEO4J:
        with get_driver().session() as neo_session:
            neo_session.execute_write(_write_dependency_subgraph_tx, org_login, repo_name, nodes, edges)

    now = datetime.now(timezone.utc)
    key_to_id: dict[str, uuid.UUID] = {}

    for node in nodes:
        node_id = uuid.uuid4()
        key_to_id[node["external_key"]] = node_id
        session.execute(
            text(
                """
                INSERT INTO graph_nodes
                    (id, graph_id, node_type, external_key, display_name,
                     workflow_file, job_id, metadata, created_at)
                VALUES
                    (:id, :graph_id, :node_type, :external_key, :display_name,
                     :workflow_file, :job_id, CAST(:metadata AS jsonb), :created_at)
                """
            ),
            {
                "id": str(node_id),
                "graph_id": str(graph_id),
                "node_type": node["node_type"],
                "external_key": node["external_key"],
                "display_name": node["display_name"],
                "workflow_file": node.get("workflow_file"),
                "job_id": node.get("job_id"),
                "metadata": json.dumps(node.get("metadata")) if node.get("metadata") is not None else None,
                "created_at": now,
            },
        )

    edge_count = 0
    for edge in edges:
        source_id = key_to_id.get(edge["source_key"])
        target_id = key_to_id.get(edge["target_key"])
        if not source_id or not target_id:
            continue
        session.execute(
            text(
                """
                INSERT INTO graph_edges
                    (id, graph_id, source_node_id, target_node_id, edge_type,
                     confidence, metadata, created_at)
                VALUES
                    (:id, :graph_id, :source_id, :target_id, :edge_type,
                     :confidence, CAST(:metadata AS jsonb), :created_at)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "graph_id": str(graph_id),
                "source_id": str(source_id),
                "target_id": str(target_id),
                "edge_type": edge["edge_type"],
                "confidence": edge.get("confidence", "certain"),
                "metadata": json.dumps(edge.get("metadata")) if edge.get("metadata") is not None else None,
                "created_at": now,
            },
        )
        edge_count += 1

    session.execute(
        text(
            """
            UPDATE graphs SET
                status = 'completed', node_count = :node_count,
                edge_count = :edge_count, built_at = :now, updated_at = :now
            WHERE id = :id
            """
        ),
        {"id": str(graph_id), "node_count": len(nodes), "edge_count": edge_count, "now": now},
    )
    session.commit()
