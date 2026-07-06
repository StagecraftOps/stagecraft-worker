from app.agents.compliance_graph import compliance_graph
from app.agents.governance_graph import governance_graph
from app.agents.graph import remediation_graph
from app.agents.peer_review_graph import peer_review_graph
from app.agents.performance_graph import performance_graph

AGENT_GRAPHS = {
    "failure_rca": remediation_graph,
    "peer_review": peer_review_graph,
    "compliance": compliance_graph,
    "governance": governance_graph,
    "performance_optimization": performance_graph,
}

def get_agent_graph(name: str):
    if name not in AGENT_GRAPHS:
        raise ValueError(f"Unknown agent graph '{name}'. Known: {sorted(AGENT_GRAPHS)}")
    return AGENT_GRAPHS[name]
