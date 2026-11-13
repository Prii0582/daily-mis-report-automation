"""
Generate synthetic production data for a fictional manufacturing plant.

Creates two CSV files in sample_data/:
  - production_log.csv   : 30 days of batch-level actuals (3 shifts per day)
  - production_plan.csv  : the plan for the day after the last logged day

All names are fictional (Plant A, Customer A, Grade G-101, Part P-1001...).
A fixed random seed keeps the output identical on every run.
A handful of deliberate data-quality problems (a duplicate row, a missing
value, a negative quantity, an out-of-range date) are injected so the
validation step in build_report.py has something real to catch.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
END_DATE = date(2026, 11, 12)      # last day with actuals ("yesterday" in the report)
DAYS = 30
PLANT = "Plant A"

SHIFTS = {  # shift: (start hour, end hour)
    "A": (6, 14),
    "B": (14, 22),
    "C": (22, 30),  # ends 06:00 next day
}
CUSTOMERS = ["Customer A", "Customer B", "Customer C", "Customer D", "Customer E"]
GRADES = ["G-101", "G-102", "G-205", "G-310", "G-415"]
PARTS = [f"P-{n}" for n in (1001, 1002, 1005, 1010, 1018, 1022, 1030, 1047)]

OUT_DIR = Path(__file__).parent / "sample_data"


def make_batches(rng, day, shift, start_seq, planned_only=False):
    """Create the batches for one shift on one day."""
    rows = []
    start_h, end_h = SHIFTS[shift]
    n_batches = rng.integers(5, 9)
    shift_start = datetime.combine(day, datetime.min.time()) + timedelta(hours=start_h)
    slot = (end_h - start_h) * 60 / n_batches

    for i in range(n_batches):
        seq = start_seq + i
        planned_qty = int(rng.integers(8, 25)) * 10
        due = shift_start + timedelta(minutes=slot * (i + 1))
        row = {
            "date": day.isoformat(),
            "shift": shift,
            "plant": PLANT,
            "batch_id": f"B{day:%y%m%d}-{seq:03d}",
            "customer": rng.choice(CUSTOMERS),
            "grade": rng.choice(GRADES),
            "part_no": rng.choice(PARTS),
            "planned_qty": planned_qty,
            "due_time": due.strftime("%Y-%m-%d %H:%M"),
        }
        if planned_only:
            row["priority"] = rng.choice(["High", "Normal", "Normal", "Low"])
            rows.append(row)
            continue

        # Status: most batches complete, a few roll over as pending.
        # Shift C and the last batch of a shift are a little more likely to slip.
        p_pending = 0.06 + (0.04 if shift == "C" else 0) + (0.05 if i == n_batches - 1 else 0)
        status = "Pending" if rng.random() < p_pending else "Completed"

        if status == "Completed":
            produced = int(planned_qty * rng.uniform(0.94, 1.03))
            # rejection rate: usually 1-3 %, occasionally a bad batch
            rej_rate = rng.uniform(0.005, 0.03) if rng.random() > 0.08 else rng.uniform(0.06, 0.12)
            rejected = int(round(produced * rej_rate))
            delay = rng.normal(-2, 12) if rng.random() > 0.10 else rng.uniform(35, 140)
            completed_at = due + timedelta(minutes=float(delay))
            completed_time = completed_at.strftime("%Y-%m-%d %H:%M")
        else:
            produced = int(planned_qty * rng.uniform(0.2, 0.7))
            rejected = int(round(produced * rng.uniform(0.0, 0.03)))
            completed_time = ""

        row.update(
            produced_qty=produced,
            rejected_qty=rejected,
            status=status,
            completed_time=completed_time,
        )
        rows.append(row)
    return rows


def main():
    rng = np.random.default_rng(SEED)
    OUT_DIR.mkdir(exist_ok=True)

    log_rows = []
    for d in range(DAYS - 1, -1, -1):
        day = END_DATE - timedelta(days=d)
        seq = 1
        for shift in SHIFTS:
            batches = make_batches(rng, day, shift, seq)
            seq += len(batches)
            log_rows.extend(batches)

    log = pd.DataFrame(log_rows)

    # --- Inject a few realistic data-quality issues -----------------------
    # 1. duplicate entry (same batch keyed in twice)
    log = pd.concat([log, log.iloc[[len(log) - 5]]], ignore_index=True)
    # 2. missing customer on one row
    log.loc[len(log) - 12, "customer"] = np.nan
    # 3. negative quantity (typo during data entry)
    log.loc[len(log) - 20, "rejected_qty"] = -4
    # 4. date outside the reporting window
    bad = log.iloc[[10]].copy()
    bad["date"] = "2025-11-02"
    bad["batch_id"] = "B251102-099"
    log = pd.concat([log, bad], ignore_index=True)

    log.to_csv(OUT_DIR / "production_log.csv", index=False)

    # --- Tomorrow's plan ---------------------------------------------------
    plan_day = END_DATE + timedelta(days=1)
    plan_rows, seq = [], 1
    for shift in SHIFTS:
        batches = make_batches(rng, plan_day, shift, seq, planned_only=True)
        seq += len(batches)
        plan_rows.extend(batches)
    pd.DataFrame(plan_rows).to_csv(OUT_DIR / "production_plan.csv", index=False)

    print(f"Wrote {len(log)} log rows and {len(plan_rows)} plan rows to {OUT_DIR}")


if __name__ == "__main__":
    main()
