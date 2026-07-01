# CortexI (副脑一代) — 基础设施清单 (2026-07-01)

## 账号/区域
- AWS Account: <AWS_ACCOUNT_ID>
- Region: us-east-2

## 资源
- CC/服务 EC2: <EC2_INSTANCE_ID> (私网 <EC2_PRIVATE_IP>, 公网 <EC2_PUBLIC_IP> 暂留)
  - 服务: systemd meeting-copilot (uvicorn app:app :8000), 开机自启
  - 代码: /opt/meeting-copilot/  数据: /home/ec2-user/meeting-copilot-data/
- ALB: mc-alb (<ALB_DNS>)
  - Listener :80 默认 403, 仅 X-Origin-Verify 匹配才转发
  - SG mc-alb-sg (<SG_ID>): 仅放行 CloudFront 前缀列表 pl-b6a144df:80
- Target Group: mc-tg (HTTP:8000, health /health) → healthy
- CC 实例 SG <SG_ID>: 加了 8000<-mc-alb-sg
- CloudFront: <CF_DIST_ID> → https://<YOUR_CLOUDFRONT_DOMAIN>.cloudfront.net
  - Origin=ALB(http-only), 注入 X-Origin-Verify 头, CachingDisabled, AllViewer
  - OriginReadTimeout=60s → 故 ask/summarize 走异步 job 轮询

## 密钥 (敏感)
- APP_TOKEN: 见 /opt systemd 环境 + mac-app/config.json
- X-ORIGIN-VERIFY: CloudFront custom header ↔ ALB rule (systemd env)

## 三道锁
1. ALB SG 仅 CloudFront IP 段可达 (实测外部 curl=000 连不上)
2. X-Origin-Verify 密钥头, 不匹配→403
3. 应用层 Bearer APP_TOKEN (实测无 token=401)

## 拆除 (如需)
aws cloudfront delete-distribution --id <CF_DIST_ID> (需先 disable)
aws elbv2 delete-load-balancer --load-balancer-arn ...(mc-alb)
aws elbv2 delete-target-group --target-group-arn ...(mc-tg)
aws ec2 delete-security-group --group-id <SG_ID>
CC SG 移除 8000 入站; ssh停 systemctl disable --now meeting-copilot
