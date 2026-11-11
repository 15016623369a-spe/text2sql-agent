"""LangGraph wiring for the recovery nodes."""
from langgraph.graph import END, START, StateGraph


def build_graph(state_type, generate, dry_run, execute, route, prepare_retry):
    graph = StateGraph(state_type)
    graph.add_node("generate", generate)
    graph.add_node("dry_run", dry_run)
    graph.add_node("execute", execute)
    graph.add_node("prepare_retry", prepare_retry)
    graph.add_edge(START, "generate")
    graph.add_conditional_edges(
        "generate",
        lambda state: "stop" if state.get("stop_reason") else "dry_run",
        {"dry_run": "dry_run", "stop": END},
    )
    retry_edges = {"repair": "prepare_retry", "expand": "prepare_retry", "stop": END}
    graph.add_conditional_edges(
        "dry_run",
        lambda state: "execute" if state["trace"][-1]["status"] == "validated" else route(state),
        {"execute": "execute", **retry_edges},
    )
    graph.add_conditional_edges("execute", route, retry_edges)
    graph.add_conditional_edges(
        "prepare_retry",
        lambda state: "stop" if state.get("stop_reason") else "generate",
        {"generate": "generate", "stop": END},
    )
    return graph.compile()
