from langgraph.graph import END, StateGraph

from app.agents.compliance_nodes import check_framework_controls
from app.agents.compliance_state import ComplianceState


def build_compliance_graph() -> StateGraph:
    graph = StateGraph(ComplianceState)
    graph.add_node("check_controls", check_framework_controls)
    graph.set_entry_point("check_controls")
    graph.add_edge("check_controls", END)
    return graph.compile()


compliance_graph = build_compliance_graph()
