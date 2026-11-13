"""
Build the Daily MIS Report.

Steps
  1. Load production actuals and tomorrow's plan from sample_data/
  2. Validate the data (missing values, duplicates, negative quantities,
     dates out of range, logical checks) and print a validation summary
  3. Calculate KPIs, the shift-wise summary and the deviations list
  4. Draw a 7-day trend chart (embedded in the HTML as an image)
  5. Render the HTML report with Jinja2 + CSS
  6. Export to PDF (and optionally a PNG preview) with headless Chrome

Usage
  python build_report.py                 # report for the latest day in the data
  python build_report.py --date 2026-11-10
  python build_report.py --name sample_daily_mis_report --png docs/report_preview.png
"""

import argparse
import base64
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "sample_data"
TEMPLATE_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"

PLANT = "Plant A"
ON_TIME_GRACE_MIN = 15      # completed within 15 min of due time counts as on time
LATE_FLAG_MIN = 30          # flag batches more than 30 min late
YIELD_FLAG_PCT = 95.0       # flag batches with yield below 95 %
LOOKBACK_DAYS = 30          # valid date window for the data

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

REQUIRED_LOG_COLS = [
    "date", "shift", "batch_id", "customer", "grade", "part_no",
    "planned_qty", "produced_qty", "rejected_qty", "status", "due_time",
]


# --------------------------------------------------------------------------
# 1. Validation
# --------------------------------------------------------------------------
@dataclass
class ValidationResult:
    checks: list = field(default_factory=list)   # (name, rows_affected, action)
    rows_in: int = 0
    rows_out: int = 0

    def add(self, name, n, action):
        self.checks.append({"name": name, "rows": int(n), "action": action if n else "OK"})

    @property
    def issues(self):
        return sum(1 for c in self.checks if c["rows"])

    def print_summary(self):
        print("\nData validation summary")
        print("-" * 64)
        for c in self.checks:
            mark = "PASS" if not c["rows"] else "FLAG"
            print(f"  [{mark}] {c['name']:<34} {c['rows']:>3} row(s)  {c['action'] if c['rows'] else ''}")
        print("-" * 64)
        print(f"  {self.rows_in} rows read, {self.rows_out} rows used, "
              f"{self.issues} of {len(self.checks)} checks raised issues\n")


def validate(log: pd.DataFrame, report_date) -> tuple[pd.DataFrame, ValidationResult]:
    """Run data-quality checks and return a clean frame plus a summary."""
    v = ValidationResult(rows_in=len(log))

    missing_cols = [c for c in REQUIRED_LOG_COLS if c not in log.columns]
    if missing_cols:
        sys.exit(f"Input file is missing required columns: {missing_cols}")
    v.add("Required columns present", 0, "")

    # Duplicates (same batch keyed in more than once)
    dup_mask = log.duplicated(subset=["batch_id"], keep="first")
    v.add("Duplicate batch entries", dup_mask.sum(), "duplicates removed")
    log = log[~dup_mask]

    # Missing values in key fields
    key = ["date", "shift", "batch_id", "customer", "part_no", "planned_qty", "produced_qty", "status"]
    miss_mask = log[key].isna().any(axis=1)
    v.add("Missing values in key fields", miss_mask.sum(), "customer set to 'Unknown'")
    log = log.copy()
    log["customer"] = log["customer"].fillna("Unknown")
    log = log[~log[[c for c in key if c != "customer"]].isna().any(axis=1)]

    # Negative quantities
    qty = ["planned_qty", "produced_qty", "rejected_qty"]
    neg_mask = (log[qty] < 0).any(axis=1)
    v.add("Negative quantities", neg_mask.sum(), "rows excluded")
    log = log[~neg_mask]

    # Dates out of range
    log["date"] = pd.to_datetime(log["date"], errors="coerce")
    start = pd.Timestamp(report_date) - pd.Timedelta(days=LOOKBACK_DAYS - 1)
    oor_mask = log["date"].isna() | (log["date"] < start) | (log["date"] > pd.Timestamp(report_date))
    v.add("Dates out of range", oor_mask.sum(), "rows excluded")
    log = log[~oor_mask]

    # Logical consistency
    illogical = log["rejected_qty"] > log["produced_qty"]
    v.add("Rejected qty within produced qty", illogical.sum(), "rows excluded")
    log = log[~illogical]

    no_time = (log["status"] == "Completed") & log["completed_time"].isna()
    v.add("Completion timestamps present", no_time.sum(), "rows excluded")
    log = log[~no_time]

    v.rows_out = len(log)
    return log, v


