# Transaction Reliability Platform

A small educational and reference implementation demonstrating reliable payment-authorization orchestration, explicit ambiguous-outcome handling, durable PostgreSQL persistence, and atomic HTTP idempotency under external network failures.

This project is an architectural reference exploring how distributed systems handle network timeouts, non-deterministic transport failures, and provider boundaries. It is **not** production-ready, PCI-DSS compliant, or fully event-driven, and does not claim distributed exactly-once execution.

---

## Current Architecture

The platform follows a ports-and-adapters (hexagonal) architecture separating HTTP transport and persistence boundaries from core business logic:

```mermaid
flowchart TD
    Consumer["API Consumer"] -->|"POST /v1/transactions\n(X-Correlation-ID, Idempotency-Key)"| API["FastAPI Transaction API\n(:8000)"]
    API -->|"Validates headers\nDispatches command"| Orch["IdempotentTransactionOrchestrator"]
    Orch -->|"1. Reserve (Short Tx 1)\n6. Complete (Short Tx 2)"| CoordPort["IdempotencyCoordinatorPort\n(Protocol)"]
    CoordPort -.->|"Implemented by"| Coord["SqlAlchemyIdempotencyCoordinator"]
    Coord -->|"Schema via Alembic\nAtomic Unit of Work"| DB[("PostgreSQL\n(:5432)")]
    Orch -->|"2. Execute after reservation"| Service["Authorization Application Service"]
    Service -->|"Enforces transitions\n(version tracking)"| Domain["Transaction Domain State Machine"]
    Service -->|"3. Invokes port"| Port["PaymentNetworkPort\n(Protocol)"]
    Port -.->|"Implemented by"| Adapter["HttpPaymentNetworkAdapter\n(HTTPX AsyncClient)"]
    Adapter -->|"4. POST /v1/authorizations\n(X-Correlation-ID)"| Emulator["Payment Network Emulator\n(:8001)"]
```

### Execution Flow:
1. **Request Ingestion**: The API consumer sends `POST /v1/transactions` with `X-Correlation-ID` and `Idempotency-Key`.
2. **Orchestration Dispatch**: FastAPI validates header formats and invokes `IdempotentTransactionOrchestrator`.
3. **Atomic Reservation**: In Short Transaction 1, the orchestrator reserves the transaction (`CREATED`, `version=0`) and an `IN_PROGRESS` idempotency record in PostgreSQL. During normal concurrent processing, only the reservation winner invokes the payment network.
4. **Network Invocation**: The authorization service executes authorization against `PaymentNetworkPort` without holding an open database transaction.
5. **Gateway Emulation**: `HttpPaymentNetworkAdapter` executes the HTTP request against the deterministic emulator.
6. **Atomic Completion**: In Short Transaction 2, the orchestrator atomically stores the final transaction state, increments the version (`WHERE version = :expected_version`), and stores the cached response envelope.

---

## Implemented Features

* **Durable PostgreSQL Transaction Persistence**: Relational persistence for transactions and idempotency state via PostgreSQL 16.
* **SQLAlchemy 2.x Asynchronous Persistence**: Asyncpg-backed async session orchestration and connection pooling.
* **Alembic Schema Migrations**: Version-controlled DDL migrations applying schema definitions and constraints.
* **Atomic Idempotency-Key Reservation**: Two-phase reservation and completion unit-of-work preventing duplicate downstream network invocations.
* **SHA-256 Idempotency-Key Lookup Digest**: Keys are hashed before storage to maintain fixed-width index lookups and prevent token leakage.
* **Canonical Request Fingerprinting**: SHA-256 digest over sorted, canonical request JSON detects payload tampering or key reuse with different parameters.
* **Completed-Response Replay with `Idempotent-Replay: true`**: Safe, immediate replay of completed operations directly from durable storage.
* **Same Key with Different Payload Returns HTTP 409**: Mismatched request fingerprints for an existing key return RFC 7807 problem details (`urn:problem-type:idempotency-conflict`).
* **Concurrent In-Progress Request Returns HTTP 409 with `Retry-After: 1`**: Competing requests while a key is in progress receive `urn:problem-type:idempotency-in-progress`.
* **Different Idempotency Keys Cannot Reuse Transaction ID**: Detects and rejects client attempts to assign an already-persisted transaction ID under a different idempotency key with HTTP 409 (`urn:problem-type:transaction-id-conflict`).
* **Optimistic Transaction-Version Locking**: Atomic updates enforce `WHERE transaction_id = :id AND version = :expected_version`, guarding against concurrent overwrites.
* **Database Check Constraints Matching Domain Invariants**: 14 PostgreSQL check constraints enforce valid statuses, currency formats, positive amounts, hash formats, response integrity, and cached-header allowlisting.
* **Docker Compose PostgreSQL Environment**: Local containerized PostgreSQL 16 Alpine service with health checks.
* **GitHub Actions CI with PostgreSQL**: CI pipeline testing migrations, linting, typing, and end-to-end integration against a live database service.
* **Framework-Independent Transaction State Machine**: Pure standard-library domain entity enforcing discrete states (`CREATED`, `AUTHORIZED`, `DECLINED`, `CAPTURED`, `REFUNDED`, `PENDING_RECONCILIATION`, `FAILED`).
* **Explicit `PENDING_RECONCILIATION` State**: Pairs ambiguous outcomes with matching typed pending operations (`AUTHORIZATION`, `CAPTURE`, `REFUND`), preventing premature failure assumptions or unsafe retries.
* **Typed Domain Exceptions & Invariants**: Enforces transition rules, terminal states, and non-empty identifier validations with typed domain errors.
* **Deterministic Payment-Network Emulator**: Isolated FastAPI service simulating approved, declined, timed-out, malformed, and rate-limited gateway responses.
* **Sanitized RFC 7807 Problem Details**: Maps validation, rate-limiting, downstream-rejection, and missing-header errors into structured problem responses without leaking provider internals or raw tokens.
* **Synthetic Token-Only Boundary**: Accepts exclusively synthetic test tokens (`tok_test_*`); strictly rejects cardholder data (`pan`, `cvv`, `card_number`) and unknown fields.
* **331 Passing Tests**: Comprehensive automated test suite spanning domain unit tests, emulator contracts, constraint enforcement, optimistic concurrency, and concurrent API coordination.

