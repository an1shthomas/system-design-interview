# Design a Metrics Monitoring Platform (Datadog)

---

## Step 1: Clarifying Requirements

**🎤 Interviewer:** "Design a metrics monitoring platform like Datadog. Where would you like to start?"

**👨‍💻 Candidate:** "Before jumping into the design, I'd like to ask a few clarifying questions."

> **Functional scope:**
> - "Are we covering the full observability stack — metrics, logs, and traces — or just metrics?" → *Just metrics*
> - "Should we support both infrastructure metrics (CPU/memory) and custom application metrics (request counts, business counters)?" → *Yes, both*
> - "For dashboards, are we building the visualization layer or just the backend query API?" → *Just the backend — API + storage*
> - "For alerting, what notification channels? Slack, PagerDuty, email?" → *Yes, all three*
>
> **Scale:**
> - "How large is the fleet we're monitoring?" → *500,000 servers*
> - "How frequently do servers emit metrics?" → *Every 10 seconds*
> - "How long do we need to retain data?" → *1 year*
>
> **Consistency & latency:**
> - "How quickly do alerts need to fire after a breach?" → *Sub-minute is fine*
> - "For dashboards, are stale reads okay?" → *Eventual consistency is fine*

**📐 Back-of-the-envelope (candidate does this out loud):**

"Let me quickly size the problem so our design decisions are grounded in numbers."

| Parameter | Value |
|---|---|
| Servers | 500,000 |
| Metrics per server | 100 |
| Emit frequency | Every 10s |
| **Metrics/second** | **500K × 100 / 10 = 5M/s** |
| Bytes per data point | ~150B (name ~20B + timestamp 8B + value 8B + labels ~100B) |
| Raw ingestion rate | ~750 MB/s |
| Raw data per day | ~65 TB → ~3–6 TB with 10–20x compression |
| 1 year (with rollups) | ~1–2 PB, manageable with tiered retention |

> "This is clearly a write-heavy system with bursty reads — engineers query during incidents. These two patterns need to be designed independently."

**🎤 Interviewer:** "What are the most important non-functional requirements given that scale?"

**👨‍💻 Candidate:**
- High write throughput — 5M metrics/sec, must not drop data
- Low-latency reads — dashboard queries should return in seconds even over weeks of data
- Alert reliability — alerts must fire even if parts of the system are degraded
- High availability — the monitoring system is most critical exactly when things are going wrong
- Cardinality control — unbounded label combinations can silently kill the system

> **✅ Key insight a staff engineer shows here:**
> Don't just list requirements mechanically — connect the scale math to design consequences ("write-heavy and read-bursty means we separate these paths") and proactively name cardinality as a non-obvious but critical constraint.

---

## Step 2: Core Entities & Data Modeling

**🎤 Interviewer:** "Before we get into the architecture, walk me through the core entities and how they relate."

**👨‍💻 Candidate:** "Let me identify the key 'nouns' in the system — getting this right avoids a lot of confusion later."

```
Label       → key-value pair giving context  e.g. host="server-1", region="us-east"
Metric      → name + labels + value + timestamp
              e.g. cpu_usage{host="server-1", region="us-east"} = 0.73 at t=1640000000
Series      → unique (metric name + label set) → sequence of data points over time
              cpu_usage{host="server-1"} over 30 days = one series
              cpu_usage{host="server-2"}             = a different series
Data Point  → single (timestamp, value) entry — written at 5M/sec
Alert Rule  → query + threshold + for-duration + notification channels
Dashboard   → collection of panels backed by metric queries (drives the read path)
```

> "A 'series' is the atomic unit of storage. The number of unique series — called **cardinality** — is the primary scaling challenge. I'll come back to this in the deep dive."

**🎤 Interviewer:** "You mentioned cardinality. Can you give a quick example of how it can explode?"

**👨‍💻 Candidate:** "Take `http_requests` with labels: `host` (500K values) × `endpoint` (200) × `status_code` (10) × `method` (5):

500,000 × 200 × 10 × 5 = **5 billion** theoretical series

