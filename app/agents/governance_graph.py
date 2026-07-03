from langgraph.graph import END, StateGraph

from app.agents.governance_nodes import compare_controls, retrieve_relevant_requirements
from app.agents.governance_state import GovernanceState


def build_governance_graph() -> StateGraph:
    graph = StateGraph(GovernanceState)
    graph.add_node("retrieve", retrieve_relevant_requirements)
    graph.add_node("compare", compare_controls)
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "compare")
    graph.add_edge("compare", END)
    return graph.compile()


governance_graph = build_governance_graph()