---

## Idempotency Behavior

| Situation | Result |
| :--- | :--- |
| New key and transaction ID | Reserve both records and invoke the network |
| Same key and same payload while processing | HTTP 409 with `Retry-After: 1` (`urn:problem-type:idempotency-in-progress`) |
| Same key and same completed payload | Return cached response with `Idempotent-Replay: true` |
| Same key with a different payload | HTTP 409 idempotency conflict (`urn:problem-type:idempotency-conflict`) |
| Different key with an existing transaction ID | HTTP 409 transaction-ID conflict (`urn:problem-type:transaction-id-conflict`) |

### Key & Token Security:
* **Correlation-ID Reflection**: Replay responses use the current request's `X-Correlation-ID`, not the original request's correlation ID.
* **No Raw Keys or Tokens Stored**: Raw idempotency keys and raw payment tokens are never persisted in plain text. Only the SHA-256 lookup digest and canonical request fingerprint are stored.
* **Entropy Requirement**: SHA-256 hashing indexes keys efficiently but does not protect predictable or low-entropy keys from offline guessing. Clients must generate high-entropy random keys (e.g., UUIDv4).

---

## Scenario Handling & Response Contracts

The transaction service maps downstream outcomes into domain states and HTTP responses:

| Token | Emulator response | Transaction API response | Domain state |
| :--- | :--- | :--- | :--- |
| `tok_test_approved` | HTTP 200 authorized result | HTTP 201 | `AUTHORIZED` |
| `tok_test_declined` | HTTP 200 declined business result | HTTP 201 | `DECLINED` |
| `tok_test_timeout` | Delayed successful response; client timeout makes the outcome ambiguous | HTTP 202 | `PENDING_RECONCILIATION` |
| `tok_test_malformed` | HTTP 200 with deliberately invalid JSON | HTTP 202 | `PENDING_RECONCILIATION` |
| `tok_test_rate_limited` | HTTP 429 with `Retry-After` | HTTP 503 | `FAILED` |

### Key Failure Boundary Behaviors:
* **Ambiguous Timeouts (HTTP 408 & Socket Timeouts)**: Upstream HTTP 408 and read/connect timeouts represent unknown outcomes; the transaction enters `PENDING_RECONCILIATION` (HTTP 202).
* **Transport Errors & HTTP 5xx**: Network transport errors (`httpx.TransportError`) and server errors (HTTP 5xx) conservatively transition to `PENDING_RECONCILIATION` (HTTP 202).
* **Explicit Provider Rejections (HTTP 4xx)**: Under the current emulator contract, non-408 and non-429 client errors (for example, 400 and 422) are treated as explicit request rejection; the transaction transitions to `FAILED` and returns HTTP 502 without exposing provider internals.
* **Rate Limiting (HTTP 429)**: Under the current emulator contract, HTTP 429 confirms rejection before authorization processing; the transaction transitions to `FAILED` and returns HTTP 503 with a persisted, sanitized `Retry-After` header. Real provider contracts must be evaluated independently.

---

## Transaction Lifecycle State Machine

```mermaid
stateDiagram-v2
    [*] --> CREATED: Request Received

    CREATED --> AUTHORIZED: Confirmed Approval (HTTP 200)
    CREATED --> DECLINED: Confirmed Decline (HTTP 200)
    CREATED --> PENDING_RECONCILIATION: Timeout / HTTP 408 / Transport Error / 5xx / Malformed 200
    CREATED --> FAILED: Provider Rejection (4xx) / Pre-processing Rate Limit (429)

    PENDING_RECONCILIATION --> AUTHORIZED: Reconciled as Approved
    PENDING_RECONCILIATION --> DECLINED: Reconciled as Declined
    PENDING_RECONCILIATION --> FAILED: Reconciled as Unprocessed / Voided

    AUTHORIZED --> CAPTURED: Capture Operation
    CAPTURED --> REFUNDED: Refund Operation

    DECLINED --> [*]
    FAILED --> [*]
    REFUNDED --> [*]
```

