# AI Procurement Agent

A WhatsApp-based AI procurement assistant that helps users find vendors, search product inventory, and create RFQs (Request for Quotations) through conversational interactions.

## Overview

This system provides intelligent procurement assistance through WhatsApp, featuring:

- **Intent Classification**: Automatically determines user intent (product search, RFQ creation, general inquiry)
- **Entity Extraction**: Extracts structured information from natural language queries
- **Vendor Matching**: Direct database queries to find suitable vendors based on requirements
- **BFS Inventory Search**: Buy From Stock product searches for immediate availability
- **RFQ Workflow**: Guided RFQ creation with iterative field collection
- **Learning System**: Continuous improvement through Category Manager feedback

## Architecture

```
├── app/
│   ├── api/                    # API endpoints and webhook handlers
│   ├── services/               # Core business logic services
│   ├── tasks/                  # Celery background tasks
│   ├── models.py              # Database models (SQLAlchemy)
│   ├── schemas.py             # API schemas (Pydantic)
│   ├── database.py            # Database connection and session management
│   ├── config.py              # Configuration and environment variables
│   ├── celery_app.py          # Celery application entry point
│   ├── celery_config.py       # Celery configuration and beat scheduler
│   └── main.py                # FastAPI application entry point
├── tests/                     # Unit tests
├── documentation/             # Project documentation
├── celery_manager.py          # Celery management script
├── test_celery_setup.py       # Celery setup testing
└── requirements.txt           # Python dependencies
```

## Key Components

### Services Layer
- **ChatService**: Main orchestration and message routing
- **IntentService**: Intent classification using OpenAI
- **EntityService**: Entity extraction from user messages
- **VendorService**: Vendor search and BFS inventory queries
- **RFQService**: RFQ creation workflow management
- **WhatsAppService**: WhatsApp Business API integration
- **OpenAIService**: OpenAI API wrapper for LLM operations

### Database Models
- **User**: User management and authentication
- **Vendor**: Vendor profiles and learned categorization
- **Product**: BFS inventory management
- **RFQ**: RFQ lifecycle tracking
- **ConversationSession**: Session and context management
- **LearningRecord**: Vendor categorization learning

## Installation

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and configure environment variables
4. Initialize database: `python -c "from app.database import init_database; init_database()"`
5. Start Redis server: `redis-server`
6. Run the application: `uvicorn app.main:app --reload`

### Background Tasks Setup

7. Test Celery setup: `python test_celery_setup.py`
8. Start Celery worker: `python celery_manager.py worker`
9. Start Celery beat scheduler: `python celery_manager.py beat`
10. Start Flower monitoring (optional): `python celery_manager.py flower`

Or start all services at once:
```bash
python celery_manager.py start-production
```

## Background Tasks

The system includes several Celery background tasks:

### Auto-Categorization Task
- **Schedule**: Every 15 minutes
- **Purpose**: Processes uncategorized RFQs and applies AI-based categorization
- **Queue**: `categorization`

### Seller Matching Task
- **Schedule**: Every 30 minutes  
- **Purpose**: Matches categorized RFQs with suitable sellers
- **Queue**: `seller_matching`

### Daily Aggregation Task
- **Schedule**: Daily at 2:00 AM IST
- **Purpose**: Calculates daily metrics and summaries
- **Queue**: `aggregation`

### Rolling Windows Update
- **Schedule**: Daily at 3:00 AM IST
- **Purpose**: Updates 7/30/90 day rolling window metrics
- **Queue**: `aggregation`

## Configuration

See `.env.example` for required environment variables including:
- Database connection settings
- OpenAI API credentials
- WhatsApp Business API configuration
- Redis configuration for session management and Celery broker

## Documentation

Detailed documentation is available in the `documentation/` folder:
- AI_Procurement_Agent_Flow_Design.md - Complete system design
- DatabaseDoc.md - Database schema documentation
- Procurement Agent SRS.md - Software requirements specification

## Celery Management

Use the `celery_manager.py` script for easy Celery operations:

```bash
# Start specific worker
python celery_manager.py worker categorization 2

# Start all workers for production
python celery_manager.py start-production

# Check status
python celery_manager.py status

# Stop all services
python celery_manager.py stop

# Purge pending tasks
python celery_manager.py purge
```

## Development Status

This project structure provides the foundation for the AI Procurement Agent with comprehensive background task processing. All classes and methods are defined with comprehensive block comments explaining their purpose and responsibilities. The Celery integration enables automated processing of RFQs, seller matching, and metrics aggregation.