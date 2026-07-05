"""GraphRAG retrieval step shared by the Governance and Compliance agents.

Pulls structural facts straight from Neo4j -- governance rules already
linked to this workflow, its known failure history, and its dependencies --
to give the LLM cross-workflow/cross-audit context that a text-only
retrieval (pgvector policy search for Governance, nothing at all for
Compliance) can't surface on its own. This is the actual "graph" half of
GraphRAG: vector search finds relevant policy *text*, this finds relevant
graph *structure*.

Works against either agent's state dict (both share repo_owner/repo_name/
workflow_file/agent_trace) without needing a shared TypedDict base.
"""
from app.core.config import settings
from app.services.neo4j_client import get_driver


def retrieve_graph_context(state: dict) -> dict:
    trace = state.get("agent_trace", [])

    # Gated on dual-write, not GRAPH_BACKEND (the dependency_graph.py read-path
    # cutover flag) -- this only needs Neo4j to have data, which dual-write
    # alone provides, independent of whether the graph API routes have been
    # cut over to reading from it yet.
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
    """Renders graph_context into a prompt-ready block, or a clear
    "none available" line if empty/skipped -- never silently omitted, so a
    reviewer reading the prompt trace can tell whether GraphRAG ran."""
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