# --------------------------------------------------------------------------
# 2. Metrics
# --------------------------------------------------------------------------
def enrich(log: pd.DataFrame) -> pd.DataFrame:
    log = log.copy()
    log["due_time"] = pd.to_datetime(log["due_time"])
    log["completed_time"] = pd.to_datetime(log["completed_time"])
    log["good_qty"] = log["produced_qty"] - log["rejected_qty"]
    log["delay_min"] = (log["completed_time"] - log["due_time"]).dt.total_seconds() / 60
    log["completed"] = log["status"].eq("Completed")
    log["on_time"] = log["completed"] & (log["delay_min"] <= ON_TIME_GRACE_MIN)
    log["yield_pct"] = (log["good_qty"] / log["produced_qty"].where(log["produced_qty"] > 0)) * 100
    return log


def pct(num, den):
    return round(100 * num / den, 1) if den else 0.0


def kpis_for(day: pd.DataFrame) -> dict:
    done = day[day["completed"]]
    return {
        "completion": pct(len(done), len(day)),
        "on_time": pct(done["on_time"].sum(), len(done)),
        "yield": pct(done["good_qty"].sum(), done["produced_qty"].sum()),
        "rejected_qty": int(day["rejected_qty"].sum()),
        "rejection_rate": pct(day["rejected_qty"].sum(), day["produced_qty"].sum()),
    }


