"""End-to-end MCP smoke test: speak JSON-RPC to `stl-mcp` over real stdio.

Importing the tool functions in-process proves the wiring. It does NOT prove the
server actually starts, negotiates a protocol version, serializes its schemas, or
survives a tool call arriving as a wire message -- and those are the failures a
user sees, because they happen before any of our code runs.

    STL_HOME=$PWD .venv/bin/python scripts/smoke_mcp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / ".venv" / "bin" / "stl-mcp"
PROTOCOL = "2025-06-18"

# Tools whose `ok` reports the state of the FEED, not the state of the call.
# `stl_assert_run` returning ok:false because realtime is stale is the tool
# working correctly, and asserting ok:true on it would make this smoke test
# demand that the world be healthy before the server can be called sound.
VERDICT_TOOLS = {"stl_assert_run", "stl_report_brief", "stl_web_check", "stl_oracle_verify"}

# The one call expected to fail, to prove failures arrive as structured errors.
EXPECT_ERROR = "stl_gtfs_stop"

# Read-only calls with arguments that must work against any imported snapshot.
CALLS = [
    ("stl_doctor", {}),
    ("stl_snapshot_sources", {}),
    ("stl_gtfs_coverage", {}),
    ("stl_gtfs_stop_resolve", {}),
    ("stl_gtfs_query", {"sql": "SELECT COUNT(*) AS n FROM stops", "limit": 1}),
    ("stl_gtfs_departures", {"stop": "15111", "at": "2026-08-05T12:00:00"}),
    ("stl_gtfs_service_day", {"timestamp": "2026-08-06T00:12:00"}),
    ("stl_assert_list", {}),
    ("stl_assert_run", {}),
    ("stl_report_brief", {}),
    ("stl_web_list", {}),
    ("stl_rt_reference", {}),
    ("stl_support_repro", {"stop": "15111", "at": "2026-08-05T12:00:00"}),
    ("stl_oracle_cases", {}),
    # Deliberate failure: the client must receive a structured error with a
    # remedy, not a crash and not a traceback.
    ("stl_gtfs_stop", {"stop": "definitely-not-a-stop"}),
]


class Client:
    def __init__(self) -> None:
        env = {**os.environ, "STL_HOME": os.environ.get("STL_HOME", str(REPO))}
        self.p = subprocess.Popen(
            [str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        self._id = 0

    def call(self, method: str, params: dict | None = None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        while True:
            line = self.p.stdout.readline()
            if not line:
                err = self.p.stderr.read()
                raise RuntimeError(f"server closed stdout. stderr:\n{err}")
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                continue  # log noise on stdout would be a bug, but do not die on it
            if out.get("id") == self._id:
                return out

    def close(self) -> None:
        self.p.stdin.close()
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def main() -> int:
    if not SERVER.is_file():
        print(f"FAIL  no server at {SERVER} -- run `.venv/bin/pip install -e .`")
        return 1

    results: list[tuple[str, bool, str]] = []
    c = Client()
    try:
        init = c.call("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "stl-smoke", "version": "0"},
        })
        ok = "result" in init
        info = init.get("result", {}).get("serverInfo", {})
        results.append(("initialize", ok,
                        f"{info.get('name')} {info.get('version')} "
                        f"proto={init.get('result', {}).get('protocolVersion')}"))

        instructions = init.get("result", {}).get("instructions") or ""
        results.append(("instructions delivered", "WHERE TO START" in instructions,
                        f"{len(instructions)} chars"))

        c.call("notifications/initialized", {}, notify=True)

        listed = c.call("tools/list", {})
        tools = listed.get("result", {}).get("tools", [])
        results.append(("tools/list", len(tools) >= 45, f"{len(tools)} tools"))

        missing = [t["name"] for t in tools if not t.get("inputSchema")]
        results.append(("every tool serializes an inputSchema", not missing, str(missing[:5])))

        thin = [t["name"] for t in tools if len(t.get("description") or "") < 60]
        results.append(("every tool has a real description", not thin, str(thin[:5])))

        unann = [t["name"] for t in tools if not t.get("annotations")]
        results.append(("every tool carries annotations", not unann, str(unann[:5])))

        for name, args in CALLS:
            resp = c.call("tools/call", {"name": name, "arguments": args})
            body = resp.get("result", {})
            text = "".join(b.get("text", "") for b in body.get("content", []))
            transport_ok = "error" not in resp
            if name == EXPECT_ERROR:
                # A protocol-level success carrying a structured error the
                # model can act on -- never a crash, never a traceback.
                good = transport_ok and '"ok": false' in text and "remedy" in text
                note = "structured error with remedy" if good else text[:120]
            elif name in VERDICT_TOOLS:
                # Judge the shape, not the verdict. A well-formed report that
                # says the feed is unhealthy is a passing call.
                try:
                    parsed = json.loads(text)
                    good = transport_ok and "provenance" in parsed and "error" not in parsed
                    verdict = parsed.get("status") or (
                        f"{parsed.get('failed', 0)} failed" if "failed" in parsed else "ok")
                    note = f"well-formed, verdict={verdict}"
                except json.JSONDecodeError:
                    good, note = False, text[:120]
            else:
                good = transport_ok and '"ok": true' in text
                note = f"{len(text)} chars"
                if not good:
                    note = text[:160] or str(resp.get("error"))[:160]
            results.append((f"call {name}", good, note))
    finally:
        c.close()

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, note in results:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {note}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
