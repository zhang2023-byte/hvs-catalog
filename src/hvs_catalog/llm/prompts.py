"""Assemble approved prompts, tools, and initial stage requests."""

from __future__ import annotations

import json
import tomllib
from importlib import resources

from pydantic import Field

from ..records.common import Config
from ..records.quantity_input import quantity_input_schema
from ..records.roster_input import roster_input_schema
from ..records.rules import load_rules, rules_bytes
from .config import (
    CallRequest,
    ImageContent,
    ImageURL,
    Message,
    ModelConfig,
    TextContent,
    Tool,
)


class PromptImage(Config):
    label: str = Field(min_length=1)
    image_url: ImageURL


class PromptInput(Config):
    """The exact supplied material and an internal citation map."""

    manuscript: str
    images: list[PromptImage] = Field(default_factory=list)
    citation_locations: dict[str, dict] = Field(default_factory=dict)


class PromptBlueprint(Config):
    step_id: str
    system: str
    schema_: dict = Field(alias="schema")
    tool_description: str

    def tool(self) -> Tool:
        if self.step_id not in {"roster", "quantities"}:
            raise ValueError("Unknown extraction stage")
        return Tool(
            name=f"submit_{self.step_id}",
            description=self.tool_description,
            parameters=self.schema_,
        )

    def request(
        self,
        model: ModelConfig,
        inputs: PromptInput,
        identifiers: list[str] | None = None,
    ) -> CallRequest:
        if self.step_id == "quantities" and not identifiers:
            raise ValueError("Provide the target star identifiers.")
        content = inputs.manuscript
        if inputs.images:
            content = [TextContent(text=inputs.manuscript)] if inputs.manuscript else []
            for image in inputs.images:
                content.extend(
                    [
                        TextContent(text=image.label),
                        ImageContent(image_url=image.image_url),
                    ]
                )
        messages = [
            Message(role="system", content=self.system),
            Message(role="user", content=content),
        ]
        if self.step_id == "quantities":
            messages.append(
                Message(
                    role="user",
                    content=json.dumps(
                        {"identifiers": identifiers}, ensure_ascii=False
                    ),
                )
            )
        return CallRequest(
            model=model,
            messages=messages,
            tool=self.tool(),
        )


def prompt_resources() -> dict:
    return tomllib.loads(
        (
            resources.files("hvs_catalog.resources") / "prompts" / "steps.toml"
        ).read_text()
    )


def science_content() -> dict:
    return tomllib.loads(rules_bytes().decode())


def assemble_blueprint(step_id: str) -> PromptBlueprint:
    schemas = {"roster": roster_input_schema, "quantities": quantity_input_schema}
    if step_id not in schemas:
        raise ValueError("Unknown extraction stage")
    step = prompt_resources()["steps"][step_id]
    rules = {r.id: r for r in load_rules().rules}
    quantity_table = "\n".join(
        ["| Quantity | Definition | Common units |", "|---|---|---|"]
        + [
            "| "
            + ", ".join(f"`{qid}`" for qid in row["ids"])
            + f" | {row['definition']} | {row['common_units']} |"
            for row in science_content()["quantities"]
        ]
    )
    science = "\n\n".join(
        f"## {rules[rid].title}\n\n"
        + rules[rid].text.replace("{{quantity_table}}", quantity_table)
        for rid in step["rules"]
    )
    system = "\n\n".join(
        [
            "# Task\n\n" + step["task"],
            "# Scientific rules\n\n" + science,
            "# Materials\n\n" + step["materials"],
            "# Submission\n\n" + step["submission"],
        ]
    )
    return PromptBlueprint(
        step_id=step_id,
        system=system,
        schema=schemas[step_id](),
        tool_description=step["tool_description"],
    )
