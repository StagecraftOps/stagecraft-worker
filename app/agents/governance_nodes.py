from sqlalchemy import text

from app.agents.compliance_nodes import _parse_json_list
from app.agents.governance_state import GovernanceState
from app.agents.graph_context import format_graph_context_block
from app.agents.nodes import _converse
from app.services.embeddings import embed_text, to_pgvector

_TOP_K = 5

def retrieve_relevant_requirements(state: GovernanceState) -> GovernanceState:
    from app.tasks.remediation import SyncSessionLocal

    query_text = f"{state.get('workflow_file', '')}\n{state.get('workflow_yaml', '')[:2000]}"
    query_embedding = to_pgvector(embed_text(query_text))

    session = SyncSessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT chunk_text FROM log_embeddings
                WHERE source_type = 'governance_doc' AND source_id = :document_id
                ORDER BY embedding <-> CAST(:query AS vector)
                LIMIT :top_k
                """
            ),
            {"document_id": state["governance_document_id"], "query": query_embedding, "top_k": _TOP_K},
        ).fetchall()
    finally:
        session.close()

    requirements = [row[0] for row in rows]
    trace = state.get("agent_trace", [])
    trace.append(f"retrieve_relevant_requirements: {len(requirements)} chunk(s) retrieved")
    return {**state, "retrieved_requirements": requirements, "agent_trace": trace}

def compare_controls(state: GovernanceState) -> GovernanceState:
    requirements = state.get("retrieved_requirements", [])
    requirements_block = "\n\n".join(f"[{i+1}] {r}" for i, r in enumerate(requirements)) or "(no relevant policy text found)"

    prompt = (
        f"You are auditing a GitHub Actions workflow ({state.get('workflow_file')}) against an "
        f"organization's governance policy document.\n\n"
        f"Relevant policy excerpts:\n{requirements_block}\n\n"
        f"Structural graph context (existing audit/dependency state for this workflow):\n"
        f"{format_graph_context_block(state)}\n\n"
        f"Workflow YAML:\n{state.get('workflow_yaml', '')[:8000]}\n\n"
        "For each distinct requirement implied by the policy excerpts, determine whether the "
        "workflow satisfies it. Use the structural graph context to note continuity (e.g. a "
        "requirement already governing this workflow, or a known failure history) where relevant "
        "to your detail. If no policy excerpts are relevant, return an empty list. Respond "
        "with ONLY valid JSON: a list of objects in this exact format:\n"
        '[{"requirement_id": "<short id>", "status": "compliant"|"gap"|"not_applicable", '
        '"detail": "<one sentence>", "severity": "low"|"medium"|"high", '
        '"remediation_suggestion": "<one sentence, empty string if compliant>"}]'
    )

    raw = _converse(prompt, max_tokens=1536)
    parsed = _parse_json_list(raw)

    trace = state.get("agent_trace", [])
    gaps = sum(1 for f in parsed if f.get("status") == "gap")
    trace.append(f"compare_controls: {gaps} gap(s) of {len(parsed)} finding(s)")

    return {**state, "findings": parsed, "agent_trace": trace}
