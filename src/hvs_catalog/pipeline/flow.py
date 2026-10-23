"""One fixed paper flow with independently advancing numerical targets."""

from langgraph.graph import END, START, StateGraph

from ..llm.managed import CallFailure, CallService
from ..llm.prompts import PromptBlueprint
from ..records.common import RecordRef
from ..runtime import DefaultFlowAdapter
from ..runtime.deferred import WaitingForCall
from ..workspace.archives import read_json
from .configuration import stored_configuration
from .inputs import read_input
from .stages import run_stage


class ExtractionFlow(DefaultFlowAdapter):
    flow_id = "hvs-extraction"
    flow_version = "3"
    replay_terminal = True
    terminal_failures = True

    def __init__(self, *, mode):
        self.mode = mode

    def initial_state(self, ctx):
        return {"execution_id": ctx.execution.execution_id}

    def build_graph(self, ctx):
        _, self.source, self.material = read_input(
            ctx.space, ctx.execution.input_snapshot
        )
        config = self.config = stored_configuration(ctx.execution.input_snapshot)
        self.blueprints = read_json(
            ctx.space, ctx.execution.input_snapshot.prompt_refs[0]
        )
        if (
            not isinstance(ctx.service, CallService)
            or ctx.service.runtime != config.runtime
        ):
            raise ValueError("Model service differs from the fixed configuration.")
        ctx.service.adapter.validate_runtime(config.runtime)
        builder = StateGraph(dict)
        builder.add_node("roster", lambda state: self._roster(ctx, state))
        builder.add_node("quantities", lambda state: self._quantities(ctx, state))
        builder.add_node("assemble", lambda state: state)
        builder.add_edge(START, "roster")
        builder.add_edge("roster", "quantities")
        builder.add_edge("quantities", "assemble")
        builder.add_edge("assemble", END)
        return builder.compile(checkpointer=ctx.checkpointer)

    def _request(self, ctx, stage, identifiers=None):
        blueprint = PromptBlueprint.model_validate(self.blueprints[stage])
        return (
            self.source,
            self.material,
            blueprint.request(self.config.model_for(stage), self.material, identifiers),
        )

    def _roster(self, ctx, state):
        imported = ctx.execution.input_snapshot.options.get("roster_ref")
        if imported:
            ref = RecordRef.model_validate_json(imported)
        else:
            source, material, request = self._request(ctx, "roster")
            ref = run_stage(ctx, request, source, material).ref()
        return {**state, "roster_ref": ref.model_dump()}

    def _quantities(self, ctx, state):
        if ctx.execution.input_snapshot.options["extraction_scope"] == "roster":
            return state
        roster = ctx.store.read_record(RecordRef.model_validate(state["roster_ref"]))
        pending, failures, refs = False, [], []
        for index, star in zip(
            roster.data.source_indices, roster.data.stars, strict=True
        ):
            source, material, request = self._request(
                ctx, "quantities", [i.value for i in star.identifiers]
            )
            try:
                record = run_stage(
                    ctx, request, source, material, roster=roster, source_index=index
                )
                refs.append(record.ref().model_dump())
            except WaitingForCall:
                pending = True
            except CallFailure as exc:
                failures.append(str(exc))
        if pending:
            raise WaitingForCall()
        if failures:
            raise CallFailure("; ".join(failures))
        return {**state, "quantity_refs": refs}
