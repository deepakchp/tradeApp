# GCP Monolithic VM Deployment Guide

This guide provides the exact steps to deploy the algorithmic trading engine onto a single Google Cloud Platform (GCP) Compute Engine instance. This approach runs the web server, async engine, PostgreSQL database, and Redis cache entirely on one $25/month `e2-medium` server.

## 1. Create a Startup Script (cloud-init)
When you create your VM in Google Cloud, you can paste this script into the **Automation -> Startup script** box. It will automatically install all dependencies, download your code, and start the system on boot.

```bash
#!/bin/bash
# GCP Compute Engine Startup Script (Ubuntu 22.04)

# 1. Update system and install system dependencies
apt-get update -y
apt-get upgrade -y
apt-get install -y postgresql-16 redis-server python3-pip python3-venv git nginx supervisor supervisor

# 2. Configure PostgreSQL
# Switch to postgres user to create the database and user
sudo -u postgres psql -c "CREATE USER trade_user WITH PASSWORD 'secure_db_password';"
sudo -u postgres psql -c "CREATE DATABASE tradeapp OWNER trade_user;"

# 3. Clone Repository (Replace URL/Token with yours)
cd /opt
# We use a Personal Access Token (PAT) for GitHub to clone private repos securely
git clone https://${GITHUB_TOKEN}@github.com/your-username/tradeApp.git
cd tradeApp

# 4. Set up Python Virtual Environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Set up Environment Variables
# Create the .env file for the application
cat <<EOF > /opt/tradeApp/.env
DATABASE_URL=postgresql+asyncpg://trade_user:secure_db_password@localhost/tradeapp
REDIS_URL=redis://localhost:6379/0
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
FLASK_ENV=production
EOF

# 6. Initialize Database Schema
# (Assuming you have an initialization script or use alembic)
# python initialize_db.py 

# 7. Configure Nginx (Reverse Proxy for Flask)
# Create an Nginx config to forward port 80 to your Flask app running on port 8080
cat <<EOF > /etc/nginx/sites-available/tradeapp
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.0:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF
ln -s /etc/nginx/sites-available/tradeapp /etc/nginx/sites-enabled/
rm /etc/nginx/sites-enabled/default
systemctl restart nginx

# 8. Start Supervisord 
# Use the exact supervisord.conf we perfected in WSL
# It will run Flask (Gunicorn), Async Engine, Auto Scanner, Redis, and Postgres in the background.
/opt/tradeApp/.venv/bin/supervisord -c /opt/tradeApp/supervisord.conf
```

## 2. Infrastructure Setup Steps

### Step 1: Provision the VM
1. Go to **Google Cloud Console** -> **Compute Engine** -> **VM instances**.
2. Click **Create Instance**.
3. **Name:** `trade-engine-server`
4. **Region:** `asia-south1` (Mumbai) - *Critical for low latency to NSE*.
5. **Machine configuration:** Choose **E2** series, Machine type **e2-medium** (2 vCPU, 4GB memory).
6. **Boot disk:** Change default Debian to **Ubuntu 22.04 LTS**. Increase size from 10GB to **20GB**.
7. **Identity and API access:** Allow default compute service account.
8. **Firewall:** Check **Allow HTTP traffic** and **Allow HTTPS traffic**.
9. Expand **Advanced Options** -> **Management**.
10. Find the **Automation** block and paste the Startup Script provided above into the text box. Customize the `GITHUB_TOKEN`, `KITE_API_KEY`, etc inside the script before pasting.
11. Click **Create**.

### Step 2: Accessing the Application
1. The VM will take about 3-4 minutes to boot up and run the entire startup script.
2. In the Google Cloud Console, note the **External IP** address of your new VM.
3. Once the setup completes, entering that IP address into your browser will load the login page of your trading application! Nginx will intercept port 80 and reverse proxy to the application running on port 8080 via Gunicorn/Supervisord. 

## 3. Logs & Maintenance
- **Supervisord UI:** You can access the supervisor UI by navigating to `http://<YOUR_EXTERNAL_IP>:9001` (Note: Ensure you configure a password in `supervisord.conf` before opening this port publicly!)
- **Logs:** Connect to the VM via SSH (`gcloud compute ssh trade-engine-server`), and check your logs in `/opt/tradeApp/logs/` or `/tmp/` depending on your `supervisord.conf` settings.


    nohup supervisord -c supervisord.conf > supervisord.log 2>&1 &


 # Stop all processes and shut down
  .venv/bin/supervisorctl -c supervisord.conf stop all
  .venv/bin/supervisorctl -c supervisord.conf shutdown

  # Start supervisord (which starts all processes)
  .venv/bin/supervisord -c supervisord.conf

  # Check status
  .venv/bin/supervisorctl -c supervisord.conf status

  Or to just restart all processes without restarting supervisord itself:

  .venv/bin/supervisorctl -c supervisord.conf restart all   