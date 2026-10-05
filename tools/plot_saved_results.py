"""Redraw two comparison curves offline as SVG, using saved CSVs only."""
import csv
import html
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLORS = ['#005ea8', '#d55e00', '#a64ca6', '#00866a', '#555555']


def render(source, group_key, equity_key, output, title):
    groups = defaultdict(list)
    with source.open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            groups[r[group_key]].append((r['trade_date'], (float(r[equity_key])/100000-1)*100))
    dates = sorted({date for rs in groups.values() for date, _ in rs})
    index = {date: i for i, date in enumerate(dates)}
    lower = min(0, min(v for rs in groups.values() for _, v in rs))
    upper = max(0, max(v for rs in groups.values() for _, v in rs))
    lower, upper = lower-2, upper+2
    x = lambda date: 65+index[date]/max(1, len(dates)-1)*665
    y = lambda value: 365-(value-lower)/(upper-lower)*295
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="425" viewBox="0 0 1000 425">', '<rect width="1000" height="425" fill="white"/>', '<g font-family="Arial,sans-serif" font-size="13" fill="#222">', f'<text x="65" y="30" font-size="18">{html.escape(title)}</text>']
    for i in range(6):
        val = lower+(upper-lower)*i/5
        svg += [f'<line x1="65" y1="{y(val):.2f}" x2="730" y2="{y(val):.2f}" stroke="#e5e5e5"/>', f'<text x="55" y="{y(val)+4:.2f}" text-anchor="end">{val:.1f}%</text>']
    for i in sorted({0, len(dates)//2, len(dates)-1}):
        svg.append(f'<text x="{x(dates[i]):.2f}" y="390" text-anchor="middle">{dates[i]}</text>')
    svg.append(f'<line x1="65" y1="{y(0):.2f}" x2="730" y2="{y(0):.2f}" stroke="#999" stroke-dasharray="4 4"/>')
    for i, (name, rs) in enumerate(groups.items()):
        color = COLORS[i % len(COLORS)]
        coords = ' '.join(f'{x(date):.2f},{y(val):.2f}' for date, val in sorted(rs))
        svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        svg.append(f'<text x="755" y="{85+i*30}" fill="{color}">{html.escape(name.replace("_", " "))}</text>')
    svg += ['<text x="65" y="415" font-size="11">Saved evaluation outputs; initial equity $100,000.</text>', '</g></svg>']
    output.parent.mkdir(exist_ok=True)
    output.write_text('\n'.join(svg)+'\n', encoding='utf-8')
    print(output.relative_to(ROOT))


if __name__ == '__main__':
    render(ROOT/'baselines/experiments/gold_baseline_eval_20260101_20260630/eventtrader_vs_baselines_progress.csv', 'strategy', 'equity_close', ROOT/'generated/main_comparison.svg', 'Main agent comparison: GCUSD 2026H1')
    render(ROOT/'experiments/timebatch/daily_equity.csv', 'variant', 'equity', ROOT/'generated/timebatch_comparison.svg', 'Input formation comparison: February 4–March 5, 2026')
