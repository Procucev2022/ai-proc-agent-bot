#!/bin/bash

# ====================================================================
# Enhanced Docker Management Script - AI Procurement Agent
# ====================================================================
# Optimized for fast development cycles and production builds
# ====================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[✓]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[!]${NC} $1"; }
print_error() { echo -e "${RED}[✗]${NC} $1"; }
print_header() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }
print_info() { echo -e "${CYAN}[i]${NC} $1"; }

# Enable BuildKit for faster builds
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# ====================================================================
# BUILD COMMANDS
# ====================================================================

# Shared dependency image. Dockerfile.app starts FROM this tag (see its
# BASE_IMAGE arg), so it has to exist before any service image is built.
BASE_IMAGE_TAG="procucev-base:local"

# Build the base image only when it is missing. Compose cannot build it itself
# and would try to pull 'procucev-base:local' from a registry instead.
ensure_base() {
    if docker image inspect "$BASE_IMAGE_TAG" >/dev/null 2>&1; then
        print_status "Base image $BASE_IMAGE_TAG present"
        return
    fi
    print_warning "Base image $BASE_IMAGE_TAG missing, building it first..."
    docker build -f Dockerfile.base -t "$BASE_IMAGE_TAG" .
}

# Full build - use when requirements.txt changes
build() {
    print_header "FULL BUILD - Base Image + Services"
    print_warning "Use this when requirements.txt changes"
    
    # Prune system before build to prevent OOM
    print_status "Pruning Docker system to free memory..."
    docker system prune -f
    
    start_time=$(date +%s)
    
    print_status "Building base image (Python + dependencies)..."
    docker build -f Dockerfile.base -t "$BASE_IMAGE_TAG" --no-cache .
    
    # --no-cache only re-runs the code COPY steps here: the service images start
    # FROM the base image above, so the dependency install happens once.
    print_status "Building service images..."
    docker compose build --no-cache --parallel
    
    end_time=$(date +%s)
    duration=$((end_time - start_time))
    
    print_header "BUILD COMPLETED"
    print_status "Time: ${duration}s"
    print_info "Next: ./procucev-agent.sh start"
}

# Quick rebuild - code changes only (FAST)
rebuild() {
    print_header "QUICK REBUILD - Code Changes Only"
    
    start_time=$(date +%s)
    
    print_status "Stopping containers..."
    docker compose down --remove-orphans
    
    ensure_base
    
    print_status "Building updated images (parallel)..."
    docker compose build --parallel
    
    print_status "Starting services..."
    docker compose up -d
    
    end_time=$(date +%s)
    duration=$((end_time - start_time))
    
    print_header "REBUILD COMPLETED"
    print_status "Time: ${duration}s"
    show_urls
}

# Hot reload - fastest (no rebuild, just restart)
reload() {
    print_header "HOT RELOAD - Restart with Code Changes"
    
    start_time=$(date +%s)
    
    ensure_base
    
    docker compose up -d --build
    
    end_time=$(date +%s)
    duration=$((end_time - start_time))
    
    print_status "Reloaded in ${duration}s"
    show_urls
}

# ====================================================================
# LIFECYCLE COMMANDS
# ====================================================================

# Full build and start - first time setup
init() {
    print_header "FULL BUILD + START"
    print_info "Use this for first time setup or when requirements.txt changes"
    
    if [ ! -f ".env" ]; then
        print_error ".env file not found"
        print_info "Copy .env.example to .env and configure it"
        exit 1
    fi
    
    # Prune system before build to prevent OOM
    print_status "Pruning Docker system to free memory..."
    docker system prune -f
    
    start_time=$(date +%s)
    
    print_status "Building base image (Python + dependencies)..."
    docker build -f Dockerfile.base -t "$BASE_IMAGE_TAG" .
    
    print_status "Building service images..."
    docker compose build --parallel
    
    print_status "Starting all services..."
    docker compose up -d
    
    end_time=$(date +%s)
    duration=$((end_time - start_time))
    
    print_header "FULL BUILD COMPLETED"
    print_status "Time: ${duration}s"
    show_urls
    print_info "Check logs: ./procucev-agent.sh logs"
}

# Start services
start() {
    print_status "Starting all services..."
    docker compose up -d
    print_status "Services started"
    show_urls
}

# Stop services
stop() {
    print_status "Stopping all services..."
    docker compose down
    print_status "Services stopped"
}

# Restart services
restart() {
    print_header "RESTARTING SERVICES"
    stop
    start
}

# ====================================================================
# MONITORING COMMANDS
# ====================================================================

