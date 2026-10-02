"""Independent scoring: strict pure-input L0/L1/L2 scores, restricted formatting
comparisons, and auxiliary scoring protocols.

Public entry points are documented in each module. score_batch(plan, gold, predictions,
auxiliary=..., agent_adapter=None) returns a JSON-serializable ScoringReport from pure
data without files or real services, except an explicitly configured frozen directory.
compare_printed/compare_condition cover restricted formatting and condition phrases
(A1-A5, B cases, C1, U1/U2). The agent module defines protocols, synchronous mocks,
frozen storage, and enablement gates.

Synthetic construction example::

    from hvs_catalog.evaluation.scoring import (
        AuxiliaryScoringConfig, GoldObjectEntry, GoldPaperEntry,
        GoldQuantityEntry, GoldValueEntry, InputReference,
        PaperEvaluationScope, PredictionObjectEntry, PredictionPaperEntry,
        PredictionQuantityEntry, PredictionValueEntry, ScoringPlan,
        build_measured_value, gold_input_digest, protocol_only_config,
        score_batch,
    )

    value = build_measured_value(
        value_id="g1", value="8.2", error="0.3", unit="kpc",
        condition="under Galactic potential M1",
    )
    gold = [
        GoldPaperEntry(
            paper_key="synthetic-scoring-demo",
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path="observed_phase_space.distance",
                            values=[GoldValueEntry(record_id="g1", value=value)],
                        )
                    ],
                )
            ],
        )
    ]
    auxiliary = protocol_only_config()
    plan = ScoringPlan(
        paper_keys=["synthetic-scoring-demo"],
        scopes=[
            PaperEvaluationScope(
                paper_key="synthetic-scoring-demo",
                quantity_paths=["observed_phase_space.distance"],
            )
        ],
        gold_ref=InputReference(
            name="synthetic-gold", version="v1",
            fingerprint=gold_input_digest(gold),
        ),
        materials_ref=InputReference("synthetic-materials", "v1", "<sha256>"),
        science_rules_ref=InputReference("science-rules", "v1", "<sha256>"),
        scoring_scheme_ref=InputReference("scoring-scheme", "v1", "<sha256>"),
        program_ref=InputReference("scoring-program", "v1", "<sha256>"),
        auxiliary_ref=InputReference(
            "auxiliary", "v1", auxiliary_config_digest(auxiliary)
        ),
    )
    predictions = [
        PredictionPaperEntry(
            paper_key="synthetic-scoring-demo",
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key="synthetic-scoring-demo",
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path="observed_phase_space.distance",
                            values=[
                                PredictionValueEntry(
                                    record_id="p1",
                                    value=build_measured_value(
                                        value_id="p1", value="8.2", error="0.3",
                                        unit="kpc",
                                        condition="under Galactic potential M1",
                                    ),
                                )
                            ],
                        )
                    ],
                )
            ],
        )
    ]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)

All examples are fictional synthetic materials, establishing neither real model
capabilities nor official Gold.
"""

from __future__ import annotations

from .agent import (
    AgentAdapter,
    AgentRequest,
    AgentResponse,
    AgentValidationRecord,
    AuxiliaryAdjudicator,
    AuxiliaryScoringConfig,
    FrozenDecisionStore,
    FrozenJudgment,
    MockAgentAdapter,
    SampleOutcome,
    ScoringAgentResolver,
    ValidationRound,
    ValidationSample,
    auxiliary_config_digest,
    build_judgment,
    judgment_entry_key,
    protocol_only_config,
)
from .builders import build_measured_value
from .errors import (
    AuxiliaryEnablementError,
    FrozenStoreError,
    ScoringError,
    ScoringInputError,
)
from .models import (
    ConfirmedRange,
    EvidenceFragment,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    InputReference,
    PaperEvaluationScope,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScopedCoverage,
    ScoringPlan,
    gold_input_digest,
    predictions_input_digest,
)
from .printed import (
    Verdict,
    compare_condition,
    compare_number,
    compare_printed,
    compare_unit,
    normalize_printed,
)
from .results import (
    AuxiliarySummary,
    BatchMicro,
    Diagnostic,
    L0Metrics,
    L1Metrics,
    L2Metrics,
    PairingDetail,
    PaperResult,
    QuantityBreakdown,
    ScoringReport,
)
from .score import score_batch

__all__ = [
    "AgentAdapter",
    "AgentRequest",
    "AgentResponse",
    "AgentValidationRecord",
    "AuxiliaryAdjudicator",
    "AuxiliaryEnablementError",
    "AuxiliaryScoringConfig",
    "AuxiliarySummary",
    "BatchMicro",
    "ConfirmedRange",
    "Diagnostic",
    "EvidenceFragment",
    "FrozenDecisionStore",
    "FrozenJudgment",
    "FrozenStoreError",
    "GoldObjectEntry",
    "GoldPaperEntry",
    "GoldQuantityEntry",
    "GoldValueEntry",
    "InputReference",
    "L0Metrics",
    "L1Metrics",
    "L2Metrics",
    "MockAgentAdapter",
    "PairingDetail",
    "PaperEvaluationScope",
    "PaperResult",
    "PredictionObjectEntry",
    "PredictionPaperEntry",
    "PredictionQuantityEntry",
    "PredictionValueEntry",
    "QuantityBreakdown",
    "SampleOutcome",
    "ScopedCoverage",
    "ScoringAgentResolver",
    "ScoringError",
    "ScoringInputError",
    "ScoringPlan",
    "ScoringReport",
    "ValidationRound",
    "ValidationSample",
    "Verdict",
    "auxiliary_config_digest",
    "build_judgment",
    "build_measured_value",
    "compare_condition",
    "compare_number",
    "compare_printed",
    "compare_unit",
    "gold_input_digest",
    "judgment_entry_key",
    "normalize_printed",
    "predictions_input_digest",
    "protocol_only_config",
    "score_batch",
]