*Note: Capture and refund transitions are fully enforced in the domain entity; their external API endpoints will be added in subsequent milestones.*

---

## Current Limitations

* **Crash Window / No Automatic Recovery of Abandoned `IN_PROGRESS` Requests**: If the service crashes after invoking the payment network but before committing the final result, the transaction remains `CREATED` and the idempotency record remains `IN_PROGRESS`. Retries are blocked from automatically invoking the network again. Operational recovery or a future reconciliation worker is required.
* **No Asynchronous Reconciliation Worker**: Transactions in `PENDING_RECONCILIATION` cannot yet be polled or resolved out-of-band by a background worker.
* **No Kafka or Transactional Outbox**: Status changes are not yet published to an event streaming platform.
* **No Authentication or mTLS**: Endpoints do not require API tokens or mutual TLS.
* **No Capture or Refund API Endpoints**: Capture and refund domain operations are implemented, but external HTTP endpoints are not yet exposed.
* **No Observability Platform**: No Prometheus metrics or OpenTelemetry distributed tracing.
* **Synthetic Test Tokens Only**: Accepts only `tok_test_*` synthetic tokens; no live payment processing or banking integration.
* **No Real Payment Processing**: Pure educational emulator integration.
* **No PCI-DSS Compliance Claim**: Demonstrates data-minimization boundaries but makes no formal PCI-DSS compliance claims.

---

## Planned Future Milestones

1. **Reconciliation Worker & Abandoned-Request Recovery**: Background daemon resolving `PENDING_RECONCILIATION` transactions and recovering abandoned `IN_PROGRESS` records.
2. **Transactional Outbox & Event Publishing**: Reliable event streaming via Kafka for transaction status changes.
3. **Capture & Refund API Endpoints**: Expanding API orchestration to post-authorization lifecycles.
4. **Authentication & Service-to-Service Security**: Adding mTLS and token-based authentication.
5. **Metrics & Distributed Tracing**: Prometheus metrics and OpenTelemetry tracing across services.

---

## Developer Instructions

### Prerequisites
* Python 3.12+
* Docker & Docker Compose

### 1. Environment Setup
```bash
# Create and activate a Python 3.12 virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install package in editable mode with development dependencies
python -m pip install -e ".[dev]"

# Start PostgreSQL container and apply database migrations
docker compose up -d
alembic upgrade head
```

### 2. Running the Services
Start each service in a separate terminal:

```bash
# Terminal 1: Payment Network Emulator (:8001)
uvicorn transaction_platform.emulator.app:app --port 8001

# Terminal 2: Transaction API (:8000)
uvicorn transaction_platform.api.app:app --port 8000
```

Service URLs and liveness health checks:
* **Transaction API**: `http://localhost:8000` (Liveness: `http://localhost:8000/health/live`)
* **Payment Network Emulator**: `http://localhost:8001` (Liveness: `http://localhost:8001/health/live`)

### 3. Send a Sample Authorization Request
```bash
curl -i -X POST http://localhost:8000/v1/transactions \
  -H "Content-Type: application/json" \
  -H "X-Correlation-ID: corr_sample_001" \
  -H "Idempotency-Key: idem_sample_001" \
  -d '{
    "transaction_id": "txn_sample_001",
    "merchant_id": "merchant_101",
    "payment_token": "tok_test_approved",
    "amount": 2500,
    "currency": "USD"
  }'
```

Re-sending the exact same request returns the cached response with `Idempotent-Replay: true`:
```bash
curl -i -X POST http://localhost:8000/v1/transactions \
  -H "Content-Type: application/json" \
  -H "X-Correlation-ID: corr_sample_002" \
  -H "Idempotency-Key: idem_sample_001" \
  -d '{
    "transaction_id": "txn_sample_001",
    "merchant_id": "merchant_101",
    "payment_token": "tok_test_approved",
    "amount": 2500,
    "currency": "USD"
  }'
```

### 4. Running Verification
Running the complete test suite requires the local PostgreSQL container and migrated schema:
```bash
pytest          # Run complete test suite (331 tests)
ruff check .    # Run linting and style checks
mypy src        # Run strict static type checks
```

To run the PostgreSQL-independent subset without a database:
```bash
DATABASE_URL=postgresql+asyncpg://invalid:invalid@127.0.0.1:1/invalid \
pytest tests/unit tests/integration/emulator
```

### 5. Continuous Integration
GitHub Actions runs for pushes to `main` and `feature/*`, and for pull requests targeting `main`:
* Ruff linting
* mypy strict type checking
* Alembic migrations against a PostgreSQL 16 service container
* The complete Pytest suite (331 tests)
* A committed-diff whitespace check (`git show --check --format= HEAD`)

### 6. Cleanup
To stop the local PostgreSQL container:
```bash
docker compose down
```
*(Note: Do not pass `-v` because `docker compose down -v` deletes the PostgreSQL volume and its data).*

---

## AI Disclosure

AI tools assisted with scaffolding, implementation suggestions, test generation, and review. Architecture decisions, acceptance criteria, validation, and code ownership remain with the author.
