# AgentLoom Memory Reconstruction

**Status:** Reference architecture — describes the shipped `agentloom_runtime.session` module
**Part of:** AgentLoom Runtime
**Companion:** [`layer-0-session-memory.md`](layer-0-session-memory.md) — that document is the *contract* (invariants, data model, CLI surface). This one is the *mechanism*: why it is shaped this way and what actually happens when an agent moves between machines.

---

## The question

An agent works with you on Windows in the afternoon. That evening you open the
same repository on a Linux box that has no IDE installed at all. The agent
should know what it was doing.

Everything else already survives that move. The code is in Git. The knowledge
graph, the task database, and the plan registry are in a shared MySQL instance,
so they were never tied to a laptop in the first place. The single thing that
does not survive is *the thread of work* — which is, inconveniently, the thing
you need first.

The obvious fix is to move the editor's session data to the other machine. That
is the wrong shape, and understanding why is most of the design.

---

## 1. Reconstruction, not migration

```mermaid
graph LR
    subgraph MIG["❌ Migration — the approach we rejected"]
        direction LR
        MA["Machine A<br/><i>editor's private store</i><br/>proprietary format"]
        MSYNC{{"sync / copy / symlink<br/><b>N×M adapters</b>"}}
        MB["Machine B<br/><i>different editor</i><br/>different format"]
        MA -->|"export"| MSYNC
        MSYNC -->|"import"| MB
        MFAIL["breaks on:<br/>• path-hashed keys<br/>• editor version bump<br/>• concurrent writer<br/>• every new IDE"]
        MSYNC -.-> MFAIL
    end

    subgraph REC["✅ Reconstruction — what AgentLoom does"]
        direction TB
        RA["Machine A<br/>Windows · editor X · x86-64"]
        RB["Machine B<br/>Linux · no IDE · aarch64"]
        RC["Machine C<br/>macOS · editor Y"]
        RDB[("Shared session store<br/><b>MySQL</b><br/>agent_sessions<br/>session_checkpoints<br/>session_transcripts")]
        RA <-->|"derive identity<br/>read / write"| RDB
        RB <-->|"derive identity<br/>read / write"| RDB
        RC <-->|"derive identity<br/>read / write"| RDB
    end

    MIG ~~~ REC

    classDef bad fill:#ffebee,stroke:#c62828,stroke-width:1px,color:#b71c1c
    classDef good fill:#e8f5e9,stroke:#2e7d32,stroke-width:1px,color:#1b5e20
    classDef db fill:#e3f2fd,stroke:#1565c0,stroke-width:2px,color:#0d47a1
    classDef note fill:#fff8e1,stroke:#f9a825,stroke-width:1px,color:#e65100,font-size:11px

    class MA,MB bad
    class MSYNC bad
    class MFAIL note
    class RA,RB,RC good
    class RDB db
```


**Migration** treats session state as a *payload* that must be carried from one
host to another. It fails for two independent reasons, either of which is
sufficient.

The first is combinatorial. Every editor stores conversations in its own private
format, so migration needs an adapter for each pair of hosts you care about —
N×M, growing quadratically, and every one of them breaks when either vendor ships
a release. Supporting a new IDE means writing new code.

The second is that those stores are hostile to external writers. They are keyed
by a hash of the *absolute workspace path*, so the same repository at
`C:\projects\x` and `/home/dev/x` is already two different keys before anything
crosses the network. The running editor caches them in memory rather than
re-reading from disk, so a concurrent external write is either ignored or
corrupts the file. Building durable memory on top of a private store you do not
control means your memory system's uptime is capped by someone else's release
notes.

**Reconstruction** inverts the direction. Nothing is carried between machines.
Each machine independently *derives* the same identity from information both
machines already have — the repository itself — and then reads the state that
belongs to that identity out of a shared database. There is no transfer step to
break, and supporting a new host costs a bootstrap instruction rather than an
adapter. The cost is linear in hosts, not quadratic in host pairs.