# Show logs
logs() {
    local service="$2"
    local pool="$3"  # For celery_worker: critical, operations, bulk, or manager
    local log_file=""
    
    if [ -z "$service" ]; then
        print_info "Showing logs for all services (Ctrl+C to exit)"
        docker compose logs -f
    else
        # Validate service name
        case "$service" in
            app|celery_worker|celery_beat|redis|chromadb)
                # Map service to log file
                case "$service" in
                    app)
                        log_file="./logs/app/app_$(date +%Y-%m-%d).log"
                        ;;
                    celery_worker)
                        # Celery has 4 log files: manager + 3 pools
                        case "$pool" in
                            critical)
                                log_file="./logs/celery_worker/critical_pool_$(date +%Y-%m-%d).log"
                                ;;
                            operations|ops)
                                log_file="./logs/celery_worker/operations_pool_$(date +%Y-%m-%d).log"
                                ;;
                            bulk)
                                log_file="./logs/celery_worker/bulk_pool_$(date +%Y-%m-%d).log"
                                ;;
                            manager)
                                log_file="./logs/celery_worker/celery_worker_$(date +%Y-%m-%d).log"
                                ;;
                            *)
                                # Default to operations pool
                                log_file="./logs/celery_worker/operations_pool_$(date +%Y-%m-%d).log"
                                ;;
                        esac
                        ;;
                    celery_beat)
                        log_file="./logs/celery_beat/celery_beat_$(date +%Y-%m-%d).log"
                        ;;
                    redis)
                        log_file="./logs/redis.log"
                        ;;
                    chromadb)
                        log_file="./logs/chromadb.log"
                        ;;
                esac
                
                if [ -f "$log_file" ]; then
                    print_info "Showing logs from: $log_file (Ctrl+C to exit)"
                    if [ "$service" = "celery_worker" ]; then
                        print_info "Other pools: critical, operations, bulk, manager"
                    fi
                    tail -f "$log_file"
                else
                    print_warning "Log file not found: $log_file"
                    if [ "$service" = "celery_worker" ]; then
                        print_info "Celery logs available:"
                        print_info "  ./logs/celery_worker/critical_pool_$(date +%Y-%m-%d).log"
                        print_info "  ./logs/celery_worker/operations_pool_$(date +%Y-%m-%d).log"
                        print_info "  ./logs/celery_worker/bulk_pool_$(date +%Y-%m-%d).log"
                        print_info "  ./logs/celery_worker/celery_worker_$(date +%Y-%m-%d).log (manager)"
                    fi
                    print_info "Falling back to Docker logs..."
                    docker compose logs -f "$service"
                fi
                ;;
            *)
                print_error "Unknown service: $service"
                print_info "Valid services: app, celery_worker, celery_beat, redis, chromadb"
                exit 1
                ;;
        esac
    fi
}

# Trigger Celery tasks manually
trigger_task() {
    local task_name="$2"
    local args="$3"
    
    if [ -z "$task_name" ]; then
        print_error "Task name required"
        print_info "Usage: ./procucev-agent.sh task <task_name> [args]"
        print_info "Use './procucev-agent.sh show-tasks' to see all available tasks"
        exit 1
    fi
    
    print_info "Triggering task: $task_name"
    print_info "This will execute the task immediately (not scheduled)"
    
    case "$task_name" in
        auto-categorization-task)
            docker exec procucev_celery_worker python -c "from app.tasks.auto_categorization_task import process_uncategorized_rfqs; result = process_uncategorized_rfqs.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        vector-store-sync-task)
            docker exec procucev_celery_worker python -c "from app.tasks.vector_store_sync_task import sync_vector_store; result = sync_vector_store.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        seller-matching-task)
            docker exec procucev_celery_worker python -c "from app.tasks.seller_matching_task import process_seller_matching; result = process_seller_matching.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        daily-category-vector-rebuild-task)
            docker exec procucev_celery_worker python -c "from app.tasks.daily_category_vector_rebuild_task import rebuild_category_vector_store; result = rebuild_category_vector_store.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        category-name-sync-task)
            docker exec procucev_celery_worker python -c "from app.tasks.category_name_sync_task import sync_category_names; result = sync_category_names.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        log-cleanup-task)
            docker exec procucev_celery_worker python -c "from app.tasks.log_cleanup_task import cleanup_logs; result = cleanup_logs.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        bfs-notification-task)
            docker exec procucev_celery_worker python -c "from app.tasks.bfs_notification_task import process_bfs_seller_notifications; result = process_bfs_seller_notifications.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        whatsapp-report-automation-task)
            docker exec procucev_celery_worker python -c "from app.tasks.whatsapp_report_automation_task import run_whatsapp_report_automation; result = run_whatsapp_report_automation.delay(); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        taxonomy-build-task)
            docker exec procucev_celery_worker python -c "from app.tasks.taxonomy_build_task import build_taxonomy; result = build_taxonomy.delay(process_all=True, parallel=True); print(f'Task ID: {result.id}'); print(f'Status: {result.status}')"
            ;;
        *)
            print_warning "Custom task: $task_name"
            print_info "Attempting to run generic task..."
            docker exec procucev_celery_worker python -c "print('Task execution attempted for: $task_name')"
            ;;
    esac
    
    print_status "Task queued successfully!"
    print_info "Check logs with: ./procucev-agent.sh logs celery_worker"
}

