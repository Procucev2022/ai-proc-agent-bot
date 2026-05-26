# AI Procurement Agent - Database Design Document

## Architecture Overview

The system uses a **2-database architecture** with external service integrations:

```
┌─────────────────────────────────────────────────────────────┐
│                    DATABASE ARCHITECTURE                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────┐  │
│  │     Redis       │  │   PostgreSQL    │  │   External  │  │
│  │   (Database 1)  │  │  (Database 2)   │  │ Integrations│  │
│  │                 │  │                 │  │             │  │
│  │ - Active        │  │ - vendors       │  │ - Auth API  │  │
│  │   Sessions      │  │ - rfq_records   │  │ - BFS API   │  │
│  │ - Cache         │  │ - categories    │  │ - RFQ API   │  │
│  │ - Workflow      │  │ - associations  │  │             │  │
│  │   State         │  │ - outcomes      │  │             │  │
│  └─────────────────┘  └─────────────────┘  └─────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Database 1: PostgreSQL (Main Application Database)

### Purpose
Core business data including vendor management, RFQ processing, product categories, learning associations, and conversation tracking.

### Tables

#### 1. vendors

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| vendor_id | UUID | PRIMARY KEY | Unique vendor identifier |
| vendor_name | VARCHAR(255) | NOT NULL | Company name |
| geographic_coverage | TEXT[] | NOT NULL | Serviceable locations array |
| vendor_services | TEXT[] | NOT NULL | Product/service categories array |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Record creation time |

#### 2. product_categories

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| category_id | UUID | PRIMARY KEY | Unique category identifier |
| category_name | VARCHAR(255) | UNIQUE NOT NULL | Standardized category name |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Record creation time |

#### 3. rfq_records

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| rfq_id | UUID | PRIMARY KEY | Unique RFQ identifier |
| external_user_id | VARCHAR(255) | NOT NULL | Reference to external auth system |
| api_payload | JSONB | NOT NULL | Data for external RFQ API submission |
| status | rfq_status_enum | DEFAULT 'collecting' | Current RFQ state |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Creation time |
| submitted_at | TIMESTAMP | | When sent to external API |

**Enum Types:**
```sql
CREATE TYPE rfq_status_enum AS ENUM ('collecting', 'ready', 'submitted', 'failed');
```

#### 4. learned_associations

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| association_id | UUID | PRIMARY KEY | Unique learning record |
| vendor_id | UUID | FOREIGN KEY NOT NULL | References vendors(vendor_id) |
| category | VARCHAR(255) | NOT NULL | Product/service category |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | When association was learned |

#### 5. conversation_outcomes

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| session_id | VARCHAR(255) | PRIMARY KEY | Unique session identifier |
| external_user_id | VARCHAR(255) | NOT NULL | Reference to external auth system |
| workflow_type | workflow_type_enum | | Type of conversation |
| outcome | outcome_enum | | How conversation ended |
| rfq_id | UUID | FOREIGN KEY | References rfq_records(rfq_id) |
| extracted_entities | JSONB | | Key LLM extractions |
| conversation_messages | JSONB | | Full message history |
| retention_date | DATE | NOT NULL | When to delete this record |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Conversation timestamp |

**Enum Types:**
```sql
CREATE TYPE workflow_type_enum AS ENUM ('product_search', 'rfq_creation', 'general_inquiry');
CREATE TYPE outcome_enum AS ENUM ('completed', 'abandoned', 'escalated', 'timeout');
```

### Data Retention Policy

#### Conversation History Retention

| Scenario | Retention Period | Logic |
|----------|------------------|-------|
| **RFQ Completed** | 7 days after completion | Short retention for completed business |
| **No RFQ Created** | 30 days from conversation | Standard retention for incomplete sessions |
| **Abandoned Conversations** | 30 days from conversation | Standard retention for failed sessions |

#### Retention Implementation

**Retention Date Calculation:**
```sql
-- For completed RFQs
UPDATE conversation_outcomes 
SET retention_date = (
    SELECT submitted_at + INTERVAL '7 days'
    FROM rfq_records 
    WHERE rfq_records.rfq_id = conversation_outcomes.rfq_id
    AND status = 'submitted'
)
WHERE outcome = 'completed' AND rfq_id IS NOT NULL;

