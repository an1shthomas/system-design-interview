# Google Docs — Collaborative Document Editor

**Problem:** Design a collaborative document editor like Google Docs.
**Difficulty:** Hard
**Core challenges:** Consistency under concurrent edits (OT/CRDTs) + stateful real-time connections at scale (WebSocket routing).

---

## Step 1: Clarifying Requirements & Scale

🎤 **Interviewer:** "Design a collaborative document editor like Google Docs. Where would you like to start?"

👨‍💻 **Candidate:** "Before jumping in, let me ask a few questions to scope the problem correctly — this one has a lot of potential surface area."

**Functional scope:**

👨‍💻 **Candidate:** "Are we supporting rich text formatting — bold, italics, images, tables — or can we assume plain text for now?"

🎤 **Interviewer:** "Assume a simple text editor."

👨‍💻 **Candidate:** "Do we need to support document permissions — who can view vs. edit?"

🎤 **Interviewer:** "Out of scope."

👨‍💻 **Candidate:** "What about document versioning — the ability to revert to previous states?"

🎤 **Interviewer:** "Out of scope for now, but good to flag as a potential deep dive."

👨‍💻 **Candidate:** "Are we supporting comments and suggestions, or just direct edits?"

🎤 **Interviewer:** "Just direct edits."

👨‍💻 **Candidate:** "Do we need offline editing — users making changes without a network connection?"

🎤 **Interviewer:** "Out of scope, but flag it."

**Collaboration scope:**

👨‍💻 **Candidate:** "How many users can concurrently edit the same document?"

🎤 **Interviewer:** "Max 100 concurrent editors per document."

👨‍💻 **Candidate:** "Do we need to show other users' cursor positions and presence?"

🎤 **Interviewer:** "Yes."

**Scale:**

👨‍💻 **Candidate:** "What's the target scale — millions of users, billions of documents?"

🎤 **Interviewer:** "Yes, millions of concurrent users across billions of documents."

**Back-of-the-envelope (candidate does this out loud):**

> "Let me quickly size the problem."
> - Millions of concurrent users, each connected via a persistent WebSocket
> - A fast typist makes ~5 keystrokes/second → ~5 edit operations/second per user
> - At 1M concurrent editors: ~5M edit operations/second globally
> - But these are spread across billions of documents — the key insight is that **contention is per-document, not global**. With max 100 editors per document, the per-document write rate is at most 500 ops/second — very manageable.
> - Each operation is small: ~50–100 bytes (operation type, position, character, timestamp, user ID)
> - Cursor updates are even more frequent but tiny (~20 bytes each)

"This shapes our design significantly. The global scale challenge is about managing millions of persistent WebSocket connections across a distributed fleet of servers. The per-document challenge is about consistency — ensuring 100 concurrent editors all converge on the same document state. **These are two separate problems and I'll tackle them separately.**"

**Key non-functional requirements:**
- **Eventual consistency** — all users must eventually see the same document state
- **Low latency** — edits should feel real-time, under 100ms round trip
- **Durability** — documents must survive server restarts (no data loss)
- **High availability** — the system must stay up even during failures
- **Scale** — millions of concurrent WebSocket connections, billions of documents

🎤 **Interviewer:** "You mentioned the 100 concurrent editor limit is interesting. Why does that matter to your design?"

👨‍💻 **Candidate:** "It's a huge simplification. It means I don't need to worry about a single document generating massive throughput — 100 editors at 5 ops/second is 500 ops/second, which a single server can handle easily. More importantly, it means I can **route all editors of the same document to a single server** without that server becoming a hotspot. This makes consistency much simpler — one server has the authoritative view of the document and can serialize all edits. If we had 10,000 concurrent editors per document, we'd need a distributed consensus mechanism, which is a much harder problem. Google Docs made this exact design choice in production — beyond a certain number of users, new joiners are downgraded to read-only. That's a hint about their architecture."

> **✅ What makes this staff-level:**
> - Immediately identifies the **two distinct challenges** — connection scaling vs. per-document consistency
> - Recognizes that the 100-editor cap is a **deliberate architectural constraint**, not just a product decision
> - Connects the back-of-the-envelope math to design consequences — per-document contention is manageable, global connection scaling is the hard part
> - Proactively flags out-of-scope items (offline mode, versioning) as potential deep dives

---

## Step 2: Core Entities & Data Modeling

🎤 **Interviewer:** "Walk me through the core entities in this system before we get into the architecture."

👨‍💻 **Candidate:** "Let me identify the key nouns and think carefully about what data we need to store and what's ephemeral — that distinction will drive some important design decisions."

**1. Document** — stores metadata, not content:
```
Document {
  docId:      UUID        -- primary key
  title:      string
  ownerId:    userId
  createdAt:  timestamp
  updatedAt:  timestamp
  versionId:  UUID        -- points to current compacted version (important for deep dive)
}
```
"Notice I'm separating document metadata from document content. The metadata is small and queried frequently — it lives in Postgres. The actual content is represented as a sequence of operations — that lives in a separate store optimized for append-heavy writes."

**2. Operation (Edit)** — the atomic unit of change:
```
Operation {
  opId:        UUID
  docId:       UUID        -- which document
  userId:      UUID        -- who made the edit
  versionId:   UUID        -- which document version this op belongs to
  type:        enum        -- INSERT | DELETE
  position:    integer     -- where in the document
  content:     string      -- the character(s) inserted (null for DELETE)
  timestamp:   timestamp   -- set by the server, not the client
}
```
"A few important design decisions here. Timestamp is set by the **server**, not the client — this gives us a total ordering of operations which is essential for Operational Transformation. The `versionId` ties operations to a specific compacted snapshot — I'll explain why in the deep dive on storage. And operations are **append-only** — we never update or delete an operation record."

