output "backend_public_ip" {
  description = "EC2 Elastic IP - use this for API_BASE in Streamlit"
  value       = aws_eip.backend.public_ip
}

output "backend_url" {
  description = "FastAPI backend URL"
  value       = "http://${aws_eip.backend.public_ip}:8000"
}

output "streamlit_url" {
  description = "Streamlit frontend URL (direct EC2)"
  value       = "http://${aws_eip.backend.public_ip}:8501"
}

output "cloudfront_url" {
  description = "CloudFront distribution URL (React/HTML frontend)"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "frontend_bucket" {
  description = "S3 bucket name for frontend deployment"
  value       = aws_s3_bucket.frontend.id
}

output "assets_bucket" {
  description = "S3 bucket for file uploads (pre-signed URLs)"
  value       = aws_s3_bucket.assets.id
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "Cognito App Client ID"
  value       = aws_cognito_user_pool_client.main.id
}

output "ssh_command" {
  description = "SSH command to connect to backend EC2"
  value       = "ssh -i ~/.ssh/id_rsa ec2-user@${aws_eip.backend.public_ip}"
}
