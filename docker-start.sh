#!/bin/bash

# ====================================================================
# Docker Startup Script for AI Procurement Agent - Development
# ====================================================================
# Fast development setup with main app running via restart.sh
# ====================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Full build (library changes)
build() {
    print_status "Full build - base image + services..."
    docker build -f Dockerfile.base -t procucev-python-base:latest --no-cache .
    docker-compose -f docker-compose.yml build --no-cache
    print_status "Full build completed successfully"
}

# Build and run (combined)
run() {
    print_status "Building base image (Python + dependencies)..."
    docker build -f Dockerfile.base -t procucev-python-base:latest .
    print_status "Building service images..."
    docker-compose -f docker-compose.yml build
    print_status "Starting background services (Redis, Celery)..."
    docker-compose -f docker-compose.yml up -d
    print_status "Background services started successfully"
    print_status "Redis: localhost:6379 (DB 0: sessions, DB 1: Celery)"
    print_warning "Main app is commented out - use restart.sh to run locally"
}

# Quick rebuild (code changes only)
rebuild() {
    print_status "Quick rebuild - code changes only..."
    docker-compose -f docker-compose.yml build
    docker-compose -f docker-compose.yml up -d --force-recreate
    print_status "Quick rebuild completed successfully"
}

# Start all services
start() {
    print_status "Starting background services..."
    docker-compose -f docker-compose.yml up -d
    print_status "Background services started successfully"
    print_status "Redis: localhost:6379 (DB 0: sessions, DB 1: Celery)"
    print_warning "Main app is commented out - use restart.sh to run locally"
}

# Stop all services
stop() {
    print_status "Stopping all services..."
    docker-compose -f docker-compose.yml down
    print_status "All services stopped"
}

# Restart all services
restart() {
    stop
    start
}

# Reload code changes (fastest)
reload() {
    print_status "Reloading with code changes..."
    docker-compose -f docker-compose.yml up -d --build
    print_status "Code reloaded successfully"
}

# Show logs
logs() {
    if [ -z "$1" ]; then
        docker-compose -f docker-compose.yml logs -f
    else
        docker-compose -f docker-compose.yml logs -f "$1"
    fi
}

# Show status
status() {
    docker-compose -f docker-compose.yml ps
}

# Clean up
clean() {
    print_status "Cleaning up Docker resources..."
    docker-compose -f docker-compose.yml down -v --remove-orphans
    docker system prune -f
    print_status "Cleanup completed"
}

# Clean cache only (no containers)
cache() {
    print_status "Cleaning Docker cache only..."
    docker system prune -a -f
    print_status "Cache cleaned"
}

# Enable main app in docker-compose
enable-app() {
    print_status "Enabling main app in docker-compose..."
    sed -i 's/^  # app:/  app:/' docker-compose.yml
    sed -i 's/^  #   /    /' docker-compose.yml
    print_status "Main app enabled - run 'reload' to apply changes"
}

# Disable main app in docker-compose
disable-app() {
    print_status "Disabling main app in docker-compose..."
    sed -i 's/^  app:/  # app:/' docker-compose.yml
    sed -i 's/^    /  #   /' docker-compose.yml
    print_status "Main app disabled - run 'reload' to apply changes"
}

# Main command handler
case "$1" in
    build)
        build
        ;;
    run)
        run
        ;;
    rebuild)
        rebuild
        ;;
    reload)
        reload
        ;;
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    logs)
        logs "$2"
        ;;
    status)
        status
        ;;
    clean)
        clean
        ;;
    cache)
        cache
        ;;
    enable-app)
        enable-app
        ;;
    disable-app)
        disable-app
        ;;
    *)
        echo "Usage: $0 {build|run|rebuild|reload|start|stop|restart|logs [service]|status|clean|cache|enable-app|disable-app}"
        echo ""
        echo "Development Commands:"
        echo "  build       - Full build (base + services) - when requirements.txt changes"
        echo "  run         - Build base + services and start - FIRST TIME USE"
        echo "  rebuild     - Quick rebuild (code changes only) - FAST"
        echo "  reload      - Reload code changes - FASTEST"
        echo "  start       - Start background services (Redis, Celery)"
        echo "  stop        - Stop all services"
        echo "  restart     - Restart all services"
        echo "  logs        - Show logs (optionally for specific service)"
        echo "  status      - Show service status"
        echo "  clean       - Clean up all Docker resources"
        echo "  cache       - Clean Docker cache only"
        echo "  enable-app  - Enable main app in docker-compose"
        echo "  disable-app - Disable main app in docker-compose"
        echo ""
        echo "Development Workflow:"
        echo "  1. ./docker-start.sh run     # First time setup"
        echo "  2. ./restart.sh              # Run main app locally"
        echo "  3. ./docker-start.sh reload  # When you change code"
        exit 1
        ;;
esac