**3. Cursor / Presence** — ephemeral, in-memory only:
```
Cursor {
  docId:     UUID
  userId:    UUID
  position:  integer     -- character offset in document
  color:     string      -- UI color assigned to this user
  name:      string      -- display name
  updatedAt: timestamp
}
```
"Here's a critical observation: cursor and presence data is **ephemeral**. It only matters while a user is connected. We don't need to persist this to a database — it lives in the Document Service's memory, associated with the WebSocket connection."

**4. Editor (User)** — assume users are already authenticated; we just need their `userId`.

**Storage mapping:**

| Entity | Storage | Why |
|---|---|---|
| Document metadata | Postgres | Relational, flexible queries, low volume |
| Operations (edits) | Cassandra | Append-only, high write throughput, partition by docId |
| Cursor / Presence | In-memory (Document Service) | Ephemeral, tied to WebSocket connection lifetime |

🎤 **Interviewer:** "Why Cassandra for operations? Why not Postgres?"

👨‍💻 **Candidate:** "Operations are append-only and partitioned naturally by docId. We'll query them as 'give me all operations for docId X after version Y, ordered by timestamp' — a simple range scan on a partition key. Cassandra is purpose-built for this: it partitions by docId so all operations for a document are co-located, orders by timestamp within a partition, and handles high append throughput without the write amplification that B-tree indexes cause in Postgres. We'd partition by `(docId, versionId)` and cluster by timestamp — that query pattern maps perfectly to Cassandra's data model."

🎤 **Interviewer:** "You said cursor data is ephemeral. What happens when a new user joins a document where others are already editing?"

👨‍💻 **Candidate:** "When a new user connects via WebSocket, the Document Service has all active cursors in memory — one per connected user. It immediately sends the new user a snapshot of all current cursor positions as part of the connection handshake. Since all editors of the same document are connected to the **same** Document Service instance, this is a simple in-memory read — no database query needed. This is one of the key reasons we want all editors of the same document co-located on the same server."

> **✅ What makes this staff-level:**
> - Separates metadata from content — not one monolithic document record
> - Critically **distinguishes persistent data** (operations) from **ephemeral data** (cursors) — and chooses appropriate storage for each
> - Justifies Cassandra over Postgres with a concrete query pattern argument
> - **Server-assigned timestamps** — a subtle but important correctness detail for OT
> - The `versionId` field foreshadows the compaction deep dive without getting lost in it yet

---

## Step 3: API Design

🎤 **Interviewer:** "Let's define the API. What interfaces does this system expose?"

👨‍💻 **Candidate:** "This system has two distinct communication patterns that require different protocols — and choosing the right one for each is itself an important design decision."

"For document management — creating, listing documents — standard REST over HTTP is fine. These are low-frequency, stateless operations. But for collaborative editing, we need **bidirectional, real-time, persistent** communication. HTTP request-response doesn't work here — the server needs to push updates to clients without the client polling. **WebSockets** are the right choice: they give us a persistent full-duplex channel with low overhead per message."

**1. Document Management API (REST)**

```
POST /v1/docs
{ "title": "My Document" }
→ 201 Created { "docId": "doc_abc123", "title": "My Document", "createdAt": 1640000000 }

GET /v1/docs/{docId}
→ 200 OK { "docId": "doc_abc123", "versionId": "ver_xyz789", "updatedAt": 1640000000 }
```

"Note that `GET /docs/{docId}` returns metadata only — not the document content. The actual content is loaded over the WebSocket when the user opens the editor."

**2. Collaborative Editing API (WebSocket)**

```
WS /v1/docs/{docId}
Headers: Authorization: Bearer <token>
```

"On successful connection, the server immediately sends the client the current document state — the latest compacted snapshot plus any subsequent operations."

**Messages the client SENDS to server:**
```json
// Insert characters
{ "type": "insert", "opId": "op_client_123", "position": 5, "content": ", world", "clientTimestamp": 1640000000 }

// Delete characters
{ "type": "delete", "opId": "op_client_124", "position": 5, "length": 3 }

// Update cursor position
{ "type": "updateCursor", "position": 7 }
```

**Messages the client RECEIVES from server:**
```json
// Initial document state on connection
{ "type": "init", "versionId": "ver_xyz789", "content": "Hello",
  "cursors": [{ "userId": "u1", "position": 3, "color": "#FF5733", "name": "Alice" }] }

// A transformed operation from another editor
{ "type": "operation", "opId": "op_server_456", "userId": "u1",
  "operationType": "insert", "position": 5, "content": ", world", "serverTimestamp": 1640000001 }

// Acknowledgment of client's own operation
{ "type": "ack", "opId": "op_client_123", "serverTimestamp": 1640000001 }

// Another user's cursor moved
{ "type": "cursorUpdate", "userId": "u1", "position": 7 }

// A user joined or left
{ "type": "presenceUpdate", "userId": "u1", "status": "joined" }
```

🎤 **Interviewer:** "Why does the client send its own opId? And why does the server echo it back in the ack?"

👨‍💻 **Candidate:** "Two reasons. First, **idempotency** — if the client sends an operation but the network drops before receiving the ack, it will retry. The server uses the client-generated opId to detect and deduplicate the retry rather than applying the same edit twice. Second, **optimistic UI** — the client applies its own edit immediately to the local document without waiting for the server (otherwise the editor would feel laggy at 100ms+ round trip). When the ack arrives with the server timestamp, the client knows the operation has been officially ordered and can reconcile its local state."

🎤 **Interviewer:** "Why not use Server-Sent Events (SSE) instead of WebSockets?"

👨‍💻 **Candidate:** "SSE is unidirectional — server to client only. For a collaborative editor, clients need to send edits to the server as well. You could technically combine SSE for receiving with HTTP POST for sending, but that's two separate connections per user, more complex state management, and higher overhead per operation. WebSockets give us a single persistent full-duplex channel — simpler, lower latency, and the right tool for this use case. SSE makes more sense for read-heavy, one-way push scenarios like live sports scores or notification feeds."