Each series has overhead in the storage engine — its own index entry, in-memory tracking, and write buffer. At that scale the database degrades silently: write throughput drops, memory spikes, queries slow down. This is why cardinality is a first-class design concern, not an afterthought."

> **✅ What makes this staff-level:**
> - Connects entities to the scaling problem (series → cardinality)
> - Doesn't just list entities — explains why each one matters to the design
> - Proactively foreshadows the cardinality deep dive without getting lost in it yet

---

## Step 3: High-Level Architecture

**🎤 Interviewer:** "Walk me through the high-level architecture. How does data flow end to end?"

**👨‍💻 Candidate:** "I'll split the system into three paths — write, read, and alert — because they have fundamentally different characteristics and need to be designed independently."

```
[Servers]
    ↓  agent (local batching every 10s)
[Kafka]  ←————————————————————— [Cardinality Enforcer (Redis + Bloom Filter)]
    ↓  ingestion consumers
[Ingestion Service]
    ↓
[Time-Series DB] ←→ [Rollup Worker]
    ↑
[Query Service] ←→ [Redis Cache]
    ↑
[Dashboard / Users]

[Alert Rules DB (Postgres)]
    ↓
[Alert Evaluator] ——polls——→ [Time-Series DB]
    ↓
[Notification Service] → Kafka → Slack / PagerDuty / Email
```

### ✍️ Write Path

> "The simplest approach: each server POSTs metrics directly to an ingestion service which writes to a database. This fails at 5M metrics/sec — the ingestion service is a bottleneck and the database gets hammered with no buffer."

**Real approach:** `Agent → Kafka → Ingestion Consumers → Time-Series DB`

- **Agent** (on every server): lightweight daemon that collects metrics locally, batches every 10s, flushes to Kafka. Instead of 5M individual writes/sec hitting a central service, agents batch locally → **~50K batched requests/sec**. 100x load reduction at the edge.
- **Kafka**: durable high-throughput buffer. Partitioned by `hash(metric_name + labels)` so the same series always lands on the same partition (important for ordering). Gives us backpressure handling, durability, and replay capability.
- **Ingestion Service**: validates data points, enforces cardinality limits, writes batches to the TSDB.
- **Time-Series DB**: append-optimized, time-partitioned, columnar compression.

### 📖 Read Path

`User → Query Service → Redis Cache → Time-Series DB`

- **Query Service**: accepts a PromQL-like DSL, selects the appropriate rollup resolution based on the requested `step`, returns formatted results.
- **Redis Cache**: caches query results keyed by `hash(query + time_range)`. Hit rates are very high — a 24-hour dashboard window queried every 30s shifts by only 30s.

### 🔔 Alert Path

`Alert Evaluator → (polls Time-Series DB) → Notification Service → Slack / PagerDuty / Email`

- **Alert Evaluator**: fetches active alert rules from Postgres every ~30s, executes metric queries, checks if thresholds are breached for the required duration.
- **Notification Service**: handles deduplication, grouping, silencing, and escalation. Writes alert events to Kafka before dispatching to external channels — if Slack is down, the alert isn't lost.

**🎤 Interviewer:** "Why Kafka? Couldn't you use a load-balanced ingestion service with auto-scaling?"

**👨‍💻 Candidate:** "Auto-scaling helps the ingestion service but just moves the bottleneck downstream to the database — 50 instances all hammering storage simultaneously with no buffer. Kafka decouples them. It also gives durability: if the DB slows for 2 minutes, Kafka holds those metrics and consumers catch up. Without it, you're dropping data during an incident — exactly when you need it most."

**🎤 Interviewer:** "What's the trade-off of agents vs. servers pushing directly?"

**👨‍💻 Candidate:** "The main trade-off is operational complexity — you now have a daemon to deploy, version, configure, and monitor on 500K servers. But the benefits outweigh it: local buffering (metrics survive brief network outages), local aggregation (compute percentiles at the edge), and 100x reduction in central ingestion load. Every production-grade monitoring system — Datadog, Prometheus, OTEL — uses this pattern for exactly these reasons."

