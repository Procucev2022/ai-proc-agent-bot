# Seller Matching & Filtering Documentation

## Overview
The seller matching system automatically finds and selects qualified sellers for RFQs using a multi-stage filtering process and vector-based matching.

## System Components & Scripts

### 1. Enhanced Seller Matching Service
**File**: `app/services/enhanced_seller_matching_service.py`
- **Purpose**: Primary vector-based seller discovery using 3-level taxonomy
- **Method**: Semantic search through ChromaDB vector store with embedded seller categories
- **Key Methods**:
  - `find_sellers_for_item()` - Main vector search
  - `find_sellers_by_category_path()` - Exact category matching
  - `get_seller_categories()` - Category lookup

### 2. Seller Recommendation Service
**File**: `app/services/seller_recommendation_service.py`
- **Purpose**: Business logic implementation for seller selection
- **Method**: Multi-stage filtering pipeline with configurable rules
- **Key Method**: `select_sellers_for_rfq()` - Main orchestration

### 3. Seller Matching Background Task
**File**: `app/tasks/seller_matching_task.py`
- **Purpose**: Celery background processing for RFQ seller matching
- **Key Functions**:
  - `process_seller_matching()` - Main Celery task
  - `get_rfqs_needing_seller_matching()` - RFQ discovery
  - `process_single_rfq_matching()` - Individual RFQ processing

### 4. Supporting Services
- **Location Service**: `app/services/location_service.py` - Geographic calculations
- **OpenAI Service**: `app/services/openai_service.py` - AI-powered selection
- **Authentication Service**: `app/services/authentication_service.py` - Seller verification

## Seller Matching Process & Components

### Stage 1: Category Matching
**Component**: `SellerRecommendationService._filter_sellers_by_category()`
- Matches RFQ categories against seller service categories
- Uses JSON contains/overlaps for database lookup
- Supports multiple categories per RFQ
- **Database**: Queries `Seller` table with JSON category fields

### Stage 2: Geographic Filtering
**Component**: `SellerRecommendationService._filter_sellers_by_location()`
**Helper**: `LocationService.calculate_distance()`
- Filters sellers within delivery radius (default 200km)
- Uses Haversine formula for distance calculation
- Respects individual seller coverage areas
- Handles both lat/lng and pincode-based locations

### Stage 3: Subscription & Activity Filtering
**Components**:
- `_filter_inactive_sellers()` - Activity check for unsubscribed
- `_filter_by_message_history()` - Rate limiting check

**Subscribed Sellers** (max 10):
- Must have subscription credits > 0
- No recent message within 24 hours

**Unsubscribed Sellers** (max 25):
- Must be inactive for 24+ hours
- No recent message within 24 hours
- Subject to cyclic selection for fairness

### Stage 4: Ranking & Selection
**Component**: `SellerRecommendationService._rank_sellers_by_criteria()`
- **Primary**: Seller ranking (Diamond → Platinum → Gold → Titanium)
- **Secondary**: Distance from delivery location
- **Tertiary**: Last notification time (for fairness)

### Stage 5: AI-Enhanced Selection
**Component**: `OpenAIService.select_best_sellers()`
**Integration**: `EnhancedSellerMatchingService.find_sellers_for_item()`
- OpenAI evaluates top candidates for best matches
- Considers item description context
- Provides reasoning for selections
- Falls back to vector scoring if AI fails

### Stage 6: Cyclic Selection
**Component**: `SellerRecommendationService._apply_cyclic_selection()`
- Implements fair distribution for unsubscribed sellers
- Tracks last notification times
- Prioritizes least recently contacted sellers

## Vector Store Setup Scripts

### Database Initialization
**File**: `Setup/3-level_vector_store/create_category_embeddings.py`
- Creates ChromaDB collections
- Generates category embeddings
- Sets up 3-level taxonomy structure

### Seller Mapping
**File**: `Setup/3-level_vector_store/map_sellers_to_categories.py`
- Maps sellers to learning taxonomy categories
- Creates seller-category embeddings
- Populates vector store with seller data

## Configuration Files

### System Configuration
**Database Table**: `SystemConfiguration`
- Stores runtime parameters
- Configurable limits and thresholds

### Default Settings
**File**: `app/config.py`
- Application-wide settings
- Database connections
- Feature flags

## Data Flow & Components

1. **Celery Beat Scheduler** → Triggers `process_seller_matching` task
2. **Seller Matching Task** → Identifies categorized RFQs needing matching
3. **Seller Recommendation Service** → Applies business rules and filters
4. **Enhanced Matching Service** → Performs vector search
5. **OpenAI Service** → AI-powered intelligent selection
6. **Location Service** → Geographic calculations
7. **Database Updates** → Results stored in vendor notification tables

## Key Configuration Parameters

| Parameter | Default | Component | Description |
|-----------|---------|-----------|-------------|
| `MAX_SUBSCRIBED_SELLERS_PER_RFQ` | 10 | SellerRecommendationService | Max subscribed sellers |
| `MAX_UNSUBSCRIBED_SELLERS_PER_RFQ` | 25 | SellerRecommendationService | Max unsubscribed sellers |
| `MAX_TIME_SINCE_LAST_MESSAGE_HOURS` | 24 | Message filtering | Rate limiting window |
| `GEO_DISTANCE_RADIUS_KM` | 200 | LocationService | Geographic search radius |

## Database Tables Used

- **`Seller`** - Seller master data and categories
- **`SellerRanking`** - Seller tier information
- **`RFQSellerNotification`** - Notification history tracking
- **`SystemConfiguration`** - Runtime configuration
- **`SellerSubscription`** - Subscription and credits
- **`rfq_header`** - RFQ master data (remote)
- **`gmt_rfq_vendors`** - Vendor notification log (remote)

## Usage Example

```python
# Background task processing
from app.tasks.seller_matching_task import process_seller_matching
result = process_seller_matching.delay()

# Direct service usage
from app.services.seller_recommendation_service import SellerRecommendationService
service = SellerRecommendationService()
result = await service.select_sellers_for_rfq(rfq_data)

# Enhanced vector search
from app.services.enhanced_seller_matching_service import EnhancedSellerMatchingService
enhanced = EnhancedSellerMatchingService()
sellers = enhanced.find_sellers_for_item("Gaming laptop", delivery_location)
```