#!/bin/bash

# Fixed MySQL Database Setup Script for AI Procurement Agent
# This handles Ubuntu MySQL auth_socket authentication

set -e  # Exit on any error

echo "Starting MySQL Database Setup for AI Procurement Agent (Fixed Version)"
echo "================================================================="

# Configuration
DB_NAME="procurement_db"
DB_USER="procurement_user"
DB_PASSWORD="procubot2025"
DB_HOST="localhost"
DB_PORT="3306"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if .env file exists
echo "Step 1: Checking .env file..."
if [ ! -f ".env" ]; then
    print_error ".env file not found. Please create it with required configuration."
    exit 1
fi
print_status ".env file found"

# Check if MySQL is installed
echo "Step 2: Checking MySQL installation..."
if ! command -v mysql &> /dev/null; then
    print_error "MySQL is not installed. Please install it first:"
    echo "  Ubuntu/Debian: sudo apt update && sudo apt install mysql-server mysql-client"
    exit 1
fi
print_status "MySQL is installed"

# Check if MySQL service is running
echo "Step 3: Checking MySQL service..."
if ! systemctl is-active --quiet mysql; then
    print_warning "MySQL service is not running. Please start it with: sudo systemctl start mysql"
    exit 1
fi
print_status "MySQL service is running"

# Fix MySQL authentication and create database/user
echo "Step 4: Setting up database and user (using sudo for MySQL root)..."

# Use sudo to access MySQL as root (bypasses auth_socket issue)
sudo mysql << EOF
-- Drop database if exists (for fresh setup)
DROP DATABASE IF EXISTS $DB_NAME;
DROP USER IF EXISTS '$DB_USER'@'localhost';

-- Create database
CREATE DATABASE $DB_NAME;

-- Create user with password
CREATE USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASSWORD';

-- Grant privileges
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';

-- Flush privileges
FLUSH PRIVILEGES;

-- Show what we created
SELECT 'Database created:' as info, '$DB_NAME' as name;
SELECT 'User created:' as info, '$DB_USER' as name;
EOF

print_status "Database '$DB_NAME' and user '$DB_USER' created successfully"

# Test connection
echo "Step 5: Testing database connection..."
if mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD -e "SELECT 1;" > /dev/null 2>&1; then
    print_status "Database connection successful"
else
    print_error "Failed to connect to database"
    exit 1
fi

# Install Python dependencies
echo "Step 6: Installing Python dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    print_status "Python dependencies installed"
else
    print_warning "requirements.txt not found. Make sure to install dependencies manually."
fi

# Initialize database schema and sample data
echo "Step 7: Initializing database schema and sample data..."
unset DATABASE_URL  # Make sure we use .env file
python -c "
from app.database import init_database
try:
    init_database()
    print('[SUCCESS] Database schema and sample data initialized')
except Exception as e:
    print(f'[ERROR] Error initializing database: {e}')
    import traceback
    traceback.print_exc()
    exit(1)
"

# Step 8: Add new columns and tables for enhanced analytics
echo "Step 8: Adding enhanced analytics columns and tables..."

# Function to safely add columns (ignores duplicate column errors)
echo "Adding new columns to conversation_sessions..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
-- Add new columns to conversation_sessions table (ignore errors for existing columns)
ALTER TABLE conversation_sessions ADD COLUMN user_type ENUM('buyer', 'seller', 'unknown') DEFAULT 'unknown';
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN session_state ENUM('active', 'inactive', 'completed', 'abandoned') DEFAULT 'active';
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN rfq_ids JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN rfq_metadata JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN product_items JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN seller_responses JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN interaction_metrics JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN parent_session_id VARCHAR(255);
EOF

echo "Adding BFS tracking columns..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bfs_products_searched JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bfs_search_count INT DEFAULT 0;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bfs_price_accepted JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bfs_counter_offers JSON;
EOF

echo "Adding bidding lifecycle columns..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN products_bid_for JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bids_received JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN bids_accepted JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN counter_offers_made JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN counter_offers_accepted JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN rfqs_with_response JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN avg_products_per_rfq DECIMAL(5,2);
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE conversation_sessions ADD COLUMN avg_categories_per_rfq DECIMAL(5,2);
EOF

echo "Adding columns to daily_summaries (if table exists)..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN user_type ENUM('buyer', 'seller', 'unknown');
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN session_states_breakdown JSON;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN seller_interaction_count INT DEFAULT 0;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN avg_session_duration INT;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN product_items_count INT DEFAULT 0;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN unique_categories_count INT DEFAULT 0;
EOF

mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF' || true
ALTER TABLE daily_summaries ADD COLUMN session_continuation_count INT DEFAULT 0;
EOF

echo "Creating new analytics tables..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << 'EOF'
-- Create new analytics tables
CREATE TABLE IF NOT EXISTS session_events (
    event_id CHAR(36) PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_data JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_id (session_id),
    INDEX idx_user_id (user_id),
    INDEX idx_event_type (event_type),
    INDEX idx_event_timestamp (event_timestamp)
);

