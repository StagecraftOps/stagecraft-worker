from langgraph.graph import END, StateGraph

from app.agents.peer_review_nodes import detect_workflow_changes, review_diff
from app.agents.peer_review_state import PeerReviewState

def build_peer_review_graph() -> StateGraph:
    graph = StateGraph(PeerReviewState)

    graph.add_node("detect_changes", detect_workflow_changes)
    graph.add_node("review", review_diff)

    graph.set_entry_point("detect_changes")
    graph.add_edge("detect_changes", "review")
    graph.add_edge("review", END)

    return graph.compile()

peer_review_graph = build_peer_review_graph()
