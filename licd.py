#!/usr/bin/env python3
"""
licd.py — Local Inference Controller daemon prototype v0.3.1

Purpose:
    A small local daemon that:
      - discovers peer systems from /etc/hosts
      - queries tortoised, clones-brain, dream-server, and optional echo_vault
      - creates local inference/context capsules in its own SQLite database
      - performs a first-pass "distributed recollection" sweep before inference
      - keeps enough provenance to later integrate with AI Broker

This is intentionally modest:
    - It does NOT execute remote commands.
    - It does NOT autonomously launch distributed inference jobs.
    - It only performs read/query style recollection against known services.
    - It stores capsules and observations locally.
    - It includes simple token/signature hooks but is not a full security layer yet.

Run:
    python3 licd.py --host 127.0.0.1 --port 8777

Useful endpoints:
    GET  /health
    GET  /diagnostics
    GET  /hosts
    GET  /service-map
    GET  /capsules
    GET  /capsule?id=<capsule_id>
    GET  /cache/search?q=<query>
    POST /capsule/create
    POST /recollect

Example:
    curl -s http://127.0.0.1:8777/health | jq
    curl -s http://127.0.0.1:8777/diagnostics | jq
    curl -s http://127.0.0.1:8777/service-map | jq

    curl -s -X POST http://127.0.0.1:8777/recollect \
      -H 'Content-Type: application/json' \
      -d '{"query":"predicting inference cost", "max_hosts":8, "max_results_per_service":3}' | jq

Environment/config knobs:
    LICD_DB=storage/licd.db
    LICD_HOSTS_FILE=/etc/hosts
    LICD_HOST_ALLOWLIST=peace,blues,jazz,optiplex310
    LICD_TIMEOUT=2.0
    LICD_REQUIRE_TOKEN=0
    LICD_SHARED_TOKEN=change-me

Default service ports:
    tortoised:    8767
    clones-brain: 8765
    dream-server: 8181
    echo_vault:   disabled unless LICD_ECHO_VAULT_PORT is set

Notes:
    Endpoint names for clones-brain, dream-server, and echo_vault may differ
    across your machines. This prototype tries several common paths and stores
    whatever useful JSON/text comes back.

    Once you upload actual service code or endpoint examples, this can be made
    surgical instead of "best effort."
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import http.server
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_DB = os.environ.get("LICD_DB", "storage/licd.db")
DEFAULT_HOSTS_FILE = os.environ.get("LICD_HOSTS_FILE", "/etc/hosts")

DEFAULT_TIMEOUT = float(os.environ.get("LICD_TIMEOUT", "2.0"))
APP_VERSION = "0.3.1"

TORTOISED_PORT = int(os.environ.get("LICD_TORTOISED_PORT", "8767"))
CLONES_BRAIN_PORT = int(os.environ.get("LICD_CLONES_BRAIN_PORT", "8765"))
DREAM_SERVER_PORT = int(os.environ.get("LICD_DREAM_SERVER_PORT", "8181"))

# echo_vault is optional because the endpoint/port may vary.
ECHO_VAULT_PORT = os.environ.get("LICD_ECHO_VAULT_PORT")
ECHO_VAULT_PORT_INT = int(ECHO_VAULT_PORT) if ECHO_VAULT_PORT else None

REQUIRE_TOKEN = os.environ.get("LICD_REQUIRE_TOKEN", "0") in ("1", "true", "yes", "on")
SHARED_TOKEN = os.environ.get("LICD_SHARED_TOKEN", "")

HOST_ALLOWLIST = {
    h.strip()
    for h in os.environ.get("LICD_HOST_ALLOWLIST", "").split(",")
    if h.strip()
}

PRIVATE_OR_LOCAL_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|::1|fe80:)",
    re.I,
)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def compact_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def truncate_text(text: str, limit: int = 1000) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def summarize_payload(payload: Any, limit: int = 1200) -> str:
    """
    Small, dependency-free summarizer. This is intentionally crude.
    Later this can call a local LLM or echo_vault semantic summarizer.
    """
    if payload is None:
        return ""
    if isinstance(payload, dict):
        interesting_keys = [
            "summary", "text", "body", "content", "result", "answer",
            "thought", "memory", "snippet", "description", "title",
            "name", "status", "node", "service", "policy",
        ]
        parts = []
        for key in interesting_keys:
            if key in payload and payload[key]:
                parts.append(f"{key}: {truncate_text(str(payload[key]), 300)}")
        if not parts:
            parts = [truncate_text(compact_json(payload), limit)]
        return truncate_text("\n".join(parts), limit)
    if isinstance(payload, list):
        chunks = [summarize_payload(item, 300) for item in payload[:5]]
        return truncate_text("\n---\n".join(chunks), limit)
    return truncate_text(str(payload), limit)


def classify_task(query: str) -> str:
    q = query.lower()
    if any(x in q for x in ("traceback", "exception", "segfault", "error", "bug", "fix code")):
        return "code_debug"
    if any(x in q for x in ("cost", "price", "token", "billing", "inference")):
        return "inference_economics"
    if any(x in q for x in ("search the web", "latest", "current", "news", "recent")):
        return "web_research"
    if any(x in q for x in ("summarize", "summary", "recap")):
        return "summarization"
    if any(x in q for x in ("calculate", "number", "spreadsheet", "csv", "compute")):
        return "number_crunch"
    if any(x in q for x in ("remember", "recall", "clones-brain", "dream", "memory")):
        return "memory_lookup"
    return "general"


def score_relevance(query: str, text: str) -> float:
    """
    Tiny lexical relevance score, good enough for v0.
    Replace/augment with embeddings later.
    """
    q_terms = {
        t for t in re.findall(r"[a-zA-Z0-9_+-]{3,}", query.lower())
        if t not in {"the", "and", "for", "with", "that", "this", "from", "have", "what", "when"}
    }
    if not q_terms:
        return 0.0
    text_l = text.lower()
    hits = sum(1 for t in q_terms if t in text_l)
    density_bonus = min(0.25, len(text_l) / 4000.0 * 0.05)
    return min(1.0, (hits / max(1, len(q_terms))) + density_bonus)


@dataclass
class HostRecord:
    host: str
    address: str
    aliases: List[str]


@dataclass
class ServiceProbe:
    host: str
    service: str
    url: str
    ok: bool
    status_code: Optional[int]
    elapsed_ms: int
    summary: str
    payload: Any = None
    error: Optional[str] = None
    relevance: float = 0.0


@dataclass
class RequestSpec:
    path: str
    method: str = "GET"
    query_params: Optional[Dict[str, Any]] = None
    json_body: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, str]] = None
    label: Optional[str] = None


class LicdDB:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS capsules (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_node TEXT,
                    user_goal TEXT NOT NULL,
                    raw_prompt TEXT NOT NULL,
                    task_type TEXT,
                    status TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    summary TEXT,
                    final_result TEXT,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS capsule_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capsule_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    host TEXT,
                    service TEXT,
                    url TEXT,
                    ok INTEGER NOT NULL,
                    status_code INTEGER,
                    elapsed_ms INTEGER,
                    relevance REAL,
                    summary TEXT,
                    payload_json TEXT,
                    error TEXT,
                    FOREIGN KEY(capsule_id) REFERENCES capsules(id)
                );

                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    task_type TEXT,
                    prompt TEXT NOT NULL,
                    prompt_summary TEXT,
                    response_summary TEXT,
                    response_json TEXT,
                    relevance REAL,
                    freshness_class TEXT,
                    source_capsule_id TEXT,
                    source_node TEXT,
                    trust_level TEXT DEFAULT 'local_self',
                    content_hash TEXT,
                    reuse_count INTEGER DEFAULT 0,
                    last_used_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_capsules_created_at
                    ON capsules(created_at);

                CREATE INDEX IF NOT EXISTS idx_capsules_task_type
                    ON capsules(task_type);

                CREATE INDEX IF NOT EXISTS idx_semantic_cache_query_hash
                    ON semantic_cache(query_hash);

                CREATE INDEX IF NOT EXISTS idx_observations_capsule
                    ON capsule_observations(capsule_id);
                """
            )

    def create_capsule(self, raw_prompt: str, user_goal: Optional[str] = None,
                       task_type: Optional[str] = None,
                       source_node: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> str:
        capsule_id = f"cap-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        task_type = task_type or classify_task(raw_prompt)
        user_goal = user_goal or raw_prompt[:200]
        t = now_iso()
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO capsules
                (id, created_at, updated_at, source_node, user_goal, raw_prompt,
                 task_type, status, query_hash, summary, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capsule_id, t, t, source_node or socket.gethostname(), user_goal,
                    raw_prompt, task_type, "created", sha256_text(raw_prompt),
                    truncate_text(raw_prompt, 500),
                    compact_json(metadata or {}),
                ),
            )
        return capsule_id

    def update_capsule_status(self, capsule_id: str, status: str,
                              summary: Optional[str] = None,
                              final_result: Optional[str] = None) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE capsules
                SET updated_at=?, status=?,
                    summary=COALESCE(?, summary),
                    final_result=COALESCE(?, final_result)
                WHERE id=?
                """,
                (now_iso(), status, summary, final_result, capsule_id),
            )

    def add_observation(self, capsule_id: str, probe: ServiceProbe) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO capsule_observations
                (capsule_id, created_at, host, service, url, ok, status_code,
                 elapsed_ms, relevance, summary, payload_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capsule_id, now_iso(), probe.host, probe.service, probe.url,
                    1 if probe.ok else 0, probe.status_code, probe.elapsed_ms,
                    probe.relevance, probe.summary,
                    compact_json(probe.payload) if probe.payload is not None else None,
                    probe.error,
                ),
            )

    def add_semantic_cache(self, capsule_id: str, prompt: str, task_type: str,
                           response: Dict[str, Any], relevance: float,
                           freshness_class: str = "fresh",
                           source_node: Optional[str] = None) -> int:
        prompt_summary = truncate_text(prompt, 500)
        response_summary = summarize_payload(response, 1200)
        content_hash = sha256_text(prompt + "\n" + compact_json(response))
        t = now_iso()
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO semantic_cache
                (created_at, updated_at, query_hash, task_type, prompt,
                 prompt_summary, response_summary, response_json, relevance,
                 freshness_class, source_capsule_id, source_node, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t, t, sha256_text(prompt), task_type, prompt,
                    prompt_summary, response_summary, compact_json(response),
                    relevance, freshness_class, capsule_id, source_node or socket.gethostname(),
                    content_hash,
                ),
            )
            return int(cur.lastrowid)

    def search_local_cache(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self.lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM semantic_cache
                ORDER BY updated_at DESC
                LIMIT 100
                """
            ).fetchall()

        scored = []
        for row in rows:
            text = " ".join(
                str(row[k] or "")
                for k in ("prompt", "prompt_summary", "response_summary", "task_type")
            )
            rel = score_relevance(query, text)
            if rel > 0:
                item = dict(row)
                item["computed_relevance"] = rel
                scored.append(item)

        scored.sort(key=lambda x: (x["computed_relevance"], x.get("updated_at", "")), reverse=True)
        return scored[:limit]

    def list_capsules(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, updated_at, source_node, user_goal,
                       task_type, status, summary
                FROM capsules
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_capsule(self, capsule_id: str) -> Optional[Dict[str, Any]]:
        with self.lock, self.connect() as conn:
            cap = conn.execute(
                "SELECT * FROM capsules WHERE id=?",
                (capsule_id,),
            ).fetchone()
            if not cap:
                return None
            obs = conn.execute(
                """
                SELECT * FROM capsule_observations
                WHERE capsule_id=?
                ORDER BY relevance DESC, created_at DESC
                """,
                (capsule_id,),
            ).fetchall()
        out = dict(cap)
        out["observations"] = [dict(r) for r in obs]
        return out


def parse_hosts_file(path: str = DEFAULT_HOSTS_FILE) -> List[HostRecord]:
    """
    Read /etc/hosts and return one preferred HostRecord per IP address.

    v0.1 treated aliases as separate machines. That was useful for proving
    discovery, but noisy: optiplex310 and optiplex could point to the same IP
    and get probed twice. v0.2 deduplicates by address while preserving aliases.
    """
    records: List[HostRecord] = []
    by_addr: Dict[str, HostRecord] = {}

    try:
        lines = Path(path).read_text(errors="ignore").splitlines()
    except Exception:
        lines = []

    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        addr = parts[0]
        names = parts[1:]
        if not PRIVATE_OR_LOCAL_RE.search(addr):
            # For v0, avoid wandering into public hostnames from /etc/hosts.
            # You can relax this later with config.
            continue

        filtered_names = []
        for name in names:
            if name in ("localhost", "localhost.localdomain"):
                continue
            if HOST_ALLOWLIST and name not in HOST_ALLOWLIST:
                continue
            filtered_names.append(name)

        if not filtered_names:
            continue

        # Prefer the first alias for this address, but preserve all aliases.
        if addr not in by_addr:
            by_addr[addr] = HostRecord(host=filtered_names[0], address=addr, aliases=filtered_names)
        else:
            merged = list(dict.fromkeys(by_addr[addr].aliases + filtered_names))
            by_addr[addr].aliases = merged

    records = list(by_addr.values())

    # Always include local host as a candidate if not already represented.
    local = socket.gethostname()
    local_names = {r.host for r in records} | {a for r in records for a in r.aliases}
    if (not HOST_ALLOWLIST or local in HOST_ALLOWLIST) and local not in local_names:
        records.append(HostRecord(host=local, address="127.0.0.1", aliases=[local]))

    return records

def render_template_value(value: Any, query: str) -> Any:
    if isinstance(value, str):
        return value.replace("{query}", query).replace("{q}", query)
    if isinstance(value, dict):
        return {k: render_template_value(v, query) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template_value(v, query) for v in value]
    return value


def http_request_json_or_text(url: str, method: str = "GET",
                              json_body: Optional[Dict[str, Any]] = None,
                              headers: Optional[Dict[str, str]] = None,
                              timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, Optional[int], Any, Optional[str], int]:
    start = time.time()
    try:
        hdrs = {
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": f"licd/{APP_VERSION}",
        }
        if headers:
            hdrs.update(headers)
        body_bytes = None
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)
            elapsed_ms = int((time.time() - start) * 1000)
            status = getattr(resp, "status", None)
            ctype = resp.headers.get("Content-Type", "")
            text = raw.decode("utf-8", errors="replace")
            if "json" in ctype.lower() or text.strip().startswith(("{", "[")):
                try:
                    return True, status, json.loads(text), None, elapsed_ms
                except json.JSONDecodeError:
                    return True, status, text, None, elapsed_ms
            return True, status, text, None, elapsed_ms
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, e.code, body, f"HTTP {e.code}", elapsed_ms
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return False, None, None, str(e), elapsed_ms


