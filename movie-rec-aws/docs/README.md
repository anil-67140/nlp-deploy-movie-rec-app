# 🎬 Movie Recommender — AWS Free Tier Deployment

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
   ├── DynamoDB (watchlist storage)
   ├── S3 Assets bucket (file uploads via pre-signed URLs)
   ├── SSM Parameter Store (secrets - no hardcoded keys)
   └── CloudWatch (logs + alarms + dashboard)
```

## Free Tier Usage (Monthly)

| Service         | Free Tier Limit        | Our Usage          |
|-----------------|------------------------|--------------------|
| EC2 t3.micro    | 750 hrs/month          | ~720 hrs (1 inst)  |
| S3 storage      | 5 GB                   | < 1 GB             |
| S3 requests     | 20,000 GET / 2,000 PUT | Low                |
| CloudFront      | 1 TB transfer          | Very low           |
| DynamoDB        | 25 GB / 25 WCU / 25 RCU| Very low (always free)|
| CloudWatch logs | 5 GB ingest            | < 100 MB (7d ret)  |
| Cognito MAU     | 50,000 users           | Very low           |
| SNS             | 1M pub / 1K email      | Very low           |
| Budget          | Free                   | Free               |

**Expected monthly cost: $0.00** (within free tier)

---

## Folder Structure

```
nlp-deploy-movie-rec-app/
├── movie-rec-aws/
│   ├── infra/                  # Terraform IaC
│   │   ├── main.tf             # VPC, EC2, S3, DynamoDB, Cognito, CloudFront, CloudWatch
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   ├── userdata.sh         # EC2 startup script (auto-installs everything)
│   │   ├── terraform.tfvars    # Your values (not committed)
│   │   └── terraform.tfvars.example
│   ├── backend/
│   │   ├── main.py             # FastAPI + AWS integrations (auth, S3, DynamoDB, CloudWatch)
│   │   └── requirements.txt    # Pinned Python dependencies
│   ├── frontend/
│   │   └── app.py              # Streamlit UI (auth, watchlist, upload)
│   ├── ci-cd/
│   │   └── deploy.yml          # GitHub Actions CI/CD workflow
│   ├── monitoring/
│   │   └── setup_dashboard.py  # CloudWatch dashboard setup
│   └── docs/
│       └── README.md
├── df.pkl                      # Movie dataset (TF-IDF)
├── indices.pkl                 # Title-to-index mapping
├── tfidf.pkl                   # TF-IDF vectorizer
├── tfidf_matrix.pkl            # TF-IDF matrix
├── movies_metadata.csv         # Raw movie metadata
├── .gitignore
└── README.md
```

---

## Prerequisites

```bash
# Install tools (Windows - already installed)
# terraform --version
# aws --version

# Configure AWS credentials
aws configure
# Enter: Access Key ID, Secret Key, Region (us-east-1), Output (json)

# Generate SSH key
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
cat ~/.ssh/id_rsa.pub  # copy this for terraform.tfvars
```

---

## Step 1: Configure Terraform

```bash
cd movie-rec-aws/infra/

# Copy and fill in your values
copy terraform.tfvars.example terraform.tfvars
```

Fill in `terraform.tfvars`:
- `ec2_public_key` → output of `cat ~/.ssh/id_rsa.pub`
- `my_ip_cidr`     → your IP + `/32` (from https://checkip.amazonaws.com)
- `tmdb_api_key`   → from https://themoviedb.org → Settings → API
- `alert_email`    → your email for billing/CloudWatch alerts

---

## Step 2: Deploy Infrastructure

```bash
cd movie-rec-aws/infra/

terraform init
terraform plan     # Review all 36 resources
terraform apply    # Type 'yes' to deploy

# Save the outputs shown at the end
terraform output
```

**Terraform creates automatically:**
- VPC, subnets, security groups, internet gateway
- EC2 t3.micro with Elastic IP
- S3 frontend bucket + CloudFront CDN
- S3 assets bucket (versioning + lifecycle)
- DynamoDB watchlist table
- Cognito user pool + client
- SSM Parameter Store (TMDB key, bucket name, Cognito IDs)
- CloudWatch log groups + CPU/status alarms
- SNS topic + email alerts
- Budget alarm at $5

---

## Step 3: Wait for EC2 Auto-Setup (5-7 minutes)

The `userdata.sh` script runs automatically on first boot and:
1. Installs Python 3.11, nginx, git
2. Clones this repo
3. Copies `backend/main.py`, `frontend/app.py`, pkl files to `/opt/movie-rec/`
4. Installs all Python dependencies (pinned versions)
5. Fetches secrets from SSM Parameter Store
6. Configures nginx as reverse proxy
7. Creates systemd services for FastAPI and Streamlit
8. Installs and starts CloudWatch agent
9. Starts both services

---

## Step 4: Verify Deployment

```bash
# SSH into EC2 (use your EC2 IP from terraform output)
ssh -i ~/.ssh/id_rsa ec2-user@<EC2_IP>

