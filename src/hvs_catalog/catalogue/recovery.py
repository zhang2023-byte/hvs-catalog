"""Validate retained container-recovery receipts; never repair live model output."""

import copy
import json
import re

from ..benchmark.repair_source import job_locations, request_from_prepared
from ..llm.config import ModelConfig
from ..llm.transport import TokenDanceAdapter
from ..records import quantity_validation
from ..records.common import ArtifactRef, sha256_of
from ..records.evidence_audit import uses_quote_warnings
from ..records.response import response_message, strict_json
from ..workspace.archives import read_json

TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|[{}\[\],:]'
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _tokens(text):
    result, end = [], 0
    for match in TOKEN.finditer(text):
        require(
            not text[end : match.start()].strip(), "Recovery contains non-JSON text."
        )
        result.append((match.group(), match.start(), match.end()))
        end = match.end()
    require(not text[end:].strip(), "Recovery contains trailing non-JSON text.")
    return result


def _scalars(text):
    return [
        strict_json(token) for token, _, _ in _tokens(text) if token not in "{}[],: "
    ]


def recover_arguments(arguments):
    """Replay the archived, scalar-preserving container reconstruction exactly."""
    original, unwrap = arguments, []
    try:
        outer = strict_json(arguments)
    except (ValueError, TypeError):
        outer = None
    if isinstance(outer, dict) and list(outer) == ["arguments"]:
        unwrap.append("remove the sole arguments envelope")
        arguments = (
            outer["arguments"]
            if isinstance(outer["arguments"], str)
            else json.dumps(outer["arguments"], ensure_ascii=False)
        )
    tokens = _tokens(arguments)
    group_indices = [
        i
        for i, (t, _, _) in enumerate(tokens[:-1])
        if t == '"quantity"'
        and i
        and tokens[i - 1][0] == "{"
        and tokens[i + 1][0] == ":"
    ]
    notes_indices = [
        i
        for i, (t, _, _) in enumerate(tokens[:-1])
        if t == '"notes"' and tokens[i + 1][0] == ":"
    ]
    require(
        group_indices and len(notes_indices) == 1,
        "Recovery requires quantity groups and one notes field.",
    )
    prefix = [t for t, _, _ in tokens[: group_indices[0] - 1]]
    removed_prefix = prefix == ["{", '"arguments"', ":", "{", '"quantities"', ":", "["]
    if removed_prefix:
        unwrap.append("remove the sole arguments object envelope")
    else:
        require(
            prefix == ["{", '"quantities"', ":", "["], "Unsupported recovery envelope."
        )
    ni = notes_indices[0]
    require(
        ni + 2 < len(tokens) and tokens[ni + 2][0].startswith('"'),
        "Recovery notes must be a string.",
    )
    notes = strict_json(tokens[ni + 2][0])
    require(
        all(t in "}],:" for t, _, _ in tokens[ni + 3 :]),
        "Recovery contains data after notes.",
    )
    groups, spans = [], []
    decoder = json.JSONDecoder()
    for offset, gi in enumerate(group_indices):
        head = [t for t, _, _ in tokens[gi - 1 : gi + 7]]
        require(
            head[:3] == ["{", '"quantity"', ":"]
            and head[4:] == [",", '"values"', ":", "["],
            "Unsupported quantity container.",
        )
        quantity = strict_json(head[3])
        end = (
            tokens[group_indices[offset + 1] - 1][1]
            if offset + 1 < len(group_indices)
            else tokens[ni][1]
        )
        position = tokens[gi + 6][2]
        values, value_spans = [], []
        while position < end:
            while position < end and arguments[position].isspace():
                position += 1
            if position >= end or arguments[position : position + 1] != "{":
                break
            value, stop = decoder.raw_decode(arguments, position)
            require(
                stop <= end and isinstance(value, dict) and "quantity" not in value,
                "Recovery value crosses a group boundary.",
            )
            require(
                strict_json(arguments[position:stop]) == value,
                "Invalid recovery value.",
            )
            values.append(value)
            value_spans.append([position, stop])
            position = stop
            while position < end and arguments[position].isspace():
                position += 1
            if arguments[position : position + 1] != ",":
                break
            position += 1
        require(
            values
            and all(t in "{}[],:" for t, _, _ in _tokens(arguments[position:end])),
            "Recovery would discard scientific content.",
        )
        groups.append({"quantity": quantity, "values": values})
        spans.append({"quantity": quantity, "values_original_spans": value_spans})
    effective = json.dumps({"quantities": groups, "notes": notes}, ensure_ascii=False)
    before = _scalars(arguments)
    if removed_prefix:
        require(before.pop(0) == "arguments", "Invalid recovery envelope token.")
    require(
        before == _scalars(effective),
        "Recovery changed, omitted or reordered a scalar.",
    )
    strict_json(effective)
    return effective, {
        "original_arguments_sha256": sha256_of(original),
        "unwrapped": unwrap,
        "scalar_sequence_sha256": sha256_of(before),
        "scalar_count": len(before),
        "all_scalars_preserved": True,
        "value_spans": spans,
        "effective_arguments_sha256": sha256_of(effective),
    }