> **✅ What makes this staff-level:**
> - Starts with the naive approach and explains why it fails before proposing the real solution
> - Clearly separates write, read, and alert paths with different design rationale for each
> - Justifies every component with trade-offs, not just "I'd use Kafka because it's good"
> - Connects decisions back to the scale numbers established in Step 1

---

## Step 4: API Design

**🎤 Interviewer:** "Let's define the API. What interfaces does this system expose and to whom?"

**👨‍💻 Candidate:** "There are three distinct APIs serving different clients with very different usage patterns."

### 📥 Ingestion API
*Client: Agents (machine-to-machine, extremely high volume)*

```
POST /v1/metrics/ingest
Content-Type: application/x-protobuf

{
  "metrics": [
    { "name": "cpu_usage",
      "labels": { "host": "server-1", "region": "us-east" },
      "value": 0.73,
      "timestamp": 1640000000 }
  ]
}

→ 202 Accepted
```

| Decision | Reason |
|---|---|
| Protobuf, not JSON | 3–10x smaller on the wire, faster to parse. At 50K req/s this matters. |
| Batched requests | Amortizes HTTP overhead across many metrics. |
| `202 Accepted` | Async — publishes to Kafka and returns immediately, does not wait for DB write. |
| Timestamps from the agent | Preserves actual observation time, handles late/out-of-order data correctly. |

### 📊 Query API
*Client: Dashboards, on-call engineers (human-driven, bursty, read-heavy)*

```
GET /v1/metrics/query
  ?query=avg(cpu_usage{region="us-east"})
  &start=1640000000
  &end=1640086400
  &step=60

→ 200 OK
{
  "metric": "cpu_usage",
  "labels": { "region": "us-east" },
  "datapoints": [
    { "timestamp": 1640000000, "value": 0.71 },
    ...
  ]
}
```

| Decision | Reason |
|---|---|
| GET, not POST | Idempotent and cacheable — Redis uses the full URL as cache key. |
| `step` parameter | Query service uses this to select the right rollup tier (e.g., 30-day + `step=3600` → hourly rollup, not raw points). |
| PromQL-like DSL | Allows the query service to optimize execution — push filters to storage, pick the right rollup resolution. |

### 🔔 Alert Rules API
*Client: Engineers configuring monitoring (low volume, write-once-read-many)*

```
POST /v1/alerts/rules
{
  "name": "High CPU - US East",
  "query": "avg(cpu_usage{region='us-east'}) > 0.9",
  "for": "5m",
  "severity": "critical",
  "notifications": ["slack:#oncall-infra", "pagerduty:team-platform"]
}
→ 201 Created  { "rule_id": "rule_abc123" }

GET    /v1/alerts/rules               → list all rules
GET    /v1/alerts/rules/{rule_id}     → get one rule
PUT    /v1/alerts/rules/{rule_id}     → update a rule
DELETE /v1/alerts/rules/{rule_id}     → delete a rule
GET    /v1/alerts/active              → currently firing alerts
```

| Decision | Reason |
|---|---|
| `for` duration | Prevents flapping — alert only fires if condition is continuously breached for the full duration. |
| `severity` | Notification Service routes differently — critical → PagerDuty immediately, warning → Slack. |
| Rule definition ≠ alert state | `POST /alerts/rules` defines the condition. `GET /alerts/active` shows what's currently firing. Stored separately. |

**🎤 Interviewer:** "You mentioned protobuf for ingestion. What about the query API?"

**👨‍💻 Candidate:** "For the query API, JSON is fine. The volume is much lower — human-driven, maybe thousands of requests per minute at peak. Readability and debuggability outweigh wire efficiency. I'd only reach for protobuf on the hot path where volume justifies the added complexity."

**🎤 Interviewer:** "How would you handle auth and rate limiting on the ingestion API?"

**👨‍💻 Candidate:** "Each agent authenticates with an API key scoped to a tenant, passed as a header. An API gateway in front handles auth validation and rate limiting per key — two purposes: protect the system from runaway agents, and enforce fair usage in a multi-tenant setup. I'd put this in the gateway, not the ingestion service itself, so the ingestion service stays focused purely on throughput."

