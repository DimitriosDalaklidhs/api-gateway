output "instance_id" {
  description = "EC2 instance id."
  value       = aws_instance.gateway.id
}

output "instance_public_ip" {
  description = "Public IPv4 address. Changes on every stop/start — the CloudWatch stop action will move it."
  value       = aws_instance.gateway.public_ip
}

output "gateway_url" {
  description = "Base URL for the gateway."
  value       = "http://${aws_instance.gateway.public_ip}:${var.gateway_port}"
}

output "health_check" {
  description = "Copy-paste smoke test once cloud-init has finished."
  value       = "curl -sf http://${aws_instance.gateway.public_ip}:${var.gateway_port}/admin/health"
}

output "ssh_command" {
  description = "SSH into the host."
  value       = "ssh ubuntu@${aws_instance.gateway.public_ip}"
}

output "ecr_repository_url" {
  description = "Set this as the ECR_URI GitHub Actions secret."
  value       = aws_ecr_repository.this.repository_url
}

output "resolved_ami_id" {
  description = "The AMI the data source picked. Pin this to a literal in main.tf once you have a build you trust."
  value       = data.aws_ami.ubuntu_2404.id
}