# Show all available Celery tasks
show_tasks() {
    print_header "AVAILABLE CELERY TASKS"
    echo ""
    print_info "Registered Celery Tasks (use with: ./procucev-agent.sh task <task-name>)"
    echo ""
    echo "┌────────────────────────────────────────────────────────────────────────────┐"
    echo "│ TASK NAME                              │ SCHEDULE        │ DESCRIPTION     │"
    echo "├────────────────────────────────────────┼─────────────────┼─────────────────┤"
    echo "│ auto-categorization-task               │ Every 15 min    │ Auto-categorize │"
    echo "│ vector-store-sync-task                 │ Daily 6:05 PM   │ Sync vectors    │"
    echo "│ seller-matching-task                   │ Every hour      │ Match sellers   │"
    echo "│ daily-category-vector-rebuild-task     │ Daily 1:10 PM   │ Rebuild vectors │"
    echo "│ category-name-sync-task                │ Daily 12:30 PM  │ Sync names      │"
    echo "│ log-cleanup-task                       │ Daily 2:00 AM   │ Cleanup logs    │"
    echo "│ bfs-notification-task                  │ Every 5 min     │ BFS notifs      │"
    echo "│ whatsapp-report-automation-task        │ Daily 10:00 AM  │ WhatsApp reports│"
    echo "│ taxonomy-build-task                    │ Daily 3:44 PM   │ Build taxonomy (parallel) │"
    echo "└────────────────────────────────────────┴─────────────────┴─────────────────┘"
    echo ""
    
    print_info "Fetching registered tasks from Celery worker..."
    echo ""
    
    # Get actual registered tasks from Celery
    if docker ps | grep -q procucev_celery_worker; then
        print_info "Registered tasks in running worker:"
        docker exec -it procucev_celery_worker celery -A app.celery_app inspect registered 2>/dev/null || {
            print_warning "Could not fetch registered tasks. Worker might be busy or unavailable."
        }
    else
        print_warning "Celery worker is not running. Start it with: ./procucev-agent.sh start"
    fi
    
    echo ""
    print_info "Examples:"
    echo "  ./procucev-agent.sh task auto-categorization-task"
    echo "  ./procucev-agent.sh task bfs-notification-task"
    echo "  ./procucev-agent.sh task seller-matching-task"
}

# Show status
status() {
    print_header "SERVICE STATUS"
    docker compose ps
    echo ""
    print_header "RESOURCE USAGE"
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"
}

# Health check
health() {
    print_header "HEALTH CHECKS"
    
    echo -n "App: "
    if curl -sf http://localhost:8005/health > /dev/null; then
        print_status "Healthy"
    else
        print_error "Unhealthy"
    fi
    
    echo -n "Redis: "
    if docker exec procucev_redis redis-cli ping > /dev/null 2>&1; then
        print_status "Healthy"
    else
        print_error "Unhealthy"
    fi
    
    echo -n "ChromaDB: "
    if curl -sf http://localhost:8000/api/v1/heartbeat > /dev/null; then
        print_status "Healthy"
    else
        print_error "Unhealthy"
    fi
}

# ====================================================================
# MAINTENANCE COMMANDS
# ====================================================================

# Show version info
version() {
    print_header "VERSION INFORMATION"
    
    # App version from file
    if [ -f ".version" ]; then
        echo "App Version: $(cat .version)"
    else
        echo "App Version: Not set"
    fi
    
    # Git commit
    if git rev-parse --git-dir > /dev/null 2>&1; then
        echo "Git Commit: $(git rev-parse --short HEAD)"
        echo "Git Branch: $(git branch --show-current)"
    fi
    
    # Docker images
    echo ""
    print_info "Local Docker images:"
    docker images | grep -E "(procucev|REPOSITORY)" | head -10
}

# Show metrics
metrics() {
    print_header "RESOURCE METRICS"
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"
}

# Clean up everything
clean() {
    print_header "CLEANING UP"
    print_warning "This will remove all containers, images, and volumes"
    
    read -p "Type YES to continue: " -r
    if [[ ! $REPLY == "YES" ]]; then
        print_info "Cancelled"
        exit 0
    fi
    
    print_status "Stopping services..."
    docker compose down -v --remove-orphans
    
    print_status "Removing images..."
    docker rmi $(docker images -q 'procucev*') 2>/dev/null || true
    
    print_status "Cleaning build cache..."
    docker builder prune -a -f
    
    print_status "Cleaning system..."
    docker system prune -a -f --volumes
    
    print_header "CLEANUP COMPLETED"
}