def spec_to_url(host: str, port: int, spec: RequestSpec, query: str) -> str:
    path = render_template_value(spec.path, query)
    if not path.startswith("/"):
        path = "/" + path
    params = render_template_value(spec.query_params, query) if spec.query_params else None
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}{qs}"
    return f"http://{host}:{port}{path}"


def request_probe(host: str, service: str, port: int, spec: RequestSpec, query: str,
                  timeout: float = DEFAULT_TIMEOUT) -> ServiceProbe:
    url = spec_to_url(host, port, spec, query)
    body = render_template_value(spec.json_body, query) if spec.json_body else None
    ok, status, payload, err, elapsed_ms = http_request_json_or_text(
        url, method=spec.method, json_body=body, headers=spec.headers, timeout=timeout
    )
    return make_probe(host, service, url, query, ok, status, payload, err, elapsed_ms)


def make_probe(host: str, service: str, url: str, query: str,
               ok: bool, status: Optional[int], payload: Any,
               err: Optional[str], elapsed_ms: int) -> ServiceProbe:
    summary = summarize_payload(payload)
    relevance = score_relevance(query, summary)
    return ServiceProbe(
        host=host,
        service=service,
        url=url,
        ok=ok,
        status_code=status,
        elapsed_ms=elapsed_ms,
        summary=summary,
        payload=payload,
        error=err,
        relevance=relevance,
    )