> **The principle:** never transfer identity, derive it. Two machines that can
> compute the same key from the same repository do not need to talk to each other.

---

## 2. How identity is derived

```mermaid
graph LR
    subgraph SRC["Three machines with nothing in common"]
        direction TB
        A["<b>Machine A</b><br/>C:\projects\widget<br/>Windows · editor X · laptop-1"]
        B["<b>Machine B</b><br/>~/scratch/wk — an empty directory<br/>Linux aarch64 · no IDE · box-2"]
        C["<b>Machine C</b><br/>/Users/dev/src/widget<br/>macOS · editor Y · studio"]
    end

    subgraph DERIVE["The only input that counts"]
        direction TB
        REMOTE["<b>git remote get-url origin</b><br/><br/>git@github.com:Acme/widget.git<br/>https://github.com/Acme/widget<br/>ssh://git@github.com:22/Acme/widget.git"]
        NORM["<b>normalize_workspace_key()</b><br/>strip scheme · strip credentials · strip port<br/>strip .git suffix · lowercase"]
        REMOTE --> NORM
    end

    KEY["<b>workspace_key</b><br/>github.com/acme/widget<br/><br/>same on all three"]

    TRIPLE["<b>Session identity</b><br/>( agent_id, operator_id, workspace_key )<br/><br/>SHA256 → <b>open_key</b><br/><i>the database enforces at most one<br/>open session per identity</i>"]

    EXCL["<b>Never part of identity</b><br/><br/>✖ filesystem path &nbsp;&nbsp; ✖ hostname<br/>✖ IDE name &nbsp;&nbsp; ✖ OS / architecture<br/><br/><i>kept as write-only provenance hints.<br/>A test fails if any of them appears<br/>in a lookup predicate or index.</i>"]

    A --> REMOTE
    B --> REMOTE
    C --> REMOTE
    NORM --> KEY --> TRIPLE
    EXCL -.->|"filtered out"| TRIPLE

    classDef machine fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef step fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef key fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20
    classDef excl fill:#ffebee,stroke:#c62828,color:#b71c1c

    class A,B,C machine
    class REMOTE,NORM step
    class KEY,TRIPLE key
    class EXCL excl
```


The VCS remote is the one fact about a workspace that is identical everywhere
and that nobody has to configure. Every transport for the same repository —
SSH, HTTPS, `ssh://` with an explicit port — normalizes to a single key, so a
checkout at `C:\projects\widget` and one at `~/scratch/wk` are recognized
as the same workspace.

Two consequences are worth stating explicitly because they are what make the
design work in practice.

**A workspace does not need to be a real checkout.** Identity comes from the
remote URL and nothing else, so an empty directory with `git init` and
`git remote add origin` is a sufficient workspace. This is how a headless
machine with no clone of the repository can still resume the session — and it is
the sharpest available test that no filesystem state leaked into the key.

**The excluded fields are the load-bearing part.** Hostname, IDE name, OS,
architecture, and absolute path are all recorded, but only as write-only
provenance. If any of them entered a lookup predicate, everything would still
work perfectly on the machine that wrote the row and silently return nothing
everywhere else — the worst failure mode available, because it passes every test
run on one machine. That is why it is enforced by a test
(`tests/test_session.py`) rather than by convention.

A directory with no remote falls back to `local:<directory-name>`, which still
matches across machines when the folder name matches. Usable, but prefer a real
remote.

---

## 3. What actually happens across two machines

