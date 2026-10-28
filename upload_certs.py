#!/usr/bin/env python3
"""
批量把 .pfx / .p12 证书上传到 Octopus Deploy（仅依赖 Python 标准库）。

三种用法：
  1) 平铺目录，统一作用域，证书名 = 文件名：
       python3 upload_certs.py ./certs --tenant TMGM --env PROD
  2) 按域名分文件夹的目录（certbot 导出那种，<domain>/fullchain.pfx 或 fullchain.pem+privkey.pem）：
       python3 upload_certs.py ./tmgm-domain-20261028 --tenant TMGM
     证书名由 --name-template 生成，默认 star_{domain_us}（tmxgm.com -> star_tmxgm_com）
  3) CSV 清单，每张证书单独指定：
       python3 upload_certs.py --manifest certs.csv

同名证书已存在时：
  - 指纹相同            -> 跳过
  - 指纹不同、域名一致  -> 调用 replace 续期（ID 不变，环境/Tenant/Tag/Notes 原样保留，旧证书自动归档）
  - 指纹不同、域名不同  -> 报失败，防止文件放错把别的域名顶上去（确认无误可加 --force-replace）
  --on-exists skip|replace|fail 改同名时的行为；--replace-only 只续期不新建。

加 --dry-run 只打印计划，不写入 Octopus。
"""
import argparse
import atexit
import base64
import csv
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://crm-nonprod-octopus.lifebyte.io"
DEFAULT_SPACE = "Spaces-82"
CERT_EXTS = {".pfx", ".p12"}