def service_probe(host: str, service: str, port: int, path: str, query: str,
                  timeout: float = DEFAULT_TIMEOUT) -> ServiceProbe:
    return request_probe(host, service, port, RequestSpec(path=path), query, timeout=timeout)


def health_preflight(host: str, service: str, port: int, query: str,
                    timeout: float = DEFAULT_TIMEOUT) -> ServiceProbe:
    return service_probe(host, service, port, "/health", query, timeout=timeout)


def _probe_snapshot(probe: ServiceProbe) -> Dict[str, Any]:
    snap: Dict[str, Any] = {
        "service": probe.service,
        "url": probe.url,
        "ok": probe.ok,
        "http_status": probe.status_code,
        "elapsed_ms": probe.elapsed_ms,
        "summary": probe.summary,
        "error": probe.error,
    }
    if probe.payload is not None and isinstance(probe.payload, dict):
        snap["response"] = probe.payload
    return snap


def _state_from_error(error: Optional[str], default: str = "local_unreachable") -> str:
    if not error:
        return default
    error_l = error.lower()
    if "connection refused" in error_l:
        return "not_listening_locally"
    if "timed out" in error_l:
        return "local_timeout"
    return default


def diagnose_local_clones_brain(timeout: float = 1.0) -> Dict[str, Any]:
    service_name = "clones-brain"
    base_url = f"http://127.0.0.1:{CLONES_BRAIN_PORT}"
    started = time.time()
    result: Dict[str, Any] = {
        "service": service_name,
        "configured": CLONES_BRAIN_PORT is not None,
        "host": "127.0.0.1",
        "port": CLONES_BRAIN_PORT,
        "base_url": base_url,
        "local_only": True,
        "remote_access_expected": False,
        "local_health_ok": False,
        "local_process_listening": False,
        "state": "unknown",
        "http_status": None,
        "elapsed_ms": None,
        "error": None,
        "security_model": "local-only by design",
        "note": "clones-brain is diagnosed from this node against its local loopback API.",
        "probes": [],
    }

    def run_probe(spec: RequestSpec) -> ServiceProbe:
        probe = request_probe("127.0.0.1", service_name, CLONES_BRAIN_PORT, spec, "licd local diagnostics", timeout=timeout)
        result["probes"].append(_probe_snapshot(probe))
        if probe.status_code is not None:
            result["local_process_listening"] = True
        return probe

    health = run_probe(RequestSpec(path="/health", method="GET", label="GET /health"))
    result["http_status"] = health.status_code
    if health.ok and health.status_code is not None and 200 <= health.status_code < 300:
        result["local_health_ok"] = True
        result["state"] = "local_healthy"
        if isinstance(health.payload, dict):
            result["health"] = health.payload
            if "thought_count" in health.payload:
                result["thought_count"] = health.payload.get("thought_count")
            if "db" in health.payload:
                result["db"] = health.payload.get("db")
        recent = run_probe(RequestSpec(path="/list_recent", method="GET", query_params={"limit": 3}, label="GET /list_recent?limit=3"))
        if recent.ok:
            result["recent"] = recent.payload
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    recent = run_probe(RequestSpec(path="/list_recent", method="GET", query_params={"limit": 3}, label="GET /list_recent?limit=3"))
    if recent.ok:
        result["state"] = "local_api_reachable_no_health_endpoint"
        result["error"] = None
        result["recent"] = recent.payload
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    search = run_probe(RequestSpec(path="/search_thoughts", method="POST", json_body={"query": "diagnostics", "limit": 1}, headers={"Content-Type": "application/json"}, label="POST /search_thoughts"))
    if search.ok:
        result["state"] = "local_api_reachable_no_health_endpoint"
        result["error"] = None
        result["search_sample"] = search.payload
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    result["error"] = health.error or recent.error or search.error
    result["state"] = _state_from_error(result["error"])
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def diagnose_local_tortoised(timeout: float = 1.0) -> Dict[str, Any]:
    service_name = "tortoised"
    base_url = f"http://127.0.0.1:{TORTOISED_PORT}"
    started = time.time()
    result: Dict[str, Any] = {
        "service": service_name,
        "configured": TORTOISED_PORT is not None,
        "host": "127.0.0.1",
        "port": TORTOISED_PORT,
        "base_url": base_url,
        "local_only": True,
        "remote_access_expected": False,
        "local_health_ok": False,
        "local_process_listening": False,
        "state": "unknown",
        "http_status": None,
        "elapsed_ms": None,
        "error": None,
        "security_model": "local-only by design",
        "note": "tortoised is diagnosed from this node against its local loopback API.",
        "probes": [],
    }

    def run_probe(spec: RequestSpec) -> ServiceProbe:
        probe = request_probe("127.0.0.1", service_name, TORTOISED_PORT, spec, "licd local diagnostics", timeout=timeout)
        result["probes"].append(_probe_snapshot(probe))
        if probe.status_code is not None:
            result["local_process_listening"] = True
        return probe

    health = run_probe(RequestSpec(path="/health", method="GET", label="GET /health"))
    result["http_status"] = health.status_code
    if health.ok and health.status_code is not None and 200 <= health.status_code < 300:
        result["local_health_ok"] = True
        result["state"] = "local_healthy"
        if isinstance(health.payload, dict):
            result["health"] = health.payload

    node = run_probe(RequestSpec(path="/node", method="GET", label="GET /node"))
    if node.ok:
        result["node"] = node.payload
        if not result["state"] or result["state"] == "unknown":
            result["state"] = "local_healthy"

    services = run_probe(RequestSpec(path="/services", method="GET", label="GET /services"))
    if services.ok:
        result["services"] = services.payload
        if not result["state"] or result["state"] == "unknown":
            result["state"] = "local_healthy"

    if result["state"] == "local_healthy":
        result["error"] = None
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    result["error"] = health.error or node.error or services.error
    result["state"] = _state_from_error(result["error"])
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def diagnose_local_dream_server(timeout: float = 1.0) -> Dict[str, Any]:
    service_name = "dream-server"
    base_url = f"http://127.0.0.1:{DREAM_SERVER_PORT}"
    started = time.time()
    result: Dict[str, Any] = {
        "service": service_name,
        "configured": DREAM_SERVER_PORT is not None,
        "host": "127.0.0.1",
        "port": DREAM_SERVER_PORT,
        "base_url": base_url,
        "local_only": True,
        "remote_access_expected": False,
        "local_health_ok": False,
        "local_process_listening": False,
        "state": "unknown",
        "http_status": None,
        "elapsed_ms": None,
        "error": None,
        "security_model": "local-only by design",
        "note": "Dream Server is diagnosed from this node against its local loopback API.",
        "probes": [],
    }

    def run_probe(spec: RequestSpec) -> ServiceProbe:
        probe = request_probe("127.0.0.1", service_name, DREAM_SERVER_PORT, spec, "licd local diagnostics", timeout=timeout)
        result["probes"].append(_probe_snapshot(probe))
        if probe.status_code is not None:
            result["local_process_listening"] = True
        return probe

    status_probe = run_probe(RequestSpec(path="/server_status", method="GET", label="GET /server_status"))
    result["http_status"] = status_probe.status_code
    if status_probe.ok and status_probe.status_code is not None and 200 <= status_probe.status_code < 300:
        result["local_health_ok"] = True
        result["state"] = "local_healthy"
        result["server_status"] = status_probe.payload
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    openapi_probe = run_probe(RequestSpec(path="/openapi.json", method="GET", label="GET /openapi.json"))
    if openapi_probe.ok and openapi_probe.status_code is not None and 200 <= openapi_probe.status_code < 300:
        result["state"] = "local_api_reachable_no_server_status"
        result["error"] = None
        if isinstance(openapi_probe.payload, dict):
            paths = openapi_probe.payload.get("paths")
            if isinstance(paths, dict):
                result["available_paths"] = sorted(str(k) for k in paths.keys())[:50]
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    recover_probe = run_probe(RequestSpec(path="/recover_context", method="POST", json_body={"query": "diagnostics", "limit": 1}, headers={"Content-Type": "application/json"}, label="POST /recover_context"))
    if recover_probe.ok:
        result["state"] = "local_api_reachable_no_server_status"
        result["error"] = None
        result["recover_sample"] = recover_probe.payload
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    result["error"] = status_probe.error or openapi_probe.error or recover_probe.error
    result["state"] = _state_from_error(result["error"])
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def build_diagnostics_snapshot(timeout: Optional[float] = None,
                               db_path: Optional[str] = None) -> Dict[str, Any]:
    timeout = min(DEFAULT_TIMEOUT, 1.0) if timeout is None else timeout
    return {
        "ok": True,
        "service": "licd",
        "version": APP_VERSION,
        "node": socket.gethostname(),
        "time": now_iso(),
        "db": str(db_path or DEFAULT_DB),
        "token_required": REQUIRE_TOKEN,
        "services": {
            "tortoised_port": TORTOISED_PORT,
            "clones_brain_port": CLONES_BRAIN_PORT,
            "dream_server_port": DREAM_SERVER_PORT,
            "echo_vault_port": ECHO_VAULT_PORT_INT,
        },
        "diagnostics": {
            "clones_brain": diagnose_local_clones_brain(timeout=timeout),
            "dream_server": diagnose_local_dream_server(timeout=timeout),
            "tortoised": diagnose_local_tortoised(timeout=timeout),
        },
    }


