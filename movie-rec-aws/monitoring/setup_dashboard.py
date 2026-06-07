"""
CloudWatch Dashboard Setup Script
Run once after Terraform apply to create monitoring dashboards.
Usage: python setup_dashboard.py --instance-id i-xxxx --region us-east-1
"""

import boto3
import json
import argparse

def create_dashboard(instance_id: str, region: str = "us-east-1"):
    cw = boto3.client("cloudwatch", region_name=region)

    dashboard_body = {
        "widgets": [
            # Row 1: EC2 CPU & Memory
            {
                "type": "metric",
                "x": 0, "y": 0, "width": 12, "height": 6,
                "properties": {
                    "title": "EC2 CPU Utilization",
                    "metrics": [["AWS/EC2", "CPUUtilization", "InstanceId", instance_id]],
                    "period": 300,
                    "stat": "Average",
                    "region": region,
                    "view": "timeSeries",
                    "yAxis": {"left": {"min": 0, "max": 100}},
                }
            },
            {
                "type": "metric",
                "x": 12, "y": 0, "width": 12, "height": 6,
                "properties": {
                    "title": "Memory Used % (via CW Agent)",
                    "metrics": [["MovieRec/EC2", "mem_used_percent"]],
                    "period": 60,
                    "stat": "Average",
                    "region": region,
                    "view": "timeSeries",
                }
            },
            # Row 2: Network & Status
            {
                "type": "metric",
                "x": 0, "y": 6, "width": 12, "height": 6,
                "properties": {
                    "title": "EC2 Network I/O",
                    "metrics": [
                        ["AWS/EC2", "NetworkIn",  "InstanceId", instance_id],
                        ["AWS/EC2", "NetworkOut", "InstanceId", instance_id],
                    ],
                    "period": 300,
                    "stat": "Sum",
                    "region": region,
                    "view": "timeSeries",
                }
            },
            {
                "type": "metric",
                "x": 12, "y": 6, "width": 12, "height": 6,
                "properties": {
                    "title": "EC2 Status Check",
                    "metrics": [
                        ["AWS/EC2", "StatusCheckFailed",         "InstanceId", instance_id],
                        ["AWS/EC2", "StatusCheckFailed_Instance","InstanceId", instance_id],
                        ["AWS/EC2", "StatusCheckFailed_System",  "InstanceId", instance_id],
                    ],
                    "period": 60,
                    "stat": "Maximum",
                    "region": region,
                    "view": "timeSeries",
                }
            },
            # Row 3: Disk & S3
            {
                "type": "metric",
                "x": 0, "y": 12, "width": 12, "height": 6,
                "properties": {
                    "title": "Disk Used %",
                    "metrics": [["MovieRec/EC2", "disk_used_percent", "path", "/", "fstype", "xfs"]],
                    "period": 300,
                    "stat": "Average",
                    "region": region,
                    "view": "timeSeries",
                }
            },
            # Row 4: Log insights widget
            {
                "type": "log",
                "x": 0, "y": 18, "width": 24, "height": 6,
                "properties": {
                    "title": "Application Errors (last 1h)",
                    "query": "SOURCE '/movie-rec/application' | fields @timestamp, @message | filter @message like /ERROR|Exception|CRITICAL/ | sort @timestamp desc | limit 50",
                    "region": region,
                    "view": "table",
                }
            },
            # Alarm status widget
            {
                "type": "alarm",
                "x": 0, "y": 24, "width": 24, "height": 4,
                "properties": {
                    "title": "Active Alarms",
                    "alarms": [
                        f"arn:aws:cloudwatch:{region}:*:alarm:movie-rec-cpu-high",
                        f"arn:aws:cloudwatch:{region}:*:alarm:movie-rec-status-check",
                    ]
                }
            },
        ]
    }

    cw.put_dashboard(
        DashboardName="MovieRec-Overview",
        DashboardBody=json.dumps(dashboard_body),
    )
    print(f"✅ Dashboard created: MovieRec-Overview")
    print(f"   View at: https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#dashboards:name=MovieRec-Overview")

def create_log_metric_filters(region: str = "us-east-1"):
    """Create metric filters for application log monitoring."""
    cw = boto3.client("cloudwatch", region_name=region)
    logs = boto3.client("logs", region_name=region)

    filters = [
        {
            "name": "APIErrors",
            "log_group": "/movie-rec/application",
            "pattern": "[timestamp, level=ERROR*, ...]",
            "metric_name": "APIErrorCount",
            "metric_namespace": "MovieRec/App",
            "metric_value": "1",
        },
        {
            "name": "TFIDFRequests",
            "log_group": "/movie-rec/application",
            "pattern": '[timestamp, ..., msg="*tfidf*" || msg="*recommend*"]',
            "metric_name": "RecommendationRequests",
            "metric_namespace": "MovieRec/App",
            "metric_value": "1",
        },
    ]

    for f in filters:
        try:
            logs.put_metric_filter(
                logGroupName=f["log_group"],
                filterName=f["name"],
                filterPattern=f["pattern"],
                metricTransformations=[{
                    "metricName":      f["metric_name"],
                    "metricNamespace": f["metric_namespace"],
                    "metricValue":     f["metric_value"],
                    "defaultValue":    0,
                }]
            )
            print(f"✅ Metric filter created: {f['name']}")
        except Exception as e:
            print(f"⚠️  Filter {f['name']} error (may already exist): {e}")

    # Alarm on API errors
    try:
        cw.put_metric_alarm(
            AlarmName="movie-rec-api-errors",
            AlarmDescription="Too many API errors",
            MetricName="APIErrorCount",
            Namespace="MovieRec/App",
            Statistic="Sum",
            Period=300,
            EvaluationPeriods=2,
            Threshold=10,
            ComparisonOperator="GreaterThanThreshold",
            TreatMissingData="notBreaching",
        )
        print("✅ API error alarm created")
    except Exception as e:
        print(f"⚠️  Alarm error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup CloudWatch dashboard")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID (i-xxxxxxxx)")
    parser.add_argument("--region",      default="us-east-1", help="AWS region")
    args = parser.parse_args()

    print(f"Setting up CloudWatch for instance {args.instance_id} in {args.region}")
    create_dashboard(args.instance_id, args.region)
    create_log_metric_filters(args.region)
    print("\nDone! Open AWS Console -> CloudWatch -> Dashboards -> MovieRec-Overview")
