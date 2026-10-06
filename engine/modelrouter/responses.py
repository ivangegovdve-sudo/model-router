"""Translate the supported Responses API subset over Chat Completions providers."""
from __future__ import annotations

import json
import secrets as pysecrets
import time


def _new_id(prefix: str) -> str:
    return prefix + "_" + pysecrets.token_hex(12)


def _responses_text(content) -> str:
    """Flatten Responses text parts for the Chat Completions-only provider layer."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise ValueError("message content must be text or a list of text parts")
    text = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in ("input_text", "output_text", "text"):
            kind = part.get("type", "unknown") if isinstance(part, dict) else "unknown"
            raise ValueError("unsupported Responses content type %r; only text is supported" % kind)
        text.append(str(part.get("text") or ""))
    return "".join(text)


def _responses_input_messages(input_value) -> list[dict]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        raise ValueError("`input` must be a string or a list of Responses input items")
    messages = []
    pending_calls = []
    roles = {"system", "developer", "user", "assistant"}

    def flush_calls():
        if pending_calls:
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in input_value:
        if not isinstance(item, dict):
            raise ValueError("each Responses input item must be an object")
        kind = item.get("type", "message")
        if kind == "message":
            flush_calls()
            role = item.get("role")
            if role not in roles:
                raise ValueError("unsupported Responses message role %r" % role)
            message = {"role": role, "content": _responses_text(item.get("content", ""))}
            if item.get("name"):
                message["name"] = item["name"]
            messages.append(message)
        elif kind == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if not call_id or not name:
                raise ValueError("Responses function_call items need call_id and name")
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            pending_calls.append({
                "id": str(call_id), "type": "function",
                "function": {"name": str(name), "arguments": arguments},
            })
        elif kind == "function_call_output":
            flush_calls()
            call_id = item.get("call_id")
            if not call_id:
                raise ValueError("Responses function_call_output items need call_id")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            messages.append({"role": "tool", "tool_call_id": str(call_id), "content": output})
        elif kind == "reasoning":
            # Chat providers cannot accept the Responses API's opaque reasoning items.
            continue
        else:
            flush_calls()
            raise ValueError("unsupported Responses input item type %r" % kind)
    flush_calls()
    return messages


def _responses_tools(tools) -> list[dict]:
    if not isinstance(tools, list):
        raise ValueError("`tools` must be a list")
    chat_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            kind = tool.get("type", "unknown") if isinstance(tool, dict) else "unknown"
            raise ValueError("unsupported Responses tool type %r; only function tools are supported" % kind)
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not function.get("name"):
            raise ValueError("Responses function tools need a name")
        definition = {key: function[key] for key in ("name", "description", "parameters", "strict")
                      if key in function}
        chat_tools.append({"type": "function", "function": definition})
    return chat_tools


def _responses_tool_choice(choice):
    if isinstance(choice, str):
        if choice not in ("auto", "none", "required"):
            raise ValueError("unsupported Responses `tool_choice` %r" % choice)
        return choice
    if not isinstance(choice, dict):
        raise ValueError("`tool_choice` must be a string or object")
    if choice.get("type") == "function":
        if not choice.get("name"):
            raise ValueError("function `tool_choice` needs a name")
        return {"type": "function", "function": {"name": choice["name"]}}
    if choice.get("type") == "allowed_tools":
        return {key: choice[key] for key in ("type", "mode", "tools") if key in choice}
    raise ValueError("unsupported Responses `tool_choice` type %r" % choice.get("type"))


def responses_to_chat(body: dict) -> dict:
    if body.get("previous_response_id"):
        raise ValueError("`previous_response_id` is not supported; send the full input history")
    if body.get("background"):
        raise ValueError("Responses background polling is not supported; use X-Router-Lane: background")
    messages = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "developer", "content": instructions})
    elif instructions is not None:
        raise ValueError("`instructions` must be a string")
    messages.extend(_responses_input_messages(body.get("input")))
    if not messages:
        raise ValueError("`input` must contain at least one message or function item")

    chat = {"model": body.get("model") or "auto", "messages": messages}
    # Only forward fields shared with the Chat Completions provider adapters. Responses-only
    # fields such as store, reasoning, include, and truncation must not leak upstream.
    shared = ("temperature", "top_p", "stop", "presence_penalty", "frequency_penalty",
              "logit_bias", "seed", "n", "user", "service_tier", "parallel_tool_calls",
              "stream", "logprobs", "top_logprobs")
    for key in shared:
        if key in body:
            chat[key] = body[key]
    if "max_output_tokens" in body:
        try:
            limit = int(body["max_output_tokens"])
        except (TypeError, ValueError):
            raise ValueError("`max_output_tokens` must be an integer")
        if limit < 1:
            raise ValueError("`max_output_tokens` must be >= 1")
        chat["max_tokens"] = limit
    elif "max_completion_tokens" in body:
        chat["max_completion_tokens"] = body["max_completion_tokens"]
    if "tools" in body:
        chat["tools"] = _responses_tools(body["tools"])
    if "tool_choice" in body:
        chat["tool_choice"] = _responses_tool_choice(body["tool_choice"])
    if "user" not in chat and body.get("prompt_cache_key"):
        chat["user"] = body["prompt_cache_key"]
    text = body.get("text")
    if isinstance(text, dict) and isinstance(text.get("format"), dict):
        fmt = text["format"]
        if fmt.get("type") == "json_object":
            chat["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema":
            schema = {key: fmt[key] for key in ("name", "description", "schema", "strict")
                      if key in fmt}
            chat["response_format"] = {"type": "json_schema", "json_schema": schema}
    return chat


def _chat_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
    return ""


def _responses_usage(usage) -> dict | None:
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {"input_tokens": prompt, "output_tokens": completion,
            "total_tokens": int(usage.get("total_tokens") or prompt + completion),
            "input_tokens_details": {"cached_tokens": int(prompt_details.get("cached_tokens") or 0)},
            "output_tokens_details": {"reasoning_tokens": int(
                completion_details.get("reasoning_tokens") or 0)}}


def _router_result(routed) -> dict:
    result = routed.doc.get("result") or {}
    return {"decision_id": routed.decision_id, "seat": routed.choice.seat,
            "because": routed.choice.because, "cost_usd": result.get("cost_usd"),
            "cost_basis": result.get("cost_basis"), "attempts": len(routed.doc.get("attempts") or [])}


def chat_completion_to_response(completion: dict, request_body: dict) -> dict:
    choices = completion.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    output = []
    text = _chat_text(message.get("content"))
    refusal = message.get("refusal")
    if text or refusal:
        content = ([{"type": "refusal", "refusal": str(refusal)}] if refusal else
                   [{"type": "output_text", "text": text, "annotations": []}])
        output.append({"id": _new_id("msg"), "type": "message", "role": "assistant",
                       "status": "completed", "content": content})
    calls = message.get("tool_calls") or []
    legacy = message.get("function_call")
    if legacy:
        calls = list(calls) + [{"id": _new_id("call"), "type": "function",
                                "function": legacy}]
    for call in calls:
        function = call.get("function") or {}
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        call_id = call.get("id") or _new_id("call")
        output.append({"id": _new_id("fc"), "type": "function_call", "status": "completed",
                       "call_id": call_id, "name": function.get("name", ""),
                       "arguments": arguments})
    finish = choice.get("finish_reason")
    status = "incomplete" if finish == "length" else "completed"
    result = {"id": _new_id("resp"), "object": "response", "created_at": int(time.time()),
              "status": status, "error": None,
              "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
              "instructions": request_body.get("instructions"),
              "model": completion.get("model") or request_body.get("model") or "auto",
              "output": output, "parallel_tool_calls": request_body.get("parallel_tool_calls", True),
              "usage": _responses_usage(completion.get("usage"))}
    if isinstance(completion.get("router"), dict):
        result["router"] = completion["router"]
    return result


def _responses_event(event: str, data: dict, sequence: int) -> bytes:
    payload = {"type": event, **data, "sequence_number": sequence}
    return ("event: %s\ndata: %s\n\n" %
            (event, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))).encode()


async def responses_stream(chat_iterator, request_body: dict, model: str, routed):
    response = {"id": _new_id("resp"), "object": "response", "created_at": int(time.time()),
                "status": "in_progress", "error": None, "incomplete_details": None,
                "instructions": request_body.get("instructions"), "model": model,
                "output": [], "parallel_tool_calls": request_body.get("parallel_tool_calls", True),
                "usage": None}
    sequence = 0

    def emit(event: str, **data):
        nonlocal sequence
        sequence += 1
        return _responses_event(event, data, sequence)

    yield emit("response.created", response=response)
    yield emit("response.in_progress", response=response)
    text_parts = []
    message_id = None
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    buffer = ""

    def consume(data: str):
        nonlocal finish_reason, usage, message_id
        if not data or data == "[DONE]":
            return []
        try:
            chunk = json.loads(data)
        except ValueError:
            return []
        if chunk.get("error"):
            failed = {**response, "status": "failed", "error": chunk["error"]}
            return [("response.failed", {"response": failed})]
        events = []
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text = str(delta["content"])
                text_parts.append(text)
                if message_id is None:
                    message_id = _new_id("msg")
                    item = {"id": message_id, "type": "message", "role": "assistant",
                            "status": "in_progress", "content": []}
                    events.append(("response.output_item.added", {
                        "response_id": response["id"], "output_index": 0, "item": item}))
                    events.append(("response.content_part.added", {
                        "response_id": response["id"], "item_id": message_id, "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}}))
                events.append(("response.output_text.delta", {
                    "response_id": response["id"], "item_id": message_id, "output_index": 0,
                    "content_index": 0, "delta": text}))
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", len(tool_calls)))
                state = tool_calls.setdefault(index, {"id": None, "name": [], "arguments": []})
                if call.get("id"):
                    state["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    state["name"].append(str(function["name"]))
                if function.get("arguments"):
                    state["arguments"].append(str(function["arguments"]))
        return events

    try:
        async for chunk in chat_iterator:
            buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.startswith("data:"):
                    continue
                for event, data in consume(line[5:].strip()):
                    yield emit(event, **data)
                    if event == "response.failed":
                        return
        if buffer.startswith("data:"):
            for event, data in consume(buffer[5:].strip()):
                yield emit(event, **data)
                if event == "response.failed":
                    return
    finally:
        close = getattr(chat_iterator, "aclose", None)
        if close is not None:
            await close()

    output_index = 0
    if text_parts:
        full_text = "".join(text_parts)
        part = {"type": "output_text", "text": full_text, "annotations": []}
        item = {"id": message_id, "type": "message", "role": "assistant",
                "status": "completed", "content": [part]}
        yield emit("response.output_text.done", response_id=response["id"], item_id=message_id,
                   output_index=output_index, content_index=0, text=full_text)
        yield emit("response.content_part.done", response_id=response["id"], item_id=message_id,
                   output_index=output_index, content_index=0, part=part)
        yield emit("response.output_item.done", response_id=response["id"],
                   output_index=output_index, item=item)
        response["output"].append(item)
        output_index += 1

    for index in sorted(tool_calls):
        state = tool_calls[index]
        call_id = state["id"] or _new_id("call")
        name = "".join(state["name"])
        arguments = "".join(state["arguments"])
        item_id = _new_id("fc")
        item = {"id": item_id, "type": "function_call", "status": "in_progress",
                "call_id": call_id, "name": name, "arguments": ""}
        yield emit("response.output_item.added", response_id=response["id"],
                   output_index=output_index, item=item)
        if arguments:
            yield emit("response.function_call_arguments.delta", response_id=response["id"],
                       item_id=item_id, output_index=output_index, delta=arguments)
        yield emit("response.function_call_arguments.done", response_id=response["id"],
                   item_id=item_id, output_index=output_index, name=name, arguments=arguments,
                   call_id=call_id)
        item = {**item, "status": "completed", "arguments": arguments}
        yield emit("response.output_item.done", response_id=response["id"],
                   output_index=output_index, item=item)
        response["output"].append(item)
        output_index += 1

    response["model"] = model
    response["status"] = "incomplete" if finish_reason == "length" else "completed"
    if response["status"] == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    response["usage"] = _responses_usage(usage)
    if routed is not None:
        response["router"] = _router_result(routed)
    event = "response.incomplete" if response["status"] == "incomplete" else "response.completed"
    yield emit(event, response=response)
