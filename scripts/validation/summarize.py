"""Merge attempts without erasing initial failures."""
import argparse
from collections import Counter, defaultdict
import csv
import json
import os
from pathlib import Path


def summarize(paths):
    attempts = defaultdict(list)
    for path in paths:
        report = json.loads(path.read_text())
        for row in report['cases']:
            attempts[row['id']].append({**row, 'lane': report['lane'], 'report': path.name})
    result = []
    for case_id in range(1, 121):
        history = attempts[case_id]
        last = history[-1] if history else {}
        first_failure = next((item for item in history if item.get('status') != 'passed'), {})
        result.append({
            'id': case_id, 'lane': last.get('lane', ''),
            'question': last.get('question', ''),
            'criterion': last.get('criterion', ''),
            'status': last.get('status', 'not_executed'),
            'first_failure': first_failure.get('reason', ''),
            'last_failure': last.get('reason', ''),
            'evidence': ', '.join(dict.fromkeys(item['report'] for item in history)),
            'attempts': len(history),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = summarize(args.reports)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(Counter(row['status'] for row in rows), ensure_ascii=False))


if __name__ == '__main__':
    main()
