# MySQL Database Setup Guide - AI Procurement Agent

## Overview

This guide provides comprehensive instructions for setting up the MySQL database for the AI Procurement Agent application. The database stores vendor information, conversation sessions, RFQ records, and learning data.

## Prerequisites

- MySQL 8.0+ installed
- Python 3.8+ environment
- Root/sudo access for database creation
- Required API keys configured

## MySQL Installation

### Ubuntu/Debian
```bash
sudo apt update
sudo apt install mysql-server mysql-client
sudo systemctl start mysql
sudo systemctl enable mysql
```

### CentOS/RHEL
```bash
sudo yum install mysql-server mysql
sudo systemctl start mysqld
sudo systemctl enable mysqld
```

### macOS
```bash
brew install mysql
brew services start mysql
```

## MySQL Service Management

### Start/Stop/Restart Commands
```bash
# Start MySQL service
sudo systemctl start mysql

# Stop MySQL service
sudo systemctl stop mysql

# Restart MySQL service
sudo systemctl restart mysql

# Enable MySQL to start on boot
sudo systemctl enable mysql

# Check MySQL service status
sudo systemctl status mysql

# Check if MySQL is running
ps aux | grep mysql
```

### macOS Service Management
```bash
# Start MySQL
brew services start mysql

# Stop MySQL
brew services stop mysql

# Restart MySQL
brew services restart mysql

# Check status
brew services list | grep mysql
```

## Database Structure

The application uses **10 tables** with **MySQL enum types**:

### Core Tables:
1. **vendors** - Vendor profiles and capabilities (5 sample records)
2. **product_categories** - Standardized product categories (8 sample records)  
3. **conversation_sessions** - User conversation context with enhanced tracking
4. **rfq_records** - RFQ lifecycle and status
5. **conversation_outcomes** - Conversation completion tracking
6. **learned_associations** - ML learning records
7. **system_logs** - Application logs

### Analytics & Aggregation Tables:
8. **session_events** - Event tracking for user interactions
9. **daily_aggregated_metrics** - Daily metrics aggregation
10. **rolling_window_metrics** - Rolling window analytics (7/30/90 day)

### Database Configuration:
- **Database Name:** `procurement_db`
- **User:** `procurement_user`
- **Password:** `procubot2025`
- **Host:** `localhost`
- **Port:** `3306`

## Automated Setup (Recommended)

Use the provided setup script for complete automation:

```bash
# Make script executable
chmod +x setup_mysql_fixed.sh

# Run setup script
./setup_mysql_fixed.sh
```

**What the script does:**
1. Checks for existing .env file
2. Verifies MySQL installation and service
3. Creates database and user
4. Tests database connection
5. Installs Python dependencies
6. Initializes database schema and sample data
7. **Adds enhanced analytics columns and tables**
8. Verifies setup with sample queries
9. Creates backup and test scripts

## Environment Configuration

Ensure your `.env` file contains:

```env
# Database Configuration
DATABASE_URL=mysql+pymysql://procurement_user:procubot2025@localhost:3306/procurement_db

# API Keys (update with actual values)
AZURE_OPENAI_API_KEY=your_azure_openai_key
AZURE_OPENAI_ENDPOINT=your_azure_endpoint
GMT_CLIENT_ID=your_gmt_client_id
GMT_CLIENT_SECRET=your_gmt_client_secret
GMT_USERNAME=your_gmt_username
GMT_PASSWORD=your_gmt_password

# IP Restrictions for Test VM
ALLOWED_IPS=192.168.1.100,203.0.113.45,10.0.0.50

# Optional Settings
DEBUG=true
SESSION_TIMEOUT_HOURS=12
LOG_LEVEL=INFO
```

## Database Schema Details

### Enum Types in MySQL

MySQL enum types are defined within table columns:

