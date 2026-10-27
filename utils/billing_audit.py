#!/usr/bin/env python3
"""查 Gemini API 用量真相。

需要先完成 gcloud 認證：
    gcloud auth login
    gcloud auth application-default login

用法：
    python billing_audit.py                # 自動找專案，查最近 30 天
    python billing_audit.py <project_id> 60
"""
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

def _find_gcloud():
    import os
    import shutil
    found = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", ""), r"C:\Program Files (x86)", r"C:\Program Files"):
        cand = os.path.join(base, "Google", "Cloud SDK", "google-cloud-sdk", "bin", "gcloud.cmd")
        if os.path.isfile(cand):
            return cand
    sys.exit("找不到 gcloud，請確認已安裝 Google Cloud SDK")


GCLOUD = _find_gcloud()


def sh(args, check=True):
    r = subprocess.run([GCLOUD] + args, capture_output=True, text=True, shell=False)
    if check and r.returncode != 0:
        return None
    return r.stdout.strip()


def token():
    t = sh(["auth", "print-access-token"])
    if not t:
        sys.exit("尚未認證。請先執行：gcloud auth login")
    return t


def api(url, tok, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        method="POST" if body else "GET",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode(errors="replace")[:600]}


def main():
    project = sys.argv[1] if len(sys.argv) > 1 else None
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    tok = token()

    print("=" * 72)
    print("1. 帳號與專案")
    print("=" * 72)
    print("登入帳號 :", sh(["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]) or "?")
    projects = sh(["projects", "list", "--format=value(projectId,name,projectNumber)"]) or ""
    for line in projects.splitlines():
        print("  專案 :", line)
    if not project:
        for line in projects.splitlines():
            pid = line.split("\t")[0]
            if "gemini" in line.lower():
                project = pid
                break
        if not project and projects:
            project = projects.splitlines()[0].split("\t")[0]
    print("\n本次查詢專案 :", project)

    print()
    print("=" * 72)
    print("2. 帳單帳戶")
    print("=" * 72)
    print(sh(["billing", "accounts", "list", "--format=table(name,displayName,open)"], check=False) or "(讀不到，可能沒有 billing 權限)")
    print(sh(["billing", "projects", "describe", project, "--format=yaml"], check=False) or "")

    print("=" * 72)
    print("3. 這個專案的 API 金鑰（看有沒有你不認得的）")
    print("=" * 72)
    keys = sh(["services", "api-keys", "list", f"--project={project}",
               "--format=table(displayName,uid,createTime,restrictions.apiTargets[].service)"], check=False)
    print(keys or "(讀不到，需要 API Keys Admin 權限或先啟用 apikeys.googleapis.com)")

    print()
    print("=" * 72)
    print(f"4. Cloud Monitoring：generativelanguage API 每日請求數（最近 {days} 天）")
    print("=" * 72)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    num = sh(["projects", "describe", project, "--format=value(projectNumber)"]) or ""
    flt = ('metric.type="serviceruntime.googleapis.com/api/request_count" '
           'AND resource.type="consumed_api" '
           'AND resource.labels.service="generativelanguage.googleapis.com"')
    url = (f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
           f"?filter={api_quote(flt)}"
           f"&interval.startTime={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&interval.endTime={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&aggregation.alignmentPeriod=86400s"
           f"&aggregation.perSeriesAligner=ALIGN_SUM"
           f"&aggregation.crossSeriesReducer=REDUCE_SUM"
           f"&aggregation.groupByFields=resource.labels.method")
    d = api(url, tok)
    if "_error" in d:
        print(f"  查詢失敗 HTTP {d['_error']}：{d['_body'][:300]}")
        print("  多半是 Monitoring API 未啟用，執行：")
        print(f"    gcloud services enable monitoring.googleapis.com --project={project}")
    else:
        per_day = defaultdict(lambda: defaultdict(int))
        for ts in d.get("timeSeries", []):
            method = ts.get("resource", {}).get("labels", {}).get("method", "?")
            method = method.split(".")[-1] if method else "?"
            for pt in ts.get("points", []):
                day = pt["interval"]["endTime"][:10]
                v = pt["value"]
                per_day[day][method] += int(v.get("int64Value") or float(v.get("doubleValue") or 0))
        if not per_day:
            print("  沒有資料（Monitoring 可能剛啟用，只保留啟用後的資料）")
        else:
            print(f"  {'日期':12s} {'請求數':>8s}  方法明細")
            for day in sorted(per_day):
                tot = sum(per_day[day].values())
                detail = ", ".join(f"{m}={n}" for m, n in sorted(per_day[day].items(), key=lambda x: -x[1])[:4])
                mark = "  <<<" if tot > 200 else ""
                print(f"  {day:12s} {tot:8,d}  {detail}{mark}")
            print(f"\n  合計請求數：{sum(sum(v.values()) for v in per_day.values()):,}")
            print("  （我們 9/5-9/6 的研究總共只有約 45 次呼叫）")

    print()
    print("=" * 72)
    print("5. 專案啟用了哪些 API（看有沒有其他東西在用）")
    print("=" * 72)
    print(sh(["services", "list", "--enabled", f"--project={project}", "--format=value(config.name)"], check=False) or "")


def api_quote(s):
    import urllib.parse
    return urllib.parse.quote(s, safe="")


if __name__ == "__main__":
    main()