-- For all other conversations
UPDATE conversation_outcomes 
SET retention_date = created_at + INTERVAL '30 days'
WHERE retention_date IS NULL;
```

**Daily Cleanup Process:**
```sql
-- Delete expired conversation messages (keep metadata)
UPDATE conversation_outcomes 
SET conversation_messages = NULL
WHERE retention_date < CURRENT_DATE;
```

### Relationships

| Relationship | Type | Description |
|--------------|------|-------------|
| vendors → learned_associations | 1:N | One vendor can have multiple learned categories |
| rfq_records → conversation_outcomes | 1:1 | Each conversation may result in one RFQ |

### Indexes

| Table | Index | Type | Purpose |
|-------|-------|------|---------|
| vendors | vendor_services | GIN | Fast array category matching |
| vendors | geographic_coverage | GIN | Fast location filtering |
| rfq_records | external_user_id | B-tree | User RFQ lookups |
| rfq_records | status | B-tree | Status-based queries |
| learned_associations | vendor_id | B-tree | Vendor category lookups |
| learned_associations | category | B-tree | Category-based queries |
| conversation_outcomes | external_user_id | B-tree | User analytics |
| conversation_outcomes | workflow_type | B-tree | Workflow analytics |
| conversation_outcomes | retention_date | B-tree | Efficient cleanup queries |

## Database 2: Redis (Session Management)

### Purpose
High-performance temporary data storage for active user sessions, caching, and real-time state management.

### Data Structures

#### Session Context
- **Key Pattern**: `session:{session_id}:context`
- **TTL**: 1800 seconds (30 minutes)
- **Data Type**: JSON string
- **Content**: User info, workflow state, conversation history, extracted entities

#### Entity Cache
- **Key Pattern**: `entity_cache:{query_hash}`
- **TTL**: 3600 seconds (1 hour)
- **Data Type**: JSON array
- **Content**: Cached LLM entity extractions

#### Rate Limiting
- **Key Pattern**: `rate_limit:{phone_number}:{minute}`
- **TTL**: 60 seconds (1 minute)
- **Data Type**: Integer counter
- **Content**: Message count per minute

#### Workflow State
- **Key Pattern**: `workflow:{session_id}:state`
- **TTL**: 1800 seconds (30 minutes)
- **Data Type**: JSON string
- **Content**: Current step, collected fields, progress tracking

### Configuration

| Setting | Value | Purpose |
|---------|-------|---------|
| maxmemory | 2GB | Memory limit |
| maxmemory-policy | allkeys-lru | Eviction strategy |
| timeout | 300 | Connection timeout |
| save | 900 1 | Persistence settings |

## External Service Integrations

### Purpose
Access external systems for authentication and inventory data without data duplication.

### API Endpoints

#### Authentication API
- **Endpoint**: `TBD`
- **Method**: POST
- **Purpose**: User verification and role management
- **Response**: User ID, role, status, company information

#### Buy From Stock (BFS) API
- **Endpoint**: `TBD`
- **Method**: POST
- **Purpose**: Product inventory searches
- **Response**: Available products, pricing, stock levels, locations

#### RFQ Submission API
- **Endpoint**: `TBD`
- **Method**: POST
- **Purpose**: Submit completed RFQs to client system
- **Response**: RFQ confirmation, tracking ID

### Integration Strategy

| API | Usage Pattern | Error Handling |
|-----|---------------|----------------|
| **Auth API** | Called per new session | Fallback to cached user data |
| **BFS API** | Called during product searches | Graceful degradation, vendor-only results |
| **RFQ API** | Called after RFQ completion | Retry mechanism, local backup |

## Performance Specifications

### PostgreSQL Performance Targets

| Operation | Target Time | Optimization Strategy |
|-----------|-------------|----------------------|
| Vendor category matching | < 200ms | GIN indexes on arrays |
| RFQ creation | < 100ms | Optimized schema, minimal joins |
| Learning data insertion | < 50ms | Batch operations, async processing |
| Analytics queries | < 500ms | Proper indexing, query optimization |

**Conversation History Storage:**
- **Average conversation**: ~5KB message data
- **Monthly conversations**: ~10K estimated
- **Monthly message storage**: ~50MB
- **Steady state**: ~100-150MB (with retention policy)

## Backup and Recovery

### PostgreSQL
- **Backup Frequency**: Daily full backup, hourly incremental
- **Retention**: 30 days full backups, 7 days incremental
- **Recovery Time Objective**: < 4 hours
- **Recovery Point Objective**: < 1 hour

### Redis
- **Backup Strategy**: Snapshot every 15 minutes if changes
- **Persistence**: RDB + AOF hybrid
- **Recovery**: Automatic restart from latest snapshot
- **Data Loss Tolerance**: < 15 minutes (session data only)

## Security Considerations

### Data Classification

| Data Type | Classification | Encryption |
|-----------|---------------|------------|
| **Vendor Information** | Business Confidential | At rest + in transit |
| **RFQ Staging Data** | Business Confidential | At rest + in transit |
| **User Sessions** | Internal Use | In transit only |
| **Learning Data** | Internal Use | At rest |

This database design provides a scalable foundation for the AI procurement agent with optimized query patterns, proper indexing strategies, clear data separation, and automated data lifecycle management for privacy compliance.