def _dedupe_request_specs(specs: List[RequestSpec]) -> List[RequestSpec]:
    out: List[RequestSpec] = []
    seen = set()
    for spec in specs:
        key = (spec.method.upper(), spec.path, compact_json(spec.query_params or {}), compact_json(spec.json_body or {}))
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def discover_openapi_paths(host: str, service: str, port: int, query: str) -> Tuple[List[RequestSpec], Optional[ServiceProbe]]:
    """
    If the service is FastAPI-ish, /openapi.json may tell us the real routes.
    Return candidate request specs plus the probe.
    """
    probe = service_probe(host, service, port, "/openapi.json", query)
    specs: List[RequestSpec] = []
    if not probe.ok or not isinstance(probe.payload, dict):
        return specs, probe

    raw_paths = probe.payload.get("paths", {})
    if not isinstance(raw_paths, dict):
        return specs, probe

    for path, methods in raw_paths.items():
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        if "{" in path and "}" in path:
            continue
        path_l = path.lower()
        method_blob = compact_json(methods).lower()
        looks_relevant = any(word in path_l or word in method_blob for word in (
            "search", "query", "find", "thought", "memory", "dream", "semantic", "ask", "recent"
        ))
        if not looks_relevant:
            continue

        if "post" in methods and any(word in path_l for word in ("search", "query", "thought", "semantic", "ask")):
            specs.append(RequestSpec(
                path=path,
                method="POST",
                json_body={"query": "{query}", "limit": 5},
                headers={"Content-Type": "application/json"},
                label=f"POST {path}",
            ))

        if "get" in methods:
            if "list_recent" in path_l or path_l.endswith("/recent"):
                specs.append(RequestSpec(
                    path=path,
                    method="GET",
                    query_params={"limit": 5},
                    label=f"GET {path}?limit=5",
                ))
            for param in ("q", "query", "term", "text", "prompt"):
                if any(word in path_l for word in ("search", "query", "find", "thought", "memory", "dream", "semantic", "ask")):
                    specs.append(RequestSpec(
                        path=path,
                        method="GET",
                        query_params={param: "{query}"},
                        label=f"GET {path}?{param}=...",
                    ))

    specs = _dedupe_request_specs(specs)
    specs.sort(key=lambda s: (0 if s.method.upper() == "POST" else 1, 0 if "search" in s.path.lower() else 1, s.path))
    return specs, probe