> **✅ What makes this staff-level:**
> - Justifies every design decision — protobuf vs. JSON, 202 vs. 200, GET vs. POST
> - Thinks about who the client is and designs the API accordingly (agent vs. human)
> - Proactively raises auth, rate limiting, and caching as first-class concerns
> - Connects API design back to the system's scaling constraints

---

## Step 5: Deep Dives

### 🗄️ Deep Dive 1: Storage & Query Performance

**🎤 Interviewer:** "You mentioned a time-series database. Why not just use Postgres?"

**👨‍💻 Candidate:**

> "Let me walk through why Postgres breaks at our scale before explaining what we'd use instead."

- At 5M writes/sec, Postgres B-tree indexes become a bottleneck — every insert updates the index; B-trees don't handle append-heavy workloads well
- Queries like "avg CPU over 30 days for 1,000 servers" require scanning billions of rows — painfully slow even with indexes
- Retention management (deleting old data) causes table bloat, autovacuum pressure, and write amplification
- Row-by-row storage wastes space — time-series data compresses dramatically when stored together

**Why a Time-Series DB (InfluxDB, VictoriaMetrics, TimescaleDB):**

| Property | Mechanism |
|---|---|
| Append-only writes | LSM-tree storage — sequential appends, no random I/O, no index update overhead |
| Time-based partitioning | Data chunked into time blocks; drop old data = delete a block (no vacuum, no fragmentation) |
| Columnar compression | Delta encoding for timestamps, XOR (Gorilla) for values → ~20x compression |

**Rollup strategy:**

| Resolution | Retention | Use Case |
|---|---|---|
| Raw (10s) | 2 days | Debugging recent incidents |
| 1-minute rollup | 2 weeks | Short-term trend analysis |
| 1-hour rollup | 90 days | Capacity planning |
| 1-day rollup | 2 years | Long-term trends / compliance |

A background **Rollup Worker** continuously computes aggregates (min, max, avg, count, sum + histograms for percentiles) from raw data.

**🎤 Interviewer:** "What about percentile metrics like p99 latency — can you compute those from rollups?"

**👨‍💻 Candidate:** "This is a subtle but important point. You **cannot** accurately compute percentiles from pre-aggregated averages — taking the average of averages loses the distribution. The right approach is to store **histograms** at each rollup level. Each data point for a latency metric is bucketed (0–10ms, 10–50ms, 50–100ms, etc.), and the histogram is what gets rolled up. From a histogram you can estimate any percentile at query time. This is exactly how Prometheus histograms and Datadog distributions work."

**🎤 Interviewer:** "How do you handle low-latency dashboard queries over weeks of data?"

**👨‍💻 Candidate:** "Three layers working together:
1. **Rollups** — the right resolution means fewer data points to scan
2. **Redis caching** — split queries into a cached historical chunk and a fresh recent chunk. The historical part is a cache hit; only the last few minutes hit the DB. Most dashboard queries are served from Redis in under 50ms.
3. **Pre-computation** — a background job identifies frequently-executed queries and pre-computes results on a schedule, warming the cache before users ask."

**Sharding:** Hash by `metric_name + label_set`. All data points for one series land on the same shard — no cross-shard fan-out for single-series time-range queries. Cross-series aggregations fan out in parallel and merge at the query layer.

---

### 🔔 Deep Dive 2: Alerting & Notifications

**🎤 Interviewer:** "Walk me through the alerting system in detail."

**👨‍💻 Candidate:** "I'll split this into two parts: alert evaluation and notification delivery — they're separate concerns with different failure modes."

#### Part A: Alert Evaluation

**Polling approach (baseline):**
- Fetches all active alert rules from Postgres (cached in-memory, refreshed every ~30s)
- Executes the metric query against the TSDB for each rule
- Checks if the condition is breached continuously for the `for` duration
- At 10,000 rules every 30s → ~333 queries/sec — very manageable
- Run multiple instances, each owning a shard of rules (by `rule_id` hash) for redundancy

