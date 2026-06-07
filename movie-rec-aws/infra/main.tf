terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "aws" {
  region = var.aws_region
}

# =============================================
# DATA SOURCES
# =============================================
data "aws_availability_zones" "available" {
  state = "available"
}

# =============================================
# VPC - Single AZ to minimize NAT costs (free tier)
# =============================================
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "${var.project_name}-vpc" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_name}-igw" }
}

# Public subnet (EC2 lives here - no NAT needed)
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public-a" }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public-b" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.project_name}-rt-public" }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

# =============================================
# SECURITY GROUPS
# =============================================

# EC2 (backend FastAPI) - allow 8000, SSH, HTTPS
resource "aws_security_group" "ec2_backend" {
  name        = "${var.project_name}-ec2-backend-sg"
  description = "Allow HTTP/HTTPS and SSH for FastAPI backend"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "FastAPI port"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Streamlit port"
    from_port   = 8501
    to_port     = 8501
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }
  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-ec2-backend-sg" }
}

# Cognito User Pool (authentication)
resource "aws_cognito_user_pool" "main" {
  name = "${var.project_name}-user-pool"

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = false
  }

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]

  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
  }

  tags = { Name = "${var.project_name}-cognito" }
}

resource "aws_cognito_user_pool_client" "main" {
  name         = "${var.project_name}-client"
  user_pool_id = aws_cognito_user_pool.main.id

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH"
  ]

  generate_secret = false

  access_token_validity  = 1   # hours - short for security
  refresh_token_validity = 7   # days
  token_validity_units {
    access_token  = "hours"
    refresh_token = "days"
  }
}

# =============================================
# S3 BUCKETS
# =============================================

# Frontend hosting bucket
resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.project_name}-frontend-${random_id.suffix.hex}"
  force_destroy = true
  tags          = { Name = "${var.project_name}-frontend" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  index_document { suffix = "index.html" }
  error_document { key = "index.html" }
}

resource "aws_s3_bucket_policy" "frontend_public" {
  bucket = aws_s3_bucket.frontend.id
  depends_on = [aws_s3_bucket_public_access_block.frontend]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadGetObject"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })
}

# Assets/uploads bucket (pre-signed URL uploads)
resource "aws_s3_bucket" "assets" {
  bucket        = "${var.project_name}-assets-${random_id.suffix.hex}"
  force_destroy = true
  tags          = { Name = "${var.project_name}-assets" }
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    id     = "cleanup-old-versions"
    status = "Enabled"
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# =============================================
# CLOUDFRONT for frontend S3
# =============================================
resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.project_name} frontend CDN"
  price_class         = "PriceClass_100" # cheapest - US/Europe only

  origin {
    domain_name = aws_s3_bucket_website_configuration.frontend.website_endpoint
    origin_id   = "s3-frontend"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${var.project_name}-cf" }
}

# =============================================
# EC2 (t3.micro - FREE TIER) for Backend
# =============================================
data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "deployer" {
  key_name   = "${var.project_name}-key"
  public_key = var.ec2_public_key
}

