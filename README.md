# Distributed Rate Limiter Backend Service

A high-performance backend rate limiting service built with **Python 3.13** and **FastAPI**, backed by **Redis** (in-memory atomic data structures) and **MongoDB** (TTL indexed documents).

---

## Table of Contents
1. [Architecture & Project Layout](#architecture--project-layout)
2. [Chosen Unstructured Data Models](#chosen-unstructured-data-models)
3. [Rate Limiting Algorithm & Lua Mechanics](#rate-limiting-algorithm--lua-mechanics)
4. [Database Failure Modes: Fail-Open vs. Fail-Closed](#database-failure-modes-fail-open-vs-fail-closed)
5. [Environment Setup](#environment-setup)
6. [Running the Application](#running-the-application)
7. [Running Tests](#running-tests)
8. [API Endpoints & Rate Limit Headers](#api-endpoints--rate-limit-headers)

---

## Architecture & Project Layout

```text
rate-limiter/
├── config/
│   ├── __init__.py
│   └── settings.py          # Pydantic-based configuration management (.env loading)
├── src/
│   ├── __init__.py
│   ├── main.py              # Application factory, lifespan hooks, and server entrypoint
│   ├── api/
│   │   ├── __init__.py
│   │   └── routes/
│   │       ├── __init__.py  # Aggregated API router
│   │       ├── health.py    # Health check endpoint (GET /health)
│   │       └── example.py   # Protected example route
│   ├── routes/
│   │   ├── __init__.py      # Sample route definitions
│   │   └── api.py           # Standard (GET /api/data) & Strict (POST /api/expensive)
│   ├── db/
│   │   ├── __init__.py      # Connection lifecycle management (init_db, close_db)
│   │   ├── redis.py         # Async Redis client with connection pooling & ping checks
│   │   └── mongo.py         # Async Motor/MongoDB client with TTL indexing
│   ├── limiter/
│   │   ├── __init__.py      # Rate limiter package exports
│   │   ├── rate_limiter.py  # Core atomic rate limiting engine (Lua + Sliding Window Log)
│   │   └── rateLimiter.py   # CamelCase alias re-export
│   └── middleware/
│       ├── __init__.py      # Middleware package exports
│       ├── rate_limit.py    # FastAPI / Starlette rate limiting middleware
│       └── rateLimit.py     # CamelCase alias re-export
├── tests/
│   ├── __init__.py
│   ├── test_db.py           # Unit tests for database connection error handling & lifecycle
│   ├── test_health.py       # Pytest suite for health check endpoint
│   ├── test_rate_limiter.py # Unit tests for rate limiting algorithms & fail-modes
│   ├── test_middleware.py   # Tests for client extraction, response headers & 429 payload
│   ├── test_sample_routes.py# Route integration and quota isolation tests
│   └── test_rate_limit_integration.py # Under limit, 429 retry-after, and window recovery
├── .env.example             # Configuration template
├── .gitignore               # Excludes virtualenvs, cache, build artifacts, and env files
├── requirements.txt         # Pinned project dependencies
└── README.md
```

---

## Chosen Unstructured Data Models

### 1. Redis In-Memory Data Structures (Primary Engine)

Redis is the industry standard for distributed rate limiting due to sub-millisecond latency, atomic transactions, and native key TTL expiration.

#### Sliding Window Log (Primary Algorithm)
- **Data Type**: Sorted Set (`ZSET`)
- **Key Pattern**:
  ```text
  ratelimit:swl:{client_id}:{method}:{endpoint}
  ```
  *Example*: `ratelimit:swl:apikey:usr_alpha:GET:/api/data`
- **Fields / Score & Member**:
  - **Score**: Unix timestamp in milliseconds (`float`, e.g. `1700000000125.0`)
  - **Member**: Unique request identifier: `{timestamp_ms}:{uuid4}`
- **TTL (Expiration)**:
  `window_seconds + 5` (grace buffer). Redis automatically purges the key once the client becomes idle.
- **Atomic Operations**:
  - `ZREMRANGEBYSCORE key -inf (now - window)`: Drops requests outside the sliding window.
  - `ZCARD key`: Counts current valid requests within the window.
  - `ZRANGE key 0 0 WITHSCORES`: Identifies the oldest request to compute exact `reset_after` and `retry_after`.
  - `ZADD key now request_id`: Adds the request if quota permits.

#### Other Supported Redis Models
- **Fixed Window Counter**:
  - **Data Type**: `String`
  - **Key Pattern**: `ratelimit:fixed:{client_id}:{endpoint}:{window_id}`
  - **Value**: Integer counter (`INCRBY`)
  - **TTL**: Window size (e.g. `60s`).
- **Token Bucket**:
  - **Data Type**: Hash (`HSET`)
  - **Key Pattern**: `ratelimit:token_bucket:{client_id}:{endpoint}`
  - **Fields**: `tokens` (float remaining), `last_updated` (epoch float)
  - **TTL**: Time required to refill bucket to full capacity.

---

### 2. MongoDB Document Model (Alternative Persistent Store)

MongoDB is used when persistent document history or auditability is desired. Automatic document cleanup is achieved via **MongoDB TTL Indexes**.

- **Collection**: `rate_limits`
- **Document Structure**:
  ```json
  {
    "_id": "rl:apikey:usr_100:GET:/api/data:1700000000",
    "client_id": "apikey:usr_100",
    "endpoint": "/api/data",
    "method": "GET",
    "window_start": "2026-11-24T16:00:00.000Z",
    "request_count": 4,
    "max_requests": 60,
    "window_seconds": 60,
    "expires_at": "2026-11-24T16:01:00.000Z",
    "metadata": {
      "tier": "enterprise",
      "user_agent": "Mozilla/5.0"
    }
  }
  ```
- **TTL & Query Indexes**:
  ```javascript
  // Background thread removes documents where expires_at <= current time
  db.rate_limits.createIndex({ "expires_at": 1 }, { expireAfterSeconds: 0 });

  // Query performance index
  db.rate_limits.createIndex({ "client_id": 1, "endpoint": 1, "window_start": -1 });
  ```

---

## Rate Limiting Algorithm & Lua Mechanics

The **Sliding Window Log** algorithm was selected as the default because it resolves the edge-burst vulnerability inherent to Fixed Window algorithms (where a client can send $2 \times$ limit across a window boundary).

### Atomic Lua Script Execution

To prevent race conditions across concurrent processes, the entire evaluation executes inside Redis via an **atomic Lua script**:

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local request_id = ARGV[4]
local cost = tonumber(ARGV[5])
local expire_seconds = tonumber(ARGV[6])

local clear_before = now - window_ms

-- 1. Remove expired entries outside the sliding window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Count active requests within the window
local current_count = redis.call('ZCARD', key)

-- 3. Find oldest request to calculate accurate reset_after
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local oldest_ts = now
if #oldest > 0 then
    oldest_ts = tonumber(oldest[2])
end
local reset_after = math.max(0, ((oldest_ts + window_ms) - now) / 1000)

-- 4. Admit or Reject
if (current_count + cost) <= limit then
    redis.call('ZADD', key, now, request_id)
    redis.call('EXPIRE', key, expire_seconds)
    current_count = current_count + cost
    local remaining = math.max(0, limit - current_count)
    return {1, remaining, tostring(reset_after), "0.0"}
else
    local remaining = math.max(0, limit - current_count)
    local retry_after = reset_after > 0 and reset_after or 0.001
    return {0, remaining, tostring(reset_after), tostring(retry_after)}
end
```

---

## Database Failure Modes: Fail-Open vs. Fail-Closed

Configured via `RATE_LIMIT_FAIL_CLOSED` in `.env`:

1. **Fail-Open (`RATE_LIMIT_FAIL_CLOSED=false`, Default)**:
   - When Redis or MongoDB is unreachable, the system catches the error, logs a diagnostic warning, and permits requests (`allowed=True`, `remaining=limit`, `retry_after=None`).
   - **Use Case**: Recommended for public-facing consumer APIs where uptime and user experience outweigh strict rate limiting during unexpected cache outages.
2. **Fail-Closed (`RATE_LIMIT_FAIL_CLOSED=true`)**:
   - When the database is down, the system rejects requests (`allowed=False`, `remaining=0`, `retry_after=window_seconds`).
   - **Use Case**: Recommended for high-cost computational APIs, paid third-party proxies, or resource-constrained database services to protect infrastructure from overload during database downtime.

---

## Environment Setup

### 1. Prerequisites
- **Python**: Version 3.10+ (tested on Python 3.13)
- **Redis**: Recommended (localhost:6379)
- **MongoDB**: Optional (localhost:27017)

### 2. Virtual Environment Setup

- **Windows (PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  ```
- **macOS / Linux**:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create your local `.env` from the provided template:

```powershell
Copy-Item .env.example .env
```

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8000` | Port for the HTTP server |
| `HOST` | `0.0.0.0` | Host address to bind |
| `ENVIRONMENT` | `development` | Deployment environment (`development`, `production`) |
| `RATE_LIMIT_FAIL_CLOSED` | `false` | Fail-mode when DB is offline (`false`=allow, `true`=deny) |
| `RATE_LIMIT_DEFAULT_LIMIT` | `100` | Fallback limit for untargeted endpoints |
| `RATE_LIMIT_DEFAULT_WINDOW` | `60` | Fallback window in seconds |
| `RATE_LIMIT_DATA_LIMIT` | `60` | Request limit for `GET /api/data` |
| `RATE_LIMIT_DATA_WINDOW` | `60` | Window in seconds for `GET /api/data` |
| `RATE_LIMIT_EXPENSIVE_LIMIT` | `5` | Request limit for `POST /api/expensive` |
| `RATE_LIMIT_EXPENSIVE_WINDOW` | `60` | Window in seconds for `POST /api/expensive` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `MONGODB_URL` | `mongodb://localhost:27017` | MongoDB connection URL |
| `MONGODB_DB_NAME` | `rate_limiter_db` | MongoDB database name |

---

## Running the Application

### 1. Start Server with Auto-Reload
```powershell
.\.venv\Scripts\python.exe -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

### 2. Interactive API Documentation
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## Running Tests

Execute the comprehensive test suite with Pytest:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

### Test Coverage Highlights (24 Automated Tests)
1. **Requests Under Limit ([tests/test_rate_limit_integration.py](file:///c:/rate-limiter/tests/test_rate_limit_integration.py))**:
   - Consecutive requests return `HTTP 200 OK`.
   - `X-RateLimit-Limit` stays fixed.
   - `X-RateLimit-Remaining` accurately decrements ($2 \to 1 \to 0$).
2. **Exceeding Limit & 429 Handling**:
   - Sending requests past the limit immediately triggers `HTTP 429 Too Many Requests`.
   - Injects `Retry-After: <seconds>` indicating the exact cooldown period.
   - Injects `X-RateLimit-Remaining: 0`.
   - Returns structured JSON error payload:
     ```json
     {
       "error": "Too Many Requests",
       "message": "Rate limit exceeded. Try again in 29 seconds.",
       "retry_after": 29,
       "limit": 5,
       "remaining": 0
     }
     ```
3. **Quota Window Recovery**:
   - Verifies that after advancing simulated time past the sliding window, expired requests are purged and quota resets completely back to initial capacity.
4. **Database Fail-Over Tests ([tests/test_rate_limiter.py](file:///c:/rate-limiter/tests/test_rate_limiter.py))**:
   - Tests both `fail_closed=False` (allows request during DB failure) and `fail_closed=True` (rejects request during DB failure).
5. **Route Quota Isolation ([tests/test_sample_routes.py](file:///c:/rate-limiter/tests/test_sample_routes.py))**:
   - Verifies that consuming quota on `POST /api/expensive` (limit 5) does not affect or exhaust `GET /api/data` (limit 60).

---

## API Endpoints & Rate Limit Headers

### Sample Endpoints

| Endpoint | Method | Rate Limit | Description |
| :--- | :--- | :--- | :--- |
| `/health` | `GET` | Bypassed | Service & database connectivity status |
| `/api/data` | `GET` | 60 req / 60s | Standard protected metric data endpoint |
| `/api/expensive` | `POST` | 5 req / 60s | Strict protected heavy computational endpoint |

### Standard Rate Limit Headers

| Header | Description | Example |
| :--- | :--- | :--- |
| `X-RateLimit-Limit` | Total request limit for the active window | `60` |
| `X-RateLimit-Remaining` | Remaining permitted requests in the active window | `45` |
| `X-RateLimit-Reset` | Ceiling seconds until the window resets or oldest request slides out | `18` |
| `Retry-After` | Cooldown wait time in seconds (returned on HTTP 429) | `18` |

### Client Identification Hierarchy

Clients are resolved automatically from incoming request headers:
1. `x-api-key`: API Key authentication (e.g. `apikey:secret_xyz`)
2. `Authorization: Bearer <token>`: Bearer authentication
3. `X-Forwarded-For`: Resolves client IP behind reverse proxies/CDNs
4. `request.client.host`: Direct socket client IP
5. `ip:anonymous`: Default fallback
