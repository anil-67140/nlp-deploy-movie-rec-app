#!/bin/bash
# EC2 User Data Script - runs once on first boot
# Installs Python, clones repo, starts FastAPI backend

set -e
exec > >(tee /var/log/userdata.log|logger -t userdata -s 2>/dev/console) 2>&1
echo "=== Starting userdata setup at $(date) ==="

# =============================================
# 1. System Updates & Dependencies
# =============================================
dnf update -y
dnf install -y python3.11 python3.11-pip python3.11-devel git nginx htop

# Set python3 and pip3 defaults
ln -sf /usr/bin/python3.11 /usr/bin/python3
ln -sf /usr/bin/pip3.11 /usr/bin/pip3

# =============================================
# 2. Install AWS CLI v2 (for S3, SSM)
# =============================================
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip"
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/aws /tmp/awscliv2.zip

# =============================================
# 3. Clone the movie recommender repo
# =============================================
APP_DIR="/opt/movie-rec"
mkdir -p $APP_DIR
cd $APP_DIR

# git clone https://github.com/anil-67140/nlp-deploy-movie-rec-app.git .
git clone https://github.com/anil-67140/nlp-deploy-movie-rec-app.git /tmp/repo

# Copy app files to correct location
cp /tmp/repo/movie-rec-aws/backend/main.py $APP_DIR/main.py
cp /tmp/repo/movie-rec-aws/backend/requirements.txt $APP_DIR/requirements.txt
cp /tmp/repo/movie-rec-aws/frontend/app.py $APP_DIR/app.py
cp /tmp/repo/df.pkl $APP_DIR/
cp /tmp/repo/indices.pkl $APP_DIR/
cp /tmp/repo/tfidf.pkl $APP_DIR/
cp /tmp/repo/tfidf_matrix.pkl $APP_DIR/
cp /tmp/repo/movies_metadata.csv $APP_DIR/

rm -rf /tmp/repo

# =============================================
# 4. Install Python dependencies
# =============================================
# pip3 install --upgrade pip
# pip3 install -r requirements.txt

# # Install additional prod dependencies
# pip3 install \
#   uvicorn[standard] \
#   gunicorn \
#   boto3 \
#   python-jose[cryptography] \
#   passlib[bcrypt] \
#   python-multipart \
#   watchtower    # CloudWatch log handler for Python

pip3 install --upgrade pip
pip3 install -r $APP_DIR/requirements.txt
pip3 install gunicorn

# =============================================
# 5. Fetch secrets from SSM Parameter Store
# =============================================
AWS_REGION="${aws_region}"
PROJECT="${project_name}"

get_param() {
  aws ssm get-parameter \
    --name "/$PROJECT/$1" \
    --with-decryption \
    --region "$AWS_REGION" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo ""
}

TMDB_KEY=$(get_param "TMDB_API_KEY")
ASSETS_BUCKET=$(get_param "ASSETS_BUCKET")
COGNITO_POOL_ID="${cognito_pool_id}"
COGNITO_CLIENT="${cognito_client}"

# Write .env file (not committed to git)
cat > $APP_DIR/.env << EOF
TMDB_API_KEY=$TMDB_KEY
ASSETS_BUCKET=$ASSETS_BUCKET
AWS_REGION=$AWS_REGION
COGNITO_POOL_ID=$COGNITO_POOL_ID
COGNITO_CLIENT_ID=$COGNITO_CLIENT
PROJECT_NAME=$PROJECT
ENVIRONMENT=production
LOG_LEVEL=INFO
EOF

chmod 600 $APP_DIR/.env
chown ec2-user:ec2-user $APP_DIR/.env

# =============================================
# 6. Nginx as Reverse Proxy (port 80 -> 8000)
# =============================================
cat > /etc/nginx/conf.d/movie-rec.conf << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    # FastAPI backend
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }

    # Streamlit frontend
    location / {
        proxy_pass http://127.0.0.1:8501/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}
NGINXEOF

# Remove default nginx config
rm -f /etc/nginx/conf.d/default.conf

systemctl enable nginx
systemctl start nginx

# =============================================
# 7. Systemd Service for FastAPI Backend
# =============================================
cat > /etc/systemd/system/fastapi-backend.service << EOF
[Unit]
Description=Movie Recommender FastAPI Backend
After=network.target
Wants=network.target

[Service]
Type=exec
User=ec2-user
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/python3 -m uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 2 \
  --log-level info \
  --access-log
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fastapi-backend

[Install]
WantedBy=multi-user.target
EOF

# =============================================
# 8. Systemd Service for Streamlit Frontend
# =============================================
cat > /etc/systemd/system/streamlit-frontend.service << EOF
[Unit]
Description=Movie Recommender Streamlit Frontend
After=fastapi-backend.service
Wants=fastapi-backend.service

[Service]
Type=exec
User=ec2-user
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=API_BASE=http://127.0.0.1:8000
ExecStart=/usr/bin/python3 -m streamlit run app.py \
  --server.port 8501 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.maxUploadSize 50
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=streamlit-frontend

[Install]
WantedBy=multi-user.target
EOF

# =============================================
# 9. CloudWatch Agent for log shipping
# =============================================
# dnf install -y amazon-cloudwatch-agent
# Fix python symlink temporarily for yum
ln -sf /usr/bin/python3.9 /usr/bin/python3
yum install -y amazon-cloudwatch-agent
ln -sf /usr/bin/python3.11 /usr/bin/python3

cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'CWEOF'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/userdata.log",
            "log_group_name": "/movie-rec/application",
            "log_stream_name": "userdata-{instance_id}",
            "timezone": "UTC"
          }
        ]
      }
    },
    "log_stream_name": "default-{instance_id}"
  },
  "metrics": {
    "namespace": "MovieRec/EC2",
    "metrics_collected": {
      "cpu": {
        "measurement": ["cpu_usage_idle", "cpu_usage_iowait"],
        "metrics_collection_interval": 60
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      },
      "disk": {
        "measurement": ["used_percent"],
        "metrics_collection_interval": 300,
        "resources": ["/"]
      }
    }
  }
}
CWEOF

systemctl enable amazon-cloudwatch-agent
systemctl start amazon-cloudwatch-agent

# =============================================
# 10. Start App Services
# =============================================
systemctl daemon-reload
systemctl enable fastapi-backend
systemctl enable streamlit-frontend
systemctl start fastapi-backend
sleep 5
systemctl start streamlit-frontend

echo "=== Setup complete at $(date) ==="
echo "FastAPI: http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8000"
echo "Streamlit: http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8501"
