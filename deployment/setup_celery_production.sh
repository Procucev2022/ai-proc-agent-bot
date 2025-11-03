#!/bin/bash
# Setup script for Celery production deployment on Linux
# This script automates the setup of Celery worker and beat as systemd services

set -e  # Exit on error

echo "======================================"
echo "Celery Production Setup Script"
echo "======================================"
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "Please do not run as root. Run as your application user."
    exit 1
fi

# Get current directory (project root)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"

echo "Project directory: $PROJECT_DIR"
echo "Virtual environment: $VENV_DIR"
echo ""

# Prompt for user/group
read -p "Enter the user to run Celery (default: $USER): " CELERY_USER
CELERY_USER=${CELERY_USER:-$USER}

read -p "Enter the group to run Celery (default: $USER): " CELERY_GROUP
CELERY_GROUP=${CELERY_GROUP:-$USER}

echo ""
echo "Configuration:"
echo "  User: $CELERY_USER"
echo "  Group: $CELERY_GROUP"
echo "  Project: $PROJECT_DIR"
echo ""

read -p "Continue with this configuration? (y/n): " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Step 1: Creating log and PID directories..."
sudo mkdir -p /var/log/celery
sudo mkdir -p /var/run/celery
sudo chown -R $CELERY_USER:$CELERY_GROUP /var/log/celery
sudo chown -R $CELERY_USER:$CELERY_GROUP /var/run/celery
echo "✓ Directories created"

echo ""
echo "Step 2: Creating systemd service files..."

# Create worker service file
cat > /tmp/celery-worker.service << EOF
[Unit]
Description=Celery Worker for Procurement Agent
After=network.target redis.service
Requires=redis.service

[Service]
Type=forking
User=$CELERY_USER
Group=$CELERY_GROUP
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$VENV_DIR/bin"
ExecStart=$VENV_DIR/bin/celery -A app.celery_app worker \\
    --loglevel=info \\
    --logfile=/var/log/celery/worker.log \\
    --pidfile=/var/run/celery/worker.pid \\
    --detach
ExecStop=$VENV_DIR/bin/celery -A app.celery_app control shutdown
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create beat service file
cat > /tmp/celery-beat.service << EOF
[Unit]
Description=Celery Beat for Procurement Agent
After=network.target redis.service celery-worker.service
Requires=redis.service

[Service]
Type=simple
User=$CELERY_USER
Group=$CELERY_GROUP
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$VENV_DIR/bin"
ExecStart=$VENV_DIR/bin/celery -A app.celery_app beat \\
    --loglevel=info \\
    --logfile=/var/log/celery/beat.log \\
    --pidfile=/var/run/celery/beat.pid
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Copy service files
sudo cp /tmp/celery-worker.service /etc/systemd/system/
sudo cp /tmp/celery-beat.service /etc/systemd/system/
rm /tmp/celery-worker.service /tmp/celery-beat.service

echo "✓ Service files created"

echo ""
echo "Step 3: Reloading systemd daemon..."
sudo systemctl daemon-reload
echo "✓ Daemon reloaded"

echo ""
echo "Step 4: Enabling services..."
sudo systemctl enable celery-worker
sudo systemctl enable celery-beat
echo "✓ Services enabled"

echo ""
echo "Step 5: Starting services..."
sudo systemctl start celery-worker
sudo systemctl start celery-beat
echo "✓ Services started"

echo ""
echo "======================================"
echo "Setup Complete!"
echo "======================================"
echo ""
echo "Service status:"
sudo systemctl status celery-worker --no-pager
echo ""
sudo systemctl status celery-beat --no-pager

echo ""
echo "Useful commands:"
echo "  View logs:    sudo tail -f /var/log/celery/worker.log"
echo "  View logs:    sudo tail -f /var/log/celery/beat.log"
echo "  Check status: sudo systemctl status celery-worker celery-beat"
echo "  Restart:      sudo systemctl restart celery-worker celery-beat"
echo "  Stop:         sudo systemctl stop celery-worker celery-beat"
echo ""
echo "Monitor with journalctl:"
echo "  sudo journalctl -u celery-worker -f"
echo "  sudo journalctl -u celery-beat -f"
echo ""
