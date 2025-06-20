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
│   ├── models.py              # Database models (SQLAlchemy)
│   ├── schemas.py             # API schemas (Pydantic)
│   ├── database.py            # Database connection and session management
│   ├── config.py              # Configuration and environment variables
│   └── main.py                # FastAPI application entry point
├── tests/                     # Unit tests
├── documentation/             # Project documentation
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
5. Run the application: `uvicorn app.main:app --reload`

## Configuration

See `.env.example` for required environment variables including:
- Database connection settings
- OpenAI API credentials
- WhatsApp Business API configuration
- Redis configuration for session management

## Documentation

Detailed documentation is available in the `documentation/` folder:
- AI_Procurement_Agent_Flow_Design.md - Complete system design
- DatabaseDoc.md - Database schema documentation
- Procurement Agent SRS.md - Software requirements specification

## Development Status

This project structure provides the foundation for the AI Procurement Agent. All classes and methods are defined with comprehensive block comments explaining their purpose and responsibilities. Implementation of the actual functionality is ready to begin based on this architectural foundation.