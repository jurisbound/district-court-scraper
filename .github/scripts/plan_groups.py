"""Split the court complexes of the chosen states into groups, one GitHub job each.

Whole districts stay together. Districts are packed into a group until it holds
at least COMPLEXES_PER_JOB complexes, so a big state gets several jobs and a
small state one. Groups never share a complex, so jobs of one run never write
the same storage files (index, markers are kept per complex).

Usage (from the repository root):
    python .github/scripts/plan_groups.py <state_codes|all> <complexes_per_job>
        -> prints  groups=<json list>  for $GITHUB_OUTPUT
    python .github/scripts/plan_groups.py --write <state> <districts> <out.csv>
        -> writes the courts list of one group (same columns as courts.csv)
"""
import csv
import json
import sys
from collections import OrderedDict

COURTS = "district-court/courts.csv"


def _rows():
    with open(COURTS, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def plan(state_arg: str, per_job: int) -> list:
    _, rows = _rows()
    known = sorted({r["state_code"] for r in rows}, key=int)
    if state_arg.strip().lower() == "all":
        states = known
    else:
        states = [s.strip() for s in state_arg.split(",") if s.strip()]
    unknown = [s for s in states if s not in known]
    if unknown:
        raise SystemExit(f"Unknown state code(s): {unknown}. Known: {known}")

    groups = []
    for state in states:
        districts = OrderedDict()           # district_code -> complexes, in file order
        for r in rows:
            if r["state_code"] == state:
                districts[r["district_code"]] = districts.get(r["district_code"], 0) + 1
        current, count, part = [], 0, 1
        for code, n in sorted(districts.items(), key=lambda kv: int(kv[0])):
            current.append(code)
            count += n
            if count >= per_job:
                groups.append({"name": f"{state}-{part}", "state": state,
                               "districts": ",".join(current), "complexes": count})
                current, count, part = [], 0, part + 1
        if current:
            groups.append({"name": f"{state}-{part}", "state": state,
                           "districts": ",".join(current), "complexes": count})
    if len(groups) > 256:
        raise SystemExit(f"{len(groups)} groups: GitHub allows at most 256 jobs in one run. "
                         "Raise complexes_per_job.")
    return groups


def write(state: str, districts: str, out: str) -> int:
    fields, rows = _rows()
    wanted = set(districts.split(","))
    chosen = [r for r in rows if r["state_code"] == state and r["district_code"] in wanted]
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(chosen)
    return len(chosen)


if __name__ == "__main__":
    if sys.argv[1] == "--write":
        n = write(sys.argv[2], sys.argv[3], sys.argv[4])
        print(f"{n} court complexes in this job")
    else:
        groups = plan(sys.argv[1], int(sys.argv[2]))
        print("groups=" + json.dumps(groups))