def replay_recovery(space, facts, job, item, *, existing=None):
    """Check a saved receipt against its exact request, response, material and validator."""
    require(
        job["kind"] == "quantities"
        and all(item[k] == job[k] for k in ("id", "paper", "step", "source_index")),
        "Recovery belongs to another target.",
    )
    prepared = read_json(space, ArtifactRef.model_validate(item["source_request"]))
    env = read_json(space, ArtifactRef.model_validate(item["source_response"]))
    require(
        env.get("request_sha256") == sha256_of(prepared),
        "Recovery response belongs to another request.",
    )
    require(
        prepared.get("evidence_policy") == job["prepared"].get("evidence_policy"),
        "Recovery changed its evidence policy.",
    )
    exchange = env["exchange"]
    transport = exchange["transport"]
    require(
        exchange["status"] == 200
        and transport["response_body_complete"]
        and transport["sse_done"]
        and not transport["raw_response_truncated"],
        "Recovery response is incomplete.",
    )
    require(
        exchange["raw_response_ref"] == item["raw_response"],
        "Recovery raw response reference differs.",
    )
    space.read_file(ArtifactRef.model_validate(item["raw_response"]))
    arguments = response_message(env)["tool_calls"][0]["function"]["arguments"]
    effective, receipt = recover_arguments(arguments)
    require(
        effective == item["effective_arguments"] and receipt == item["receipt"],
        "Recovery receipt differs from the archived response.",
    )
    derived = copy.deepcopy(env)
    derived["exchange"].pop("tool_arguments_normalization", None)
    derived["exchange"]["body"]["choices"][0]["message"]["tool_calls"][0]["function"][
        "arguments"
    ] = effective
    request = request_from_prepared(prepared, ModelConfig.model_validate(job["model"]))
    parsed = TokenDanceAdapter().parse(derived["exchange"], request)
    derived.update(error=None, result=parsed, local_structure_validation=True)
    locations = job_locations(facts, job)
    check = quantity_validation.check_response(
        derived,
        locations,
        quote_warnings=uses_quote_warnings(prepared.get("evidence_policy")),
    )
    require(
        check.passed, "Recovered submission fails the original scientific validator."
    )
    values, paths, rejected, notes = quantity_validation.select_partial(
        check, locations
    )
    require(not rejected, "Recovered submission contains rejected values.")
    groups = [v.model_dump(mode="json") for v in values]
    additions = groups
    if existing is not None:
        require(
            all(group in groups for group in existing),
            "Recovery changed an accepted quantity group.",
        )
        keys = {g["quantity"] for g in existing}
        require(
            len({g["quantity"] for g in groups}) == len(groups),
            "Duplicate recovered quantity group.",
        )
        additions = [g for g in groups if g["quantity"] not in keys]
        require(
            additions
            and item["validated_full_groups"] == groups
            and item["preserved_existing_groups"] == existing
            and item["added_groups"] == additions,
            "Recovery did not preserve its saved quantity groups.",
        )
    fragment = {
        "status": "complete",
        "passed": True,
        "readable": True,
        "values": additions,
        "indices": paths,
        "rejected": {},
        "issues": [],
        "notes": notes,
        "warnings": [w.model_dump() for w in check.warnings],
        "local_structure_recovery": True,
    }
    if existing is not None:
        fragment["accepted_existing_groups_omitted"] = sorted(keys)
    require(
        fragment == item["fragment"],
        "Recovery fragment differs from its validated submission.",
    )
    return fragment