def daily_series(log: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for d, g in log.groupby("date"):
        k = kpis_for(g)
        rows.append({"date": d, **k, "good_qty": int(g["good_qty"].sum())})
    return pd.DataFrame(rows).sort_values("date")


def shift_summary(day: pd.DataFrame) -> list[dict]:
    out = []
    for shift, g in day.groupby("shift"):
        done = g[g["completed"]]
        out.append({
            "shift": shift,
            "planned": len(g),
            "completed": int(g["completed"].sum()),
            "pending": int((~g["completed"]).sum()),
            "planned_qty": int(g["planned_qty"].sum()),
            "good_qty": int(g["good_qty"].sum()),
            "rejected_qty": int(g["rejected_qty"].sum()),
            "completion": pct(len(done), len(g)),
            "on_time": pct(done["on_time"].sum(), len(done)),
            "yield": pct(done["good_qty"].sum(), done["produced_qty"].sum()),
        })
    total = day
    done = total[total["completed"]]
    out.append({
        "shift": "Total", "planned": len(total),
        "completed": int(total["completed"].sum()), "pending": int((~total["completed"]).sum()),
        "planned_qty": int(total["planned_qty"].sum()), "good_qty": int(total["good_qty"].sum()),
        "rejected_qty": int(total["rejected_qty"].sum()),
        "completion": pct(len(done), len(total)),
        "on_time": pct(done["on_time"].sum(), len(done)),
        "yield": pct(done["good_qty"].sum(), done["produced_qty"].sum()),
    })
    return out


def deviations(day: pd.DataFrame, limit=10) -> list[dict]:
    items = []
    for _, r in day.iterrows():
        base = {"batch": r["batch_id"], "shift": r["shift"], "customer": r["customer"],
                "part": r["part_no"], "grade": r["grade"]}
        if not r["completed"]:
            items.append({**base, "issue": "Pending", "severity": 3,
                          "detail": f"{int(r['produced_qty'])} of {int(r['planned_qty'])} done; carry over"})
            continue
        if r["delay_min"] > LATE_FLAG_MIN:
            items.append({**base, "issue": "Late", "severity": 2,
                          "detail": f"{int(r['delay_min'])} min after due time"})
        if r["yield_pct"] < YIELD_FLAG_PCT:
            items.append({**base, "issue": "Low yield", "severity": 2,
                          "detail": f"Yield {r['yield_pct']:.1f}% ({int(r['rejected_qty'])} rejected)"})
    items.sort(key=lambda x: (-x["severity"], x["shift"], x["batch"]))
    return items[:limit], len(items)


def plan_summary(plan: pd.DataFrame, carry_over: int) -> dict:
    rows = []
    for shift, g in plan.groupby("shift"):
        rows.append({
            "shift": shift,
            "batches": len(g),
            "planned_qty": int(g["planned_qty"].sum()),
            "high": int((g["priority"] == "High").sum()),
            "customers": g["customer"].nunique(),
            "top_parts": ", ".join(g["part_no"].value_counts().index[:2]),
        })
    rows.append({
        "shift": "Total", "batches": len(plan), "planned_qty": int(plan["planned_qty"].sum()),
        "high": int((plan["priority"] == "High").sum()), "customers": plan["customer"].nunique(),
        "top_parts": "",
    })
    return {"rows": rows, "carry_over": carry_over}


# --------------------------------------------------------------------------
# 3. Chart
# --------------------------------------------------------------------------
NAVY, TEAL, AMBER, GREY = "#1B2A4A", "#14897F", "#D08C1F", "#8A94A6"


def trend_chart(series: pd.DataFrame) -> str:
    """Return a base64 PNG of the last-7-day trend."""
    s = series.tail(7)
    labels = [d.strftime("%d %b") for d in s["date"]]
    fig, ax = plt.subplots(figsize=(4.2, 2.15), dpi=220)
    ax.plot(labels, s["completion"], marker="o", color=NAVY, lw=2, label="Completion %")
    ax.plot(labels, s["on_time"], marker="o", color=AMBER, lw=2, label="On-time %")
    ax.plot(labels, s["yield"], marker="o", color=TEAL, lw=2, label="Yield %")
    for col, c in (("completion", NAVY), ("on_time", AMBER), ("yield", TEAL)):
        ax.annotate(f"{s[col].iloc[-1]:.0f}", (labels[-1], s[col].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=7, color=c, fontweight="bold")
    lo = min(s[["completion", "on_time", "yield"]].min().min(), 80)
    ax.set_ylim(lo - 3, 101)
    ax.grid(axis="y", color="#E3E7EE", lw=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#C9D0DB")
    ax.tick_params(colors="#4A5568", labelsize=7, length=0)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=7,
              frameon=False, ncol=3, handlelength=1.5, columnspacing=1.2)
    fig.tight_layout(pad=0.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------
# 4. Render + export
# --------------------------------------------------------------------------
def find_chrome():
    for c in CHROME_CANDIDATES:
        if Path(c).exists() or shutil.which(c):
            return shutil.which(c) or c
    return None


def run_chrome(chrome, args):
    with tempfile.TemporaryDirectory() as profile:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
               "--hide-scrollbars", f"--user-data-dir={profile}", *args]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def export(html_path: Path, pdf_path: Path, png_path: Path | None):
    chrome = find_chrome()
    if not chrome:
        print("Chrome not found - HTML report written, PDF export skipped.")
        return
    url = html_path.resolve().as_uri()
    run_chrome(chrome, ["--no-pdf-header-footer", f"--print-to-pdf={pdf_path.resolve()}", url])
    print(f"PDF  -> {pdf_path.relative_to(ROOT)}")
    if png_path:
        # A4 at 96 dpi is 794 x 1123 px; scale 1.5625 gives a ~1240 x 1754 image
        run_chrome(chrome, ["--window-size=794,1123", "--force-device-scale-factor=1.5625",
                            f"--screenshot={png_path.resolve()}", url])
        print(f"PNG  -> {png_path}")


def delta(today, avg, higher_is_better=True):
    d = round(today - avg, 1)
    good = (d >= 0) if higher_is_better else (d <= 0)
    return {"value": f"{d:+.1f}", "good": good}


def main():
    ap = argparse.ArgumentParser(description="Build the daily MIS report")
    ap.add_argument("--date", help="Report date (YYYY-MM-DD). Default: latest date in data")
    ap.add_argument("--png", help="Also save a PNG preview to this path")
    ap.add_argument("--name", help="Output file name without extension "
                                   "(default: daily_mis_report_YYYYMMDD)")
    args = ap.parse_args()

    started = datetime.now()
    raw = pd.read_csv(DATA_DIR / "production_log.csv")
    plan = pd.read_csv(DATA_DIR / "production_plan.csv")

    if args.date:
        report_date = pd.Timestamp(args.date)
    else:
        # latest date that actually has a reasonable number of rows
        counts = pd.to_datetime(raw["date"], errors="coerce").value_counts()
        report_date = counts[counts >= 5].index.max()

    # Rows dated after the report date belong to later reports, not this one
    later = pd.to_datetime(raw["date"], errors="coerce") > report_date
    raw = raw[~later]

    clean, v = validate(raw, report_date)
    v.print_summary()
    log = enrich(clean)

    day = log[log["date"] == report_date]
    if day.empty:
        sys.exit(f"No data for {report_date:%Y-%m-%d}")

    series = daily_series(log)
    prev7 = series[(series["date"] < report_date)].tail(7)
    k = kpis_for(day)
    avg = {c: prev7[c].mean() for c in ("completion", "on_time", "yield", "rejection_rate")}

    kpi_cards = [
        {"label": "Completion", "value": f"{k['completion']:.1f}%",
         "sub": f"{int(day['completed'].sum())} of {len(day)} batches",
         "delta": delta(k["completion"], avg["completion"])},
        {"label": "On-time delivery", "value": f"{k['on_time']:.1f}%",
         "sub": f"within {ON_TIME_GRACE_MIN} min of due time",
         "delta": delta(k["on_time"], avg["on_time"])},
        {"label": "Yield", "value": f"{k['yield']:.1f}%",
         "sub": f"{int(day['good_qty'].sum()):,} good pcs",
         "delta": delta(k["yield"], avg["yield"])},
        {"label": "Rejections", "value": f"{k['rejected_qty']:,} pcs",
         "sub": f"{k['rejection_rate']:.1f}% of produced",
         "delta": delta(k["rejection_rate"], avg["rejection_rate"], higher_is_better=False)},
    ]

    plan_date = report_date + timedelta(days=1)
    plan_day = plan[pd.to_datetime(plan["date"]) == plan_date]
    dev_rows, dev_total = deviations(day)

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    tpl = env.get_template("report.html")
    css = (TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
    html = tpl.render(
        css=css,
        plant=PLANT,
        report_date=report_date.strftime("%A, %d %B %Y"),
        plan_date=plan_date.strftime("%A, %d %B %Y"),
        generated_at=started.strftime("%d %b %Y %H:%M"),
        kpis=kpi_cards,
        shifts=shift_summary(day),
        plan=plan_summary(plan_day, int((~day["completed"]).sum())),
        deviations=dev_rows,
        deviation_total=dev_total,
        chart=trend_chart(series[series["date"] <= report_date]),
        validation=v,
        thresholds={"late": LATE_FLAG_MIN, "yield": YIELD_FLAG_PCT},
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = args.name or f"daily_mis_report_{report_date:%Y%m%d}"
    html_path = OUTPUT_DIR / f"{stem}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"HTML -> {html_path.relative_to(ROOT)}")

    export(html_path, OUTPUT_DIR / f"{stem}.pdf", Path(args.png) if args.png else None)
    print(f"Done in {(datetime.now() - started).total_seconds():.1f} s")


if __name__ == "__main__":
    main()