**🎤 Interviewer:** "What if someone needs alerts in under 5 seconds?"

**👨‍💻 Candidate:** "Polling fundamentally can't go below its interval. For sub-10-second alerting we need stream processing with **Flink**:
- Flink runs as a second consumer on the same Kafka topic
- Maintains windowed in-memory state per series (e.g., 5-minute sliding window)
- Evaluates conditions against that state — no DB query needed
- Latency: 2–5 seconds after the metric arrives in Kafka, vs. up to 60s with polling

Trade-offs to call out proactively:
- **Operational complexity**: Flink clusters need checkpointing, state management, careful failure recovery
- **Rule changes are hard**: updating a rule mid-stream without losing in-flight window state is non-trivial
- **Memory pressure**: windowed state for millions of series across thousands of rules needs careful capacity planning

My recommendation: use polling for the vast majority of alerts — simpler, cheaper, sub-minute is fine. Reserve Flink for a premium 'real-time alerts' tier users explicitly opt into. This is actually how Datadog structures it."

#### Part B: Notification Delivery

**🎤 Interviewer:** "How do you make notifications reliable without overwhelming on-call engineers?"

**👨‍💻 Candidate:** "Having the Alert Evaluator call Slack directly is fragile — if Slack has a 30-second outage, we lose the alert exactly when we need it. And if 500 servers breach a CPU threshold simultaneously, the engineer gets 500 pages. Neither is acceptable."

The Alert Evaluator emits events to Kafka. The **Notification Service** consumes from that topic and handles:

| Concern | Mechanism |
|---|---|
| **Deduplication** | Alert state machine: `inactive → pending → firing → resolved`. One page when it starts, one when it ends — not one per evaluation cycle. |
| **Grouping** | Collect events in a 30s window, group by common labels — e.g., 47 CPU alerts in `us-east` → one notification. |
| **Silencing** | Check incoming alerts against active silence rules before dispatching. |
| **Escalation** | If no ack within N minutes, re-notify via escalation channel. |

Reliability: since alert events are durably in Kafka, the Notification Service uses at-least-once delivery — Kafka offset only committed after successful delivery. A crash mid-delivery replays on recovery.

**🎤 Interviewer:** "What about the Notification Service itself going down?"

**👨‍💻 Candidate:** "It just resumes from its last committed offset — no events are lost. I'd run multiple instances in a consumer group. The bigger risk is a prolonged outage where Kafka retention expires — so I'd set alert topic retention to 48 hours. I'd also add **meta-monitoring**: a watchdog that independently checks whether the Notification Service is processing events, and pages through a completely separate out-of-band channel (e.g., direct SES email that doesn't go through our own stack) if it falls behind."

**🎤 Interviewer:** "How would you monitor the monitoring system?"

**👨‍💻 Candidate:** "The wrong answer is to use the same monitoring system to monitor itself — if it's down, so is your monitoring of it. I'd use a completely separate, minimal meta-monitoring stack — possibly a managed service like AWS CloudWatch or a simple external uptime checker. It watches for: ingestion lag in Kafka, alert evaluator heartbeats, notification service throughput, and TSDB write success rates. Notifies through an out-of-band channel, not through the system being monitored."

> **✅ What makes this staff-level:**
> - Starts simple (polling) and justifies when to add complexity (Flink only for real-time tier)
> - Treats notification delivery as a separate reliability problem from alert evaluation
> - Proactively covers deduplication, grouping, silencing, escalation — the real-world messiness
> - Addresses the "monitoring the monitor" meta-problem with a concrete answer

---

### ⚡ Deep Dive 3: High Availability & Failure Handling

**🎤 Interviewer:** "Monitoring systems are most critical exactly when everything else is on fire. How do you ensure this stays up during failures? And how do you handle late or out-of-order data?"

**👨‍💻 Candidate:** "Let me walk through failure modes layer by layer, then cover out-of-order data separately."

