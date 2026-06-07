# 🎬 Movie Recommender — AWS Free Tier Deployment Guide

## Architecture Overview

```
Internet
   │
   ▼
CloudFront (HTTPS CDN) ──► S3 (static frontend / HTML fallback)
   │
   ▼
EC2 t3.micro (us-east-1)
   ├── Nginx (port 80) ─► FastAPI (port 8000) ─► TMDB API
   │                  └► Streamlit (port 8501)  └► TF-IDF pkl files
   ├── AWS Cognito (user auth / JWT)
   ├── S3 Assets bucket (watchlists, uploads via pre-signed URLs)
   ├── SSM Parameter Store (secrets - no hardcoded keys)
   └── CloudWatch (logs + alarms + dashboard)
```

## Free Tier Usage (Monthly)
| Service         | Free Tier Limit       | Our Usage        |
|-----------------|----------------------|------------------|
| EC2 t3.micro    | 750 hrs/month        | ~720 hrs (1 inst)|
| S3 storage      | 5 GB                 | < 1 GB           |
| S3 requests     | 20,000 GET / 2,000 PUT| Low              |
| CloudFront      | 1 TB transfer        | Very low          |
| CloudWatch logs | 5 GB ingest          | < 100 MB (7d ret)|
| Cognito MAU     | 50,000 users         | Very low          |
| SNS             | 1M pub / 1K email    | Very low          |
| Budget          | Free                 | Free             |

**Expected monthly cost: $0.00** (within free tier)

---

## Step 1: Prerequisites

```bash
# Install tools
brew install terraform awscli       # macOS
# or: apt install terraform awscli  # Ubuntu

# Configure AWS credentials
aws configure
# Enter: Access Key ID, Secret, Region (us-east-1), Output (json)

# Generate SSH key if you don't have one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
cat ~/.ssh/id_rsa.pub  # copy this for terraform.tfvars
```

---

## Step 2: Configure Terraform

```bash
cd infra/

# Copy and fill in your values
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

Fill in:
- `ec2_public_key` → output of `cat ~/.ssh/id_rsa.pub`
- `my_ip_cidr`     → your IP + `/32` (get from https://checkip.amazonaws.com)
- `tmdb_api_key`   → from https://themoviedb.org → Settings → API
- `alert_email`    → your email for billing alerts

---

## Step 3: Deploy Infrastructure

```bash
cd infra/
terraform init
terraform plan    # Review what will be created
terraform apply   # Type 'yes' to confirm

# Save the outputs!
terraform output  # Shows EC2 IP, CloudFront URL, etc.
```

---

## Step 4: Copy App Files to EC2

The userdata.sh script auto-clones your GitHub repo and starts the app.
Wait 3-5 minutes after `terraform apply`, then:

```bash
# Get the EC2 IP
EC2_IP=$(terraform output -raw backend_public_ip)

# SSH in and check status
ssh -i ~/.ssh/id_rsa ec2-user@$EC2_IP

# On the EC2:
sudo systemctl status fastapi-backend
sudo systemctl status streamlit-frontend
sudo journalctl -u fastapi-backend -n 50 --no-pager
```

### Copy AWS-enhanced files to EC2:
```bash
# Copy the AWS-enhanced backend
scp -i ~/.ssh/id_rsa backend/main_aws.py ec2-user@$EC2_IP:/opt/movie-rec/main.py
scp -i ~/.ssh/id_rsa backend/requirements.txt ec2-user@$EC2_IP:/opt/movie-rec/requirements.txt

# Copy the AWS-enhanced frontend
scp -i ~/.ssh/id_rsa frontend/app_aws.py ec2-user@$EC2_IP:/opt/movie-rec/app.py

# Restart services
ssh -i ~/.ssh/id_rsa ec2-user@$EC2_IP "
  cd /opt/movie-rec
  pip3 install -r requirements.txt -q
  sudo systemctl restart fastapi-backend
  sleep 5
  sudo systemctl restart streamlit-frontend
