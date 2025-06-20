# AI Procurement Agent Flow Design

## Core Agent Flow Architecture

### Updated Technical Architecture with Design Decisions

**Main Agent Flow:**

```
WhatsApp Message → Authentication Check → Intent Classification → Workflow Router → Entity Extraction → Direct Database Query → Response Generation
```

### Technical Architecture Decision: Direct Database Query Approach

**Vendor/Product Matching Strategy:** Direct database queries with LLM-extracted structured entities

#### Rationale
- LLM handles semantic understanding during entity extraction phase
- Database handles structured data retrieval efficiently
- Eliminates redundant semantic processing
- Simpler infrastructure stack (FastAPI + PostgreSQL vs FastAPI + PostgreSQL + Vector DB)
- Faster query performance and easier debugging

#### Architecture Flow
```
User Query → Intent Classification → Entity Extraction Loop (LLM) → 
Structured Entities → Direct SQL Queries → Ranked Results
```

#### When Vector DB Would Be Considered
- Direct query-to-vendor matching without entity extraction
- Cross-language semantic search requirements
- Unstructured vendor descriptions requiring semantic similarity
- Future enhancement if semantic search becomes critical

## 1. Authentication Flow

```
Incoming Message
↓
Check User Registration Status (API Call)
├── Registered (Buyer/Vendor/Category Manager confirmed) → Continue to Intent Classification
└── Not Registered → Registration Workflow
    ├── Create registration request
    ├── Send "Team will contact you" message
    └── End conversation
```

## 2. Intent Classification Flow

```
User Message + Conversation History
↓
Intent Classifier (Uses OpenAI or similar model)
├── product_search (80% confidence) → BFS Workflow
├── rfq_creation (75% confidence) → RFQ Workflow
├── general_inquiry (60% confidence) → General Handler
└── ambiguous (<60% confidence) → Clarification Request
```

### Intent Classifier Detailed Explanation

The intent classifier is a routing component that determines which workflow to execute based on the user's message.

#### Core Function
Classifies user messages into predefined categories to route them to the appropriate workflow handler.

#### How It Works

**Input:** User Message: "Do you have industrial motors in Chennai?"

**Processing:**
```
Intent Classifier
↓
Analyzes message for:
- Keywords (motors, price, RFQ, quote)
- Sentence structure
- Context from conversation history
↓
Outputs confidence scores for each intent:
- product_search: 85%
- rfq_creation: 10%
- general_inquiry: 5%
```

**Output & Routing:**
```
Highest confidence intent + threshold check
↓
product_search (85% > 80% threshold) → Route to BFS Workflow
```

#### Intent Categories

1. **product_search** (80% confidence threshold)
   - **Triggers:** "Do you have...", "Looking for...", "Need motors...", "Available in Chennai?"
   - **Routes to:** BFS (Buy From Stock) Workflow

2. **rfq_creation** (75% confidence threshold)
   - **Triggers:** "Create RFQ", "Quote request", "I want to buy...", "Submit requirements"
   - **Routes to:** RFQ Generation Workflow

3. **general_inquiry** (60% confidence threshold)
   - **Triggers:** "How does this work?", "Help", "What can you do?"
   - **Routes to:** General Handler (template responses)

4. **ambiguous** (<60% confidence)
   - **Triggers:** Unclear messages, mixed intents
   - **Routes to:** Clarification Request

The intent classifier acts as a smart router that prevents the system from having to "think" about what the user wants - it just classifies and routes efficiently.

## 3. BFS (Buy From Stock) Workflow

### Step 3.1: Entity Extraction (LLM-Powered)

```
BFS Query: "Do you have industrial motors, less than 3 years old, in Chennai?"
↓
Entity Extractor (LLM with structured prompts)
↓
Extracted Entities:
- product_type: "industrial_motors"
- age_requirement: "< 3 years"
- location: "chennai"
- availability_check: true
- product_category: "electrical_equipment"
```

### Step 3.2: Query Completeness Check

```
Check Required Fields for BFS Search:
├── Complete Query → Proceed to Database Query
└── Incomplete Query → Clarification Sub-flow
    ├── "What type of motor specifications?"
    ├── Wait for user response
    ├── Extract additional entities
    └── Loop until complete
```

### Step 3.3: Direct Database Query