| Layer | Failure Mode | Mitigation |
|---|---|---|
| **Agent** | Network partition | Local disk buffer (bounded, e.g., 100MB). Drop oldest first on overflow — recent data is more valuable. Retry with exponential backoff + jitter. |
| **Kafka** | Broker failure | Replication factor 3 across AZs. ISR acks before producer confirmation. Consumer group reassigns partitions on crash within seconds. 48h retention for catch-up. |
| **Ingestion Service** | Instance crash | Stateless — LB routes around it. Idempotent writes: deterministic `hash(name + labels + timestamp)` deduplicates replayed batches. |
| **Time-Series DB** | Node failure / slow queries | Multi-node with replication. Separate read replicas for dashboards so expensive queries can't starve the write path. Circuit breaker: back off gracefully rather than overwhelming the DB. |
| **Alert Evaluator** | Crash mid-cycle | Stateless per cycle. Watchdog reassigns rules if any rule goes unevaluated for 2× its interval. |
| **Notification Service** | Crash mid-delivery | Resumes from last committed Kafka offset on restart. Run multiple instances in a consumer group. |

**The catch-up problem:**

> "5 minutes of downtime = ~1.5B metrics backlog. At 100% normal utilization, catch-up takes another 5 minutes (10 min total behind). At 80%, it takes much longer — a dangerous positive feedback loop. **Design for 50% normal utilization** specifically to have headroom for catch-up. Add a circuit breaker: if Kafka lag exceeds a threshold, start dropping lower-priority metrics to protect critical ones."

**Graceful degradation tiers:**

| Component Down | Impact | Degraded Behavior |
|---|---|---|
| One ingestion instance | None | LB routes around it |
| Kafka partition leader | Seconds of delay | Automatic leader election |
| TSDB read replica | Dashboard slowness | Fall back to primary, extend cache TTL |
| Alert Evaluator instance | Some rules delayed | Other instances cover via watchdog |
| Notification Service | Alert delivery delayed | Kafka buffers, retries on recovery |
| Full TSDB primary | No writes or reads | Metrics buffer in Kafka; dashboards serve stale cache with staleness warning |

> "The system should never fully go dark. Even in a catastrophic TSDB failure, agents keep buffering locally, Kafka keeps accumulating, and dashboards serve cached data with a staleness warning."

**🎤 Interviewer:** "How do you handle metrics that arrive late or out of order?"

**👨‍💻 Candidate:** "Late data comes in two flavors:"

| Lateness | Handling |
|---|---|
| Slightly late (seconds–minutes) | TSDB accepts writes for any timestamp — inserts into the right time block. No special handling needed. |
| Very late (hours — e.g., agent buffer flush after partition) | Mark affected rollup blocks as "dirty". Background recomputation job re-rolls them. Queries can optionally specify `include_late_data=true` to re-scan raw data instead of rollups. |
| Out-of-order within a series | LSM-based TSDBs handle this natively — sort by timestamp during compaction. No special handling at ingestion. |

**🎤 Interviewer:** "Where do you draw the line? How late is too late?"

**👨‍💻 Candidate:** "I'd configure a maximum out-of-order tolerance window — e.g., 2 hours. Data older than that is rejected at ingestion with a specific error code so the agent knows not to retry. This prevents unbounded rollup recomputation and protects against a misbehaving agent suddenly dumping days of buffered data. The 2-hour threshold is configurable per tenant — a high-value customer might get a longer window."

> **✅ What makes this staff-level:**
> - Thinks about every layer independently with specific failure modes and mitigations
> - Addresses the catch-up problem quantitatively — not just "Kafka buffers it" but "here's the math on why we need 50% headroom"
> - Has a graceful degradation strategy — the system never fully goes dark
> - Handles out-of-order data with nuance — distinguishes slightly late vs. very late vs. out-of-order and proposes appropriate solutions for each

---

### 📈 Deep Dive 4: Cardinality Explosion

**🎤 Interviewer:** "You've mentioned cardinality a few times. Let's go deep. What exactly is the problem and how would you solve it?"