"
```

---

## Step 5: Verify Deployment

```bash
EC2_IP=$(cd infra && terraform output -raw backend_public_ip)

# Health check
curl http://$EC2_IP:8000/health

# Access the app
echo "Streamlit: http://$EC2_IP:8501"
echo "FastAPI docs: http://$EC2_IP:8000/docs"
echo "Via Nginx: http://$EC2_IP"
```

---

## Step 6: Setup CloudWatch Dashboard

```bash
# Get instance ID
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=movie-rec-backend" \
  --query "Reservations[0].Instances[0].InstanceId" --output text)

# Create dashboard
cd monitoring/
pip install boto3
python setup_dashboard.py --instance-id $INSTANCE_ID --region us-east-1
```

---

## Step 7: Setup GitHub Actions CI/CD

Add these secrets in your GitHub repo → Settings → Secrets → Actions:

| Secret Name           | Value                              |
|-----------------------|------------------------------------|
| `AWS_ACCESS_KEY_ID`   | Your AWS access key                |
| `AWS_SECRET_ACCESS_KEY`| Your AWS secret key               |
| `EC2_PUBLIC_IP`       | EC2 Elastic IP from Terraform      |
| `EC2_SSH_PRIVATE_KEY` | Content of `~/.ssh/id_rsa`        |
| `TMDB_API_KEY`        | Your TMDB API key                  |
| `FRONTEND_S3_BUCKET`  | From `terraform output frontend_bucket` |
| `CLOUDFRONT_DIST_ID`  | From `terraform output` (CloudFront ID)|

Copy the workflow file:
```bash
cp ci-cd/deploy.yml .github/workflows/deploy.yml
git add .github/workflows/deploy.yml
git push
```

Now every push to `main` auto-deploys!

---

## User Flows (satisfies CCP requirement)

### Flow 1: Browse & Discover (no login)
1. Open app → Home feed (trending/popular movies)
2. Search by keyword → see suggestions dropdown + poster grid
3. Click any movie → view details + TF-IDF NLP recommendations + genre recommendations

### Flow 2: Register, Login, Manage Watchlist (auth)
1. Click Login → Register with email/password (stored in AWS Cognito)
2. Verify email → Login → get JWT access token
3. On any movie detail page → click "Add to Watchlist"
4. Go to Watchlist → see all saved movies → remove any

### Flow 3: File Upload via S3 Pre-signed URL
1. Login → click Upload in sidebar
2. Choose a file (image/PDF/CSV)
3. Click Upload → backend generates pre-signed URL → file goes directly to S3
4. File stored in your personal S3 folder

---

## Cost Monitoring

```bash
# Check current month spend
aws ce get-cost-and-usage \
  --time-period Start=2025-06-01,End=2025-07-01 \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --query "ResultsByTime[0].Total.BlendedCost"
```

Budget alarm fires at $5 forecast (configured in Terraform).

---

## Stopping EC2 to Save Free Tier Hours

```bash
# Stop when not presenting (saves hours)
aws ec2 stop-instances --instance-ids $INSTANCE_ID

# Start when needed
aws ec2 start-instances --instance-ids $INSTANCE_ID
```

---

## Cleanup (Destroy Everything)

```bash
cd infra/
terraform destroy  # Type 'yes'
```

---

## Folder Structure

```
movie-rec-aws/
├── infra/                  # Terraform IaC
│   ├── main.tf             # VPC, EC2, S3, Cognito, CloudFront, CloudWatch
│   ├── variables.tf
│   ├── outputs.tf
│   ├── userdata.sh         # EC2 startup script
│   └── terraform.tfvars.example
├── backend/
│   ├── main_aws.py         # FastAPI + AWS integrations (auth, S3, CloudWatch)
│   └── requirements.txt
├── frontend/
│   └── app_aws.py          # Streamlit + auth, watchlist, upload UI
├── ci-cd/
│   └── deploy.yml          # GitHub Actions workflow
├── monitoring/
│   └── setup_dashboard.py  # CloudWatch dashboard setup
└── docs/
    └── README.md           # This file
```