```
Complete Query Entities
↓
Construct SQL Query with Extracted Parameters
↓
Execute Direct Database Queries:
├── BFS Database Search (Buy From Stock)
├── Vendor Database Search
└── Combine and Rank Results
↓
Rank Results by Relevance:
├── Exact matches first
├── Similar products second
└── Alternative suggestions third
```

### Step 3.4: Response Generation

```
Retrieved Results + User Context
↓
Response Generator (GPT-4.1/Claude 4 with structured prompt)
↓
Generate Response Including:
├── Direct stock availability answers
├── Price and location info for available items
├── Vendor recommendations if not in stock
├── Alternative suggestions from stock or vendors
└── Proactive RFQ suggestion if nothing available
```

### Step 3.5: Conversation Continuation

```
Send Response to User
↓
Update Conversation Context
↓
Wait for User Response
├── Follow-up question → Back to Entity Extraction
├── RFQ interest → Transfer to RFQ Workflow
├── New search → Reset and start BFS Workflow
└── End conversation → Session cleanup
```

## 4. RFQ Generation Workflow

### Step 4.1: Context Initialization

```
RFQ Intent Detected
↓
Initialize RFQ State Object:
- required_fields: [item_description, specification, uom, category, quantity, location, age_of_asset, buy_price]
- collected_fields: {}
- current_step: "item_description"
↓
Pre-fill from conversation context (if available)
```

### Step 4.2: Iterative Field Collection

```
Current State: Collecting Field X
↓
Ask User for Field X (contextual prompt)
↓
User Response
↓
Extract & Validate Field Value (LLM-powered)
├── Valid → Store in state, move to next field
├── Invalid → Ask for clarification, retry
└── Partial → Ask follow-up questions
↓
Check if all required fields collected
├── Complete → Proceed to Step 4.3
└── Incomplete → Continue collection loop
```

### Step 4.3: RFQ Validation & Submission

```
All Fields Collected
↓
Final Validation Check
├── Business rule validation
├── Data format validation
└── Completeness check
↓
Generate RFQ Summary for User Confirmation
↓
User Confirms → Submit to Backend API
├── Success → Send confirmation message with RFQ ID
└── Failure → Handle error, offer retry
```

## 5. Vendor Profile Structure & Management

### Vendor Profile Components

```javascript
Vendor Profile:
- vendor_id: Unique identifier
- vendor_name: Company name
- vendor_services: Array of service categories/sectors
- last_order_date: Most recent transaction date
- contact_info: Phone, email, address details
- geographic_coverage: Array of serviceable locations
- performance_metrics: Rating, delivery time, quality scores
- specializations: Specific product/service expertise
- learned_associations: Services learned from CM assignments
```

### Vendor Matching Process

```
Entity Extraction Output:
- product_category: "industrial_motors"
- location: "chennai"
- specifications: {"power": "5HP", "type": "AC"}
↓
Direct Database Query Construction:
- Filter vendors by product_category in vendor_services
- Filter by geographic_coverage containing location
- Order by performance metrics and last_order_date
↓
Return ranked vendor list
```

## 6. Learning Feedback Loop

### Automated Vendor Categorization

**Scenario:** RFQ raised → No suitable vendor in database → Category Manager manually assigns vendor

#### Learning Process:

1. **Trigger:** CM assigns vendor X to RFQ for product/service Y
2. **Data Collection:** Extract RFQ categories/keywords + assigned vendor info
3. **Profile Update:** Add product/service Y to vendor X's service categories
4. **Association Learning:** Create learned_association record linking vendor to product category
5. **Future Matching:** Similar RFQs will now suggest vendor X automatically

#### Learning Data Structure:

```javascript
{
  rfq_id: "Source RFQ identifier",
  vendor_id: "Assigned vendor",
  learned_categories: "Extracted product categories",
  assignment_date: "When learning occurred",
  confidence_score: "Initial confidence (increases with repeated assignments)"
}
```

### Continuous Improvement

#### Pattern Recognition:
- Track CM assignment patterns
- Identify frequently assigned vendor-category pairs
- Boost confidence scores for repeated successful assignments
- Flag unusual assignments for review

## 7. Context Management Flow

### Session Context Structure

