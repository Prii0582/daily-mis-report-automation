# Daily MIS Report Automation

A Python script that takes one day's production data and makes a one-page daily MIS report as a PDF. It checks the data first, so wrong rows don't end up in the numbers.

The data here is dummy data. I built similar daily reports for manufacturing customers at work; I can't share those, so I rebuilt the idea with made-up data. Plant, customer, grade and part names are all made up.

## Sample output

![Daily MIS report](docs/report_preview.png)

Sample report generated from the dummy data. Full PDF: [output/sample_daily_mis_report.pdf](output/sample_daily_mis_report.pdf)

## Why I made this

Every morning plant managers want to know what was planned yesterday, what got done, what is pending and what needs attention today. Making this by hand from shift logs and sheets usually takes 2-3 hours a day. The format changes depending on who makes it, and copy-paste mistakes like duplicate rows or wrong dates get into the numbers.

## What it does

- KPI cards for completion %, on-time %, yield % and rejections, compared with the last 7-day average
- Yesterday's summary: planned vs completed vs pending, by Shift A / B / C
- Tomorrow's plan: batches and quantity by shift, high priority jobs and carried over work
- A list of pending, late or low-yield batches that need attention
- A 7-day trend chart
- Data checks before the report is made: missing values, duplicates, negative quantities, dates outside the report window and records that don't add up. Bad rows are fixed or left out, and the check results are printed and shown at the bottom of the report.

With this the report takes under a minute instead of 2-3 hours, and it looks the same every day.

The sample data has a few mistakes put in on purpose (a duplicate batch, a missing customer, a negative quantity and a date outside the window), so you can see the checks working:

```
Data validation summary
----------------------------------------------------------------
  [PASS] Required columns present               0 row(s)
  [FLAG] Duplicate batch entries                1 row(s)  duplicates removed
  [FLAG] Missing values in key fields           1 row(s)  customer set to 'Unknown'
  [FLAG] Negative quantities                    1 row(s)  rows excluded
  [FLAG] Dates out of range                     1 row(s)  rows excluded
  [PASS] Rejected qty within produced qty       0 row(s)
  [PASS] Completion timestamps present          0 row(s)
----------------------------------------------------------------
  587 rows read, 584 rows used, 4 of 7 checks raised issues
```

## How to run

Needs Python 3.10+ and Google Chrome (for the PDF).

```bash
pip install -r requirements.txt
python generate_sample_data.py
python build_report.py
python build_report.py --date 2026-11-10   # report for a particular day
```

The report is saved in `output/` as HTML and PDF.

## Notes

- `generate_sample_data.py` makes 30 days of dummy production data with a fixed seed.
- `build_report.py` does the checks, KPIs, chart and the PDF.
- The report layout is in `templates/` (Jinja2 HTML and CSS, A4 size).
- Limits like the on-time grace period, late flag and yield target are at the top of `build_report.py`.
- Made with Python, pandas, Jinja2, matplotlib and headless Chrome.