> **✅ What makes this staff-level:**
> - Justifies the **protocol choice** (WebSocket vs REST vs SSE) with concrete reasoning
> - Designs the `init` message carefully — document state + all cursor positions sent on connection
> - **Client-generated opId** for idempotency and optimistic UI — a subtle but production-critical detail
> - Server timestamps for authoritative ordering — connects back to the OT requirement from entities
> - Separates `ack` messages from broadcast messages — client needs to know its own op was accepted

---

## Step 4: High-Level Architecture

🎤 **Interviewer:** "Walk me through the high-level architecture. How does the system work end to end?"

👨‍💻 **Candidate:** "I'll build this up incrementally — starting with document creation, then the collaborative editing write path, then the read path, and finally cursor/presence. I'll flag scaling concerns as I go and save them for deep dives."

**Components:**
- **API Gateway** — handles auth, routing, rate limiting
- **Document Metadata Service** — CRUD for document metadata, backed by Postgres
- **Document Service** — the stateful core: manages WebSocket connections, applies OT, broadcasts updates. **One instance owns all connections for a given document.**
- **Document Operations DB** — Cassandra, append-only log of operations partitioned by docId
- **Document Metadata DB** — Postgres, document metadata

**1. Creating a Document (simple)**

```
Client → API Gateway → Document Metadata Service → Postgres
                                ↓
                         { docId: "doc_abc123" }
```

"Standard horizontally-scaled stateless CRUD service. I won't dwell here — the interesting parts are ahead."

**2. Collaborative Editing — Write Path**

*Step 1 — Connection:* Client opens a WebSocket to the Document Service responsible for that docId. (Routing mechanism deferred to deep dive.)

*Step 2 — Document Load:* On connection, the Document Service:
1. Fetches the current `versionId` from Postgres
2. Loads the compacted snapshot + all subsequent operations from Cassandra
3. Reconstructs the current document state in memory
4. Sends the `init` message to the client

*Step 3 — Receiving an Edit:*
```
Client A --[insert op]--> Document Service
```
The Document Service:
1. Assigns a **server timestamp** — authoritative ordering for OT
2. Applies **Operational Transformation** against any concurrent ops already applied
3. Writes the transformed op to Cassandra — **durability guaranteed before ack**
4. Sends `ack` to Client A
5. Broadcasts the transformed op to all other connected clients (B, C, etc.)

```
Document Service → Cassandra (write op)
                → Client A (ack)
                → Client B, C... (broadcast transformed op)
```

🎤 **Interviewer:** "Why write to Cassandra before sending the ack? Why not ack immediately and write asynchronously?"

👨‍💻 **Candidate:** "Durability. If we ack the client and then crash before writing to Cassandra, that operation is lost forever — the client thinks it was saved, but it wasn't. The user could close the browser, the document would reload from Cassandra, and their edit is silently gone. That's a catastrophic user experience. By writing to Cassandra first, we guarantee the operation survives any server crash. This is essentially **write-ahead logging** applied to collaborative editing."

**3. Read Path — Viewing Changes in Real-Time**

"The read path is simple precisely because of our design choice to co-locate all editors on the same Document Service instance."

When Client A's edit is processed:
- The Document Service has all connected WebSocket handles in memory
- It simply iterates over connected clients for that docId and sends the transformed op to each one
- **No pub/sub, no message broker, no cross-server communication needed**

"This is the key payoff of the co-location design. Broadcasting to 100 concurrent editors is just 100 in-memory WebSocket writes — trivially fast, no network hops."

**4. Cursor & Presence**

- Client sends `updateCursor` message → Document Service updates its in-memory cursor map → broadcasts `cursorUpdate` to all other connected clients
- On disconnect: Document Service removes cursor from in-memory map → broadcasts `presenceUpdate` with `status: "left"`
- **None of this touches Cassandra or Postgres.** Cursor state is entirely in-memory.

**Full architecture:**
```
[Client A] ←──WebSocket──→ ┐
[Client B] ←──WebSocket──→ ├─ [Document Service]  ←→ [Cassandra: Operations DB]
[Client C] ←──WebSocket──→ ┘         ↕
                                [Postgres: Metadata DB]

[Client D] ──REST──→ [API Gateway] → [Document Metadata Service] → [Postgres]
```

🎤 **Interviewer:** "What happens if the Document Service crashes while editors are connected?"

👨‍💻 **Candidate:** "Two things happen. First, all connected WebSocket clients detect the connection drop and enter a reconnect loop with exponential backoff. Second, they reconnect to a new Document Service instance. Because all operations were durably written to Cassandra before being acked, the new instance can reconstruct the full document state by replaying operations from Cassandra. Any operations the client had sent but not yet received acks for get retried — the client-generated opId ensures they're deduplicated. The in-memory cursor state is lost but clients re-broadcast their cursor positions on reconnect. We lose ephemeral state (cursors momentarily) but **never persistent state** (document content)."

> **✅ What makes this staff-level:**
> - Builds up incrementally — document creation → write path → read path → presence, each with clear rationale
> - **Write-to-Cassandra before ack** — explicitly connects this to durability and the write-ahead log pattern
> - **Co-location as a first-class design choice** — not an afterthought, but the reason the read path is simple
> - Clearly separates ephemeral cursor state from durable operation state
> - Handles crash recovery with a concrete answer: replay from Cassandra + client retry with opId dedup

---

## Deep Dive 1: Collaborative Editing — OT vs. CRDTs

🎤 **Interviewer:** "You mentioned Operational Transformation. Walk me through why it's needed and how it actually works. And how does it compare to CRDTs?"