```javascript
ConversationContext: {
  user_id: "Unique user identifier",
  session_id: "Current session identifier",
  conversation_history: "Array of message exchanges",
  current_workflow: "Active workflow state (Product/RFQ/General)",
  extracted_entities: "Accumulated entity bank from conversation",
  user_preferences: "Location, language, preferred vendors",
  workflow_state: "Current step in active workflow",
  last_activity: "Timestamp for session timeout"
}
```

### Context Updates

```
Every Message Exchange:
↓
Update conversation history
↓
Update extracted entities bank (merge with existing)
↓
Update workflow state progression
↓
Persist to session storage (Redis/Database)
↓
Set context expiry (30 minutes inactive)
```

### Entity Bank Management

- **Entity Accumulation:** Entities persist across conversation turns
- **Entity Validation:** Cross-validate entities for consistency
- **Entity Prioritization:** Recent extractions override older conflicting data
- **Entity Completion:** Track which required fields are still missing

## 8. Error Handling Strategy

### Error Scenarios & Responses

```
Error Scenarios:
├── LLM Timeout → Fallback to template response, retry with simpler prompt
├── API Failure → Graceful degradation message, offer manual escalation
├── Invalid Input → Clarification request (max 3 attempts), then escalate
├── Database Unavailable → Cached responses, notify of limited functionality
├── Workflow Abandonment → Save state, offer to resume later
├── Context Loss → Restart with apology, attempt to recover key entities
└── Authentication Failure → Re-authentication flow, contact admin if persistent
```

### Fallback Mechanisms

#### LLM Failure Cascades:
1. Retry with simplified prompt
2. Use rule-based entity extraction (regex patterns)
3. Switch to template-based responses
4. Escalate to human agent

#### Database Failure Handling:
1. Serve from cache (Redis)
2. Use read replicas if available
3. Provide degraded functionality message
4. Queue requests for retry when service recovers

## 9. Code Architecture Patterns

### Clean Architecture (Simplified)

#### API Layer: FastAPI routes and controllers
- WhatsApp webhook endpoints
- Health check and monitoring endpoints
- Request/response handling and validation

#### Service Layer: Business logic and workflow orchestration
- Chat service (main orchestrator)
- Intent classification service
- Entity extraction service
- Vendor matching service
- RFQ workflow service
- WhatsApp messaging service

#### Repository Layer: Data access and external API management
- User data operations
- Vendor data operations
- RFQ data operations
- Session management
- External API wrappers (OpenAI, WhatsApp, Procurement APIs)

#### Infrastructure Layer: Database, caching, and external services
- PostgreSQL database connections
- Redis caching layer
- OpenAI API client
- WhatsApp provider integration

### Key Design Patterns

- **Repository Pattern:** Abstract data access for easy testing and swapping
- **Dependency Injection:** Loose coupling between layers, configurable components
- **Strategy Pattern:** Different intent handlers, swappable AI models, multiple WhatsApp providers
- **Factory Pattern:** Create workflow instances based on intent classification
- **Observer Pattern:** Event-driven learning from CM assignments

## 10. Technical Assumptions & Constraints

### Key Technical Assumptions

- **Entity Extraction Accuracy:** LLM can reliably extract structured entities (90%+ accuracy expected)
- **Category Standardization:** Product categories can be normalized to controlled vocabulary
- **Vendor Data Quality:** Vendor service categories are maintained and regularly updated
- **Query Performance:** Direct SQL queries will handle expected load (<100ms response time)
- **Session Management:** 30-minute inactive session timeout is acceptable for user experience
- **Learning Effectiveness:** CM assignment patterns provide sufficient signal for vendor categorization

### Performance Constraints

- **Response Time:** <3 seconds for simple queries, <10 seconds for complex RFQ flows
- **Concurrent Users:** Support 100+ simultaneous conversations
- **Database Load:** Optimize for read-heavy workload (vendor searches)
- **Memory Usage:** Session state kept under 1MB per active conversation
- **API Rate Limits:** OpenAI API within tier limits, WhatsApp provider message limits

### Integration Constraints

- **WhatsApp Limitations:** Message size limits, media handling restrictions
- **Client API Dependencies:** Availability and response time of procurement APIs
- **Database Schema:** Must work with existing client database structure
- **Security Requirements:** Compliance with client security standards
- **Deployment Environment:** Containerized deployment on client infrastructure

## 11. Conversation Boundaries & Session Management

### Session Management Rules