def _normalize_specs(items: List[Any]) -> List[RequestSpec]:
    specs: List[RequestSpec] = []
    for item in items:
        if isinstance(item, RequestSpec):
            specs.append(item)
        elif isinstance(item, str):
            specs.append(RequestSpec(path=item))
        else:
            raise TypeError(f"Unsupported request spec: {item!r}")
    return specs


def try_service_paths(host: str, service: str, port: int, paths: List[Any],
                      query: str, require_health: bool = True,
                      use_openapi: bool = True) -> List[ServiceProbe]:
    """
    v0.3 behavior:
      - Probe /health first.
      - If /health is unavailable, skip speculative endpoint hammering.
      - Try /openapi.json after health to learn real FastAPI routes.
      - Support both GET and JSON POST request specs.
      - Strongly prefer POST search endpoints when available.
    """
    probes: List[ServiceProbe] = []

    health = health_preflight(host, service, port, query)
    probes.append(health)
    if require_health and not health.ok:
        return probes

    candidate_specs: List[RequestSpec] = []
    if use_openapi:
        openapi_specs, openapi_probe = discover_openapi_paths(host, service, port, query)
        if openapi_probe is not None:
            probes.append(openapi_probe)
        candidate_specs.extend(openapi_specs)

    candidate_specs.extend(_normalize_specs(paths))
    candidate_specs = [s for s in _dedupe_request_specs(candidate_specs) if s.path != "/health"]

    for spec in candidate_specs:
        probe = request_probe(host, service, port, spec, query)
        probes.append(probe)

        path_l = spec.path.lower()
        is_search_like = (spec.method.upper() == "POST" and any(k in path_l for k in ("search", "query", "semantic", "thought")))

        if probe.ok and is_search_like:
            break
        if probe.ok and probe.summary and probe.relevance > 0.05:
            break

    return probes


