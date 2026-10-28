---
name: upload-certs
description: 把一批 SSL 证书（certbot 目录或 pfx）上传 / 续期到 Octopus Deploy，含体检、dry-run、测试、回退流程。用户说"上传证书""续期""传到 octopus""检查证书"时使用。
---

# 上传 / 续期证书到 Octopus

脚本是仓库根目录的 `upload_certs.py`，参数见 `README.md`。先读 `AGENTS.md` 的规矩。

## 标准流程

每一步的输出都要给用户看，不要跳步。

1. **确认目录**：`ls <目录>` 看是平铺 pfx 还是按域名分文件夹。每个文件夹里应有 `fullchain.pem + privkey.pem`，或 `*.pfx`。缺的要告诉用户。
2. **体检**：
   ```bash
   python3 upload_certs.py <目录> --tenant <TENANT> --check --check-out check.csv
   ```
   看最后一行"共 N 张，异常 M 张"。有异常先逐条解释原因再往下走，不要带着异常上传。
   `action` 列是 `replace` 的，说明 Octopus 已有同名证书，要告诉用户哪几张会被顶替、原证书是谁签的、何时到期。
3. **看计划**：
   ```bash
   python3 upload_certs.py <目录> --tenant <TENANT> [--env ENV] [--notes auto] --dry-run
   ```
   把汇总表贴给用户：几张 would-create、几张 would-replace、几张 skip-same。
4. **等用户确认**。用户没说"可以""执行""上传"之类的明确话，不要进入第 5 步。
5. **真跑**：去掉 `--dry-run`，其他参数不变。
6. **收尾**：贴汇总表；`tail logs/upload.log` 给用户看这次写了什么；如果有 FAILED，解释每一条。

## 先在测试环境试

用户想先试时：

```bash
python3 upload_certs.py <目录> --tenant TMGM --env DEV --name-template 'test001_star_{domain_us}' --dry-run
```

名字加 `test001_` 前缀避免和正式证书冲突，`--env DEV` 让它不进 PROD。测完清理：在 Octopus 页面 Library → Certificates 搜 `test001_`，Archive 后 Delete；或 `DELETE /api/Spaces-82/certificates/<ID>`，ID 在 `logs/upload.log` 里。

## 续期（同名已存在）

- 指纹相同自动跳过，不用管。
- 指纹不同、域名一致走 `replace`，ID 不变，环境 / Tenant / Tag / Notes 沿用现有的，命令行的 `--env --tenant --notes` 对它无效。
- 域名不一致会 REFUSE。先让用户确认文件没放错，确认后才加 `--force-replace`。
- 只想续期、怕名字打错变成新建：加 `--replace-only`。

## 回退

- 新建错了：删掉那张证书即可。
- replace 错了：`logs/upload.log` 里那行的 `old_thumb` 是旧证书指纹。Octopus Certificates 页面勾 Include archived 找到它，`GET /api/Spaces-82/certificates/<归档ID>/export?format=Pkcs12&password=...` 导出，再用导出的 pfx replace 回去（加 `--force-replace`）。
- Octopus 里换证书不等于服务器上换了，真正下发靠后续部署。没部署前回退对线上无影响。

## 常见报错

| 现象 | 原因 / 处理 |
| --- | --- |
| `目录不存在: xxx（当前目录下有: ...）` | 目录名打错，按提示改 |
| `缺少 OCTOPUS_API_KEY` | `.env` 没配，或 `--env-file` 指错文件 |
| files 列"软链接失效" | 从 certbot 只拷了 `live/`，重新 `cp -rL` |
| pfx_pw `WRONG` | `.env` 的 `PFX_PASSWORD` 和这张 pfx 不一致 |
| pfx_pw `ok(legacy)` | 正常，老式加密，脚本已兼容 |
| `域名不一致 ... 已拒绝 replace` | 文件夹和 Octopus 里同名证书不是一个域名，核对后决定是否 `--force-replace` |
| `连接 ... 失败` | 网络 / VPN，或 `OCTOPUS_URL` 错 |