```mermaid
sequenceDiagram
    autonumber
    participant A as Machine A<br/>Windows · editor X · x86-64
    participant DB as Session store<br/>MySQL
    participant B as Machine B<br/>Linux · no IDE · aarch64

    rect rgb(240, 248, 255)
    note over A,DB: Working session on A
    A->>A: derive (agent, operator, workspace_key)<br/>from the VCS remote
    A->>DB: open session
    A->>DB: checkpoint — next action, open plan,<br/>redacted working-tree state
    A->>DB: archive transcript (redacted, compressed)
    end

    note over A: laptop closed.<br/>Nothing exported, nothing synced.

    rect rgb(240, 255, 240)
    note over B,DB: Same work picked up on B
    B->>B: git init + git remote add origin<br/>(empty directory — no clone needed)
    B->>B: derive identity → same workspace_key
    B->>DB: resume
    DB-->>B: resume pack: next action, open plan,<br/>VCS state, decisions
    note over B: The agent knows what it was doing.<br/>No chat bubbles were restored — by design.
    end

    rect rgb(255, 250, 240)
    note over B,DB: B branches the work
    B->>DB: open --fork-from (A's session)<br/>--reason host_switch
    B->>DB: checkpoint — next action for whoever is next
    end

    rect rgb(248, 245, 255)
    note over A,DB: Round trip closes
    A->>DB: resume
    DB-->>A: the checkpoint B wrote
    A->>DB: search "why did we fork here"
    DB-->>A: pointers — (source_ref, seq)
    A->>DB: replay --ref ... --around ...
    DB-->>A: the exact exchange, reconstructed
    end
```


Note what is absent between the first phase and the second: there is no export,
no sync, no handshake between A and B. A wrote to the database and closed its
laptop. B computed the same key and read.

The fork in the third phase is what makes the topology a DAG rather than a
chain. When B picks up work that A started, it records `parent_session_id` and
`fork_reason`, so the history keeps the branch structure instead of flattening
it into one confusing linear stream. `agentloom-session tree` renders the shape
directly, rather than requiring a diagram of it:

```text
● 8f2a1c…  cross-host session                 [parked]  laptop-1 / editor X
└── ● 3d9e77…  continued after host switch    [open]    box-2 / none
        fork_reason: host_switch
```

The host and editor shown per node come from the provenance hints — useful for
reading the history, never consulted when looking a session up.

The final phase is the part people find surprising: **A can read what B wrote.**
Continuity is not one-directional "move my work to the new laptop"; both
machines are peers against the same store. That round trip is what a deployment's
remote-host acceptance test asserts, over SSH, against a genuinely different OS
and CPU architecture.

---

## 4. Three tiers, separated by cost

```mermaid
graph TB
    Q["<b>Agent needs context</b>"]

    T1["<b>TIER 1 — Checkpoint</b><br/><i>where was I?</i><br/><br/>agentloom-session resume<br/><br/>hundreds of bytes · one indexed row<br/><b>always, on every session start</b>"]

    T2["<b>TIER 2 — Locator</b><br/><i>when did we decide X?</i><br/><br/>session_search / agentloom-session search<br/><br/>returns pointers, not content<br/>sub-second lexical · hybrid adds one embedding call<br/><b>on demand, mid-conversation</b>"]

    T3["<b>TIER 3 — Archive</b><br/><i>what exactly was said?</i><br/><br/>replay --ref &lt;id&gt; --around &lt;seq&gt;<br/><br/>hundreds of kilobytes per conversation<br/><b>rarely, and only the window you were pointed at</b>"]

    Q --> T1
    T1 -->|"terse — not enough<br/>to settle a question"| T2
    T2 -->|"pointer (source_ref, seq)"| T3

    ANTI["<b>The anti-pattern this shape exists to prevent</b><br/>loading a full transcript on every resume.<br/>A checkpoint is too terse to settle an argument,<br/>an archive is too large to load every time —<br/>so the checkpoint <i>cites</i> the transcript it came from."]

    T3 -.-> ANTI

    classDef q fill:#ede7f6,stroke:#4527a0,stroke-width:2px,color:#311b92
    classDef cheap fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20
    classDef mid fill:#fff8e1,stroke:#f9a825,stroke-width:2px,color:#e65100
    classDef exp fill:#fce4ec,stroke:#ad1457,stroke-width:2px,color:#880e4f
    classDef note fill:#f5f5f5,stroke:#757575,color:#424242,font-size:11px

    class Q q
    class T1 cheap
    class T2 mid
    class T3 exp
    class ANTI note
```