resource "aws_iam_role" "ec2_role" {
  name = "${var.project_name}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ec2_policy" {
  name = "${var.project_name}-ec2-policy"
  role = aws_iam_role.ec2_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # S3 access - assets bucket only (least privilege)
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.assets.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.assets.arn
      },
      {
        # CloudWatch logs - for app logging
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # Cognito - verify tokens
        Effect   = "Allow"
        Action   = ["cognito-idp:GetUser", "cognito-idp:AdminGetUser"]
        Resource = aws_cognito_user_pool.main.arn
      },
      {
        # SSM Parameter Store - read secrets (instead of hardcoding)
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project_name}/*"
      },
      {
        # DynamoDB - watchlist table only (least privilege)
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.watchlist.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "ec2_profile" {
  name = "${var.project_name}-ec2-profile"
  role = aws_iam_role.ec2_role.name
}

resource "aws_instance" "backend" {
  ami                    = data.aws_ami.amazon_linux_2023.id
  instance_type          = "t3.micro"   # Free tier eligible (750 hrs/month)
  credit_specification {
    cpu_credits = "standard"            # ADD THIS - prevents Unlimited mode charges
  }
  key_name               = aws_key_pair.deployer.key_name
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.ec2_backend.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  # Stop instance when not in use to save free tier hours
  # 750 hrs/month = ~31 days continuous, so you're safe if running 1 instance
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20   # 30 GB free, we use 20 to be safe
    delete_on_termination = true
    encrypted             = true
  }

  user_data = base64encode(templatefile("${path.module}/userdata.sh", {
    project_name    = var.project_name
    aws_region      = var.aws_region
    assets_bucket   = aws_s3_bucket.assets.id
    cognito_pool_id = aws_cognito_user_pool.main.id
    cognito_client  = aws_cognito_user_pool_client.main.id
  }))

  tags = { Name = "${var.project_name}-backend" }
}

# Elastic IP (free when attached to running instance)
resource "aws_eip" "backend" {
  instance = aws_instance.backend.id
  domain   = "vpc"
  tags     = { Name = "${var.project_name}-eip" }
}

# =============================================
# CLOUDWATCH - Monitoring & Alarms
# =============================================
resource "aws_cloudwatch_log_group" "app_logs" {
  name              = "/movie-rec/application"
  retention_in_days = 7   # Keep logs 7 days only - save storage costs
  tags              = { Name = "${var.project_name}-logs" }
}

resource "aws_cloudwatch_log_group" "access_logs" {
  name              = "/movie-rec/access"
  retention_in_days = 3
  tags              = { Name = "${var.project_name}-access-logs" }
}

# CPU Alarm for EC2
resource "aws_cloudwatch_metric_alarm" "ec2_cpu_high" {
  alarm_name          = "${var.project_name}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "EC2 CPU > 80% for 10 minutes"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  dimensions = {
    InstanceId = aws_instance.backend.id
  }
}

# Instance Status Check Alarm
resource "aws_cloudwatch_metric_alarm" "ec2_status" {
  alarm_name          = "${var.project_name}-status-check"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "EC2 instance status check failed"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  dimensions = {
    InstanceId = aws_instance.backend.id
  }
}

# Budget Alarm (CRITICAL for free tier)
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly-budget"
  budget_type  = "COST"
  limit_amount = "5"   # Alert at $5 to stay free
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# =============================================
# SNS TOPIC for alerts
# =============================================
resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
  tags = { Name = "${var.project_name}-sns" }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# =============================================
# SSM PARAMETER STORE (store secrets safely)
# =============================================
resource "aws_ssm_parameter" "tmdb_api_key" {
  name  = "/${var.project_name}/TMDB_API_KEY"
  type  = "SecureString"
  value = var.tmdb_api_key
  tags  = { Name = "${var.project_name}-tmdb-key" }
}

resource "aws_ssm_parameter" "cognito_pool_id" {
  name  = "/${var.project_name}/COGNITO_POOL_ID"
  type  = "String"
  value = aws_cognito_user_pool.main.id
  tags  = { Name = "${var.project_name}-cognito-pool-id" }
}

resource "aws_ssm_parameter" "assets_bucket" {
  name  = "/${var.project_name}/ASSETS_BUCKET"
  type  = "String"
  value = aws_s3_bucket.assets.id
  tags  = { Name = "${var.project_name}-assets-bucket" }
}

# =============================================
# RANDOM ID for unique bucket names
# =============================================
resource "random_id" "suffix" {
  byte_length = 4
}


# =============================================
# DYNAMODB - Watchlist (always free tier)
# =============================================
resource "aws_dynamodb_table" "watchlist" {
  name         = "movie-watchlist"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "movie_id"

  attribute {
    name = "user_id"
    type = "S"
  }
  attribute {
    name = "movie_id"
    type = "S"
  }

  tags = { Name = "${var.project_name}-watchlist" }
}