# upload-certificate

批量把证书上传到 Octopus Deploy（CRM Space）。只依赖 Python 3 标准库 + 本机 `openssl`。

```bash
cp .env.example .env          # 填 PFX_PASSWORD 和 OCTOPUS_API_KEY
python3 upload_certs.py tmgm-domain-20261028 --tenant TMGM --check      # 1. 体检
python3 upload_certs.py tmgm-domain-20261028 --tenant TMGM --dry-run    # 2. 看计划
python3 upload_certs.py tmgm-domain-20261028 --tenant TMGM              # 3. 真跑
```

## 参数一览

| 参数 | 作用 | 例子 |
| --- | --- | --- |
| `dir` | 证书目录，平铺 pfx 或按域名分文件夹 | `python3 upload_certs.py ./certs` |
| `--manifest` `-m` | 用 CSV 清单代替目录，逐张指定（见 `certs.csv.example`） | `-m certs.csv` |
| `--check` | 只体检不上传，每张一行，有异常退出码非 0 | `--check --check-out check.csv` |
| `--check-out` | 体检结果另存 CSV（带 BOM，Excel 不乱码） | `--check-out check.csv` |
| `--warn-days` | 体检时剩余天数少于此值算异常，默认 30 | `--warn-days 15` |
| `--dry-run` | 只打印计划，不写 Octopus，不写日志 | `--dry-run` |
| `--env` | 新建证书的环境，可重复，默认 PROD | `--env DEV --env QA` |
| `--tenant` | 新建证书的 Tenant，可重复 | `--tenant TMGM` |
| `--tenant-tag` | Tenant Tag；默认按环境自动加 `Environment Tags/<ENV>`，传 `''` 不加 | `--tenant-tag 'CRM-Tag/Web'` |
| `--name-template` | 域名文件夹模式下的证书名，默认 `star_{domain_us}` | `--name-template 'le_{domain_us}'` |
| `--notes` | 新建证书的 Notes，`auto` 自动生成，也可用模板 | `--notes auto` |
| `--password` | PFX 密码，默认读 `.env` 的 `PFX_PASSWORD` | `--password xxx` |
| `--on-exists` | 同名已存在时：`replace`（默认）/ `skip` / `fail` | `--on-exists skip` |
| `--replace-only` | 只续期，不存在的名字报失败不新建 | `--replace-only` |
| `--force-replace` | 域名不一致也强制 replace（默认拒绝） | `--force-replace` |
| `--write-pfx` | 从 pem 打包的 pfx 写回域名文件夹（默认放临时目录，退出即删） | `--write-pfx` |
| `--log` | 操作日志路径，默认 `logs/upload.log` | `--log /share/upload.log` |
| `--no-log` | 不写操作日志 | `--no-log` |
| `--env-file` | 配置文件，默认 `.env`；多个 Octopus 一人一个 | `--env-file .env.prod` |
| `--octopus-url` | 临时覆盖 `OCTOPUS_URL` | `--octopus-url https://...` |
| `--space` | 临时覆盖 `OCTOPUS_SPACE` | `--space Spaces-82` |
| `--no-verify` | 跳过本机 openssl 预检 | `--no-verify` |

`.env` 变量：`PFX_PASSWORD`、`OCTOPUS_API_KEY`（需 Certificate Create/Edit 权限）、`OCTOPUS_URL`（默认 crm-nonprod）、`OCTOPUS_SPACE`（默认 Spaces-82）、`OCTOPUS_CA_BUNDLE`（选填）。

## 目录格式

**按域名分文件夹**（certbot 导出那种，推荐）：

```txt
tmgm-domain-20261028/
  tmxgm.com/        cert.pem chain.pem fullchain.pem privkey.pem [fullchain.pfx]
  tmgm-asia.com/    ...
```

- 证书名 = `star_<域名，点换下划线>`，`tmxgm.com` → `star_tmxgm_com`，和 Space-82 现有命名一致
- 有 pfx 直接用，没有就用 `fullchain.pem + privkey.pem` 现场打包；老式 RC2 加密的 pfx 自动兼容