CREATE TABLE IF NOT EXISTS daily_aggregated_metrics (
    metric_id CHAR(36) PRIMARY KEY,
    date DATE NOT NULL,
    metric_type VARCHAR(50) NOT NULL,
    metric_data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_complete BOOLEAN DEFAULT FALSE,
    UNIQUE KEY unique_daily_metric (date, metric_type),
    INDEX idx_date (date),
    INDEX idx_metric_type (metric_type)
);

CREATE TABLE IF NOT EXISTS rolling_window_metrics (
    window_id CHAR(36) PRIMARY KEY,
    window_type ENUM('7day', '30day', '90day') NOT NULL,
    end_date DATE NOT NULL,
    buyer_metrics JSON,
    seller_metrics JSON,
    category_metrics JSON,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_window_metric (window_type, end_date),
    INDEX idx_window_type (window_type),
    INDEX idx_end_date (end_date)
);

SELECT 'Enhanced analytics schema applied successfully' as status;
EOF

if [ $? -eq 0 ]; then
    print_status "Enhanced analytics columns and tables added"
else
    print_error "Failed to add enhanced analytics schema"
    exit 1
fi

# Verify setup
echo "Step 9: Verifying setup..."
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME << EOF
SELECT 'Database Tables:' as info;
SHOW TABLES;

SELECT 'Sample Data Verification:' as info;
SELECT 'Vendors' as table_name, COUNT(*) as count FROM vendors
UNION ALL
SELECT 'Categories', COUNT(*) FROM product_categories
UNION ALL
SELECT 'Sessions', COUNT(*) FROM conversation_sessions
UNION ALL
SELECT 'RFQ Records', COUNT(*) FROM rfq_records
UNION ALL
SELECT 'Associations', COUNT(*) FROM learned_associations
UNION ALL
SELECT 'System Logs', COUNT(*) FROM system_logs
UNION ALL
SELECT 'Session Events', COUNT(*) FROM session_events
UNION ALL
SELECT 'Daily Metrics', COUNT(*) FROM daily_aggregated_metrics
UNION ALL
SELECT 'Window Metrics', COUNT(*) FROM rolling_window_metrics;

SELECT 'Vendor Sample:' as info;
SELECT vendor_name, JSON_LENGTH(vendor_services) as service_count FROM vendors LIMIT 3;

SELECT 'Category Sample:' as info;
SELECT category_name FROM product_categories LIMIT 5;
EOF

print_status "Setup verification completed"

# Create backup script
echo "Step 10: Creating backup script..."
cat > backup_database.sh << 'EOF'
#!/bin/bash
# Database backup script
DATE=$(date +%Y%m%d_%H%M%S)
DB_NAME="procurement_db"
DB_USER="procurement_user"
DB_PASSWORD="procubot2025"

echo "Creating backup..."
mysqldump -h localhost -u $DB_USER -p$DB_PASSWORD $DB_NAME > "backup_${DATE}.sql"
echo "Backup created: backup_${DATE}.sql"
EOF
chmod +x backup_database.sh
print_status "Backup script created (backup_database.sh)"

# Create test script
echo "Step 11: Creating test script..."
cat > test_database.sh << 'EOF'
#!/bin/bash
# Database test script
DB_NAME="procurement_db"
DB_USER="procurement_user"
DB_PASSWORD="procubot2025"

echo "Testing database connection and data..."
mysql -h localhost -u $DB_USER -p$DB_PASSWORD $DB_NAME -e "
SELECT 
    'Connection' as test, 
    'PASSED' as status, 
    NOW() as timestamp
UNION ALL
SELECT 
    'Vendors Count', 
    CASE WHEN COUNT(*) > 0 THEN 'PASSED' ELSE 'FAILED' END,
    NOW()
FROM vendors
UNION ALL
SELECT 
    'Categories Count', 
    CASE WHEN COUNT(*) > 0 THEN 'PASSED' ELSE 'FAILED' END,
    NOW()
FROM product_categories;
"
EOF
chmod +x test_database.sh
print_status "Test script created (test_database.sh)"

echo ""
echo "MySQL Database Setup Complete!"
echo "======================================"
echo ""
echo "What was created:"
echo "  - Database: $DB_NAME"
echo "  - User: $DB_USER"
echo "  - Password: $DB_PASSWORD"
echo "  - 10 tables with sample data and enhanced analytics"
echo "  - backup_database.sh script"
echo "  - test_database.sh script"
echo ""
echo "To start the application:"
echo "  unset DATABASE_URL"  # Use .env file
echo "  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo ""
echo "To test database:"
echo "  ./test_database.sh"
echo ""
echo "To backup database:"
echo "  ./backup_database.sh"
echo ""
echo "🎉 Migration from PostgreSQL to MySQL completed successfully!"
echo "Key changes applied:"
echo "  - Port changed from 5432 to 3306"
echo "  - Driver changed to PyMySQL"
echo "  - UUID fields converted to CHAR(36)"
echo "  - JSONB fields converted to JSON"
echo "  - Array fields converted to JSON arrays"
echo ""
echo "The application is now ready to run on MySQL!"