The three tiers answer the same subject — *what happened before* — and differ
only in cost. That is deliberate, and it is the difference between a memory
system that gets used and one that gets disabled for being slow.

A checkpoint is a few hundred bytes and is loaded unconditionally at every
session start. It is far too terse to settle an argument about a past decision,
which is exactly why the archive exists. The archive is a few hundred kilobytes
per conversation and would be absurd to load on every resume, which is exactly
why the checkpoint exists. The locator bridges them by returning *pointers* —
`(source_ref, seq)` — so the agent pages in one window of one conversation
rather than the whole thing.

Each checkpoint cites the transcript it was derived from, so any tier-1 answer
can be expanded into a tier-3 answer on demand. The ladder is navigable in both
directions.

The locator indexes prose only — human and agent text, never tool-call noise —
at two granularities: one node per session, plus overlapping turn windows.
Ranking is hybrid lexical + vector RRF. Time is a filter (`--since`), not
something cosine similarity is asked to encode.

---

## 5. Where this sits in the memory stack

```mermaid
graph LR
    subgraph STACK["AgentLoom memory layers — one question each"]
        direction TB
        L0["<b>Layer 0 — Working session</b><br/><i>Where did we leave off here?</i><br/>checkpoints · transcripts · session DAG<br/><b>scope: this agent + operator + repo</b>"]
        L1["<b>Layer 1 — Curated knowledge</b><br/><i>What is the accepted design?</i><br/>knowledge graph · skills · behaviors<br/><b>scope: the organization</b>"]
        L2["<b>Layer 2 — Management</b><br/><i>What is the team doing now?</i><br/>projects · tasks · messages<br/><b>scope: the team</b>"]
        L3["<b>Layer 3 — Plan / provenance</b><br/><i>Did we plan this before?</i><br/>plan registry · plan embeddings<br/><b>scope: the repository's history</b>"]
    end

    ROUTER{{"Retrieval router<br/><i>route by question,<br/>not by keyword</i>"}}

    ROUTER --> L0
    ROUTER --> L1
    ROUTER --> L2
    ROUTER --> L3

    NEW["<b>Layer 0 is the newest layer</b><br/>and the only one that is<br/><b>per-host volatile by default</b>.<br/><br/>The other three already survived a machine<br/>change because they were always in a shared<br/>database. Layer 0 is what it took to make<br/><i>the thread of work</i> survive too."]

    L0 -.-> NEW

    classDef l0 fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20
    classDef other fill:#eceff1,stroke:#546e7a,color:#263238
    classDef router fill:#ede7f6,stroke:#4527a0,stroke-width:2px,color:#311b92
    classDef note fill:#fff8e1,stroke:#f9a825,color:#e65100,font-size:11px

    class L0 l0
    class L1,L2,L3 other
    class ROUTER router
    class NEW note
```


Layer 0 is numbered zero because it comes first in time, not because it is the
foundation of the others. It is the layer you touch at the start of every
session, before you know which of the other three you will need.

It is also the only layer that was ever host-local. Layers 1–3 were in a shared
database from the beginning, which is why moving machines never lost the
knowledge graph or the task list. Reconstruction is the work of bringing the
last layer up to the same standard.

The boundary matters in the other direction too. A checkpoint is **overwritten
by the next checkpoint** — it is a resume pointer, not a record. Putting a
durable decision in one is a mistake; it belongs in the curated KG (Layer 1) or
a plan (Layer 3). The locator finds where a decision was *discussed*; it does not
become the authority on what was *decided*.

| Question | Layer |
|---|---|
| Where did we leave off in this repository? | **Layer 0** — checkpoint |
| When did we decide X? | **Layer 0** — locator, then replay |
| What is the accepted design? | Layer 1 — curated knowledge |
| What is the team doing now? | Layer 2 — management |
| Did we plan this before? | Layer 3 — plan / provenance |

