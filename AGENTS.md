# upload-certificate

批量把 SSL 证书上传到 Octopus Deploy（CRM Space，`Spaces-82`）。只有一个脚本 `upload_certs.py`，参数全在 `README.md` 的"参数一览"。
要上传或续期证书时，按 `.agents/skills/upload-certs/SKILL.md` 的流程走。

## 任何时候都要守的规矩

- 不要 `cat` / 打印 `.env`、`*.pfx`、`privkey.pem`、`logs/` 以外任何含私钥或密码的东西。回答里也不要复述密码和 API Key。
- 真正写入 Octopus（不带 `--dry-run` / `--check`）之前必须先跑 `--check` 和 `--dry-run`，并让用户看过计划点头。不要自己决定真跑。
- `--force-replace` 只在用户明确说"确认替换"时才加。它绕过的是防止把别的域名顶上去的保护。
- 不要加 `--no-verify`，不要给脚本加任何关闭 TLS 校验的开关（Python 3.14 的 strict 校验问题已经在 `make_ssl_context` 里处理）。
- 测试一律在 `--env DEV` 下用带 `test001_` 前缀的名字，测完要删掉。默认作用域 PROD + 品牌 Tenant 是生产用的。
- 证书原始目录（`tmgm-domain-*`、`domains*`）、`.env*`、`logs/` 都在 `.gitignore`，不要提交，也不要改 `.gitignore` 把它们放进来。

## 已知事实

- Space-82 约 320 张证书，全部 Pkcs12，命名 `star_<域名，点换下划线>`，作用域 PROD + 品牌 Tenant（TMGM / DLS / ANZO…）+ tag `Environment Tags/PROD`。
- 9 个域名已有同名的 Cloudflare Origin 证书（到期 2034–2040）：tmg11、tmg288、tmgm-asia、tmgm-sea、tmgmprogo、tmhubxchange、tmxgm、tradedynamix、tradeginvest。用 Let's Encrypt 证书 replace 它们等于把 15 年证书换成 90 天证书，动手前要提醒用户。
- 老 pfx 是 RC2-40 加密，脚本自动用 `openssl -legacy` 回退，不用手动转。
- certbot `live/` 里的 pem 是软链接，拷过来要 `cp -rL`，否则 `--check` 的 files 列会报"软链接失效"。