# ---------------------------------------------------------------- .env / config
def load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# ---------------------------------------------------------------- Octopus client
def make_ssl_context():
    """证书链和主机名照常校验。
    Python 3.13+ 默认额外开了 VERIFY_X509_STRICT，证书链里的 CA 若缺 keyUsage 扩展会直接报
    'CA cert does not include key usage extension'，这里只关掉这个 strict 标志。
    如需自定义根证书，设 OCTOPUS_CA_BUNDLE=/path/to/ca.pem。"""
    bundle = os.environ.get("OCTOPUS_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=bundle or None)
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


class Octopus:
    def __init__(self, url, api_key, space):
        self.base = url.rstrip("/")
        self.key = api_key
        self.space = space
        self.ctx = make_ssl_context()

    def _req(self, method, path, body=None):
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Octopus-ApiKey", self.key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120, context=self.ctx) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.URLError as e:
            if not isinstance(e, urllib.error.HTTPError):
                raise RuntimeError(f"连接 {url} 失败: {e.reason}") from None
            txt = e.read().decode(errors="replace")
            try:
                j = json.loads(txt)
                msg = j.get("ErrorMessage", "")
                errs = j.get("Errors") or []
                detail = "; ".join([msg] + [str(x) for x in errs]).strip("; ")
            except Exception:
                detail = txt[:500]
            raise RuntimeError(f"HTTP {e.code} {method} {path}: {detail}") from None

    def get_all(self, path):
        items = []
        url = f"/api/{self.space}/{path}?take=200"
        while url:
            page = self._req("GET", url)
            items.extend(page.get("Items", []))
            nxt = (page.get("Links") or {}).get("Page.Next")
            url = nxt if nxt else None
        return items

    def environments(self):
        return self.get_all("environments")

    def tenants(self):
        return self.get_all("tenants")

    def tagsets(self):
        return self.get_all("tagsets")

    def certificates(self):
        return self.get_all("certificates")

    def create_certificate(self, payload):
        return self._req("POST", f"/api/{self.space}/certificates", payload)

    def replace_certificate(self, cert_id, b64, password):
        return self._req(
            "POST",
            f"/api/{self.space}/certificates/{cert_id}/replace",
            {"CertificateData": b64, "Password": password or ""},
        )


# ---------------------------------------------------------------- helpers
def name_from_file(path: Path) -> str:
    return path.stem


def norm_domain(d: str) -> str:
    """'*.tmxgm.com' / 'DNS Name=tmxgm.com' -> 'tmxgm.com'，用于新旧证书域名比对。"""
    d = d.strip().replace("DNS Name=", "").lower()
    return d[2:] if d.startswith("*.") else d


def dangling_links(folder: Path):
    """返回文件夹里失效的软链接 [(文件名, 指向), ...]。certbot live/ 目录单独拷出来就会这样。"""
    out = []
    for f in folder.iterdir():
        if f.is_symlink() and not f.exists():
            out.append((f.name, os.readlink(f)))
    return out


def octopus_cert_domains(c: dict) -> set:
    out = set()
    for s in c.get("SubjectAlternativeNames") or []:
        if "DNS" in s:
            out.add(norm_domain(s))
    cn = (c.get("SubjectCommonName") or "").strip()
    if cn and "." in cn and " " not in cn:   # 排掉 'CloudFlare Origin Certificate' 这类非域名 CN
        out.add(norm_domain(cn))
    return out


def split_list(s):
    if not s:
        return []
    return [x.strip() for x in re.split(r"[;,|]", s) if x.strip()]


def openssl_info(path: Path, password: str):
    """用本机 openssl 预检密码并读出 CN / 到期日。没有 openssl 或失败就返回 None（不中断）。"""
    exe = shutil.which("openssl")
    if not exe:
        return None
    env = dict(os.environ, PFXPW=password or "")
    for extra in ([], ["-legacy"]):
        try:
            r = subprocess.run(
                [exe, "pkcs12", "-in", str(path), "-nokeys", "-clcerts", "-passin", "env:PFXPW", *extra],
                capture_output=True, text=True, timeout=30, env=env,
            )
        except Exception:
            return None
        if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
            r2 = subprocess.run(
                [exe, "x509", "-noout", "-subject", "-issuer", "-enddate", "-ext", "subjectAltName"],
                input=r.stdout, capture_output=True, text=True, timeout=30,
            )
            info = {"ok": True, "subject": "", "notAfter": "", "domains": set(), "issuer": "", "wildcard": False}
            for line in r2.stdout.splitlines():
                if line.startswith("issuer="):
                    m = re.search(r"O\s*=\s*([^,/]+)", line)
                    info["issuer"] = (m.group(1).strip() if m else line[7:].strip()).replace("\\,", ",")
                elif "DNS:*." in line:
                    info["wildcard"] = True
                if line.startswith("subject="):
                    info["subject"] = line[len("subject="):].strip()
                    m = re.search(r"CN\s*=\s*([^,/]+)", info["subject"])
                    if m:
                        info["domains"].add(norm_domain(m.group(1)))
                elif line.startswith("notAfter="):
                    info["notAfter"] = line[len("notAfter="):].strip()
                elif "DNS:" in line:
                    for d in re.findall(r"DNS:([^,\s]+)", line):
                        info["domains"].add(norm_domain(d))
            return info
        if "invalid password" in (r.stderr or "").lower() or "mac verify failure" in (r.stderr or "").lower():
            return {"ok": False, "error": "密码错误 (MAC verify failure)"}
    return None


class Resolver:
    def __init__(self, octo: Octopus):
        self.env_by_name = {e["Name"].lower(): e["Id"] for e in octo.environments()}
        self.tenant_by_name = {t["Name"].lower(): t["Id"] for t in octo.tenants()}
        self.tags = set()
        for ts in octo.tagsets():
            for t in ts.get("Tags", []):
                self.tags.add(t["CanonicalTagName"])
        self.tags_lower = {t.lower(): t for t in self.tags}

    def envs(self, names):
        out = []
        for n in names:
            key = n.lower()
            if key.startswith("environments-"):
                out.append(n); continue
            if key not in self.env_by_name:
                raise ValueError(f"找不到 Environment: {n}（可用: {', '.join(sorted(self.env_by_name))}）")
            out.append(self.env_by_name[key])
        return out

    def tenants(self, names):
        out = []
        for n in names:
            key = n.lower()
            if key.startswith("tenants-"):
                out.append(n); continue
            if key not in self.tenant_by_name:
                raise ValueError(f"找不到 Tenant: {n}")
            out.append(self.tenant_by_name[key])
        return out

    def tenant_tags(self, names):
        out = []
        for n in names:
            key = n.lower()
            if key not in self.tags_lower:
                raise ValueError(f"找不到 Tenant Tag: {n}（可用: {', '.join(sorted(self.tags))}）")
            out.append(self.tags_lower[key])
        return out


# ---------------------------------------------------------------- 检测
def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


def _x509_fields(pem_text: str):
    """从 PEM 证书文本读 CN / SAN / 有效期 / 签发者 / SHA1 指纹。"""
    exe = shutil.which("openssl")
    r = _run([exe, "x509", "-noout", "-subject", "-issuer", "-startdate", "-enddate",
              "-fingerprint", "-sha1", "-ext", "subjectAltName"], input=pem_text)
    out = {"domains": set(), "cn": "", "issuer": "", "notBefore": "", "notAfter": "", "thumb": ""}
    for line in r.stdout.splitlines():
        if line.startswith("subject="):
            m = re.search(r"CN\s*=\s*([^,/]+)", line); out["cn"] = m.group(1).strip() if m else ""
            if out["cn"] and "." in out["cn"]: out["domains"].add(norm_domain(out["cn"]))
        elif line.startswith("issuer="):
            m = re.search(r"CN\s*=\s*([^,/]+)", line); out["issuer"] = m.group(1).strip() if m else line[7:]
        elif line.startswith("notBefore="): out["notBefore"] = line.split("=", 1)[1].strip()
        elif line.startswith("notAfter="): out["notAfter"] = line.split("=", 1)[1].strip()
        elif "Fingerprint=" in line: out["thumb"] = line.split("=", 1)[1].replace(":", "").upper()
        elif "DNS:" in line:
            for d in re.findall(r"DNS:([^,\s]+)", line): out["domains"].add(norm_domain(d))
    return out


def _parse_openssl_date(s: str):
    import datetime
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y GMT"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    return None


def check_job(j: dict, by_name: dict, by_thumb: dict, args) -> dict:
    """对一张证书做全套体检，返回一行结果。problems 非空就是异常。"""
    import datetime
    exe = shutil.which("openssl")
    now = datetime.datetime.now(datetime.timezone.utc)
    row = {"name": j["name"], "source": "", "files": "", "pfx_pw": "-", "key_match": "-", "chain": "-",
           "cn": "", "domains": "", "domain_ok": "-", "issuer": "", "not_after": "", "days_left": "",
           "octopus": "", "action": "", "problems": []}
    prob = row["problems"]
    if j.get("_error"):
        prob.append(j["_error"]); row["action"] = "FAIL"; return row
    if not exe:
        prob.append("本机没有 openssl，无法检测"); row["action"] = "?"; return row

    p: Path = j["file"]
    folder = j.get("_folder")
    pw = j["password"] or ""
    pem_cert = None

    # ---- 文件完整性（域名文件夹模式）
    if folder:
        row["source"] = folder.name
        names = ["cert.pem", "chain.pem", "fullchain.pem", "privkey.pem", "fullchain.pfx"]
        have = [n for n in names if (folder / n).exists()]
        row["files"] = ",".join(n.split(".")[0] if n != "fullchain.pfx" else "pfx" for n in have)
        missing = [n for n in names if n not in have]
        bad = dangling_links(folder)
        if bad:
            row["files"] = (row["files"] + "," if row["files"] else "") + "软链接失效:" + ",".join(n for n, _ in bad)
            prob.append(f"{len(bad)} 个 pem 是失效软链接（指向 {bad[0][1]}），certbot live/ 要连 archive/ 一起拷或用 cp -rL")
        if "privkey.pem" in missing and "fullchain.pfx" in missing:
            prob.append("既没有 privkey.pem 也没有 pfx，没有私钥")
        if "fullchain.pem" in missing and "cert.pem" in missing and "fullchain.pfx" in missing:
            prob.append("没有任何证书文件")
        # 私钥是否匹配证书
        cert_pem = folder / "cert.pem" if (folder / "cert.pem").exists() else folder / "fullchain.pem"
        if cert_pem.exists() and (folder / "privkey.pem").exists():
            a = _run([exe, "x509", "-in", str(cert_pem), "-noout", "-pubkey"]).stdout
            b = _run([exe, "pkey", "-in", str(folder / "privkey.pem"), "-pubout"]).stdout
            row["key_match"] = "ok" if a and a == b else "MISMATCH"
            if row["key_match"] != "ok": prob.append("privkey.pem 和证书不匹配")
        # 证书链能否验证到系统根证书
        if (folder / "fullchain.pem").exists():
            v = _run([exe, "verify", "-untrusted", str(folder / "fullchain.pem"), str(cert_pem)])
            row["chain"] = "ok" if v.returncode == 0 else "BAD"
            if v.returncode != 0: prob.append("证书链验证失败: " + (v.stderr or v.stdout).strip().splitlines()[-1][:80])
        if cert_pem.exists():
            pem_cert = cert_pem.read_text()
    else:
        row["source"] = p.name

    # ---- pfx 密码 / 从 pfx 读证书
    if p.exists() and p.suffix.lower() in CERT_EXTS:
        pfx_pem = None
        for extra in ([], ["-legacy"]):
            r = _run([exe, "pkcs12", "-in", str(p), "-nokeys", "-clcerts", "-passin", "env:PFXPW", *extra],
                     env=dict(os.environ, PFXPW=pw))
            if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
                pfx_pem = r.stdout; row["pfx_pw"] = "ok" if not extra else "ok(legacy)"; break
            if "invalid password" in r.stderr.lower() or "mac verify" in r.stderr.lower():
                row["pfx_pw"] = "WRONG"; prob.append("pfx 密码错误"); break
        if pfx_pem is None and row["pfx_pw"] == "-":
            row["pfx_pw"] = "UNREADABLE"; prob.append("pfx 无法读取")
        if pfx_pem:
            # pfx 里有没有私钥
            rk = _run([exe, "pkcs12", "-in", str(p), "-nocerts", "-nodes", "-passin", "env:PFXPW",
                       *([] if row["pfx_pw"] == "ok" else ["-legacy"])], env=dict(os.environ, PFXPW=pw))
            if "PRIVATE KEY" not in rk.stdout: prob.append("pfx 里没有私钥")
            if pem_cert:
                # pfx 和 pem 是不是同一张
                if _x509_fields(pfx_pem)["thumb"] != _x509_fields(pem_cert)["thumb"]:
                    prob.append("fullchain.pfx 和 cert.pem 不是同一张证书")
            else:
                pem_cert = pfx_pem
    elif not p.exists() and not pem_cert:
        prob.append(f"文件不存在: {p}")

    # ---- 证书本身
    if pem_cert:
        f = _x509_fields(pem_cert)
        row["cn"] = f["cn"]; row["issuer"] = f["issuer"]; row["not_after"] = f["notAfter"]
        row["domains"] = " ".join(sorted(f["domains"]))
        na = _parse_openssl_date(f["notAfter"]); nb = _parse_openssl_date(f["notBefore"])
        if na:
            days = (na - now).days; row["days_left"] = days
            if days < 0: prob.append(f"已过期 {-days} 天")
            elif days < args.warn_days: prob.append(f"{days} 天后到期")
        if nb and nb > now: prob.append("生效时间在未来（notBefore）")
        # 域名和文件夹/名字是否对得上
        want = folder.name.lower() if folder else None
        if want:
            row["domain_ok"] = "ok" if want in f["domains"] else "MISMATCH"
            if row["domain_ok"] != "ok": prob.append(f"证书域名 {sorted(f['domains'])} 不含文件夹名 {want}")
        # ---- Octopus 现状
        old = by_name.get(j["name"].lower())
        if old:
            same = old.get("Thumbprint", "").upper() == f["thumb"]
            od = octopus_cert_domains(old)
            row["octopus"] = f"{old['Id']} exp {(old.get('NotAfter') or '')[:10]} issuer={old.get('IssuerOrganization') or old.get('IssuerCommonName')}"
            if same:
                row["action"] = "skip-same"
            elif od and not (od & f["domains"]):
                row["action"] = "REFUSE"; prob.append(f"Octopus 同名证书域名 {sorted(od)} 和新证书不一致")
            else:
                row["action"] = "replace" if args.on_exists == "replace" else args.on_exists
                if not od: row["action"] += "(旧证书无域名可比对)"
        else:
            dup = by_thumb.get(f["thumb"])
            row["octopus"] = f"同指纹已存在于 {dup['Name']}" if dup else "不存在"
            row["action"] = "FAIL(replace-only)" if args.replace_only else "create"
            if dup: prob.append(f"同一张证书已以名字 {dup['Name']} 存在")
    else:
        row["action"] = "FAIL"
    return row


def run_check(jobs, by_name, by_thumb, args):
    rows = [check_job(j, by_name, by_thumb, args) for j in jobs]
    cols = ["name", "source", "files", "pfx_pw", "key_match", "chain", "domain_ok", "days_left", "issuer", "octopus", "action", "problems"]
    disp = []
    for r in rows:
        d = dict(r); d["problems"] = "; ".join(r["problems"]) or "ok"
        disp.append(d)
    widths = {c: max(len(c), *(len(str(d[c])) for d in disp)) for c in cols}
    widths["octopus"] = min(widths["octopus"], 60); widths["problems"] = min(widths["problems"], 80)
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for d in disp:
        print("  ".join(str(d[c])[:widths[c]].ljust(widths[c]) for c in cols))
    bad = [d for d in disp if d["problems"] != "ok"]
    print(f"\n共 {len(rows)} 张，异常 {len(bad)} 张" + (f"：{', '.join(d['name'] for d in bad)}" if bad else ""))
    if args.check_out:
        import io
        with open(args.check_out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "source", "files", "pfx_pw", "key_match", "chain", "cn", "domains",
                                               "domain_ok", "issuer", "not_after", "days_left", "octopus", "action", "problems"])
            w.writeheader()
            for d in disp: w.writerow({k: d.get(k, "") for k in w.fieldnames})
        print(f"已写入 {args.check_out}")
    return 1 if bad else 0


