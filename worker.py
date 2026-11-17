"""Independent, sandboxed MCP worker using a runner-local, free model.

All job material is withheld from stdout. The only output containing job data
is an OpenSSL CMS encrypted envelope addressed to the local harness.
"""

import base64
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


TRANSCRIPT = []
RESPONSES = []
SESSION = None
REQUEST_ID = 0
JOB_URL = os.environ["JOB_1_URL"]
SKILL_ZIP = "https://hq.mega.dev/media/two-places/incident-lookup-03b5d3a7.zip"
SKILL_URI = "skill://incident-lookup/SKILL.md"
MODEL = "qwen3:4b"
WORKER_FILES = {}


def redact(value):
    if isinstance(value, str):
        return value.replace(JOB_URL, "[JOB_URL]")
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def log(kind, value):
    TRANSCRIPT.append({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "kind": kind, "data": redact(value)})


def parse_rpc(raw):
    text = raw.decode()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        events = []
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines()
                             if line.startswith("data:"))
            if data:
                events.append(json.loads(data))
        for event in reversed(events):
            if "result" in event or "error" in event:
                return event
        if not text.strip():
            return {}
        raise ValueError("No JSON-RPC response in event stream")


def rpc(method, params=None, notification=False):
    global SESSION, REQUEST_ID
    REQUEST_ID += 1
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notification:
        body["id"] = REQUEST_ID
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if SESSION:
        headers["Mcp-Session-Id"] = SESSION
    if method != "initialize":
        headers["MCP-Protocol-Version"] = "2024-11-05"
    log("mcp_request", body)
    request = urllib.request.Request(JOB_URL, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            SESSION = response.headers.get("Mcp-Session-Id", SESSION)
            value = parse_rpc(response.read())
    except urllib.error.HTTPError as error:
        value = {"error": {"http_status": error.code,
                           "body": error.read().decode(errors="replace")}}
    log("mcp_response", value)
    RESPONSES.append({"method": method, "params": params, "response": value})
    return value


def result_of(value):
    if "error" in value:
        raise RuntimeError("MCP request failed: " + json.dumps(value["error"]))
    return value.get("result", value)


def text_content(result):
    return "\n".join(item.get("text", "") for item in result.get("contents", [])
                     if isinstance(item, dict))


def call_model(messages):
    body = {"model": MODEL, "messages": messages, "stream": False,
            "think": False, "format": "json", "keep_alive": "45m",
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 1500}}
    log("model_request", body)
    request = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                     json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        result = json.load(response)
    log("model_response", result)
    return result["message"]["content"]


def has_receipt(value):
    if isinstance(value, dict):
        if any("receipt" in key.lower() and bool(item) for key, item in value.items()):
            return True
        return any(has_receipt(item) for item in value.values())
    if isinstance(value, list):
        return any(has_receipt(item) for item in value)
    if isinstance(value, str):
        try:
            return has_receipt(json.loads(value))
        except (ValueError, RecursionError):
            return False
    return False


