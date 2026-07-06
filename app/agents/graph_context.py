from app.core.config import settings
from app.services.neo4j_client import get_driver

def retrieve_graph_context(state: dict) -> dict:
    trace = state.get("agent_trace", [])

    if not settings.GRAPH_DUAL_WRITE_NEO4J:
        trace.append("retrieve_graph_context: skipped (GRAPH_DUAL_WRITE_NEO4J is off)")
        return {**state, "graph_context": {}, "agent_trace": trace}

    org = state.get("repo_owner", "")
    repo = state.get("repo_name", "")
    workflow_file = state.get("workflow_file", "")

    with get_driver().session() as neo_session:
        record = neo_session.run(
            """
            MATCH (w:GraphNode:Workflow {org_login: $org, repo_name: $repo, workflow_file: $wf})
            OPTIONAL MATCH (rule:GraphNode:GovernanceRule)-[:GOVERNS]->(w)
            OPTIONAL MATCH (fail:GraphNode:Failure)-[:CAUSED_BY]->(w)
            OPTIONAL MATCH (w)-[:NEEDS|USES_REUSABLE|USES_COMPOSITE]->(dep:GraphNode)
            RETURN collect(DISTINCT rule.display_name) AS rules,
                   collect(DISTINCT fail.display_name) AS failures,
                   collect(DISTINCT dep.display_name)  AS deps
            """,
            org=org, repo=repo, wf=workflow_file,
        ).single()

    context = {
        "existing_rules": [r for r in record["rules"] if r] if record else [],
        "known_failures": [f for f in record["failures"] if f] if record else [],
        "dependencies": [d for d in record["deps"] if d] if record else [],
    }
    trace.append(
        f"retrieve_graph_context: {len(context['existing_rules'])} existing rule(s), "
        f"{len(context['known_failures'])} known failure(s), {len(context['dependencies'])} dependency(ies)"
    )
    return {**state, "graph_context": context, "agent_trace": trace}

def format_graph_context_block(state: dict) -> str:
    context = state.get("graph_context") or {}
    rules = context.get("existing_rules") or []
    failures = context.get("known_failures") or []
    deps = context.get("dependencies") or []

    if not rules and not failures and not deps:
        return "(no structural graph context available)"

    lines = []
    if rules:
        lines.append(f"Already governed by: {', '.join(rules)}")
    if failures:
        lines.append(f"Known failure history: {', '.join(failures)}")
    if deps:
        lines.append(f"Depends on: {', '.join(deps)}")
    return "\n".join(lines)