# Check services
sudo systemctl status fastapi-backend
sudo systemctl status streamlit-frontend

# Health check
curl http://localhost:8000/health
```

**Your live URLs:**
- Streamlit app: `http://<EC2_IP>:8501`
- FastAPI docs:  `http://<EC2_IP>:8000/docs`
- Via Nginx:     `http://<EC2_IP>`
- CloudFront:    `https://<cloudfront_url>` (from terraform output)

---

## Step 5: Setup CloudWatch Dashboard

```bash
# Get instance ID
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=movie-rec-backend" \
  --query "Reservations[0].Instances[0].InstanceId" --output text)

# Create dashboard
cd movie-rec-aws/monitoring/
pip install boto3
python setup_dashboard.py --instance-id $INSTANCE_ID --region us-east-1
```

---

## Step 6: Setup GitHub Actions CI/CD

Add these secrets in GitHub repo → Settings → Secrets → Actions:

| Secret Name              | Value                                        |
|--------------------------|----------------------------------------------|
| `AWS_ACCESS_KEY_ID`      | Your AWS access key                          |
| `AWS_SECRET_ACCESS_KEY`  | Your AWS secret key                          |
| `EC2_PUBLIC_IP`          | Elastic IP from `terraform output`           |
| `EC2_SSH_PRIVATE_KEY`    | Content of `~/.ssh/id_rsa`                   |
| `TMDB_API_KEY`           | Your TMDB API key                            |
| `FRONTEND_S3_BUCKET`     | From `terraform output frontend_bucket`      |
| `CLOUDFRONT_DIST_ID`     | From `terraform output`                      |

```bash
# Enable CI/CD workflow
mkdir -p .github/workflows
cp movie-rec-aws/ci-cd/deploy.yml .github/workflows/deploy.yml
git add .github/workflows/deploy.yml
git push
```

Every push to `main` now auto-deploys to EC2.

---

## User Flows (CCP Requirement — 3 Flows)

### Flow 1: Browse & Discover (no login required)
1. Open app → Home feed (trending/popular/top-rated movies)
2. Search by keyword → dropdown suggestions + poster grid
3. Click any movie → details + TF-IDF NLP recommendations + genre recommendations

### Flow 2: Register, Login, Manage Watchlist (Cognito auth)
1. Click Login → Register with email/password (stored in AWS Cognito)
2. Verify email → Login → receive JWT access token
3. Click "Add to Watchlist" on any movie → saved to DynamoDB
4. Open Watchlist tab → view all saved movies → remove any

### Flow 3: File Upload via S3 Pre-signed URL
1. Login → click Upload in sidebar
2. Choose a file (image/PDF/CSV)
3. Backend generates pre-signed URL → file uploads directly to S3
4. File stored in your personal S3 folder (`uploads/<your_email>/`)

---

## Stop EC2 to Save Free Tier Hours

```bash
# Stop when not presenting
aws ec2 stop-instances --instance-ids <INSTANCE_ID>

# Start again before demo
aws ec2 start-instances --instance-ids <INSTANCE_ID>

# After starting, SSH in and start services
ssh -i ~/.ssh/id_rsa ec2-user@<EC2_IP>
sudo systemctl start fastapi-backend
sudo systemctl start streamlit-frontend
```

---

## Destroy Everything (Cleanup)

```bash
cd movie-rec-aws/infra/
terraform destroy  # Type 'yes'
```

All 36 AWS resources deleted. $0 charges after destroy.

---

## Cost Monitoring

```bash
# Check current month spend
aws ce get-cost-and-usage \
  --time-period Start=2026-06-01,End=2026-07-01 \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --query "ResultsByTime[0].Total.BlendedCost"
```

Budget alarm fires email alert at $5 forecast (configured in Terraform).
