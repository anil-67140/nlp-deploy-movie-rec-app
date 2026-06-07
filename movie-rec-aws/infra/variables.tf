variable "aws_region" {
  description = "AWS region to deploy in"
  type        = string
  default     = "us-east-1"  # Most free-tier resources available here
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "movie-rec"
}

variable "ec2_public_key" {
  description = "Your SSH public key content (run: cat ~/.ssh/id_rsa.pub)"
  type        = string
}

variable "my_ip_cidr" {
  description = "Your IP for SSH access (e.g. 203.0.113.5/32). Get it at: https://checkip.amazonaws.com"
  type        = string
  default     = "0.0.0.0/0"  # Change this to your IP for better security!
}

variable "tmdb_api_key" {
  description = "TMDB API key (from themoviedb.org)"
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Email to receive CloudWatch and budget alerts"
  type        = string
}
