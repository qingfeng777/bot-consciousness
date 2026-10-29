"""Versioned cloud task contracts. JSON mode is never treated as validation."""
from importlib.resources import files
import hashlib
import math

from .domain import InvalidInput, canonical
from .planning import validate_plan


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def array(item, maximum=16, minimum=0):
    return {"type": "array", "items": item, "maxItems": maximum, "minItems": minimum}


TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
ID = {"type": "string", "minLength": 1, "maxLength": 256}
SCALAR = {"anyOf": [TEXT, {"type": "number"}, {"type": "boolean"}]}
EVIDENCE = array(ID, 32, 1)
PREDICATE = obj(entity=ID, attribute=ID, value=SCALAR)
STEP = obj(id=ID, capability={"enum": ["say", "alert"]}, arguments=obj(text=TEXT), after=array(ID, 8))
STEP["properties"].update(requires=array(PREDICATE), when=PREDICATE)
PLAN = obj(goal=ID, steps=array(STEP, 8, 1), evidence=EVIDENCE)
PLAN["properties"]["requires"] = array(PREDICATE)
CLAIM = obj(entity=ID, attribute=ID, value=SCALAR, evidence=EVIDENCE,
            uncertainty={"enum": ["observed", "reported", "hypothesis"]})
SCHEMAS = {
    "plan": PLAN,
    "perception": obj(observations=array(obj(text=TEXT, evidence=EVIDENCE,
                                            uncertainty={"enum": ["observed", "uncertain"]})),
                      missing=array(TEXT)),
    "memory_extract": obj(claims=array(CLAIM)),
    "summarize": obj(summary=TEXT, evidence=EVIDENCE, unresolved=array(TEXT)),
    "scene": obj(location=TEXT, entities=array(obj(track=ID, label=TEXT, evidence=EVIDENCE)),
                 uncertainties=array(TEXT)),
    "goal": obj(description=TEXT, predicate=PREDICATE, evidence=EVIDENCE),
    "motivation": obj(candidates=array(obj(description=TEXT, predicate=PREDICATE, evidence=EVIDENCE), 5)),
    "role": obj(candidates=array(obj(role=ID, style={"enum": ["neutral", "concise", "friendly"]},
                                     evidence=EVIDENCE, counterexamples=array(TEXT)), 5)),
    "learning": obj(plan=PLAN, training=array(ID, 32, 1), counterexamples=array(TEXT), scope=TEXT),
    "conflict": obj(assessment={"enum": ["conflict", "temporal_change", "unit_difference", "uncertain"]},
                    evidence=EVIDENCE, questions=array(TEXT, 5)),
    "association": obj(hypotheses=array(obj(question=TEXT, evidence=EVIDENCE), 5)),
    "appraisal": obj(stimulus={"enum": ["success", "failure", "emergency", "calm"]}, evidence=EVIDENCE),
    "expression": obj(text=TEXT, evidence=EVIDENCE),
}
PROMPT_VERSION = "1.0.0"


def validate_schema(value, schema):
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_schema(value, option)
                return
            except InvalidInput:
                pass
        raise InvalidInput("output does not match an allowed type")
    if "enum" in schema:
        if value not in schema["enum"]:
            raise InvalidInput("output enum outside contract")
        return
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or not set(schema["required"]) <= set(value) or set(value) - set(schema["properties"]):
            raise InvalidInput("output object has missing or unknown fields")
        for name, item in value.items():
            validate_schema(item, schema["properties"][name])
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise InvalidInput("output array outside budget")
        for item in value:
            validate_schema(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or not value.strip() or not schema["minLength"] <= len(value) <= schema["maxLength"]:
            raise InvalidInput("invalid output string")
    elif kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise InvalidInput("invalid output number")
    elif kind == "boolean" and type(value) is not bool:
        raise InvalidInput("invalid output boolean")


def prompt(purpose):
    if purpose not in SCHEMAS:
        raise InvalidInput("unsupported cloud purpose")
    root = files("consciousness").joinpath("prompts")
    text = root.joinpath("system.md").read_text(encoding="utf-8") + "\n" + root.joinpath(purpose + ".md").read_text(encoding="utf-8")
    text += "\n成功时 data 必须符合以下 JSON Schema：\n" + canonical(SCHEMAS[purpose])
    text += '\n固定外层格式：{"status":"ok","data":{...},"questions":[]}。'
    text += '\n证据不足时：{"status":"needs_clarification","data":null,"questions":["需要补充的问题"]}。'
    return text, hashlib.sha256(text.encode()).hexdigest()


def references(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence":
                result.update(item)
            else:
                result.update(references(item))
    elif isinstance(value, list):
        for item in value:
            result.update(references(item))
    return result


def validate_output(purpose, value, allowed_evidence, *, goal=None, training=()):
    if not isinstance(value, dict) or set(value) != {"status", "data", "questions"}:
        raise InvalidInput("cloud response requires status, data, questions")
    validate_schema(value["questions"], array(TEXT, 5))
    if value["status"] == "needs_clarification":
        if value["data"] is not None or not value["questions"]:
            raise InvalidInput("clarification requires questions and null data")
        return value
    if value["status"] != "ok" or value["questions"]:
        raise InvalidInput("invalid cloud response status")
    validate_schema(value["data"], SCHEMAS[purpose])
    if not references(value["data"]) <= set(allowed_evidence):
        raise InvalidInput("model cited evidence outside its input snapshot")
    plan = value["data"] if purpose == "plan" else value["data"]["plan"] if purpose == "learning" else None
    if plan is not None:
        validate_plan(plan)
        if goal is None or plan["goal"] != goal:
            raise InvalidInput("model plan changed the requested goal")
    if purpose == "learning" and not set(value["data"]["training"]) <= set(training):
        raise InvalidInput("model cited unknown training experiences")
    return value
