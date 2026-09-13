# ─────────────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────────────

data "aws_vpc" "default" {
  default = true
}

# most_recent = true means this AMI moves underneath you, exactly like an
# unpinned :slim base image tag. The instance below sets ignore_changes on it
# so a new Canonical release does not silently force a replacement. The
# resolved id is in outputs — pin it to a literal once you have one you trust.
data "aws_ami" "ubuntu_2404" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_security_group" "gateway" {
  name        = "${var.project_name}-sg"
  description = "Ingress for the API gateway host"
  vpc_id      = data.aws_vpc.default.id

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.gateway.id
  description       = "SSH from the operator only"
  cidr_ipv4         = var.allowed_ssh_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "gateway" {
  security_group_id = aws_security_group.gateway.id
  description       = "Gateway HTTP"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = var.gateway_port
  to_port           = var.gateway_port
  ip_protocol       = "tcp"
}

# Port 8010 is deliberately absent. The mock service is reachable only from
# inside the compose network, which is the whole point of putting a gateway in
# front of it. Redis on 6379 is absent for the same reason.

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.gateway.id
  description       = "All outbound"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ─────────────────────────────────────────────────────────────────────────────
# Container registry
# ─────────────────────────────────────────────────────────────────────────────

# MUTABLE because the CD pipeline overwrites the :gateway and :mock tags on
# every push. That is convenient and it is also how a tag stops meaning one
# specific image — the same failure mode as the floating python:3.12-slim tag.
# IMMUTABLE plus a commit-SHA tag is the stricter option.
resource "aws_ecr_repository" "this" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # lets `terraform destroy` remove a non-empty repo

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Instance identity
# ─────────────────────────────────────────────────────────────────────────────

# An instance profile means the box pulls from ECR using temporary credentials
# it fetches from the instance metadata service. No long-lived AWS access keys
# on disk, and nothing to rotate.
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${var.project_name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project_name}-ec2"
  role = aws_iam_role.ec2.name
}

resource "aws_key_pair" "deployer" {
  key_name   = "${var.project_name}-deployer"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# ─────────────────────────────────────────────────────────────────────────────
# Compute
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_instance" "gateway" {
  ami                    = data.aws_ami.ubuntu_2404.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.deployer.key_name
  vpc_security_group_ids = [aws_security_group.gateway.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region = var.aws_region
    ecr_url    = aws_ecr_repository.this.repository_url
  })

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  # IMDSv2 required. Without this, any SSRF in the gateway can read the
  # instance profile credentials with a plain GET.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  lifecycle {
    ignore_changes = [ami]
  }

  tags = {
    Name = "${var.project_name}-host"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Cost protection
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
}

# AWS emails a confirmation link on first apply. Until you click it the
# subscription sits in "pending confirmation" and delivers nothing.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
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

# arn:aws:automate:<region>:ec2:stop is a built-in alarm action. No Lambda, no
# IAM role — CloudWatch stops the instance directly.
resource "aws_cloudwatch_metric_alarm" "cpu_stop" {
  alarm_name          = "${var.project_name}-cpu-high-stop"
  alarm_description   = "Stops the instance after sustained high CPU, as a runaway-cost guard."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = var.cpu_alarm_evaluation_periods
  threshold           = var.cpu_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.gateway.id
  }

  alarm_actions = [
    "arn:aws:automate:${var.aws_region}:ec2:stop",
    aws_sns_topic.alerts.arn,
  ]
}