def query_tortoised(host: str, query: str) -> List[ServiceProbe]:
    # Tortoised exposes structured identity/policy endpoints; /health may not be
    # the most meaningful route, but it is still used as a reachability preflight.
    paths: List[Any] = [
        "/node",
        "/services",
        "/policy",
    ]
    return try_service_paths(host, "tortoised", TORTOISED_PORT, paths, query, require_health=True, use_openapi=True)


def query_clones_brain(host: str, query: str) -> List[ServiceProbe]:
    # Verified best path: POST /search_thoughts with JSON body.
    # Keep /list_recent as a low-priority fallback, not as semantic search.
    paths: List[Any] = [
        RequestSpec(
            path="/search_thoughts",
            method="POST",
            json_body={"query": "{query}", "limit": 5},
            headers={"Content-Type": "application/json"},
            label="POST /search_thoughts",
        ),
        RequestSpec(path="/list_recent", method="GET", query_params={"limit": 5}, label="GET /list_recent?limit=5"),
        RequestSpec(path="/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/search", method="GET", query_params={"query": "{query}"}),
        RequestSpec(path="/query", method="GET", query_params={"q": "{query}"}),
    ]
    return try_service_paths(host, "clones-brain", CLONES_BRAIN_PORT, paths, query, require_health=True, use_openapi=True)


