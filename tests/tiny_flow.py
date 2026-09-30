"""A genuinely independent one-node trusted runtime adapter, not extraction."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from hvs_catalog.records.common import sha256_of
from hvs_catalog.runtime import DefaultFlowAdapter


class TinyState(TypedDict, total=False):
    execution_id: str
    result_refs: list[dict]


class TinyFlow(DefaultFlowAdapter):
    flow_id = "test-source-acknowledgement"
    flow_version = "1"
    mode = "synthetic"

    def initial_state(self, ctx):
        return {"execution_id": ctx.execution.execution_id, "result_refs": []}

    def build_graph(self, ctx):
        def acknowledge(state):
            refs = ctx.execution.input_snapshot.source_refs
            digest = sha256_of({"sources": [r.model_dump(mode="json") for r in refs]})
            if ctx.reuse_step("acknowledge", digest) is None:
                ctx.commit_step(
                    step_id="acknowledge",
                    input_digest=digest,
                    records=[],
                    result_refs=refs,
                )
            return {"result_refs": [r.model_dump(mode="json") for r in refs]}

        graph = StateGraph(TinyState)
        graph.add_node("acknowledge", acknowledge)
        graph.add_edge(START, "acknowledge")
        graph.add_edge("acknowledge", END)
        return graph.compile(checkpointer=ctx.checkpointer)

    def verify_state(self, ctx, state):
        return (
            []
            if state.get("execution_id") == ctx.execution.execution_id
            else ["independent adapter execution binding mismatch"]
        )