# ---------------------------------------------------------------- build job list
def build_pfx_from_pem(folder: Path, out: Path, password: str):
    """用 fullchain.pem + privkey.pem 打一个 pfx（AES-256，不用老式 RC2）。"""
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("没有 pfx 又找不到 openssl，无法从 pem 打包")
    chain = folder / "fullchain.pem"
    if not chain.exists():
        chain = folder / "cert.pem"
    key = folder / "privkey.pem"
    if not chain.exists() or not key.exists():
        bad = dangling_links(folder)
        if bad:
            raise FileNotFoundError(
                f"{folder.name}: pem 是失效的软链接（如 {bad[0][0]} -> {bad[0][1]}），"
                "certbot 的 live/ 目录要连 archive/ 一起拷，或用 cp -rL 把真实文件拷出来")
        raise FileNotFoundError(f"{folder.name}: 缺 fullchain.pem/cert.pem 或 privkey.pem，无法打包")
    env = dict(os.environ, PFXPW=password or "")
    r = subprocess.run(
        [exe, "pkcs12", "-export", "-in", str(chain), "-inkey", str(key), "-out", str(out),
         "-passout", "env:PFXPW", "-keypbe", "AES-256-CBC", "-certpbe", "AES-256-CBC", "-macalg", "sha256"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{folder.name}: openssl 打包失败: {r.stderr.strip()[:300]}")
    return out


AUTO_NOTES = "{issuer}{wildcard} | {domains} | 批次 {batch} | {date} upload_certs.py 上传"


class UploadLog:
    """追加式操作日志，一张证书一行，只在真正改动 Octopus 时写。"""

    def __init__(self, path, url, space):
        self.path = Path(path) if path else None
        self.ctx = f"octopus={url.split('//')[-1]} space={space}"
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, action, name, cert_id, **fields):
        if not self.path:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        kv = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, "", []))
        line = f"{ts}  {action:<8} {name}  {cert_id}  {self.ctx} {kv}".rstrip()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def render_notes(template: str, info, job) -> str:
    """--notes 支持 {issuer} {domains} {wildcard} {batch} {date} 占位符；'auto' 等于 AUTO_NOTES。"""
    if not template:
        return ""
    if template == "auto":
        template = AUTO_NOTES
    import datetime
    info = info or {}
    doms = sorted(info.get("domains") or [])
    # 展示成 *.x.com, x.com 的样子
    shown = []
    for d in doms:
        if info.get("wildcard"):
            shown.append(f"*.{d}")
        shown.append(d)
    folder = job.get("_folder")
    batch = folder.parent.name if folder else Path(job["file"]).parent.name
    return template.format(
        issuer=info.get("issuer") or "未知签发方",
        wildcard=" 通配符" if info.get("wildcard") else "",
        domains=", ".join(shown) or job["name"],
        batch=batch,
        date=datetime.date.today().isoformat(),
    )


