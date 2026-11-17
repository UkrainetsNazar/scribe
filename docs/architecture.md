# Architecture

The diagrams are checked against the code by `tests/unit/test_docs.py`: the entity-relationship
diagram must match the database schema, the state diagram the state machine and the message
topology diagram the declared queues.

## Containers

One image runs as five processes; `api` and `ws` are the same code with different routes
(`APP_ROLE`). Only Traefik is reachable from outside, except the presigned URLs that point
straight at MinIO.

```mermaid
flowchart LR
    client(["Client app"])
    subgraph edge["Edge"]
        traefik["Traefik gateway<br/>routing, forward-auth,<br/>rate limit, CORS"]
    end
    subgraph app["Application (one image, several processes)"]
        api["api<br/>REST"]
        ws["ws<br/>WebSocket"]
        relay["relay<br/>outbox to broker"]
        consumer["consumer<br/>inbox and handlers"]
        worker["worker<br/>Celery pipeline"]
    end
    subgraph stores["Stores"]
        pg[("PostgreSQL")]
        mongo[("MongoDB")]
        redis[("Redis")]
        minio[("MinIO")]
        rabbit{{"RabbitMQ"}}
    end
    subgraph obs["Observability"]
        prom["Prometheus"]
        grafana["Grafana"]
        jaeger["Jaeger"]
    end

    client -->|"REST and WebSocket"| traefik
    client -.->|"presigned PUT and GET"| minio
    traefik -->|"/api (forward-auth first)"| api
    traefik -->|"/ws"| ws
    api --> pg
    api --> mongo
    api --> minio
    api --> redis
    ws --> pg
    ws --> redis
    relay --> pg
    relay -->|"publish"| rabbit
    rabbit -->|"events"| consumer
    consumer --> pg
    consumer --> redis
    consumer --> mongo
    consumer --> minio
    consumer -->|"dispatch stage 1"| rabbit
    rabbit -->|"Celery tasks"| worker
    worker --> pg
    worker --> mongo
    worker --> minio
    worker --> redis
    prom -.->|"scrape /metrics"| app
    prom -.->|"scrape"| rabbit
    prom -.->|"scrape"| traefik
    grafana --> prom
    grafana --> jaeger
    app -.->|"OTLP traces"| jaeger
    traefik -.->|"OTLP traces"| jaeger
```

## From upload to a finished transcript

The state change and the event that announces it are written in one transaction (Transactional
Outbox). The consumer registers the message id and runs the handler in one transaction (Inbox), so
a redelivered message has no second effect.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant G as Gateway
    participant A as API
    participant S as MinIO
    participant P as PostgreSQL
    participant R as Relay
    participant Q as RabbitMQ
    participant K as Consumer
    participant W as Worker
    participant X as Redis

    C->>G: POST /recordings
    G->>A: forward-auth, then the request
    A->>P: insert recording (created) and its outbox event
    A-->>C: 201 and a presigned upload URL
    C->>S: PUT the file (the API does not proxy bytes)
    C->>G: WebSocket /ws/recordings/{id}
    C->>G: POST complete-upload
    G->>A: forward-auth, then the request
    A->>S: copy the upload to a private key pinned to its ETag
    A->>A: ffprobe the private copy
    rect rgb(235, 245, 255)
    Note over A,P: one transaction
    A->>P: state processing, charge the quota, create the stages
    A->>P: outbox event processing_requested
    end
    A-->>C: 202
    R->>P: SELECT ... FOR UPDATE SKIP LOCKED
    R->>Q: publish with publisher confirms
    R->>P: published_at = now()
    Q->>K: processing_requested
    K->>P: inbox row and handler in one transaction
    K->>Q: dispatch stage 1 (Celery)
    K->>X: store the state and publish it
    X-->>C: WebSocket: status processing
    loop six stages
        Q->>W: stage task
        W->>S: read and write artifacts
        W->>P: stage rows (recording row lock, run check)
        W->>X: progress and partial segments (best effort)
        X-->>C: WebSocket: progress and segments
    end
    W->>P: last stage: recording completed and its outbox event
    R->>Q: publish completed
    Q->>K: completed
    K->>X: store the final state and publish it
    X-->>C: WebSocket: completed, the server closes the socket
