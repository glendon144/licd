# licd — Local Inference Controller daemon

licd is a prototype Local Inference Controller for Glen's Distributed Cognition architecture.

It creates local inference/context capsules in SQLite, discovers known machines from /etc/hosts, probes local services such as clones-brain, dream-server, and tortoised, and performs first-pass distributed recollection before expensive inference.

Current milestone: v0.3 supports JSON POST requests and prefers the verified clones-brain route:

    POST /search_thoughts

This allows licd to retrieve prior context from clones-brain and use it to support future inference planning.

Current role:

licd is not an autonomous agent. It is a local preprocessing, recollection, cache, and capsule daemon.

AI Broker should remain the executive authority for routing, approvals, and policy.

Run:

    python3 licd_v0_3.py --host 127.0.0.1 --port 8777

Useful endpoints:

    GET  /health
    GET  /hosts
    GET  /service-map
    GET  /capsules
    GET  /capsule?id=<capsule_id>
    GET  /cache/search?q=<query>
    POST /capsule/create
    POST /recollect

Example recollection:

    curl -s -X POST http://127.0.0.1:8777/recollect \
      -H 'Content-Type: application/json' \
      -d '{"query":"predicting inference cost", "max_hosts":8, "max_results_per_service":3}' | jq

Milestone:

On 2026-06-01, licd reached its first recursive recollection milestone: its own retrieval of prior clones-brain context helped guide development of v0.3.