**👨‍💻 Candidate:** "Cardinality explosion is one of the sneakiest problems in metrics systems because it doesn't fail loudly — it degrades silently until the system falls over."

**The problem:**

Every unique `(metric_name + label_set)` creates a new series. Each series has TSDB overhead: an index entry, in-memory write buffer, and WAL entry — maybe 1–5KB per series. At 10M active series × 5KB = 50GB of RAM just for series metadata. At some point the TSDB starts swapping, index lookups slow down, write throughput collapses, queries time out.

> "The particularly nasty part: degradation is gradual. Works fine at 1M series, starts slowing at 5M, becomes unstable at 20M. By the time you notice, you're already in trouble."

**Common causes:**
- User IDs / request IDs / trace IDs as label values — unbounded unique values
- Unnormalized URL paths as `endpoint` labels (`/api/users/12345` vs. `/api/users/{id}`)
- Timestamps as label values — catastrophic

**Detection:**

- **Per-metric series count** tracked in Redis, exposed as a meta-metric `tsdb_series_count{metric="http_requests"}` — alert if > threshold
- **Rate-of-change alerts**: fire before the absolute limit is hit (e.g., if `http_requests` grows from 100K to 500K series in 10 minutes)
- **Daily top-N cardinality report**: ranks metrics by series count, emails the top 10 — forces visibility

**Prevention — Policy Store (Postgres):**

```yaml
metric: http_requests
allowed_labels: [host, region, endpoint, status_code, method]
max_series: 500,000
per_label_value_limits:
  endpoint: 1000
  status_code: 50
```

Ingestion Service strips unknown label keys silently and rejects data points exceeding per-label value limits. Teams must explicitly register new labels through a review process — a natural gate.

**Series cap enforcement:**

```
For each incoming data point:
  series_id = hash(metric_name + sorted_label_set)
  if SISMEMBER series_set:{metric_name} series_id  →  accept (existing series)
  else:
    if current_count < cap  →  SADD + accept
    else                    →  drop + increment dropped_metrics + fire alert
```

**🎤 Interviewer:** "5M Redis lookups per second is expensive. How do you optimize that?"

**👨‍💻 Candidate:** "This is where it gets interesting. The hot-path check — 'have I seen this series before?' — is a membership query. The vast majority of incoming data points are for **existing** series. New series creation is rare. So we use a **Bloom filter** per metric in memory on each Ingestion Service instance:

- `"definitely not seen"` → new series → go to Redis to confirm and register
- `"probably seen"` → skip Redis, accept immediately

Bloom filters have no false negatives — existing series always pass. False positives occasionally let a new series slip through the cap check — minor over-count, not catastrophic.

- Memory: ~12MB for 10M series at 1% false positive rate
- Sync from Redis every ~5 minutes to correct drift
- Each ingestion instance has its own local Bloom filter — acceptable since cap enforcement is approximate anyway"

**Recovery runbook when cardinality explodes in production:**
1. Automated cap enforcement kicks in → `dropped_metrics` alert fires
2. Cardinality report identifies the offending metric and label (e.g., `user_id`)
3. Update Policy Store allowlist to remove the bad label → takes effect at ingestion immediately
4. TSDB cleanup: dead series stop receiving data; tombstone them to exclude from query planning before the retention window expires
5. Post-mortem: add label pattern linter to CI for metric registration PRs

**🎤 Interviewer:** "What about multi-tenancy — if one team causes a cardinality explosion, should it affect others?"

**👨‍💻 Candidate:** "Absolutely not. Per-tenant cardinality budgets enforced at ingestion — one team hitting their budget hits their own cap, not a global one. In the TSDB, large tenants get physically isolated clusters. Strict per-tenant query quotas prevent one tenant's expensive query from starving another's. This is one of the hardest parts of building a SaaS monitoring platform — Datadog charges by custom metrics volume precisely because cardinality is so operationally expensive."

> **✅ What makes this staff-level:**
> - Explains cardinality from first principles — not just "it's bad" but why, with math
> - Covers detection, prevention, and recovery — the full lifecycle
> - Goes deep on the Bloom filter optimization — shows algorithmic thinking
> - Raises multi-tenancy isolation proactively — a real-world concern most candidates miss
> - Connects to business reality — Datadog's pricing model is a direct consequence of this problem

