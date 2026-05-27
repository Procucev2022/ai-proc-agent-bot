#!/bin/sh

# ====================================================================
# Static Celery Worker Manager - 3-Pool Architecture (ALWAYS ON)
# D8as v5 (8 vCPUs, 32GB RAM + 32GB Swap)
# App: 16GB / 4 cores  |  Celery: 24GB / 3 cores (+ swap)
#
# ALL POOLS RUN 24/7 - No schedule-based start/stop
# - Pool 1: Critical (2 workers) - BFS + idle fallback
# - Pool 2: Operations (4 workers) - Primary ops + idle fallback  
# - Pool 3: Bulk (6 workers) - Bulk tasks + idle fallback
# Total: 12 workers continuously running
# ====================================================================

set -e

LOG_DIR="/app/logs/celery_worker"
mkdir -p "$LOG_DIR"

# Get current date for log files
CURRENT_DATE=$(date +%Y-%m-%d)

log() { 
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_DIR/celery_worker_${CURRENT_DATE}.log"
}

log "Starting 3-Pool Static Worker Manager (ALL POOLS ALWAYS ON)..."
log "Critical Pool : 2 workers (bfs_notification + idle: categorization,seller_matching)"
log "Operations Pool: 4 workers (categorization,seller_matching + idle: vector_store,...)"
log "Bulk Pool      : 6 workers (vector_store,taxonomy_build + idle: categorization,...)"
log ""
log "NOTE: All pools run 24/7. Task scheduling handled by Celery Beat."
log "      When bulk queues are empty, bulk workers help with operations tasks."

# ── Pool 1: Critical (BFS always available, NO fallback to ops queues) ──
log "Starting Critical Pool (2 workers)..."
CRITICAL_LOG="$LOG_DIR/critical_pool_${CURRENT_DATE}.log"
celery -A app.celery_app worker \
    --loglevel=info \
    --concurrency=2 \
    --queues=bfs_notification,default \
    --prefetch-multiplier=1 \
    --hostname=critical_pool.%h \
    >> "$CRITICAL_LOG" 2>&1 &
CRITICAL_PID=$!
log "Critical Pool PID: $CRITICAL_PID"

# ── Pool 2: Operations (primary ops, idle helps bulk) ──
log "Starting Operations Pool (4 workers)..."
OPERATIONS_LOG="$LOG_DIR/operations_pool_${CURRENT_DATE}.log"
celery -A app.celery_app worker \
    --loglevel=info \
    --concurrency=4 \
    --queues=categorization,seller_matching,vector_store,report_automation,taxonomy_build,maintenance \
    --prefetch-multiplier=1 \
    --hostname=operations_pool.%h \
    >> "$OPERATIONS_LOG" 2>&1 &
OPERATIONS_PID=$!
log "Operations Pool PID: $OPERATIONS_PID"

# ── Pool 3: Bulk (long-running background tasks, idle helps ops) ──
log "Starting Bulk Pool (6 workers)..."
BULK_LOG="$LOG_DIR/bulk_pool_${CURRENT_DATE}.log"
celery -A app.celery_app worker \
    --loglevel=info \
    --concurrency=6 \
    --queues=vector_store,taxonomy_build,report_automation,maintenance,categorization,seller_matching \
    --prefetch-multiplier=1 \
    --hostname=bulk_pool.%h \
    >> "$BULK_LOG" 2>&1 &
BULK_PID=$!
log "Bulk Pool PID: $BULK_PID"

log ""
log "✓ All 3 pools started (12 total workers)"
log "  PIDs: critical=$CRITICAL_PID ops=$OPERATIONS_PID bulk=$BULK_PID"
log ""
log "Queue Priority Order:"
log "  bfs_notification(9) > categorization(6) > seller_matching(5) > "
log "  report_automation(5) > vector_store(3) > taxonomy_build(1) > maintenance(1)"
log ""
log "Idle Worker Behavior:"
log "  - Critical Pool: Processes bfs_notification,default only (DEDICATED BFS WORKERS)"
log "  - Operations Pool: Processes categorization,seller_matching + vector_store,taxonomy when idle"
log "  - Bulk Pool: Processes vector_store,taxonomy_build + categorization,seller_matching when idle"
log ""
log "Task Scheduling: Handled by Celery Beat (see celery_config.py)"
log "  - BFS Notifications: Every 5 minutes"
log "  - Auto-categorization: Every 15 minutes"
log "  - Vector Store Sync: Daily 5:42 PM IST"
log "  - Taxonomy Build: Daily 12:30 AM IST"
log "  - etc."
log ""

# Monitor pool health - restart individual pools if they die (don't kill all workers for single pool failure)
log "Starting pool health monitor..."
while true; do
    sleep 5
    
    # Check if Critical Pool is alive
    if ! kill -0 $CRITICAL_PID 2>/dev/null; then
        log "❌ CRITICAL: Critical Pool (PID $CRITICAL_PID) has died! Restarting..."
        # Restart Critical Pool only
        CURRENT_DATE=$(date +%Y-%m-%d)
        CRITICAL_LOG="$LOG_DIR/critical_pool_${CURRENT_DATE}.log"
        celery -A app.celery_app worker \
            --loglevel=info \
            --concurrency=2 \
            --queues=bfs_notification,default \
            --prefetch-multiplier=1 \
            --hostname=critical_pool.%h \
            >> "$CRITICAL_LOG" 2>&1 &
        CRITICAL_PID=$!
        log "Critical Pool restarted with PID: $CRITICAL_PID"
    fi
    
    # Check if Operations Pool is alive
    if ! kill -0 $OPERATIONS_PID 2>/dev/null; then
        log "❌ CRITICAL: Operations Pool (PID $OPERATIONS_PID) has died! Restarting..."
        # Restart Operations Pool only
        CURRENT_DATE=$(date +%Y-%m-%d)
        OPERATIONS_LOG="$LOG_DIR/operations_pool_${CURRENT_DATE}.log"
        celery -A app.celery_app worker \
            --loglevel=info \
            --concurrency=4 \
            --queues=categorization,seller_matching,vector_store,report_automation,taxonomy_build,maintenance \
            --prefetch-multiplier=1 \
            --hostname=operations_pool.%h \
            >> "$OPERATIONS_LOG" 2>&1 &
        OPERATIONS_PID=$!
        log "Operations Pool restarted with PID: $OPERATIONS_PID"
    fi
    
    # Check if Bulk Pool is alive
    if ! kill -0 $BULK_PID 2>/dev/null; then
        log "❌ CRITICAL: Bulk Pool (PID $BULK_PID) has died! Restarting..."
        # Restart Bulk Pool only
        CURRENT_DATE=$(date +%Y-%m-%d)
        BULK_LOG="$LOG_DIR/bulk_pool_${CURRENT_DATE}.log"
        celery -A app.celery_app worker \
            --loglevel=info \
            --concurrency=6 \
            --queues=vector_store,taxonomy_build,report_automation,maintenance,categorization,seller_matching \
            --prefetch-multiplier=1 \
            --hostname=bulk_pool.%h \
            >> "$BULK_LOG" 2>&1 &
        BULK_PID=$!
        log "Bulk Pool restarted with PID: $BULK_PID"
    fi
done