---

## 6. What to verify in a deployment

Architecture documents describe intent. A deployment should be able to
*measure* whether the intent holds, because every property below has a failure
mode that leaves the system looking healthy.

Absolute numbers depend on your corpus size, database, embedding provider, and
network, so none are given here. What transfers is the list — and the reason
each item is on it.

| Property to check | The silent failure it catches |
|---|---|
| A resume returns a checkpoint written by a **different** host | The claim is cross-host continuity. If only one machine ever wrote, nothing has been proven — everything works on the machine that wrote it. |
| Search actually reaches hybrid mode | If the embedding provider is unreachable or unconfigured, hybrid search degrades to lexical **and still returns plausible results**. Nothing errors; recall just quietly gets worse. |
| Every chunk has an embedding, from exactly **one** model | Partial coverage silently drops rows from vector ranking. Mixing models is worse: the vectors are not comparable, so scores are meaningless rather than absent. |
| Every archived transcript is indexed | Archive and index are separate steps. If only the first is scheduled, conversations are stored but unfindable. |
| The archive is fresh | A scheduled capture job that dies leaves a store that looks fine and stops growing. Compare newest archived conversation against wall-clock. |
| Vectors decode, none are zero-norm, blob length matches the stored dimension | Encoding bugs and truncation produce vectors that still *have* a cosine similarity — just the wrong one. |
| Index size against the content it indexes | A ratio that drifts upward means the index is becoming the dominant cost. Worth a decision before it is a bill. |
| Search latency, tracked over time | This matters as a **slope**, not a reading. Lexical ranking is a linear scan: comfortable at ten thousand chunks, not at fifty thousand. A single measurement cannot tell you which side of that you are on. |

The last row generalizes: run these checks periodically and keep the results,
rather than checking once at deployment. A single run tells you the system is
healthy now; a sequence tells you the direction, and the direction is what
decides when to act.

The audit itself belongs in your deployment repository rather than in this
library, because the queries are necessarily shaped by your schema and your
operational thresholds. Two properties are worth copying from any
implementation of it: make it strictly read-only — an audit tool that can
modify what it audits is a source of incidents — and have it report exact
counts rather than `information_schema` row estimates, which are approximate
for InnoDB and can be off by a factor of two.

---

## 7. What this deliberately does not do

It does not restore native chat bubbles in the target editor's own sidebar. That
is the one capability that requires writing a host's private chat store, and it
is refused for the reasons in §1.

The trade-off is narrower than "no chat history", which is the usual
misreading. The conversation itself moves — captured, redacted, compressed,
stored, searchable, and replayable on any machine. What does not move is its
*rendering inside one vendor's UI widget*. You read it with `replay` or in the
web viewer instead of by scrolling a sidebar.

Everything else on the anti-pattern list follows from the same commitment:
no symlinking editor application-data directories, no keying a session on an
absolute checkout path, no filtering a resume lookup on a machine name, no
requiring an editor extension for any operation.

The same argument applies one layer down. Derived archive state — a translated
turn, a listing title, a reviewer's score — is still Layer 0. If it lives in a
file beside the checkout, switching machines drops it. Overlays go in
`presentation_json`; locator rows are keyed by `locale`; batch progress and
judgement go in `session_job_*`. The deployment-repo specification is
`docs/architecture/memory/layer-0-archive-presentation-and-job-trace.md`.

---

## Related

- [`layer-0-session-memory.md`](layer-0-session-memory.md) — the Layer 0 contract: host-neutrality invariants, data model, host adapter conformance checklist.
- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md) — layers 1–3 and the retrieval router.
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md) — the file → database sync contract for curated knowledge.

### Reproducing the diagrams

Diagrams are inline Mermaid so this document stays a single portable file with
no binary assets. Rendered PNG exports (for slides or papers) can be produced
with:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i diagram.mmd -o diagram.png -b white -s 2
```