👨‍💻 **Candidate:** "Let me build up to OT by showing why naive approaches fail, then explain OT and CRDTs as two different philosophies for solving the same problem."

**❌ Naive Approach 1: Send Full Document Snapshots**

Why it fails:
- Extremely inefficient — a 50KB document sends 50KB on every keystroke
- **Lost updates**: If User A and User B both edit and send their snapshots simultaneously, whoever arrives last wins — the other's changes are silently overwritten

**❌ Naive Approach 2: Send Operations (No Transformation)**

Concrete failure example:
```
Document starts as: Hello!
Position:  0 1 2 3 4 5
Character: H e l l o !

User A inserts ", world" after position 4 → sends INSERT(5, ", world")
User B deletes "!" at position 5       → sends DELETE(5)

Both send at the same time. Server applies User A's op first:
  Hello, world!
  0123456789012  ← "!" is now at position 12, not 5

Now User B's DELETE(5) arrives — it deletes "," instead of "!":
  Hello world!  ← WRONG! Comma deleted, "!" survived
```

"The problem: User B's operation was based on the document state they saw locally. When concurrent operations arrive out of order, positional references become stale. This is the **core consistency problem**."

**✅ Good Solution: Operational Transformation (OT)**

Core idea: Transform each incoming operation based on operations that have already been applied, so its **intent is preserved** regardless of ordering.

Same example with OT:
```
User B's DELETE(5) arrives after User A's INSERT(5, ", world") has already been applied.
The server knows:
  - User B's op was based on a state where "!" was at position 5
  - User A's INSERT added 7 characters before position 5+
  - Therefore, "!" is now at position 5 + 7 = 12

The server transforms User B's op: DELETE(5) → DELETE(12)
Result: Hello, world  ← correct!
```

