from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

from app.services.analytics import AnalyticsSnapshot, CategoryStat

logger = logging.getLogger(__name__)

sns.set_style("whitegrid")
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['font.size'] = 10
plt.rcParams['figure.figsize'] = (10, 7)
plt.rcParams['figure.dpi'] = 100


def generate_pie_chart(category_stats: list[CategoryStat]) -> io.BytesIO | None:
    if not category_stats:
        return None

    category_colors = {
        "Навчання": "#4285F4",  # Синій
        "Робота": "#34A853",  # Зелений
        "Особисте": "#9C27B0",  # Фіолетовий
        "Фокус": "#FF9800",  # Помаранчевий
        "Інше": "#9E9E9E",  # Сірий
    }

    labels = [stat.label for stat in category_stats]
    values = [stat.hours for stat in category_stats]
    colors = [category_colors.get(label, "#9E9E9E") for label in labels]

    total = sum(values)
    if total == 0:
        return None

    percentages = [v / total * 100 for v in values]
    labels_with_pct = [f"{label}\n{percent:.1f}%" for label, percent in zip(labels, percentages)]

    fig, ax = plt.subplots(figsize=(10, 7))
    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels_with_pct,
        colors=colors,
        autopct='',
        startangle=90,
        textprops={'fontsize': 11},
    )

    if wedges:
        max_idx = values.index(max(values))
        wedges[max_idx].set_edgecolor('white')
        wedges[max_idx].set_linewidth(2)

    ax.set_title("Розподіл часу по категоріях", fontsize=14, fontweight='bold', pad=20)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    plt.close(fig)
    buf.seek(0)

    return buf


def generate_heatmap(events: list[Any], days: int = 7) -> io.BytesIO | None:
    from app.services.analytics import _extract_datetime

    heatmap_data: dict[tuple[int, int], float] = {}

    day_names_uk = ["Пн", "Вв", "Ср", "Чт", "Пт", "Сб", "Нд"]

    for event in events:
        if hasattr(event, 'start'):
            start_payload = event.start
            end_payload = event.end
        else:
            start_payload = event.get("start")
            end_payload = event.get("end")
        
        start_dt = _extract_datetime(start_payload)
        end_dt = _extract_datetime(end_payload)
        if not start_dt or not end_dt:
            continue

        duration_hours = (end_dt - start_dt).total_seconds() / 3600
        if duration_hours <= 0:
            continue

        day_of_week = start_dt.weekday()
        start_hour = start_dt.hour

        current_hour = start_hour
        remaining_duration = duration_hours

        while remaining_duration > 0 and current_hour < 24:
            hour_duration = min(1.0, remaining_duration)
            key = (day_of_week, current_hour)
            heatmap_data[key] = heatmap_data.get(key, 0.0) + hour_duration
            remaining_duration -= hour_duration
            current_hour += 1

    if not heatmap_data:
        return None

    heatmap_matrix = [[0.0 for _ in range(24)] for _ in range(7)]

    for (day, hour), hours_value in heatmap_data.items():
        if 0 <= day < 7 and 0 <= hour < 24:
            heatmap_matrix[day][hour] = hours_value

    fig, ax = plt.subplots(figsize=(14, 6))

    sns.heatmap(
        heatmap_matrix,
        xticklabels=list(range(24)),
        yticklabels=day_names_uk,
        cmap='YlOrRd',
        annot=False,
        fmt='.1f',
        cbar_kws={'label': 'Години'},
        linewidths=0.5,
        linecolor='white',
        ax=ax,
    )

    ax.set_xlabel("Година дня", fontsize=12, fontweight='bold')
    ax.set_ylabel("День тижня", fontsize=12, fontweight='bold')
    ax.set_title("Теплова карта продуктивності (заняття по днях та годинах)", fontsize=14, fontweight='bold', pad=15)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    plt.close(fig)
    buf.seek(0)

    return buf


def generate_daily_bar_chart(day_totals: dict[str, float]) -> io.BytesIO | None:
    if not day_totals:
        return None

    sorted_days = sorted(day_totals.items(), key=lambda x: x[0])
    days = [day for day, _ in sorted_days]
    hours = [hours for _, hours in sorted_days]

    fig, ax = plt.subplots(figsize=(12, 6))

    bars = ax.bar(days, hours, color='#4285F4', alpha=0.7, edgecolor='white', linewidth=1.5)

    if hours:
        max_idx = hours.index(max(hours))
        bars[max_idx].set_color('#FF6B6B')
        bars[max_idx].set_alpha(0.9)

    ax.set_xlabel("День", fontsize=12, fontweight='bold')
    ax.set_ylabel("Години", fontsize=12, fontweight='bold')
    ax.set_title("Завантаженість по днях", fontsize=14, fontweight='bold', pad=15)
    ax.grid(axis='y', alpha=0.3, linestyle='--')

    for bar, hour in zip(bars, hours):
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2.,
            height + 0.1,
            f'{hour:.1f}',
            ha='center',
            va='bottom',
            fontsize=10,
        )

    plt.xticks(rotation=45, ha='right')
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    plt.close(fig)
    buf.seek(0)

    return buf


def generate_all_charts(snapshot: AnalyticsSnapshot, events: list[Any] | None = None) -> list[tuple[str, io.BytesIO]]:
    from app.services.analytics import _extract_datetime

    charts = []

    if snapshot.category_stats:
        pie_chart = generate_pie_chart(snapshot.category_stats)
        if pie_chart:
            charts.append(("Розподіл по категоріях", pie_chart))

    if events:
        heatmap = generate_heatmap(events, days=snapshot.days)
        if heatmap:
            charts.append(("Теплова карта продуктивності", heatmap))

        day_totals: dict[str, float] = {}
        for event in events:
            if hasattr(event, 'start'):
                start_payload = event.start
                end_payload = event.end
            else:
                start_payload = event.get("start")
                end_payload = event.get("end")
            
            start_dt = _extract_datetime(start_payload)
            end_dt = _extract_datetime(end_payload)
            if not start_dt or not end_dt:
                continue

            duration = (end_dt - start_dt).total_seconds() / 60
            if duration <= 0:
                continue

            day_key = start_dt.strftime("%a %d.%m")
            day_totals[day_key] = day_totals.get(day_key, 0.0) + duration / 60

        if day_totals:
            bar_chart = generate_daily_bar_chart(day_totals)
            if bar_chart:
                charts.append(("Завантаженість по днях", bar_chart))

    return charts