**平铺 pfx**：`certs/star_tmxgm_com.pfx`，证书名 = 文件名去后缀。

**CSV 清单**：列 `file,name,environments,tenants,tenant_tags,password,notes`，多值用 `;`，留空走默认，`tenant_tags` 填 `-` 表示不加。

### 从 certbot 拷证书

`live/` 下的 pem 都是软链接，只拷 `live/` 会全部失效（`ls` 看得到、`cat` 读不到）。用 `-L` 跟随链接：

```bash
cp -rL /etc/letsencrypt/live/ ./tmgm-domain-$(date +%Y%m%d)/
# 或 rsync -aL server:/etc/letsencrypt/live/ ./tmgm-domain-$(date +%Y%m%d)/
```

拷完跑 `--check`，`files` 列出现"软链接失效"就是没拷对。

## 同名证书已存在时

| 情况 | 动作 |
| --- | --- |
| 指纹相同 | 跳过 |
| 指纹不同、域名一致（续期） | `replace`：**ID 不变**，环境 / Tenant / Tag / Notes 沿用 Octopus 现有的，旧证书自动归档 |
| 指纹不同、域名不一致 | 拒绝并报失败，防止文件放错顶掉别的域名；确认无误加 `--force-replace` |

续期时命令行的 `--env` `--tenant` `--notes` 不起作用，只对新建生效。

## `--check` 查什么

files（pem / pfx 齐不齐、软链接是否失效）、pfx_pw（密码对不对）、key_match（私钥和证书是否一对）、chain（能否验到根证书）、domain_ok（CN / SAN 是否含文件夹域名）、days_left、octopus（同名证书现状，或同指纹是否已用别的名字存在）、action（真跑会 create / replace / skip-same / REFUSE）。

## 操作日志

真正改动 Octopus 的每张证书追加一行到 `logs/upload.log`，`--dry-run` / `--check` 不写，`logs/` 不提交：

```txt
2026-10-28 15:30:35  create   star_tm-fxzho_com  Certificates-1334  octopus=crm-nonprod-octopus.lifebyte.io space=Spaces-82 batch=tmgm-domain-20261028 file=tm-fxzho.com.pfx thumb=7B9E…3D exp=2027-01-25 env=PROD tenants=TMGM tags=Environment Tags/PROD
2026-10-28 15:30:35  replace  star_tmxgm_com     Certificates-890   octopus=crm-nonprod-octopus.lifebyte.io space=Spaces-82 batch=tmgm-domain-20261028 file=tmxgm.com.pfx old_thumb=2C4F…A1 new_thumb=7B9E…3D old_exp=2040-04-04 new_exp=2027-01-25
```

replace 记下旧指纹和旧到期日，要回退时拿 `old_thumb` 去 Octopus 归档列表（勾 Include archived）找那张，导出后再 replace 回去。

## Notes

默认空。`--notes auto` 生成：

```txt
Let's Encrypt 通配符 | *.tm-fxzho.com, tm-fxzho.com | 批次 tmgm-domain-20261028 | 2026-10-28 upload_certs.py 上传
```

自定义模板占位符：`{issuer}` `{domains}` `{wildcard}` `{batch}` `{date}`。故意不放到期日，Octopus 自己会显示，续期后写在 Notes 里反而是错的。

## 多个 Octopus 实例

一个实例一个配置文件：

```bash
cp .env.example .env.prod      # 正式 Octopus 的地址、Space、API Key
python3 upload_certs.py ./certs --tenant TMGM --env-file .env.prod --dry-run
```

## 注意

- `.env`、`.env.*`、`*.pfx`、`*.pem`、证书目录、`logs/` 都在 `.gitignore`，不要提交
- 末尾打汇总表，有失败退出码非 0
- 从 pem 临时打包的 pfx 含私钥，放系统临时目录，脚本退出时自动删除