---

## Step 6: Wrap-up & Trade-offs

**🎤 Interviewer:** "We're coming up on time. Summarize your design, the key trade-offs, and what you'd do differently given more time."

**👨‍💻 Candidate:**

### System Summary

**Write path** (optimized for throughput & durability):
`Agents → Kafka → Ingestion Service (cardinality enforcement, idempotent writes) → Time-Series DB`

**Read path** (optimized for low latency):
`User → Query Service (DSL translation, rollup tier selection) → Redis Cache → Time-Series DB (rollups + raw)`

**Alert path** (optimized for reliability):
`Alert Evaluator (sharded, polling) → Kafka → Notification Service (dedup, grouping, silencing, escalation) → Slack / PagerDuty / Email`

**Supporting components:**
- **Postgres**: alert rules, cardinality policies, tenant config
- **Redis**: query cache, series cardinality tracker, Bloom filter sync
- **Rollup Worker**: background computation of 1-min / 1-hour / 1-day aggregates
- **Meta-monitor**: independent stack watching ingestion lag, evaluator heartbeats, notification throughput

### Key Trade-offs

| Decision | Choice | Rationale |
|---|---|---|
| Alert evaluation | Polling (30–60s) | Sub-minute SLA is met. Flink reserved for a premium real-time tier — it's dramatically simpler to operate. |
| Dashboard freshness | Eventual consistency (cache + rollups) | Sub-second response time beats perfect freshness during incidents. |
| Cardinality enforcement | Bloom filter (approximate) | Exact Redis lookup per data point too expensive at 5M/s. 0.1% over cap is not a crisis. |
| Collection model | Agent-based push | 100x ingestion load reduction, local buffering, every production monitoring system uses this. |
| Long-range queries | Rollups with histograms | Without rollups, long-range queries scan billions of raw points. Histograms preserve percentile accuracy. |

### What I'd do differently given more time

- **Push vs. pull debate**: Prometheus uses pull (monitoring system scrapes each server) — interesting properties (dead servers auto-detected, no server-side config) but doesn't scale easily to 500K servers
- **Multi-region replication**: metrics written to primary region, async-replicated to secondary; alert evaluation in both with deduplication at the Notification Service layer
- **Schema registry**: versioned metric schemas with backward compatibility rules, similar to Confluent Schema Registry — prevents breaking changes
- **Tiered storage**: move rollup data > 90 days to object storage (S3/GCS) as Parquet, query with Athena or BigQuery — dramatically reduces storage costs for cold data
- **Tenant-aware query routing**: large tenants with massive cardinality should get physically isolated TSDB clusters — hard to retrofit, needs to be designed in from the start

---

## Core Insight

> A metrics monitoring system is fundamentally **three different problems** that happen to share a storage layer:
> 1. **Extreme write throughput** → solved by edge batching, Kafka buffering, write-optimized storage
> 2. **Low-latency analytics** → solved by rollups, columnar compression, aggressive caching
> 3. **Reliability and trust** → solved by durable queuing, graceful degradation, independent meta-monitoring
>
> The moment you let dashboard query spikes affect ingestion, or let alert evaluation compete with write throughput, you've lost the properties that make the system trustworthy — and a monitoring system you can't trust is worse than no monitoring system at all.

---

## Staff-Level Differentiators

- Start with the **naive approach** and explain why it fails before proposing the real solution
- **Separate write/read/alert paths** — different characteristics, designed independently
- **Ground every decision in the scale numbers** from Step 1
- **Raise cardinality proactively** — don't wait to be asked
- **Name the catch-up problem** with capacity math (50% utilization headroom)
- **Distinguish slightly-late vs. very-late vs. out-of-order** — different solutions for each
- **Cover meta-monitoring** — the wrong answer is using the system to monitor itself
- **Connect to business reality** — Datadog's pricing model is a direct consequence of cardinality cost
