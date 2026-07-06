from langgraph.graph import END, StateGraph

from app.agents.performance_nodes import draft_future_yaml, propose_optimizations, simulate_savings
from app.agents.performance_state import PerformanceState

def build_performance_graph() -> StateGraph:
    graph = StateGraph(PerformanceState)

    graph.add_node("propose", propose_optimizations)
    graph.add_node("draft_yaml", draft_future_yaml)
    graph.add_node("simulate", simulate_savings)

    graph.set_entry_point("propose")
    graph.add_edge("propose", "draft_yaml")
    graph.add_edge("draft_yaml", "simulate")
    graph.add_edge("simulate", END)

    return graph.compile()

performance_graph = build_performance_graph()