def download_skill():
    request = urllib.request.Request(SKILL_ZIP, headers={"User-Agent": "MEGA-course-worker"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            if not item.is_dir() and item.file_size < 1_000_000:
                WORKER_FILES[item.filename] = archive.read(item).decode(errors="replace")
    log("skill_download", {"url": SKILL_ZIP, "files": WORKER_FILES})


def run():
    init = result_of(rpc("initialize", {"protocolVersion": "2024-11-05",
        "capabilities": {}, "clientInfo": {"name": "mega-remote-incident-worker", "version": "1"}}))
    rpc("notifications/initialized", notification=True)
    tools = result_of(rpc("tools/list"))
    resource_reply = rpc("resources/read", {"uri": SKILL_URI})
    skill = text_content(resource_reply.get("result", {}))
    download_skill()
    if not skill:
        skill = "\n".join(text for name, text in WORKER_FILES.items()
                           if name.endswith("SKILL.md"))
    if not skill:
        raise RuntimeError("The worker could not load its required skill")

    system = """You are the independent REMOTE worker for MEGA.dev S02E04 job-1.
Your job is incident lookup. Load and FOLLOW the incident-lookup skill below.
The main harness is NOT doing your job; you must complete it with your own tools.
Your only model is the free model running on this GitHub Actions machine.
Use your job MCP tools to learn the assigned task, do the lookup, and submit it.
Read each tool response and follow its course instructions. Do not invent any
record, field, result, or receipt. A rejection is not a server fault: revise your
work on evidence. On a server failure retry at most once, then report a blocker.
Do not use other services, execute commands, spend money, or ask for secrets.
Keep job links, receipts, and all task data confidential: results are encrypted
for the main harness. The tools listed below are available on YOUR job endpoint.

Return ONLY one JSON object per turn, one of these shapes:
{"action":"call","name":"EXACT_MCP_TOOL_NAME","arguments":{...}}
{"action":"resource","uri":"exact resource URI"}
{"action":"read_skill_file","path":"exact file path from the list"}
{"action":"done","result":"exact accepted result or specific blocker"}

Do not return done before the course accepts your submit and issues a receipt.
No markdown fences. Call tools one by one. Use the tool inputSchema exactly.
Do not assume tool argument names or use a wrapper around arguments.
"""
    system += "\nSERVER INSTRUCTIONS:\n" + init.get("instructions", "")
    system += "\nREQUIRED SKILL:\n" + skill
    system += "\nSKILL FILE PATHS:\n" + json.dumps(list(WORKER_FILES))
    system += "\nAVAILABLE MCP TOOLS:\n" + json.dumps(tools, ensure_ascii=False)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Begin your incident-lookup job. Follow the skill and complete your submit."}]
    tool_names = {tool["name"] for tool in tools.get("tools", [])}
    for turn in range(70):
        raw = call_model(messages)
        messages.append({"role": "assistant", "content": raw})
        try:
            command = json.loads(raw)
            action = command.get("action")
            if action == "call":
                if command.get("name") not in tool_names:
                    observation = {"error": "Unknown tool. Use an exact name from AVAILABLE MCP TOOLS."}
                else:
                    observation = rpc("tools/call", {"name": command["name"],
                                                     "arguments": command.get("arguments", {})})
                    if has_receipt(observation):
                        return {"status": "receipt_returned", "accepted_response": observation,
                                "model": MODEL, "turns": turn + 1}
            elif action == "resource":
                observation = rpc("resources/read", {"uri": command["uri"]})
            elif action == "read_skill_file":
                observation = {"text": WORKER_FILES.get(command.get("path"), "File not found")}
            elif action == "done":
                observation = {"error": "No receipt has been returned yet. Continue until submit is accepted, or return action blocked with a precise reason."}
            elif action == "blocked":
                return {"status": "blocked", "reason": command.get("result", command)}
            else:
                observation = {"error": "Return one JSON object using action call, resource, read_skill_file, or done."}
        except (ValueError, KeyError, TypeError) as error:
            observation = {"error": str(error), "instruction": "Use valid JSON and exact action schema."}
        messages.append({"role": "user", "content": json.dumps(observation, ensure_ascii=False)})
        print("Worker completed tool turn " + str(turn + 1) + ".", flush=True)
    return {"status": "turn_limit", "reason": "No accepted receipt after 70 worker turns"}


def emit_encrypted(bundle):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "result.json"
        target = Path(directory) / "result.cms"
        source.write_text(json.dumps(bundle, ensure_ascii=False))
        source.chmod(0o600)
        subprocess.run(["openssl", "cms", "-encrypt", "-aes-256-cbc", "-binary",
                        "-in", str(source), "-out", str(target), "-outform", "DER",
                        "result-recipient.pem"], check=True, capture_output=True)
        encrypted = base64.b64encode(target.read_bytes()).decode()
        print("MEGA_ENCRYPTED_RESULT_BEGIN", flush=True)
        for start in range(0, len(encrypted), 2000):
            print("MEGA_ENCRYPTED_RESULT_CHUNK " + encrypted[start:start + 2000], flush=True)
        print("MEGA_ENCRYPTED_RESULT_END", flush=True)


if __name__ == "__main__":
    try:
        outcome = run()
    except Exception as error:
        outcome = {"status": "worker_error", "type": type(error).__name__, "detail": str(error)}
        log("exception", outcome)
    emit_encrypted({"outcome": outcome, "responses": RESPONSES, "log": TRANSCRIPT,
                    "where": "GitHub Actions", "run_id": os.environ.get("GITHUB_RUN_ID"),
                    "runner": os.environ.get("RUNNER_NAME"), "model": MODEL})
    print("Encrypted worker report emitted.", flush=True)
    sys.exit(0 if outcome.get("status") == "receipt_returned" else 1)
