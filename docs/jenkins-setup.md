# Jenkins 接入指南

平台提供 `jenkins/Jenkinsfile`，可在 Jenkins 上实现参数化发布。

## 前置条件（Jenkins Agent）

- Python 3 + pip（安装 `requirements.txt` 依赖）
- Docker（本地构建镜像 / 直传模式 docker save）
- Maven + JDK（或已在 `config/deploy.yaml` 配置 build.maven_home / java_home）
- 可访问所有目标服务器的 SSH（22 端口）
- 平台仓库检出路径固定（如 `/data/jenkins/workspace/agencys-deploy`），
  因为 `deploy.project_root`、`build.maven_home` 等配置为绝对路径

## 凭据（Jenkins Credentials）

在 Jenkins → Manage Jenkins → Credentials → Global 创建以下 **Secret text**：

| ID | 用途 |
|----|------|
| `xxx-ssh-password` | SSH 密码（对应 `config/deploy.yaml` 的 ssh.password；密钥认证场景可跳过） |
| `xxx-feishu-webhook` | 飞书机器人 Webhook（可选，通知用） |

按 `scripts/common.py` 的环境变量规范，需要时补充：
`REGISTRY_USERNAME / REGISTRY_PASSWORD`（push 模式）、`NACOS_USERNAME / NACOS_PASSWORD`、
`MYSQL_USERNAME / MYSQL_PASSWORD`。凭据 ID 与 Jenkinsfile 中 `credentials('...')` 名称一致。

## 创建流水线任务

1. Jenkins → New Item → Pipeline
2. 配置 → Pipeline → Definition: `Pipeline script from SCM`
   - SCM: Git，Repository URL = 平台仓库地址（含凭据）
   - Script Path: `jenkins/Jenkinsfile`
3. 保存后点击「Build with Parameters」选择参数发布

## 参数说明

| 参数 | 说明 |
|------|------|
| DEPLOY_MODE | 单服务（deploy.py）或流水线（deploy_pipeline.py） |
| PROJECT / PIPELINE | 目标项目/流水线名 |
| AUTO_VERSION | 勾选后自动查询服务器当前版本（--auto-version），VERSION/PREVIOUS_VERSION 忽略 |
| VERSION / PREVIOUS_VERSION | 手动指定版本号（不勾选 AUTO_VERSION 时生效） |
| ENV | prod / test，决定加载 deploy.{env}.yaml 与 servers.{env}.yaml |
| SKIP_BUILD / CLEAR_SUMMARY | 透传给部署脚本 |

## 动态项目列表

Jenkinsfile 中的 PROJECT / PIPELINE choices 为静态维护列表（更新需改 Jenkinsfile）。
如需完全动态，安装 [Active Choices Plugin](https://plugins.jenkins.io/uno-choice/)，
将 PROJECT 改为 Active Choices Parameter，Groovy 脚本读取 `config/projects.yaml`：

```groovy
def text = new File('config/projects.yaml').text
// 解析 yaml 需要插件或调用 python 输出列表
return ['xxx', 'xxx'] // 示例
```

## 回滚

在 Jenkins 上执行回滚的两种方式：

1. **全量回滚任务**：构建参数填 DEPLOY_MODE、PROJECT/PIPELINE、ENV，
   VERSION=当前版本、PREVIOUS_VERSION=目标回滚版本，然后在 Jenkinsfile 的
   Deploy 命令中临时追加 `--rollback`（或在 Agent 上直接执行）：
   ```bash
   python deploy.py --project xxx --rollback --version 1.0.30 --previous-version 1.0.29 --env prod
   python deploy_pipeline.py --pipeline xxx --rollback --version 1.0.30 --previous-version 1.0.29 --env prod
   ```

2. **CLI 回滚**：在 Agent 终端直接执行上述命令。

## 常见问题

- **Maven 找不到**：确认 Agent 上配置了 MAVEN_HOME，或在 deploy.yaml 显式指定 build.maven_home
- **SSH 认证失败**：确认 xxx-ssh-password 凭据已创建，或改用密钥认证（key_file 配置在服务器挂载路径）
- **直传模式慢**：镜像传输是网络瓶颈，可先用 `--build-only` + `--push-only` 预热，再发布