def domain_name(dom: str, template: str) -> str:
    return template.format(domain=dom, domain_us=dom.replace(".", "_"))


def jobs_from_dir(d: Path, args):
    """两种目录结构都支持：
    A) 平铺：dir/star_xxx_com.pfx            -> 名字 = 文件名
    B) 按域名分文件夹（certbot 导出那种）：dir/<domain>/{fullchain.pfx|fullchain.pem+privkey.pem}
       -> 名字 = --name-template（默认 star_<domain_us>）；没 pfx 就现场从 pem 打包
    """
    flat = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in CERT_EXTS)
    sub = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
    common = dict(environments=args.env, tenants=args.tenant, tenant_tags=args.tenant_tag,
                  password=args.password, notes=args.notes or "")
    if flat:
        return [dict(file=p, name=name_from_file(p), **common) for p in flat]
    if not sub:
        sys.exit(f"目录 {d} 里没有 .pfx / .p12 文件，也没有域名子文件夹")

    tmpdir = Path(tempfile.mkdtemp(prefix="upload_certs_"))
    atexit.register(shutil.rmtree, tmpdir, True)   # 临时 pfx 含私钥，退出时删掉
    jobs = []
    for folder in sub:
        dom = folder.name
        pfx = next((folder / n for n in ("fullchain.pfx", f"{dom}.pfx", "cert.pfx") if (folder / n).exists()), None)
        if pfx is None:
            pfx = next(iter(sorted(folder.glob("*.pfx"))), None)
        built = False
        if pfx is None:
            target = (folder / "fullchain.pfx") if args.write_pfx else (tmpdir / f"{dom}.pfx")
            try:
                pfx = build_pfx_from_pem(folder, target, args.password)
                built = True
            except Exception as ex:
                jobs.append(dict(file=folder / "fullchain.pfx", name=domain_name(dom, args.name_template),
                                 _error=str(ex), _folder=folder, **common))
                continue
        jobs.append(dict(file=pfx, name=domain_name(dom, args.name_template), _built=built, _folder=folder, **common))
    return jobs


