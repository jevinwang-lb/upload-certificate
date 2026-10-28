# upload-certificate

批量把 `.pfx / .p12` 证书上传到 Octopus Deploy（CRM Space）。只依赖 Python 3 标准库，本机有 `openssl` 时会顺带预检密码、读 CN / 到期日。

## 准备

```bash
cp .env.example .env     # 填 PFX_PASSWORD 和 OCTOPUS_API_KEY
```

| 变量                | 说明                                                  |
| ------------------- | ----------------------------------------------------- |
| `PFX_PASSWORD`      | 证书统一密码（清单里可逐张覆盖）                      |
| `OCTOPUS_API_KEY`   | Octopus API Key，需要 Certificate 的 Create/Edit 权限 |
| `OCTOPUS_URL`       | 默认 `https://crm-nonprod-octopus.lifebyte.io`        |
| `OCTOPUS_SPACE`     | 默认 `Spaces-82`（CRM）                               |
| `OCTOPUS_CA_BUNDLE` | 选填，自定义根证书 PEM 路径                           |

## 多个 Octopus 实例 / 多个 Space

一个实例一个配置文件，用 `--env-file` 切换，默认读 `.env`：

```bash
cp .env.example .env.nonprod   # crm-nonprod-octopus，Spaces-82
cp .env.example .env.prod      # 正式 Octopus 的地址、Space 和 API Key

python3 upload_certs.py ./certs --tenant TMGM --env-file .env.nonprod --dry-run
python3 upload_certs.py ./certs --tenant TMGM --env-file .env.prod    --dry-run
```

临时换 Space 或地址也可以直接用 `--space Spaces-xx`、`--octopus-url https://...` 覆盖配置文件里的值。
`.env.*` 已在 `.gitignore` 里，不会被提交。

## 用法

先 `--dry-run` 看计划，确认无误再去掉。

**方式一：一个目录，统一作用域**（证书名 = 文件名去掉后缀）

```bash
python3 upload_certs.py ./certs --tenant TMGM --env PROD --dry-run
python3 upload_certs.py ./certs --tenant TMGM --env PROD
```

**方式二：CSV 清单，逐张指定**（参考 `certs.csv.example`）

```bash
cp certs.csv.example certs.csv   # 编辑
python3 upload_certs.py --manifest certs.csv --dry-run
python3 upload_certs.py --manifest certs.csv
```

清单列：`file,name,environments,tenants,tenant_tags,password,notes`。多值用 `;` 分隔；留空走默认。
`tenant_tags` 不填时自动按环境加 `Environment Tags/<ENV>`，填 `-` 表示不加 tag。

## 上传前先体检：`--check`

只检测不上传，每张证书一行，有任何异常退出码非 0：

```bash
python3 upload_certs.py tmgm-domain-20261028 --tenant TMGM --check --check-out check.csv
```

| 列        | 检查内容                                                            |
| --------- | ------------------------------------------------------------------- |
| files     | 文件夹里 cert / chain / fullchain / privkey / pfx 哪些在            |
| pfx_pw    | pfx 密码对不对（`ok(legacy)` 表示老式 RC2 加密，能用）              |
| key_match | privkey.pem 和证书是否一对                                          |
| chain     | fullchain 能否验证到系统根证书                                      |
| domain_ok | 证书的 CN / SAN 是否包含文件夹名的域名                              |
| days_left | 剩余天数，少于 `--warn-days`（默认 30）算异常，已过期也算           |
| octopus   | 同名证书现状（ID、到期、签发方），或同指纹是否已用别的名字存在      |
| action    | 真跑时会做什么：create / replace / skip-same / REFUSE（域名不一致） |
| problems  | 所有异常汇总，`ok` 表示这张没问题                                   |

另外还会查：pfx 里有没有私钥、pfx 和 cert.pem 是不是同一张、生效时间是否在未来。
`--check-out` 写出的 CSV 带 BOM，Excel 直接打开不乱码。

## Notes

默认空。加 `--notes auto` 自动生成，格式：

```txt
Let's Encrypt 通配符 | *.tm-fxzho.com, tm-fxzho.com | 批次 domains1028 | 2026-10-28 upload_certs.py 上传
```

也可以自定义模板：`--notes "LE for {domains}, batch {batch}"`，占位符有 `{issuer}` `{domains}` `{wildcard}` `{batch}` `{date}`。
清单模式的 `notes` 列同样支持这些占位符。Notes 只在新建时写入，续期不会改。
故意不放到期日：Octopus 自己会显示，写进 Notes 续期后反而变成错的。

## 同名证书已存在时

| 情况                 | 动作                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| 指纹相同             | 跳过                                                                              |
| 指纹不同（续期）     | 调 `replace`：**ID 不变**，引用它的变量 / 部署步骤不用改，旧证书自动归档          |
| 指纹不同、域名不一致 | 拒绝 replace 并报失败，防止文件放错把别的域名顶上去；确认无误加 `--force-replace` |
| `--on-exists skip`   | 跳过                                                                              |
| `--on-exists fail`   | 报错                                                                              |
| `--replace-only`     | 只续期，不存在的名字报失败不新建                                                  |

## 现有命名约定（Space-82）

- 名字：`star_<域名，点换下划线>`，如 `*.tmgm.com` → `star_tmgm_com`
- 作用域：绝大多数是 `PROD` 环境 + 品牌 Tenant（TMGM / DLS / ANZO…）+ tag `Environment Tags/PROD`
- 把 pfx 按这个规则命名放进目录即可，脚本自动套用

## 注意

- `.env`、`certs/`、`*.pfx` 已在 `.gitignore`，不要提交
- 全部失败/成功都会在末尾打汇总表，有失败则退出码非 0
- 续期（replace）只换证书内容和密码，环境 / Tenant / Tag / Notes 一律沿用 Octopus 里已有的，命令行给的 `--env`、`--tenant` 只对新建生效
- 从 pem 临时打包的 pfx 放在系统临时目录，脚本退出时自动删除

## 目录按域名分文件夹（certbot 导出那种）也能直接用

```bash
tmgm-domain-20261028/
  tmxgm.com/        cert.pem chain.pem fullchain.pem privkey.pem [fullchain.pfx]
  tmgm-asia.com/    ...
```

```bash
python3 upload_certs.py tmgm-domain-20261028 --tenant TMGM --dry-run
```

- 证书名按 `--name-template` 生成，默认 `star_{domain_us}`，即 `tmxgm.com` → `star_tmxgm_com`
- 文件夹里有 `fullchain.pfx` 就直接用；没有就用 `fullchain.pem + privkey.pem` 现场打包（AES-256，密码用 `PFX_PASSWORD`）。加 `--write-pfx` 可把打好的 pfx 写回文件夹
- 老式 RC2 加密的 pfx（新版 openssl 要 `-legacy` 才能读）脚本会自动兼容

### 从 certbot 服务器拷证书的正确姿势

`/etc/letsencrypt/live/<域名>/` 里的四个 pem 都是**软链接**，指向 `../../archive/<域名>/xxx1.pem`。
只拷 `live/` 的话链接全部失效，`ls` 看得到、`cat` 读不到，脚本会报"pem 是失效的软链接"。

```bash
# 推荐：-L 跟随软链接，拷出真实文件
cp -rL /etc/letsencrypt/live/ ./tmgm-domain-$(date +%Y%m%d)/
# 或者 rsync
rsync -aL server:/etc/letsencrypt/live/ ./tmgm-domain-$(date +%Y%m%d)/
# 或者连 archive/ 一起拷，保持相对路径结构
```

拷完先跑 `--check`，`files` 列里出现"软链接失效"就是没拷对。
