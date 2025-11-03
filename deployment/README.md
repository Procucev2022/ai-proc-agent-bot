# Production Deployment - Linux Server

## Path: /projects/procucev_proc_agent

---

## Deployment

```bash
cd /projects/procucev_proc_agent
chmod +x deployment/setup_celery_production.sh
./deployment/setup_celery_production.sh
```

The script will prompt for user/group and set everything up automatically.

---

## Managing Services

```bash
# Start
sudo systemctl start celery-worker celery-beat

# Stop
sudo systemctl stop celery-worker celery-beat

# Restart
sudo systemctl restart celery-worker celery-beat

# Check status
sudo systemctl status celery-worker celery-beat
```

---

## View Logs

```bash
# Tail worker logs
sudo tail -f /var/log/celery/worker.log

# Tail beat logs
sudo tail -f /var/log/celery/beat.log

# View with journalctl
sudo journalctl -u celery-worker -f
sudo journalctl -u celery-beat -f

# View recent logs
sudo journalctl -u celery-worker -n 50
```

---

## Permissions

```bash
# Create log directories
sudo mkdir -p /var/log/celery /var/run/celery

# Set permissions (replace 'your-user' with actual user)
sudo chown -R your-user:your-user /var/log/celery
sudo chown -R your-user:your-user /var/run/celery

# Project directory permissions
sudo chown -R your-user:your-user /projects/procucev_proc_agent
```