# Clean cache only
cache() {
    print_header "CLEANING CACHE"
    print_warning "This will clear Docker build cache"
    
    read -p "Type YES to continue: " -r
    if [[ ! $REPLY == "YES" ]]; then
        print_info "Cancelled"
        exit 0
    fi
    
    print_status "Cleaning Docker cache..."
    docker builder prune -a -f
    docker system prune -f
    print_status "Cache cleaned"
}

# Prune unused resources
prune() {
    print_status "Pruning unused Docker resources..."
    docker system prune -f
    print_status "Pruning completed"
}

# ====================================================================
# UTILITY FUNCTIONS
# ====================================================================

show_urls() {
    echo ""
    print_header "SERVICE URLS"
    echo "  Main App:    http://localhost:8005"
    echo "  Redis:       localhost:6379"
    echo "  ChromaDB:    http://localhost:8000"
    echo ""
}

# ====================================================================
# MAIN COMMAND HANDLER
# ====================================================================

case "$1" in
    # Build commands
    init)
        init
        ;;
    build)
        build
        ;;
    rebuild)
        rebuild
        ;;
    reload)
        reload
        ;;
    
    # Lifecycle commands
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    
    # Monitoring commands
    logs)
        logs "$@"
        ;;
    task)
        trigger_task "$@"
        ;;
    show-tasks)
        show_tasks
        ;;
    status)
        status
        ;;
    health)
        health
        ;;
    version)
        version
        ;;
    metrics)
        metrics
        ;;
    
    # Maintenance commands
    clean)
        clean
        ;;
    cache)
        cache
        ;;
    prune)
        prune
        ;;
    
    *)
        echo ""
        echo "=================================================================================================================="
        echo "                                     PROCUCEV AGENT - OPERATIONS CLI"
        echo "=================================================================================================================="
        echo ""

        echo "SERVICE TOPOLOGY"
        echo "  Services: app | celery_worker | celery_beat | redis | chromadb"
        echo ""

        echo "BUILD & DEPLOYMENT COMMANDS"
        echo "  init         - Build images and start stack (initial setup / dependency changes)"
        echo "  build        - Build images only (no startup)"
        echo "  rebuild      - Incremental rebuild (application changes only)"
        echo "  reload       - Restart services with latest containers (no rebuild)"
        echo ""

        echo "SERVICE LIFECYCLE MANAGEMENT"
        echo "  start        - Start all services"
        echo "  stop         - Gracefully stop all services"
        echo "  restart      - Restart all services"
        echo ""

        echo "OBSERVABILITY & MONITORING"
        echo "  logs [svc] [pool] - Stream logs (all services or specific service)"
        echo "                 Examples:"
        echo "                   ./procucev-agent.sh logs                  # All services"
        echo "                   ./procucev-agent.sh logs app              # Main application"
        echo "                   ./procucev-agent.sh logs celery_worker    # Operations pool (default)"
        echo "                   ./procucev-agent.sh logs celery_worker critical   # Critical pool (BFS)"
        echo "                   ./procucev-agent.sh logs celery_worker operations # Operations pool"
        echo "                   ./procucev-agent.sh logs celery_worker bulk       # Bulk pool (taxonomy)"
        echo "                   ./procucev-agent.sh logs celery_worker manager    # Manager logs"
        echo "                   ./procucev-agent.sh logs celery_beat      # Celery beat scheduler"
        echo "                   ./procucev-agent.sh logs redis            # Redis database"
        echo "                   ./procucev-agent.sh logs chromadb         # ChromaDB vector store"
        echo ""
        echo "  task [name]  - Manually trigger Celery tasks"
        echo "                 Examples:"
        echo "                   ./procucev-agent.sh task category-name-sync-task"
        echo "                   ./procucev-agent.sh task taxonomy-build-task"
        echo "                   ./procucev-agent.sh task bfs-notification-task"
        echo ""
        echo "  show-tasks   - List all available Celery tasks with descriptions"
        echo ""
        echo "  status       - Display container status and resource metrics"
        echo "  health       - Run application health checks"
        echo "  version      - Show app version, git commit, docker images"
        echo "  metrics      - Show container CPU/memory usage summary"
        echo ""

        echo "SYSTEM MAINTENANCE"
        echo "  clean        - Remove containers, images, volumes (destructive)"
        echo "  cache        - Clear Docker build cache"
        echo "  prune        - Remove unused Docker resources"
        echo ""
        exit 1
        ;;

esac
