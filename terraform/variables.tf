variable "aws_region" {
  description = "Region for all resources."
  type        = string
  default     = "eu-north-1"
}

variable "project_name" {
  description = "Name prefix applied to every resource and to the default_tags Project tag."
  type        = string
  default     = "api-gateway"
}

variable "instance_type" {
  description = "EC2 instance type. t3.micro is the free-tier eligible size."
  type        = string
  default     = "t3.micro"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size. 8 GiB is the AMI default and fills up fast once Docker images accumulate."
  type        = number
  default     = 16
}

variable "ssh_public_key_path" {
  description = "Path to the public half of the SSH keypair used for deploys. Only the public key is read; the private key never touches Terraform."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "allowed_ssh_cidr" {
  description = "CIDR block permitted on port 22. Set this to your own address as a /32."
  type        = string

  validation {
    condition     = var.allowed_ssh_cidr != "0.0.0.0/0"
    error_message = "Refusing to open SSH to the whole internet. Use your own address as a /32, e.g. 203.0.113.7/32."
  }
}

variable "gateway_port" {
  description = "Port the gateway listens on. Exposed publicly."
  type        = number
  default     = 8000
}

variable "alert_email" {
  description = "Address that receives budget notifications and CloudWatch alarms. The SNS subscription must be confirmed by clicking the link in the email AWS sends."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly spend ceiling that triggers a budget notification."
  type        = number
  default     = 5
}

variable "cpu_alarm_threshold" {
  description = "Average CPU percentage that, sustained across the evaluation window, stops the instance."
  type        = number
  default     = 80
}

variable "cpu_alarm_evaluation_periods" {
  description = "Number of consecutive 5-minute periods above the threshold before the stop action fires. 3 = 15 minutes sustained, which a t3.micro burst will not trip."
  type        = number
  default     = 3
}