def query_dream_server(host: str, query: str) -> List[ServiceProbe]:
    paths: List[Any] = [
        RequestSpec(path="/search_capsules", method="POST", json_body={"query": "{query}", "limit": 5}, headers={"Content-Type": "application/json"}, label="POST /search_capsules"),
        RequestSpec(path="/recover_context", method="POST", json_body={"query": "{query}", "limit": 5}, headers={"Content-Type": "application/json"}, label="POST /recover_context"),
        RequestSpec(path="/server_status", method="GET", label="GET /server_status"),
        RequestSpec(path="/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/search", method="GET", query_params={"query": "{query}"}),
        RequestSpec(path="/dream", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/query", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/api/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/associations", method="GET", query_params={"q": "{query}"}),
    ]
    return try_service_paths(host, "dream-server", DREAM_SERVER_PORT, paths, query, require_health=False, use_openapi=True)


def query_echo_vault(host: str, query: str) -> List[ServiceProbe]:
    if ECHO_VAULT_PORT_INT is None:
        return []
    paths: List[Any] = [
        RequestSpec(path="/semantic/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/api/search", method="GET", query_params={"q": "{query}"}),
        RequestSpec(path="/query", method="GET", query_params={"q": "{query}"}),
    ]
    return try_service_paths(host, "echo_vault", ECHO_VAULT_PORT_INT, paths, query, require_health=True, use_openapi=True)

def recollect(db: LicdDB, query: str, max_hosts: int = 10,
              max_results_per_service: int = 3,
              include_services: Optional[List[str]] = None,
              capsule_id: Optional[str] = None) -> Dict[str, Any]:
    include_services = include_services or ["tortoised", "clones-brain", "dream-server", "echo_vault"]
    task_type = classify_task(query)

    if not capsule_id:
        capsule_id = db.create_capsule(
            raw_prompt=query,
            user_goal=query[:200],
            task_type=task_type,
            metadata={"operation": "recollect"},
        )
    else:
        db.update_capsule_status(capsule_id, "recollecting")

    hosts = parse_hosts_file()[:max_hosts]

    all_probes: List[ServiceProbe] = []

    for rec in hosts:
        host = rec.host
        if "tortoised" in include_services:
            all_probes.extend(query_tortoised(host, query))
        if "clones-brain" in include_services:
            all_probes.extend(query_clones_brain(host, query))
        if "dream-server" in include_services:
            all_probes.extend(query_dream_server(host, query))
        if "echo_vault" in include_services:
            all_probes.extend(query_echo_vault(host, query))

    # Store observations.
    for p in all_probes:
        db.add_observation(capsule_id, p)

    useful = [
        p for p in all_probes
        if p.ok and p.summary and (p.relevance > 0 or p.service == "tortoised")
    ]

    # Limit per service/host so one noisy source does not dominate.
    grouped_counts: Dict[Tuple[str, str], int] = {}
    filtered: List[ServiceProbe] = []
    for p in sorted(useful, key=lambda x: (x.relevance, -x.elapsed_ms), reverse=True):
        key = (p.host, p.service)
        n = grouped_counts.get(key, 0)
        if n < max_results_per_service:
            filtered.append(p)
            grouped_counts[key] = n + 1

    cache_hits = db.search_local_cache(query, limit=5)

    response = {
        "capsule_id": capsule_id,
        "created_at": now_iso(),
        "query": query,
        "task_type": task_type,
        "hosts_considered": [asdict(h) for h in hosts],
        "local_cache_hits": cache_hits,
        "matches": [asdict(p) for p in filtered],
        "probe_count": len(all_probes),
        "match_count": len(filtered),
        "notes": [
            "v0 uses lexical scoring; replace with embeddings later.",
            "tortoised results are included even with low lexical relevance because service identity/policy may still matter.",
            "echo_vault is only queried when LICD_ECHO_VAULT_PORT is set.",
        ],
    }

    # Store the recollection bundle in the semantic cache.
    relevance = max([p.relevance for p in filtered], default=0.0)
    db.add_semantic_cache(
        capsule_id=capsule_id,
        prompt=query,
        task_type=task_type,
        response=response,
        relevance=relevance,
        freshness_class="fresh",
    )

    summary_lines = [
        f"Recollection sweep for: {query}",
        f"Task type: {task_type}",
        f"Hosts considered: {len(hosts)}",
        f"Useful matches: {len(filtered)}",
    ]
    if filtered:
        summary_lines.append("Top matches:")
        for p in filtered[:5]:
            summary_lines.append(
                f"- {p.host}/{p.service} relevance={p.relevance:.2f} url={p.url}"
            )

    db.update_capsule_status(
        capsule_id,
        "recollected",
        summary="\n".join(summary_lines),
        final_result=compact_json(response),
    )

    return response


class LicdHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"licd/{APP_VERSION}"

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = pretty_json(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        data = {"ok": False, "error": message}
        if extra:
            data.update(extra)
        self._send_json(data, status=status)

    def _read_json_body(self) -> Dict[str, Any]:
        length = safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1024 * 1024))
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                return obj
            return {"value": obj}
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}")

    def _check_token(self) -> bool:
        if not REQUIRE_TOKEN:
            return True
        supplied = self.headers.get("X-LICD-Token", "")
        if not SHARED_TOKEN:
            return False
        return hmac.compare_digest(supplied, SHARED_TOKEN)

    @property
    def db(self) -> LicdDB:
        return self.server.db  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            f"{self.address_string()} - - [{self.log_date_time_string()}] {fmt % args}\n"
        )

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/health":
                self._send_json(build_diagnostics_snapshot(db_path=str(self.db.db_path)))
                return

            if path == "/diagnostics":
                self._send_json(build_diagnostics_snapshot(db_path=str(self.db.db_path)))
                return

            if path == "/hosts":
                self._send_json(
                    {
                        "ok": True,
                        "hosts_file": DEFAULT_HOSTS_FILE,
                        "allowlist": sorted(HOST_ALLOWLIST),
                        "hosts": [asdict(h) for h in parse_hosts_file()],
                    }
                )
                return

            if path == "/service-map":
                query = qs.get("q", ["service discovery"])[0]
                hosts = parse_hosts_file()
                service_map = []
                for rec in hosts:
                    entry = {"host": rec.host, "address": rec.address, "aliases": rec.aliases, "services": {}}
                    for service, port in (
                        ("tortoised", TORTOISED_PORT),
                        ("clones-brain", CLONES_BRAIN_PORT),
                        ("dream-server", DREAM_SERVER_PORT),
                    ):
                        hp = health_preflight(rec.host, service, port, query)
                        entry["services"][service] = asdict(hp)
                        if hp.ok:
                            specs, op = discover_openapi_paths(rec.host, service, port, query)
                            entry["services"][service + "_openapi"] = {
                                "ok": bool(op and op.ok),
                                "candidate_requests": [asdict(s) for s in specs[:20]],
                                "url": op.url if op else None,
                                "error": op.error if op else None,
                            }
                    if ECHO_VAULT_PORT_INT is not None:
                        hp = health_preflight(rec.host, "echo_vault", ECHO_VAULT_PORT_INT, query)
                        entry["services"]["echo_vault"] = asdict(hp)
                    service_map.append(entry)
                self._send_json({"ok": True, "service_map": service_map})
                return

            if path == "/capsules":
                limit = safe_int(qs.get("limit", ["50"])[0], 50)
                self._send_json({"ok": True, "capsules": self.db.list_capsules(limit)})
                return

            if path == "/capsule":
                cid = qs.get("id", [""])[0]
                if not cid:
                    self._send_error_json(400, "Missing id")
                    return
                cap = self.db.get_capsule(cid)
                if not cap:
                    self._send_error_json(404, "Capsule not found")
                    return
                self._send_json({"ok": True, "capsule": cap})
                return

            if path == "/cache/search":
                query = qs.get("q", [""])[0]
                if not query:
                    self._send_error_json(400, "Missing q")
                    return
                hits = self.db.search_local_cache(query)
                self._send_json({"ok": True, "query": query, "hits": hits})
                return

            self._send_error_json(404, f"Unknown endpoint: {path}")

        except Exception as e:
            self._send_error_json(
                500,
                str(e),
                {"traceback": traceback.format_exc()},
            )

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_token():
            self._send_error_json(401, "Unauthorized: missing or invalid X-LICD-Token")
            return

        try:
            data = self._read_json_body()

            if path == "/capsule/create":
                prompt = str(data.get("prompt") or data.get("query") or "").strip()
                if not prompt:
                    self._send_error_json(400, "Missing prompt or query")
                    return
                cid = self.db.create_capsule(
                    raw_prompt=prompt,
                    user_goal=data.get("user_goal") or prompt[:200],
                    task_type=data.get("task_type") or classify_task(prompt),
                    source_node=data.get("source_node") or socket.gethostname(),
                    metadata=data.get("metadata") or {},
                )
                self._send_json({"ok": True, "capsule_id": cid})
                return

            if path == "/recollect":
                query = str(data.get("query") or data.get("prompt") or "").strip()
                if not query:
                    self._send_error_json(400, "Missing query or prompt")
                    return

                include_services = data.get("include_services")
                if include_services is not None and not isinstance(include_services, list):
                    self._send_error_json(400, "include_services must be a list")
                    return

                result = recollect(
                    db=self.db,
                    query=query,
                    max_hosts=safe_int(data.get("max_hosts"), 10),
                    max_results_per_service=safe_int(data.get("max_results_per_service"), 3),
                    include_services=include_services,
                    capsule_id=data.get("capsule_id"),
                )
                self._send_json({"ok": True, **result})
                return

            self._send_error_json(404, f"Unknown endpoint: {path}")

        except ValueError as e:
            self._send_error_json(400, str(e))
        except Exception as e:
            self._send_error_json(
                500,
                str(e),
                {"traceback": traceback.format_exc()},
            )


def run_server(host: str, port: int, db_path: str) -> None:
    db = LicdDB(db_path)
    server = http.server.ThreadingHTTPServer((host, port), LicdHandler)
    server.db = db  # type: ignore[attr-defined]
    print(f"licd v{APP_VERSION} listening on http://{host}:{port}")
    print(f"database: {db.db_path}")
    print(f"token required: {REQUIRE_TOKEN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nlicd shutting down")
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local Inference Controller daemon prototype")
    parser.add_argument("--host", default=os.environ.get("LICD_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LICD_BIND_PORT", "8777")))
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--once", metavar="QUERY", help="Run one recollection sweep and print JSON")
    parser.add_argument("--max-hosts", type=int, default=10)
    args = parser.parse_args(argv)

    if args.once:
        db = LicdDB(args.db)
        result = recollect(db=db, query=args.once, max_hosts=args.max_hosts)
        print(pretty_json({"ok": True, **result}))
        return 0

    run_server(args.host, args.port, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