def jobs_from_manifest(m: Path, args):
    jobs = []
    base = m.parent
    raw = m.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk")   # Excel 直接"另存为 CSV"出来的编码
    import io
    with io.StringIO(text, newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("file"):
                continue
            p = Path(row["file"])
            if not p.is_absolute():
                p = base / p
            jobs.append(dict(
                file=p,
                name=row.get("name") or name_from_file(p),
                environments=split_list(row.get("environments")) or args.env,
                tenants=split_list(row.get("tenants")) or args.tenant,
                tenant_tags=([] if row.get("tenant_tags") == "-" else (split_list(row.get("tenant_tags")) or args.tenant_tag)),
                password=row.get("password") or args.password,
                notes=row.get("notes") or args.notes or "",
                _line=i,
            ))
    if not jobs:
        sys.exit(f"清单 {m} 没有有效行")
    return jobs


# ---------------------------------------------------------------- main
def main():
    # 先只解析 --env-file，把对应的 .env 读进环境变量，再构建完整参数（--password 默认值依赖它）
    # allow_abbrev=False 必须加：否则 argparse 会把 --env DEV 当成 --env-file 的缩写
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args()
    load_dotenv(pre_args.env_file)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("dir", nargs="?", help="包含 .pfx/.p12 的目录")
    ap.add_argument("--env-file", default=".env",
                    help="配置文件，默认 .env；多个 Octopus 实例就一人一个，如 --env-file .env.prod")
    ap.add_argument("--octopus-url", default=None, help="覆盖 OCTOPUS_URL")
    ap.add_argument("--space", default=None, help="覆盖 OCTOPUS_SPACE，如 Spaces-82")
    ap.add_argument("--force-replace", action="store_true",
                    help="同名证书域名不一致时仍然 replace（默认拒绝，防止文件放错）")
    ap.add_argument("--manifest", "-m", help="CSV 清单（列: file,name,environments,tenants,tenant_tags,password,notes）")
    ap.add_argument("--env", action="append", default=None, help="环境名，可重复；默认 PROD")
    ap.add_argument("--tenant", action="append", default=None, help="Tenant 名，可重复")
    ap.add_argument("--tenant-tag", action="append", default=None,
                    help="Tenant Tag，如 'Environment Tags/PROD'，可重复；默认按每张证书的环境自动加 'Environment Tags/<ENV>'；传 --tenant-tag '' 表示不加")
    ap.add_argument("--password", default=os.environ.get("PFX_PASSWORD"), help="PFX 密码，默认读 .env 的 PFX_PASSWORD")
    ap.add_argument("--notes", default="",
                    help="新建证书的 Notes。'auto' 生成「签发方 通配符 | 域名 | 批次 目录名 | 日期 upload_certs.py 上传」；"
                         "也可自定义模板，占位符 {issuer} {domains} {wildcard} {batch} {date}。续期不改 Notes")
    ap.add_argument("--on-exists", choices=["replace", "skip", "fail"], default="replace")
    ap.add_argument("--replace-only", action="store_true",
                    help="只续期：遇到 Octopus 里不存在的名字直接报失败，不新建（批量续期时防止名字打错变成新证书）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="只体检不上传：文件完整性、私钥匹配、证书链、有效期、域名、pfx 密码、Octopus 现状，每张一行")
    ap.add_argument("--check-out", default=None, help="配合 --check，把结果另存为 CSV，如 --check-out check.csv")
    ap.add_argument("--warn-days", type=int, default=30, help="--check 时剩余天数少于此值视为异常，默认 30")
    ap.add_argument("--no-verify", action="store_true", help="跳过本机 openssl 密码预检")
    ap.add_argument("--name-template", default="star_{domain_us}",
                    help="域名子文件夹模式下的证书名模板，可用 {domain} {domain_us}；默认 star_{domain_us}")
    ap.add_argument("--log", default="logs/upload.log",
                    help="操作日志路径（追加写，带时间戳），默认 logs/upload.log；--dry-run / --check 不写")
    ap.add_argument("--no-log", action="store_true", help="不写操作日志")
    ap.add_argument("--write-pfx", action="store_true",
                    help="域名子文件夹模式下，没有 pfx 的从 pem 打包后把 fullchain.pfx 写回该文件夹（默认只放临时目录）")
    args = ap.parse_args()

    if not args.dir and not args.manifest:
        ap.error("请给目录，或 --manifest 清单")
    if args.dir and args.manifest:
        ap.error("目录和 --manifest 二选一")
    # 先检查本地路径，别等连上 Octopus 才发现目录不存在
    if args.dir and not Path(args.dir).is_dir():
        sys.exit(f"目录不存在: {args.dir}（当前目录下有: {', '.join(sorted(p.name for p in Path('.').iterdir() if p.is_dir() and not p.name.startswith('.')))}）")
    if args.manifest and not Path(args.manifest).is_file():
        sys.exit(f"清单文件不存在: {args.manifest}")

    api_key = os.environ.get("OCTOPUS_API_KEY")
    if not api_key:
        sys.exit(f"缺少 OCTOPUS_API_KEY（放 {args.env_file} 或环境变量）")
    url = args.octopus_url or os.environ.get("OCTOPUS_URL") or DEFAULT_URL
    space = args.space or os.environ.get("OCTOPUS_SPACE") or DEFAULT_SPACE

    args.env = args.env or ["PROD"]
    args.tenant = args.tenant or []
    if args.tenant_tag is not None:
        args.tenant_tag = [t for t in args.tenant_tag if t.strip()]

    octo = Octopus(url, api_key, space)
    print(f"Octopus: {url}  Space: {space}  模式: {'CHECK' if args.check else 'DRY-RUN' if args.dry_run else 'WRITE'}")
    try:
        rs = Resolver(octo)
        existing = octo.certificates()
    except RuntimeError as ex:
        sys.exit(f"读取 Octopus 失败: {ex}")
    by_name = {c["Name"].lower(): c for c in existing}
    by_thumb = {c["Thumbprint"]: c for c in existing if c.get("Thumbprint")}
    print(f"已有证书 {len(existing)} 张，环境 {len(rs.env_by_name)} 个，Tenant {len(rs.tenant_by_name)} 个\n")

    jobs = jobs_from_manifest(Path(args.manifest), args) if args.manifest else jobs_from_dir(Path(args.dir), args)
    batch = Path(args.manifest or args.dir).name
    writing = not (args.dry_run or args.check or args.no_log)
    log = UploadLog(args.log if writing else None, url, space)
    if writing:
        print(f"操作日志: {log.path}")

    if args.check:
        sys.exit(run_check(jobs, by_name, by_thumb, args))

    results = []
    for j in jobs:
        tag = f"[{j['name']}]"
        try:
            if j.get("_error"):
                raise RuntimeError(j["_error"])
            p: Path = j["file"]
            if not p.exists():
                raise FileNotFoundError(f"文件不存在: {p}")
            if j.get("_built"):
                print(f"{tag} 没有 pfx，已从 pem 打包 -> {p}")
            pw = j["password"] or ""
            info = None
            if not args.no_verify:
                info = openssl_info(p, pw)
                if info and not info["ok"]:
                    raise ValueError(info["error"])
                if info and info["ok"]:
                    print(f"{tag} {info['subject']}  到期 {info['notAfter']}")
            env_ids = rs.envs(j["environments"])
            tenant_ids = rs.tenants(j["tenants"])
            # 没显式给 tenant_tags 时，默认跟随这张证书自己的环境：Environment Tags/<ENV>
            tags = j["tenant_tags"]
            if tags is None:
                tags = [f"Environment Tags/{e.upper()}" for e in j["environments"]]
            tag_names = rs.tenant_tags(tags)
            tenanted = "Tenanted" if (tenant_ids or tag_names) else "Untenanted"

            b64 = base64.b64encode(p.read_bytes()).decode()
            fp = thumbprint(p, pw) if shutil.which("openssl") else None
            fp = fp.upper() if fp else None
            exp = (info or {}).get("notAfter")
            old = by_name.get(j["name"].lower())
            scope = f"env={j['environments']} tenants={j['tenants']} tags={tag_names}"

            if old:
                # 同名已存在
                # 用 thumbprint 比对：openssl 能算 SHA1 指纹时才比较
                same = bool(fp) and fp == (old.get("Thumbprint") or "").upper()
                if same:
                    print(f"{tag} 已存在且内容相同 ({old['Id']})，跳过")
                    results.append((j["name"], "skip-same", old["Id"])); continue
                if args.on_exists == "fail":
                    raise RuntimeError(f"同名证书已存在 {old['Id']}（--on-exists fail）")
                if args.on_exists == "skip":
                    print(f"{tag} 同名已存在 ({old['Id']})，按 --on-exists skip 跳过")
                    results.append((j["name"], "skip-exists", old["Id"])); continue
                # 域名比对：新文件的域名必须和 Octopus 里旧证书的域名有交集，防止文件放错顶掉别的域名
                new_doms = (info or {}).get("domains") or set()
                old_doms = octopus_cert_domains(old)
                if new_doms and old_doms and not (new_doms & old_doms):
                    msg = f"域名不一致：新文件 {sorted(new_doms)} vs Octopus 现有 {sorted(old_doms)}"
                    if not args.force_replace:
                        raise RuntimeError(msg + "，已拒绝 replace（确认无误可加 --force-replace）")
                    print(f"{tag} 警告: {msg}，按 --force-replace 继续")
                elif not old_doms:
                    print(f"{tag} 提示: Octopus 里旧证书没有可比对的域名（CN={old.get('SubjectCommonName')}），跳过域名校验")
                print(f"{tag} 同名已存在 ({old['Id']}, 到期 {(old.get('NotAfter') or '')[:10]}) -> REPLACE 续期")
                if not args.dry_run:
                    new = octo.replace_certificate(old["Id"], b64, pw)
                    print(f"{tag}   -> 完成 {new['Id']}  新到期 {(new.get('NotAfter') or '')[:10]}")
                    log.write("replace", j["name"], new["Id"], batch=batch, file=p.name,
                              old_thumb=old.get("Thumbprint"), new_thumb=new.get("Thumbprint") or fp,
                              old_exp=(old.get("NotAfter") or "")[:10], new_exp=(new.get("NotAfter") or "")[:10])
                    results.append((j["name"], "replaced", new["Id"]))
                else:
                    results.append((j["name"], "would-replace", old["Id"]))
                continue

            if args.replace_only:
                raise RuntimeError("Octopus 里没有这个名字的证书（--replace-only 不新建）")

            dup = by_thumb.get(fp) if fp else None
            if dup:
                print(f"{tag} 注意: 相同指纹的证书已存在，名字是 {dup['Name']} ({dup['Id']})，仍将按新名字创建")

            print(f"{tag} 新建  {scope}  {tenanted}")
            notes = render_notes(j["notes"], info, j)
            if notes:
                print(f"{tag} Notes: {notes}")
            payload = {
                "Name": j["name"],
                "Notes": notes,
                "CertificateData": {"HasValue": True, "NewValue": b64},
                "Password": {"HasValue": True, "NewValue": pw},
                "EnvironmentIds": env_ids,
                "TenantedDeploymentParticipation": tenanted,
                "TenantIds": tenant_ids,
                "TenantTags": tag_names,
            }
            if not args.dry_run:
                new = octo.create_certificate(payload)
                print(f"{tag}   -> 完成 {new['Id']}  CN={new.get('SubjectCommonName')}  到期 {(new.get('NotAfter') or '')[:10]}")
                log.write("create", j["name"], new["Id"], batch=batch, file=p.name,
                          thumb=new.get("Thumbprint") or fp, exp=(new.get("NotAfter") or "")[:10],
                          env=",".join(j["environments"]), tenants=",".join(j["tenants"]), tags=",".join(tag_names))
                by_name[j["name"].lower()] = new
                results.append((j["name"], "created", new["Id"]))
            else:
                results.append((j["name"], "would-create", "-"))
        except Exception as ex:
            print(f"{tag} 失败: {ex}", file=sys.stderr)
            log.write("FAILED", j["name"], "-", batch=batch, error=str(ex).replace("\n", " "))
            results.append((j["name"], "FAILED", str(ex)))

    print("\n==== 汇总 ====")
    w = max((len(r[0]) for r in results), default=4)
    for name, status, detail in results:
        print(f"{name:<{w}}  {status:<14} {detail}")
    failed = sum(1 for r in results if r[1] == "FAILED")
    print(f"\n共 {len(results)} 张，失败 {failed}")
    sys.exit(1 if failed else 0)


def thumbprint(path: Path, password: str):
    """SHA1 指纹（Octopus 的 Thumbprint 就是 SHA1）。失败返回 None。"""
    exe = shutil.which("openssl")
    if not exe:
        return None
    env = dict(os.environ, PFXPW=password or "")
    for extra in ([], ["-legacy"]):
        try:
            r = subprocess.run(
                [exe, "pkcs12", "-in", str(path), "-nokeys", "-clcerts", "-passin", "env:PFXPW", *extra],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if r.returncode != 0:
                continue
            r2 = subprocess.run([exe, "x509", "-noout", "-fingerprint", "-sha1"],
                                input=r.stdout, capture_output=True, text=True, timeout=30)
            m = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", r2.stdout)
            if m:
                return m.group(1).replace(":", "")
        except Exception:
            return None
    return None


if __name__ == "__main__":
    main()