```

## Recording states

```mermaid
stateDiagram-v2
    [*] --> created
    created --> uploaded: complete-upload
    created --> deleted: DELETE
    uploaded --> processing: validated and within quota
    uploaded --> failed: invalid file or quota exceeded
    processing --> completed: all stages completed
    processing --> failed: permanent error or retries exhausted
    processing --> cancelled: cancel
    failed --> processing: retry
    cancelled --> processing: retry
    failed --> deleted: DELETE
    cancelled --> deleted: DELETE
    completed --> deleted: DELETE
    deleted --> [*]
```

Each start of processing increments the recording's `run`. Every pipeline task carries its `run`
and changes state only while it is the current one, under the recording's row lock, so tasks of a
cancelled or retried run cannot touch the new one.

## Messaging topology

```mermaid
flowchart LR
    relay["relay"] -->|"routing key = event type"| events{{"scribe.events (topic)"}}
    events -->|"recording.processing_requested"| start["pipeline.start"]
    events -->|"recording.processing_requested<br/>recording.completed<br/>recording.failed<br/>recording.cancelled"| progress["progress.events"]
    events -->|"recording.deleted"| cleanup["recording.cleanup"]
    events -->|"recording.completed<br/>recording.failed"| webhook["webhook.delivery"]
    start --> consumers["consumers (inbox)"]
    progress --> consumers
    cleanup --> consumers
    webhook --> consumers
    consumers -->|"handler failed"| retry{{"scribe.retry<br/>5 s, 30 s, 2 min, 10 min"}}
    retry -->|"TTL expired: back to the main queue"| consumers
    consumers -->|"retries exhausted"| dlx{{"scribe.dlx"}}
    dlx --> dlq["*.dlq<br/>alert DeadLetterQueueNotEmpty"]
```

Handlers: `pipeline.start` queues the first Celery stage, `progress.events` feeds the WebSocket
state in Redis, `recording.cleanup` removes files, documents and rows of a deleted recording,
`webhook.delivery` calls the project's webhook.

## Data model

PostgreSQL holds the transactional data; MongoDB the transcript segments (one document each, so a
long recording never reaches the 16 MB document limit), transcript metadata and summaries; Redis
refresh tokens, the last progress state per recording and Pub/Sub channels; MinIO the uploads,
pipeline artifacts and exports.

```mermaid
erDiagram
    plans ||--o{ users : "subscribed"
    users ||--o{ usage : "monthly minutes"
    users ||--o{ projects : "owns"
    projects ||--o{ recordings : "contains"
    users ||--o{ recordings : "owns"
    recordings ||--o{ processing_stages : "has"
    recordings ||--o{ exports : "has"

    plans {
        int id PK
        string name UK
        int monthly_minutes
        int max_file_mb
    }
    users {
        uuid id PK
        string email UK
        string password_hash
        int plan_id FK
        bool is_active
        datetime created_at
    }
    usage {
        uuid user_id PK
        string period PK
        int minutes_used
    }
    projects {
        uuid id PK
        uuid owner_id FK
        string name
        string description
        datetime created_at
        datetime deleted_at
        string webhook_url
        string webhook_secret
    }
    recordings {
        uuid id PK
        uuid project_id FK
        uuid owner_id FK
        string title
        string source_filename
        string content_type
        bigint size_bytes
        float duration_sec
        string language
        string status
        string storage_key
        int run
        datetime created_at
        datetime updated_at
    }
    processing_stages {
        int id PK
        uuid recording_id FK
        string name
        string status
        int progress
        int attempts
        datetime started_at
        datetime finished_at
        string error
    }
    exports {
        uuid id PK
        uuid recording_id FK
        string format
        string storage_key
        string status
        datetime created_at
    }
    outbox_events {
        bigint id PK
        uuid event_id UK
        string aggregate_type
        uuid aggregate_id
        string event_type
        jsonb payload
        jsonb headers
        datetime created_at
        datetime published_at
    }
    inbox_messages {
        uuid message_id PK
        string consumer PK
        datetime received_at
        datetime processed_at
    }
```

## Guarantees

- **Events are not lost:** an event is committed together with the state change; the relay marks it
  published only after the broker confirmed it. A crash in between publishes it twice.
- **Effects are not doubled:** a consumer commits its inbox row, the handler's database changes and
  nothing else in one transaction; a duplicate finds the row and is skipped. Calls to external
  systems (webhooks) are at-least-once and carry a stable delivery id.
- **Progress is eventually consistent:** status changes are reliable (outbox); stage progress and
  partial segments go straight to Redis, are best effort and never fail a stage. The stored state
  never moves backwards within a run.
- **Traces are continuous:** the W3C trace context is stored in the outbox row, so one trace covers
  the request, the relay, the broker, the consumer, the Celery dispatch and every stage.