# CortexI 服务端部署（一键 CloudFormation）

CortexI 服务端 = 私有 EC2 跑 FastAPI，前面挂 ALB（只允许 CloudFront）+ CloudFront。
分析大脑 = EC2 上的 Claude Code（headless）走 Amazon Bedrock。

## 前置条件

1. 一个 AWS 账号，已在目标区域**开通 Bedrock 的 Claude 模型访问**（Bedrock 控制台 → Model access）。
2. 一个 VPC + 至少 2 个跨可用区的子网（给 ALB）。默认 VPC 即可。
3. AWS CLI 已配置。

## 查你区域的 CloudFront 前缀列表 ID

ALB 安全组用它来「只放行 CloudFront」。查一下（大多数区域是 `pl-b6a144df`）：

```bash
aws ec2 describe-managed-prefix-lists \
  --filters "Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing" \
  --query 'PrefixLists[0].PrefixListId' --output text --region <你的区域>
```

## 一键部署

```bash
aws cloudformation deploy \
  --template-file cloudformation.yaml \
  --stack-name cortexi \
  --capabilities CAPABILITY_IAM \
  --region us-east-2 \
  --parameter-overrides \
      VpcId=vpc-xxxx \
      SubnetIds=subnet-a,subnet-b \
      ServerSubnetId=subnet-a \
      CloudFrontPrefixList=pl-b6a144df \
      GitHubRepo=CrypticDriver/cortexi \
      BedrockRegion=us-east-2
```

`AppToken` 留空 → 自动生成。部署完拿输出：

```bash
# 客户端要填的 server_url
aws cloudformation describe-stacks --stack-name cortexi --region us-east-2 \
  --query 'Stacks[0].Outputs' --output table

# app token（自动生成的话从 SSM 取）
aws ssm get-parameter --name /cortexi/cortexi/app-token \
  --query Parameter.Value --output text --region us-east-2
```

把 `CloudFrontURL` 填进客户端 `config.json` 的 `server_url`，token 填 `app_token`。

## 安全模型（三道锁）

1. **ALB 安全组**只放行 CloudFront 托管前缀列表（别人直连 ALB 连不上）。
2. **X-Origin-Verify** 共享密钥头：CloudFront 回源时注入，ALB 规则校验，不匹配 → 403。
3. **应用层 Bearer token**：客户端必须带，否则 401。

EC2 在安全组后面，入站只放 ALB 的 8000，出站走 Bedrock/SSM。

## Claude Code + Bedrock 说明

UserData 会 `npm i -g @anthropic-ai/claude-code`，并给 systemd 注入
`CLAUDE_CODE_USE_BEDROCK=1` + `AWS_REGION`。EC2 的 IAM 角色带 `bedrock:InvokeModel`，
所以 CC 走实例角色调 Bedrock，**不需要 API key**。前提是该区域已开通对应 Claude 模型访问。

## 拆除

```bash
aws cloudformation delete-stack --stack-name cortexi --region us-east-2
```
（CloudFront 删除较慢，属正常。）