```sql
-- RFQ Status: ENUM('collecting', 'ready', 'submitted', 'failed')
-- Workflow Types: ENUM('product_search', 'rfq_creation', 'general_inquiry', 'rfq_submitted', 'excel_rfq_upload')
-- Conversation Outcomes: ENUM('completed', 'abandoned', 'escalated', 'timeout')
```

### Key Tables Schema

#### vendors
```sql
CREATE TABLE vendors (
    vendor_id CHAR(36) PRIMARY KEY,
    vendor_name VARCHAR(255) NOT NULL,
    geographic_coverage JSON NOT NULL,
    vendor_services JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### product_categories
```sql
CREATE TABLE product_categories (
    category_id CHAR(36) PRIMARY KEY,
    category_name VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### conversation_sessions (Enhanced)
```sql
CREATE TABLE conversation_sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    external_user_id VARCHAR(255) NOT NULL,
    workflow_type ENUM('product_search', 'rfq_creation', 'general_inquiry', 'rfq_submitted', 'excel_rfq_upload'),
    outcome ENUM('completed', 'abandoned', 'escalated', 'timeout'),
    rfq_id CHAR(36),
    workflow_state JSON NOT NULL,
    conversation_history JSON NOT NULL,
    extracted_entities JSON,
    whatsapp_context JSON,
    error_details JSON,
    performance_metrics JSON,
    retention_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP NULL,
    
    -- Enhanced Analytics Fields
    user_type ENUM('buyer', 'seller', 'unknown') DEFAULT 'unknown',
    session_state ENUM('active', 'inactive', 'completed', 'abandoned') DEFAULT 'active',
    rfq_ids JSON,
    rfq_metadata JSON,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    product_items JSON,
    seller_responses JSON,
    interaction_metrics JSON,
    parent_session_id VARCHAR(255),
    
    -- BFS (Buy From Stock) Tracking
    bfs_products_searched JSON,
    bfs_search_count INT DEFAULT 0,
    bfs_price_accepted JSON,
    bfs_counter_offers JSON,
    
    -- Bidding & RFQ Lifecycle Tracking
    products_bid_for JSON,
    bids_received JSON,
    bids_accepted JSON,
    counter_offers_made JSON,
    counter_offers_accepted JSON,
    rfqs_with_response JSON,
    avg_products_per_rfq DECIMAL(5,2),
    avg_categories_per_rfq DECIMAL(5,2),
    
    FOREIGN KEY (rfq_id) REFERENCES rfq_records(rfq_id)
);
```

#### session_events
```sql
CREATE TABLE session_events (
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
```

#### daily_aggregated_metrics
```sql
CREATE TABLE daily_aggregated_metrics (
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
```

#### rolling_window_metrics
```sql
CREATE TABLE rolling_window_metrics (
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
```

## Sample Data

### Product Categories (8 total)
- Agriculture Equipments
- Air Logistics
- Automotive
- Batteries & UPS
- Bearings & Accessories
- Building Works
- CCTV & BMS
- Cables

### Vendors (5 total)
1. **Chennai Motors & Equipment** (Chennai, Bangalore, Coimbatore)
   - Agriculture Equipments, Automotive, Bearings & Accessories

2. **Mumbai Industrial Solutions** (Mumbai, Pune, Nashik)
   - Air Logistics, Cables, Building Works

3. **Delhi Safety Systems** (Delhi, Gurgaon, Noida)
   - CCTV & BMS, Batteries & UPS

4. **Hyderabad Tech Solutions** (Hyderabad, Secunderabad, Warangal)
   - Agriculture Equipments, CCTV & BMS, Cables

5. **Kolkata Engineering Works** (Kolkata, Durgapur, Asansol)
   - Automotive, Building Works, Bearings & Accessories

## IP Restrictions Configuration

For test VM deployment, configure IP restrictions in your `.env` file:

```env
# Allow specific IPs (comma-separated)
ALLOWED_IPS=192.168.1.100,203.0.113.45,10.0.0.50

# Allow IP ranges (if needed)
ALLOWED_IPS=192.168.1.0/24,10.0.0.0/16
```

The application middleware will automatically restrict access to only these IPs.

## Database Maintenance

### Backup Database

```bash
# Run backup (created by setup script)
./backup_database.sh
```

### Test Database

```bash
# Test database connection and data
./test_database.sh
```

### Check Database Status

```bash
# Check table counts
mysql -h localhost -u procurement_user -pprocubot2025 procurement_db -e "
SELECT 
    'vendors' as table_name, COUNT(*) as count FROM vendors
UNION ALL
SELECT 'categories', COUNT(*) FROM product_categories
UNION ALL
SELECT 'sessions', COUNT(*) FROM conversation_sessions
UNION ALL
SELECT 'rfq_records', COUNT(*) FROM rfq_records;"
```

## Troubleshooting

### Common Issues

#### 1. MySQL Service Not Running
```bash
# Check service status
sudo systemctl status mysql

# Start service
sudo systemctl start mysql
sudo systemctl enable mysql
```

#### 2. Connection Refused
```bash
# Check if MySQL is listening
sudo netstat -plunt | grep 3306

# Restart MySQL
sudo systemctl restart mysql
```

#### 3. Access Denied
```bash
# Reset user permissions
mysql -u root -p -e "GRANT ALL PRIVILEGES ON procurement_db.* TO 'procurement_user'@'localhost';"
mysql -u root -p -e "FLUSH PRIVILEGES;"
```

#### 4. Database Already Exists
```bash
# Drop and recreate (WARNING: Deletes all data)
mysql -u root -p -e "DROP DATABASE IF EXISTS procurement_db;"
mysql -u root -p -e "CREATE DATABASE procurement_db;"
mysql -u root -p -e "GRANT ALL PRIVILEGES ON procurement_db.* TO 'procurement_user'@'localhost';"
```

### Verification Commands

```bash
# Test database connection
mysql -h localhost -u procurement_user -pprocubot2025 procurement_db -e "SELECT VERSION();"

# Check tables exist
mysql -h localhost -u procurement_user -pprocubot2025 procurement_db -e "SHOW TABLES;"

# Check sample data
mysql -h localhost -u procurement_user -pprocubot2025 procurement_db -e "SELECT COUNT(*) FROM vendors;"

# Test application startup
python -c "from app.database import get_db_session; session = get_db_session(); print('Database connection successful')"
```

## Starting the Application

After successful database setup:

```bash
# Start the application
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Or for development with auto-reload
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Security Considerations

### For Production Deployment

1. **Change default password** from `procubot2025`
2. **Configure firewall** to allow only necessary connections
3. **Enable SSL/TLS** for database connections
4. **Set up regular backups** with retention policy
5. **Monitor database performance** and logs
6. **Update IP restrictions** for production environment

## Key Differences from PostgreSQL

### Data Type Changes
- **UUID → CHAR(36)**: All UUID fields now use 36-character strings
- **JSONB → JSON**: PostgreSQL JSONB converted to MySQL JSON
- **ARRAY → JSON**: PostgreSQL arrays converted to JSON arrays
- **TEXT[] → JSON**: Array fields stored as JSON

### Connection Changes
- **Port**: Changed from 5432 to 3306
- **Driver**: Changed from psycopg2-binary to PyMySQL
- **URL Format**: `mysql+pymysql://` instead of `postgresql://`

### Query Differences
- **Array Functions**: `JSON_EXTRACT()` instead of array operators
- **JSON Functions**: MySQL JSON functions instead of JSONB operators
- **UUID Generation**: Application-level UUID generation

## Next Steps

1. Verify all API keys are configured in `.env`
2. Test the application with sample requests
3. Configure monitoring and alerting
4. Set up automated backups
5. Deploy to test VM with IP restrictions
6. Conduct user acceptance testing

The database is now ready for the AI Procurement Agent application with MySQL backend and IP restrictions configured for your test VM deployment.