```
Session Lifecycle:
├── Max conversation length: 50 exchanges per session
├── Timeout: 30 minutes inactive
├── Context reset: After successful RFQ submission
├── Memory cleanup: Remove expired sessions every hour
└── Escalation: After 3 consecutive failed attempts
```

#### Session State Persistence:
- **Active sessions:** In-memory (Redis) for fast access
- **Completed sessions:** Database for analytics and learning
- **Failed sessions:** Logged for debugging and improvement

### Conversation Flow Control

#### Turn Management:
- Maximum 3 clarification requests per entity
- Automatic workflow switching based on user intent changes
- Graceful handling of topic changes mid-conversation
- Proactive workflow completion suggestions

#### User Experience Boundaries:
- Acknowledge receipt within 1 second
- Provide progress indicators for multi-step flows
- Offer escape options ("type STOP to end conversation")
- Clear restart instructions when sessions expire

## Complete Updated Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     WHATSAPP MESSAGE                            │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                ┌─────────▼─────────┐
                │   AUTHENTICATION  │
                │      CHECK        │
                └─────────┬─────────┘
                          │
                ┌─────────▼─────────┐
                │     INTENT        │
                │  CLASSIFICATION   │
                └─────────┬─────────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
┌───────▼──────┐ ┌────────▼────────┐ ┌──────▼─────┐
│   PRODUCT    │ │      RFQ        │ │  GENERAL   │
│   WORKFLOW   │ │   WORKFLOW      │ │  HANDLER   │
│              │ │                 │ │            │
│ 1.Entity     │ │ 1.Context Init  │ │ Template   │
│   Extract    │ │ 2.Field Loop    │ │ Response   │
│ 2.DB Query   │ │ 3.Validation    │ │            │
│ 3.Response   │ │ 4.Submission    │ │            │
│   Generate   │ │ 5.Learning      │ │            │
└──────────────┘ └─────────────────┘ └────────────┘
                          │
┌─────────────────────────▼─────────────────────────┐
│              LEARNING FEEDBACK LOOP               │
│          (CM Assignment → Vendor Categorization)  │
└───────────────────────────────────────────────────┘
```

## 12. Code Architecture & Design Patterns

### Basic Architecture Pattern

```
┌─────────────────────────────────────────────────────────────┐
│                    API Layer (FastAPI)                     │
│                   Routes & Endpoints                       │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                 Services Layer                             │
│              Core Business Logic                           │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│              Models & Database                             │
│            SQLAlchemy Models & DB Access                   │
└─────────────────────────────────────────────────────────────┘
```

### Simple Design Approach

#### Service-Based Architecture
- Each service handles one main responsibility
- Services can call other services directly
- Database access through SQLAlchemy models
- External API calls within service methods

#### Key Services
- **ChatService:** Main orchestration and message routing
- **IntentService:** Classify user intent using OpenAI
- **EntityService:** Extract entities from user messages
- **VendorService:** Search and match vendors from database
- **RFQService:** Handle RFQ creation workflow
- **WhatsAppService:** Send/receive WhatsApp messages
- **OpenAIService:** Wrapper for OpenAI API calls

## 13. Simplified Project Structure

```
procurement_bot/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app initialization
│   ├── config.py                   # Configuration and environment variables
│   │
│   ├── api/                        # API endpoints
│   │   ├── __init__.py
│   │   └── webhook.py              # WhatsApp webhook endpoint
│   │
│   ├── services/                   # Core business logic
│   │   ├── __init__.py
│   │   ├── chat_service.py         # Main chat orchestration
│   │   ├── intent_service.py       # Intent classification
│   │   ├── entity_service.py       # Entity extraction
│   │   ├── vendor_service.py       # Vendor matching & search
│   │   ├── rfq_service.py          # RFQ creation workflow
│   │   ├── whatsapp_service.py     # WhatsApp API integration
│   │   └── openai_service.py       # OpenAI API integration
│   │
│   ├── models.py                   # Database models (SQLAlchemy)
│   ├── schemas.py                  # API schemas (Pydantic)
│   └── database.py                 # Database connection setup
│
├── tests/
│   ├── __init__.py
│   └── test_services.py            # Basic service tests
│
├── requirements.txt                # Project dependencies
├── .env.example                   # Environment variables template
├── .gitignore
└── README.md
```