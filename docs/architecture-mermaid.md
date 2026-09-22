flowchart LR
  subgraph OPS["Ops / Monitoring account"]
    CTRL["Ansible controller<br/>(EC2 runner)<br/>instance role, no static keys"]
    WK["Enrollment worker<br/>(on the controller)"]
    BUS["Central event bus"]
    Q["SQS queue + DLQ"]
    SINK["CloudWatch OAM Sink<br/>(central metrics view)"]
    DASH["Dashboard: fleet-disk-utilization"]
    SNS["SNS: disk-alerts"]
    CHAT["Slack / PagerDuty / Email"]
    BUS --> Q --> WK
    WK -- "enroll.yml -l new VM" --> CTRL
    SINK --> DASH
    SNS --> CHAT
  end

  subgraph ORG["AWS Organizations (StackSets, auto-deploy to new accounts)"]
    direction TB
    subgraph A1["Member account 1..N"]
      ROLE["AnsibleOpsRole<br/>(least privilege)"]
      DHMC["SSM Default Host Mgmt"]
      LINK["OAM Link"]
      EVR["EventBridge rule<br/>VM started"]
      ALARM["CloudWatch Alarms<br/>80% / 90% / fill-rate"]
      subgraph VPC["Private subnets - no inbound ports, no SSH"]
        VM1["EC2 VM<br/>CloudWatch agent"]
        VM2["EC2 VM<br/>CloudWatch agent"]
      end
      VM1 -- "disk_used_percent (push, 60s)" --> ALARM
      VM2 --> ALARM
      VM1 -. "state: running" .-> EVR
    end
  end

  EVR -- "event" --> BUS
  CTRL -- "1 sts:AssumeRole (org-conditioned)" --> ROLE
  CTRL -- "2 Discover: aws_ec2 dynamic inventory" --> VM1
  CTRL -- "3 Configure: SSM Session Manager (aws_ssm plugin)" --> VM1
  CTRL -. "3" .-> VM2
  CTRL -- "4 create/update alarms" --> ALARM
  LINK -- "metrics shared" --> SINK
  ALARM -- "notify" --> SNS