How OT works at the server — when a new operation arrives:
1. Determine which operations the client had already seen (via the client's last-known version)
2. Transform the incoming op against all ops applied since then
3. Apply the transformed op and append to the log
4. Broadcast the transformed op to other clients

OT also happens on the client: when Client B receives a server broadcast, it transforms it against any local unacknowledged ops before applying. This ensures local optimistic updates stay consistent with incoming server ops.

```
Server: [Ea, Eb_transformed]
Client A sees: Ea → Eb_transformed  ✅
Client B sees: Eb (local) → Ea_transformed  ✅
Both converge to same document ✅
```

**OT trade-offs:**
- ✅ Low memory — no need to keep tombstones, just the operation log
- ✅ Efficient — works well for text specifically
- ✅ Battle-tested — Google Docs, Notion, and most production editors use it
- ❌ Requires a central server to establish authoritative op ordering — can't be fully peer-to-peer
- ❌ Tricky to implement correctly — transformation functions must handle all op combinations

---

**✅ Alternative: Conflict-free Replicated Data Types (CRDTs)**

Core idea: Instead of transforming operations, design the data structure so all operations are **commutative** — they produce the same result regardless of order.

**How CRDTs handle positions:**

Instead of integer positions (which shift when characters are inserted/deleted), CRDTs assign each character a **globally unique, stable fractional position** — like a real number between 0 and 1 that never changes.

```
Document: H(0.1)  e(0.2)  l(0.3)  l(0.4)  o(0.5)  !(0.6)

User A inserts ", world" → assigns it position 0.53
User B deletes "!" at 0.6

Both ops can be applied in any order:
  - DELETE(0.6) removes the "!" regardless of when applied
  - INSERT(0.53, ", world") lands between "o" and "!" regardless

Result: H e l l o ,   w o r l d
        0.1 0.2 0.3 0.4 0.5 0.53 [0.6 is tombstoned]
```

**Deletions use tombstones** — deleted characters are marked as deleted but not removed from the list. This prevents position conflicts when two users both try to delete the same character:
- User A deletes position 0.5 → marks it as tombstone
- User B also deletes position 0.5 → finds it already tombstoned → no-op ✅

**CRDT trade-offs:**

| | OT | CRDT |
|---|---|---|
| Central server | Required (for ordering) | Not required |
| Memory | Low (no tombstones) | Higher (tombstones accumulate) |
| Offline support | Hard | Natural |
| Peer-to-peer | ❌ | ✅ |
| Implementation complexity | High (transform functions) | High (position allocation, GC) |
| Best for | Online collaborative editing with server | Offline-first, P2P scenarios |

**When to choose CRDTs:** Offline-first applications (Notion offline mode), P2P sync (local-first software like Obsidian), or scenarios where network partitions are expected.

**When to choose OT:** Online collaborative editors with a server (Google Docs, Notion online) where you can guarantee a single authoritative ordering point.

---

### 📚 Supplementary: CRDTs in Depth

#### 1. Fractional ID Generation in Practice

**The problem with naive fractional IDs:**

*Problem 1 — Collisions:* If User A and User B both insert between positions 0.5 and 0.6 and both randomly pick 0.53, they've generated the same ID for two different characters. The document becomes ambiguous.

*Problem 2 — Precision exhaustion:* If users repeatedly insert between the same two characters, IDs grow longer and longer:
```
0.5 → 0.51 → 0.511 → 0.5111 → 0.51111...
```
After thousands of inserts in the same spot, IDs become arbitrarily long — consuming huge memory and slowing down sorting.

**Solution to collisions — Site ID tie-breaking:**

Each client is assigned a globally unique site ID (e.g., a UUID). Every position is a tuple:
```
Position = (fractional_number, site_id, sequence_number)
```
Comparison is lexicographic: fractional number first, then site_id, then sequence_number. Two clients can independently pick the same fractional number and still produce a total, unambiguous ordering.
```
User A inserts at: (0.53, "site-A", 42)
User B inserts at: (0.53, "site-B", 17)
→ site-A < site-B alphabetically → User A's character comes first ✅
```

**Solution to precision exhaustion — LSEQ (tree-based position allocation):**

Instead of simple decimals, production CRDTs use a tree structure:
- Each level of the tree has a fixed number of slots (e.g., 32 at level 1, 1024 at level 2, etc.)
- A position is a path from root to leaf: e.g., `[15, 7, 22]`
- To insert between two positions, go one level deeper and pick a slot in between

```
Level 1:  [0 ............ 15 ............. 31]
                           ↓
Level 2:          [0 .... 7 .......... 31]
                           ↓
Level 3:      [0 .. 3 .. 7 .. 14 .. 31]
```

Positions grow logarithmically, not unboundedly. LSEQ alternates between two strategies at each tree level:
- **Boundary+**: allocate near the left boundary (good for left-to-right typing)
- **Boundary-**: allocate near the right boundary (good for right-to-left typing)

This heuristic dramatically reduces ID length for common editing patterns.

---

#### 2. Garbage Collection of Tombstones

**The problem:** Tombstones accumulate forever. In a long-lived document with 100,000 characters typed and 80,000 deleted, the CRDT stores all 100,000 entries (80,000 tombstones + 20,000 visible).

**The safe GC condition:** A tombstone can only be removed when every client that will ever interact with this document has already seen and applied the deletion. Otherwise, a client coming back online might try to insert relative to a position that no longer exists in the data structure.

**Method 1 — Version Vectors:**

Each client maintains a version vector — a map of `{siteId → last_sequence_number_seen}`:
```
User A's version vector: { "site-A": 42, "site-B": 38, "site-C": 15 }
```
A tombstone for operation `(site-B, seq=30)` can be GC'd when all currently connected clients show `site-B ≥ 30`. The server periodically broadcasts version vectors; when all vectors confirm they've seen a deletion, the tombstone is purged.

**Method 2 — Epoch-based GC (used by Yjs):**

1. The server periodically takes a snapshot of the current document state (visible text only, no tombstones), assigned an epoch number
2. New clients joining after this epoch load from the snapshot, not from scratch
3. Tombstones from before the snapshot epoch are safe to delete — no new client will ever need to reference them

```
Epoch 1: [snapshot of document at t=0]
    ↓ new ops accumulate...
Epoch 2: [snapshot of document at t=1hr]
    → tombstones from Epoch 1 can now be GC'd
```

This is the same compaction concept as the Document Service's online compaction — just applied to CRDTs.

**Method 3 — Causal Stability (academic):**

An operation is causally stable when it's been seen by all sites and no future operation can possibly causally precede it. Once causally stable, its tombstone is safe to collect. Provably correct but requires sophisticated vector clock tracking.

---

#### 3. How Yjs Works — A Real Production CRDT

Yjs is the most widely used open-source CRDT library (used in production by many collaborative tools including Notion's offline mode).

**Core data structure — doubly linked list of Items:**
```
Item {
  id:        (clientID, clock)    ← unique ID
  content:   string               ← the character(s)
  deleted:   boolean              ← tombstone flag
  left:      Item                 ← left neighbor at time of insertion
  right:     Item                 ← right neighbor at time of insertion
  origin:    Item                 ← left neighbor WHEN THIS ITEM WAS INSERTED
}
```
The `origin` field is crucial — it records what was to the left of this item **at the time it was inserted**, not the current left neighbor. This is how Yjs resolves concurrent insertions at the same position deterministically.

**Yjs insertion algorithm (YATA):**

When User A inserts character X between items L and R:
```
Item X { origin: L, right: R }
```
When this op arrives at another client, Yjs finds where to place X using this rule:
1. Start from `L.right` (the item immediately right of X's left origin)
2. Walk right through items until finding the correct position
3. For concurrent insertions at the same spot: items with an origin further to the left go further right; if origins are equal, break ties by clientID (consistent across all clients)

This guarantees all clients place the item in the same position, even if they receive ops in different orders. **No server needed for ordering.**

**Yjs awareness protocol:**

For ephemeral state (cursor positions, user presence, selection ranges) — kept completely separate from the document CRDT because:
- Cursor state doesn't need to be persisted
- It doesn't need tombstones or version vectors
- It can be lossy — missing one cursor update is fine

Uses a simple last-write-wins map per client:
```json
{
  "client-A": { "cursor": 42, "name": "Alice", "color": "#FF5733", "timestamp": 1640000001 },
  "client-B": { "cursor": 17, "name": "Bob",   "color": "#33FF57", "timestamp": 1640000002 }
}
```
Each client owns its own entry. Conflicts are impossible — each client only writes to its own entry. This is exactly the in-memory cursor map from the Google Docs architecture — Yjs just formalizes it as part of the protocol.

**Yjs state vectors for efficient sync:**

```
Client A state: { A: 50, B: 30 }
Client B state: { A: 45, B: 35 }
→ A needs to send ops: B[31..35]
→ B needs to send ops: A[46..50]
```
When two clients connect (or reconnect after offline), they exchange state vectors and each sends only the missing ops — no full sync needed.

**CRDT internals summary:**

| Concept | Problem it solves | How |
|---|---|---|
| Fractional IDs (LSEQ) | Stable positions despite inserts/deletes | Tree-based position allocation |
| Site ID tie-breaking | Collision-free concurrent inserts | Lexicographic comparison of (position, siteID) |
| Tombstones | Safe concurrent deletes | Never remove, just mark deleted |
| Version vectors | Know what each client has seen | Map of {siteId → last_seq_seen} |
| Garbage collection | Reclaim tombstone memory | Delete tombstones seen by all clients |
| Yjs YATA algorithm | Deterministic concurrent insert ordering | Origin-based placement rule |
| Yjs awareness | Ephemeral cursor/presence | Last-write-wins per-client map |
| Yjs state vectors | Efficient reconnect sync | Exchange vectors, send only missing ops |

---

## Deep Dive 2: Scaling WebSocket Connections to Millions of Users

🎤 **Interviewer:** "Your current design has all editors of a document on a single Document Service instance. How do you scale this to millions of concurrent WebSocket connections?"

👨‍💻 **Candidate:** "This is the hardest scaling challenge in this system. Let me explain why it's tricky before proposing solutions."

**Why this is hard:**

"WebSocket connections are stateful and long-lived. Unlike HTTP where any server can handle any request, a WebSocket connection is pinned to a specific server for its entire lifetime. This creates two tensions:"

1. **Connection scaling:** A single server can handle ~50,000–100,000 concurrent WebSocket connections. At 1M concurrent editors we need at least 10–20 servers.
2. **Co-location requirement:** For OT to work correctly, all editors of the same document must be on the same server. We can't have User A on Server 1 and User B on Server 2 editing the same document — they'd each apply OT independently with no shared ordering, causing divergence.

"So we need horizontal scaling, but with a constraint: **same document → same server**. This is a classic consistent routing problem."

**❌ Bad Solution: Simple Round-Robin Load Balancing**

"A standard load balancer distributes connections round-robin across servers. User A might land on Server 1, User B on Server 2. Now we have two servers with no shared document state, applying OT independently — classic split-brain. The two users will see diverging documents. This fundamentally breaks correctness."

**✅ Good Solution: Consistent Hash Ring**

Route each document to a deterministic server by hashing the docId. All editors of the same document always hash to the same server — co-location guaranteed.

How it works:
1. Document Service instances join a consistent hash ring, each owning a range of hash values
2. When a client wants to connect for `doc_abc123`, it first makes an HTTP request to any server
3. That server computes `hash(doc_abc123)` → determines which server owns that hash range
4. If it's not the current server, it responds with a redirect to the correct server
5. The client reconnects directly to the correct server and upgrades to WebSocket

**ZooKeeper for ring coordination:**
- Each Document Service instance registers itself in ZooKeeper on startup
- ZooKeeper maintains the current ring configuration
- All servers watch ZooKeeper for ring changes
- Clients don't talk to ZooKeeper directly — only servers do

```
Client → any Server → check ZooKeeper hash ring
                    → redirect to correct Server
                    → WebSocket connection established
```

"Why consistent hashing specifically? When we add or remove a server, consistent hashing minimizes disruption. A standard hash (`docId % N`) would remap ~50% of all documents when N changes. **Consistent hashing remaps only ~1/N documents** — only those whose hash range now belongs to the new server."

🎤 **Interviewer:** "What happens when a server fails or a new server is added?"

👨‍💻 **Candidate:**

**Server failure:**
1. ZooKeeper detects the failed server via heartbeat timeout (typically 3–10 seconds)
2. ZooKeeper removes the server from the ring and notifies all remaining servers
3. The failed server's hash ranges are redistributed to neighboring servers
4. Clients detect the WebSocket drop and enter a reconnect loop
5. On reconnect, they hit any live server, which redirects them to the new owner
6. The new owner loads document state from Cassandra and resumes

"No data is lost — all operations were written to Cassandra before being acked. The reconnect is seamless from a data perspective."

**Adding a new server (scaling up):**
1. New server joins the ring and registers with ZooKeeper
2. It takes ownership of a portion of the hash range from its neighbors
3. Affected clients are disconnected and redirected to the new server
4. During transition, ZooKeeper's linearizable writes ensure only one server is authoritative at any time

"The hairiest part is the migration protocol: the old server **stops accepting new ops for migrating documents** and drains in-flight ops to Cassandra before handing off. The new server only starts accepting connections after confirming the handoff is complete via a ZooKeeper transaction."

🎤 **Interviewer:** "That's complex. Is there a simpler alternative for scaling WebSocket connections?"

👨‍💻 **Candidate:** "Yes — and it's worth discussing the trade-off."

**Alternative: WebSocket Gateway + Redis Pub/Sub**
```
[Client A] ──WebSocket──→ [WS Gateway 1] ──subscribe(docId)──→ [Redis Pub/Sub]
[Client B] ──WebSocket──→ [WS Gateway 2] ──subscribe(docId)──→ [Redis Pub/Sub]
                                                                       ↑
                                          [Document Service] ──publish(docId, op)──┘
```
- WebSocket Gateways are stateless — any gateway can accept any client
- Document Service is stateless — it processes ops and publishes results to Redis
- Redis Pub/Sub fans out to all gateways subscribed to that docId

Why this is simpler: No consistent hashing, no ZooKeeper, no connection migration.

Why we still choose consistent hashing: "The pub/sub approach **breaks our OT requirement**. OT needs a single authoritative server to establish a total ordering of operations for each document. If two Document Service instances both process ops for the same document concurrently, they'd produce different orderings and clients would diverge. You'd need a distributed lock or consensus mechanism — which adds back the complexity we tried to avoid. **The pub/sub approach works well for simpler broadcast problems** — live comments, presence-only — where there's no ordering dependency. For OT-based collaborative editing, we need the centralized ordering that consistent hashing gives us."

🎤 **Interviewer:** "What about memory? With millions of documents, can you keep them all in memory?"

👨‍💻 **Candidate:** "No — and we shouldn't try. We only keep **active documents** in memory — those with at least one connected editor. When the last editor disconnects, we evict the document from memory. Let me size this: at 1M concurrent editors across at most 100 editors per document, we have at least 10,000 active documents. At 50KB average memory per active document, that's 500MB of active document state per server — very manageable. The key is aggressive eviction of idle documents."

> **✅ What makes this staff-level:**
> - Names the exact tension — **horizontal scaling vs. co-location requirement** — before proposing solutions
> - Explains why **consistent hashing over simple hashing** — minimizes remapping on topology changes
> - Addresses failure and scaling events with a concrete migration protocol
> - Honestly evaluates the **pub/sub alternative** — knows when it works and why it doesn't fit here
> - Sizes the memory problem — does the math rather than just saying "evict inactive documents"

---

## Deep Dive 3: Storage Compaction & Snapshots

🎤 **Interviewer:** "You're storing every edit operation forever in Cassandra. A popular document could have millions of operations. What problems does this cause and how do you solve it?"

👨‍💻 **Candidate:** "Great question — this is a problem that grows silently and bites you in production."

**Why unbounded operation storage is a problem:**

*Problem 1: Cassandra storage growth*
- At 5M ops/second globally, each op ~100 bytes → **500MB/second** of raw operation data
- Per day: **~43TB** of operation logs
- Unlike the metrics system where old data ages out naturally, we can't just delete old operations — they're needed to reconstruct the document.

*Problem 2: Document load time*
- When a new editor connects to a document with 1 million operations, the Document Service must fetch all 1M ops from Cassandra and replay them sequentially
- At 10,000 ops/second replay speed, that's **100 seconds** of load time — completely unacceptable

*Problem 3: Memory pressure*
- Keeping a million operations in memory per active document quickly exhausts the Document Service's heap

**The solution: Operation Compaction (Snapshotting)**

"Periodically collapse many operations into a single snapshot representing the document's full current state. Instead of storing 1M individual edits, store one `INSERT(0, "full document text")` operation. This is directly analogous to **database checkpointing** in WAL-based systems like Postgres."

**The versionId mechanism** — this is why the `versionId` field exists:

```
Document { docId, versionId: "ver_002", ... }

Operations:
(docId, versionId="ver_001", t=1) INSERT(0, "Hello")         ← old version
(docId, versionId="ver_001", t=2) INSERT(5, ", world")       ← old version
(docId, versionId="ver_002", t=3) INSERT(0, "Hello, world")  ← compacted snapshot
(docId, versionId="ver_002", t=4) INSERT(12, "!")            ← new ops after snapshot
```

When loading a document, the Document Service:
1. Reads the current `versionId` from Postgres
2. Fetches only operations with that `versionId` from Cassandra
3. Applies them in order to reconstruct the document

Old operations under previous versionIds can be safely deleted — they're no longer referenced. "The `versionId` is the atomic switch between old and new compacted state."

**❌ Option 1: Offline Compaction Service**

A separate background job that:
1. Identifies documents that are large, haven't been compacted recently, and are currently inactive
2. Reads all operations for the current versionId from Cassandra
3. Replays them to reconstruct the document text
4. Writes a single `INSERT(0, fullText)` under a new versionId
5. Atomically flips the versionId in Postgres via compare-and-swap:
   ```sql
   UPDATE docs SET versionId="ver_002" WHERE docId=X AND versionId="ver_001"
   ```
6. Schedules deletion of old operations

"The **compare-and-swap is critical**: ensures we only flip if nothing else has changed the versionId concurrently. If a Document Service instance started a compaction at the same time, one will lose the CAS and retry."

Challenges:
- Must verify the document is truly inactive before compacting — otherwise racing with live edits
- Compaction Service is a heavy Cassandra reader — needs rate limiting to avoid impacting live writes
- Deletion of old ops should be deferred — immediate deletion risks a race where a client loaded the old versionId but hasn't finished replaying yet

**✅ Option 2: Online Compaction by the Document Service (preferred)**

"The offline Compaction Service has a fundamental awkwardness: it needs to coordinate with the Document Service to check if a document is active. What if we eliminate that coordination by **having the Document Service do compaction itself**?"

When does it compact? **When the last editor disconnects** — the Document Service already knows the document is now idle, and it has the full document state in memory — no need to replay from Cassandra.

```
Last client disconnects
        ↓
Document Service (async, low priority):
  → Serialize current in-memory document state → fullText
  → Write INSERT(0, fullText) to Cassandra under new versionId
  → CAS flip versionId in Postgres
  → Schedule old op deletion
  → Evict document from memory
```

Why this is better:
- **No coordination needed** — Document Service inherently knows when a document is idle
- **Already have the state in memory** — no need to replay from Cassandra
- **Natural timing** — documents are always compact when inactive; next editor loads instantly
- **Simpler architecture** — one fewer service to deploy and operate

The low-priority process detail: "We offload compaction to a separate OS process with lower CPU scheduling priority (via `nice` on Linux). This prevents a large document compaction from consuming CPU that a simultaneously connecting new editor needs."

**Edge cases:**
- *Server crash during compaction:* Old versionId is still in Postgres — the next load replays from Cassandra as normal. Compaction simply didn't happen — correctness preserved.
- *New editor connects while compaction is running:* Document Service serves from in-memory state. Compaction finishes independently and doesn't affect already-connected editors.
- *Very large documents:* Serialization might take a few seconds. Fine since it's async and low-priority.

🎤 **Interviewer:** "How would you extend this to support document versioning — letting users revert to earlier versions?"

👨‍💻 **Candidate:** "Elegant extension — our current design almost supports it for free. Instead of deleting old versionIds after compaction, we **keep them**. Each versionId becomes a named checkpoint:"

```
DocumentVersions {
  docId:       UUID
  versionId:   UUID
  createdAt:   timestamp
  label:       string    ← optional user-assigned label e.g. "Before major rewrite"
  compactedOp: UUID      ← points to the snapshot operation in Cassandra
}
```

"To revert to a previous version, we replay from that version's compacted snapshot. We could apply tiered retention: full granularity for 30 days, then hourly snapshots, then daily snapshots for older history. This is exactly how Google Docs version history works."

🎤 **Interviewer:** "What about memory optimization for the Document Service?"

👨‍💻 **Candidate:** "Three levers:
1. **Aggressive eviction on last disconnect** — evict immediately after compaction. No idle documents in memory.
2. **LRU eviction for recently accessed documents** — if a viewer disconnected recently, keep briefly in case they return.
3. **Operation log trimming in memory** — once an op has been acked and broadcast, it only needs to stay in memory for OT transformation against new incoming ops. We bound the in-memory operation history to a rolling window (e.g., last 1,000 ops) — very old ops are unlikely to be needed for transformation against new edits.

With these three, the memory footprint per active document is bounded to: document text size + last N operations buffer + cursor map — well under 1MB for a typical document."

> **✅ What makes this staff-level:**
> - **Sizes the problem** — 43TB/day and 100-second load times make the urgency concrete
> - Connects to **WAL/checkpointing** — shows understanding of how databases solve the same problem
> - The **versionId mechanism** — a clean, atomic solution with no distributed locks needed
> - Compares offline vs. online compaction — and **picks online with clear reasoning**
> - Handles edge cases — crash during compaction, new editor during compaction
> - Extends to versioning naturally — shows the design was forward-thinking

---

## Step 6: Wrap-up & Trade-offs

🎤 **Interviewer:** "We're coming up on time. Summarize your design, the key trade-offs, and what you'd revisit given more time."

👨‍💻 **Candidate:** "Let me zoom out and give you the full picture, then walk through the deliberate trade-offs."

**Full System Summary:**

*1. Document Management (stateless, simple):*
```
Client → API Gateway → Document Metadata Service → Postgres
```
Standard horizontally-scaled CRUD. Deliberately kept separate from the collaborative editing layer.

*2. Collaborative Editing (stateful, complex):*
```
Client A, B, C ←──WebSocket──→ Document Service
                                (owns docId via consistent hash ring + ZooKeeper)
                                        ↕
                                [Cassandra: operation log]
```
The Document Service is the authoritative OT server for each document. It holds all active WebSocket connections, assigns server timestamps, applies OT, broadcasts transformed ops, and keeps cursor/presence state in memory.

*3. Storage Lifecycle:*
```
Document Service (on last disconnect)
  → compacts ops
  → writes snapshot to Cassandra
  → CAS flips versionId in Postgres
  → evicts from memory
```

**Key Trade-offs Made:**

| Trade-off | Chose | Rationale |
|---|---|---|
| OT vs CRDTs | OT | Centralized architecture, lower memory, battle-tested. CRDTs better for offline-first/P2P. |
| Consistent hash ring vs pub/sub gateway | Hash ring | OT correctness requires single authoritative server per document. Pub/sub would need distributed locking. |
| Online vs offline compaction | Online (Document Service) | Eliminates cross-service coordination, leverages already-in-memory state, simpler architecture. |
| Cassandra vs Postgres for operations | Cassandra | Append-only, partitioned by docId, simple range queries — perfect Cassandra use case. |
| Server-assigned vs client timestamps | Server | Client clocks are unreliable. OT requires total ordering — server provides single authoritative clock. |

**What I'd Do Differently Given More Time:**

1. **Read-only mode for large audiences** — viewers don't need WebSocket connections to the Document Service. A separate read-only path using stateless Read Gateway servers subscribing to document update streams via Redis pub/sub would decouple viewer scale from editor scale completely.

2. **Offline editing support** — with OT, offline is hard (must transform local ops against the server's full op history since disconnect). CRDTs handle this more elegantly. Consider a hybrid: OT for online, CRDT-based sync for offline reconnect (Yjs's awareness protocol is a good reference).

3. **Rich text beyond plain text** — the OT transformation functions become significantly more complex for structured content. The industry has converged on document models like ProseMirror's schema or Quill's Delta format — I'd adopt one rather than inventing my own.

4. **Throttled cursor updates** — cursor positions need to be transformed when characters are inserted before a cursor. For very large documents, transmitting 100 cursors on every keystroke has UI performance implications — throttle cursor updates to ~100ms intervals rather than on every keystroke.

5. **Multi-region deployment** — our design is single-region. Multi-region collaborative editing is extremely hard with OT's ordering guarantee. Practical approaches: geo-routing editors to the nearest region with replication lag for reads, or accepting higher latency for inter-region collaboration.

🎤 **Interviewer:** "Strong design overall. Thank you."

**🎯 Final Reflection**

"The core insight of this design is that Google Docs is actually two very different problems that share a storage layer:

**The first is a consistency problem** — how do you make concurrent edits from multiple users converge to the same document? OT solves this elegantly by transforming operations relative to each other, with the Document Service as the authoritative ordering point. This is fundamentally a single-threaded problem per document — and we lean into that rather than fighting it.

**The second is a connection scaling problem** — how do you maintain millions of stateful WebSocket connections while preserving the co-location constraint that OT requires? Consistent hashing with ZooKeeper solves this by making routing deterministic — every server always knows which server owns any given document.

The design deliberately keeps these two concerns separate. The OT logic doesn't care how we route connections. The routing logic doesn't care how OT works. **That separation of concerns is what makes the system comprehensible and maintainable as it scales.**"

---

**Overall Assessment:**

| Area | Quality | Notes |
|---|---|---|
| Requirements clarification | ✅ Strong | Identified two distinct challenges immediately |
| Core entities | ✅ Strong | Persistent vs. ephemeral data distinction |
| API design | ✅ Strong | WebSocket message design, opId for idempotency |
| High-level architecture | ✅ Strong | Write path, read path, presence all covered |
| OT vs. CRDTs deep dive | ✅ Strong | Built from first principles, worked examples |
| WebSocket scaling deep dive | ✅ Strong | Consistent hashing, pub/sub trade-off |
| Compaction deep dive | ✅ Strong | Online vs. offline, versionId CAS mechanism |
| Wrap-up & trade-offs | ✅ Strong | Clear opinions backed by reasoning |
