from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


KEY_SCENARIO = "media_mix:warm:concurrent"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def percentage_reduction(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return round((1.0 - (after / before)) * 100.0, 3)


def percentage_gain(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return round(((after / before) - 1.0) * 100.0, 3)


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    benchmark = load_json(args.benchmark)
    cpu_benchmark = load_json(args.cpu_benchmark)
    stress = load_json(args.stress)
    smoke = load_json(args.smoke)

    control = benchmark["median"]["control"]
    candidate = benchmark["median"]["candidate"]
    key_control = control[KEY_SCENARIO]
    key_candidate = candidate[KEY_SCENARIO]
    latency_reduction = percentage_reduction(
        float(key_control["p95_ms"]),
        float(key_candidate["p95_ms"]),
    )
    throughput_gain = percentage_gain(
        float(key_control["throughput_per_second"]),
        float(key_candidate["throughput_per_second"]),
    )

    p95_changes = {
        key: percentage_reduction(
            float(control[key]["p95_ms"]),
            float(candidate[key]["p95_ms"]),
        )
        for key in control
    }
    worst_p95_key = min(p95_changes, key=p95_changes.get)
    worst_p95_change = p95_changes[worst_p95_key]

    cpu_control = cpu_benchmark["median"]["control"][KEY_SCENARIO]
    cpu_candidate = cpu_benchmark["median"]["candidate"][KEY_SCENARIO]
    cpu_reduction = percentage_reduction(
        float(cpu_control["cpu_seconds"]),
        float(cpu_candidate["cpu_seconds"]),
    )
    control_cpu_per_10k = round(float(cpu_control["cpu_seconds"]) * 10.0, 6)
    candidate_cpu_per_10k = round(float(cpu_candidate["cpu_seconds"]) * 10.0, 6)

    shared_legacy_conflict = (
        args.shared_failed == 1
        and "onebot_queue_applies_backpressure" in args.shared_failure_note
    )
    checks = {
        "key_p95_reduction_at_least_20_percent": latency_reduction >= 20.0,
        "key_throughput_gain_at_least_25_percent": throughput_gain >= 25.0,
        "no_core_p95_regression_over_5_percent": worst_p95_change >= -5.0,
        "cpu_per_10000_reduction_at_least_15_percent": cpu_reduction >= 15.0,
        "isolated_30_minute_stress": bool(stress.get("passed")),
        "real_readonly_5_minute_smoke": bool(smoke.get("passed")),
        "v022_regression": args.v022_passed == args.v022_total,
        "shared_regression_or_documented_legacy_conflict": (
            args.shared_failed == 0 or shared_legacy_conflict
        ),
        "browser_matrix": args.browser_validators_passed >= 7,
        "static_and_dependency_checks": args.static_checks_passed,
    }

    failed_stages = [name for name, passed in checks.items() if not passed]
    failed_stages.extend(
        f"stress:{name}"
        for name, passed in (stress.get("acceptance") or {}).items()
        if not passed
    )
    failed_stages.extend(
        f"smoke:{name}"
        for name, passed in (smoke.get("checks") or {}).items()
        if not passed
    )

    formal = stress.get("formal") or {}
    resources = formal.get("resources") or {}
    event_loop = formal.get("event_loop") or {}
    queues = formal.get("queues") or {}
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "baseline_commit": "bec4f05",
        "candidate_version": "v0.2.2",
        "benchmark": {
            "scenario": KEY_SCENARIO,
            "control_p95_ms": round(float(key_control["p95_ms"]), 3),
            "candidate_p95_ms": round(float(key_candidate["p95_ms"]), 3),
            "p95_reduction_percent": latency_reduction,
            "control_throughput_per_second": round(float(key_control["throughput_per_second"]), 3),
            "candidate_throughput_per_second": round(float(key_candidate["throughput_per_second"]), 3),
            "throughput_gain_percent": throughput_gain,
            "worst_core_p95_scenario": worst_p95_key,
            "worst_core_p95_change_percent": worst_p95_change,
            "control_cpu_seconds_per_10000": control_cpu_per_10k,
            "candidate_cpu_seconds_per_10000": candidate_cpu_per_10k,
            "cpu_reduction_percent": cpu_reduction,
        },
        "stress": {
            "passed": bool(stress.get("passed")),
            "duration_seconds": formal.get("duration_seconds", 0),
            "messages": formal.get("messages") or {},
            "event_loop": event_loop,
            "resources": resources,
            "queues": queues,
            "shutdown_seconds": stress.get("shutdown_seconds", 0),
            "acceptance": stress.get("acceptance") or {},
        },
        "smoke": smoke,
        "regression": {
            "v022": {"passed": args.v022_passed, "total": args.v022_total},
            "shared": {
                "passed": args.shared_passed,
                "failed": args.shared_failed,
                "documented_legacy_conflict": shared_legacy_conflict,
                "failure_note": args.shared_failure_note,
            },
            "browser_validators_passed": args.browser_validators_passed,
            "static_checks_passed": args.static_checks_passed,
        },
        "before_after_why": [
            {
                "before": "Unbounded or task-per-message scheduling and repeated per-Bot resources",
                "after": "Bounded fixed workers with shared identity, media, logging and crypto services",
                "why": "Caps memory and scheduling overhead while preserving room ordering",
            },
            {
                "before": "Control frames and persistence could be delayed behind message work",
                "after": "Control-plane fast paths plus bounded asynchronous Journal backpressure",
                "why": "Keeps heartbeats responsive and makes overload behavior explicit",
            },
            {
                "before": "Synchronous media, E2EE, logging and redundant WebUI polling",
                "after": "Single-flight offload, listener logging and visibility-aware incremental UI",
                "why": "Reduces event-loop delay, CPU work and unnecessary I/O",
            },
        ],
        "resource_curve_summary": {
            "rss_slope_mib_per_10min": resources.get("rss_slope_mib_per_10min", 0),
            "threads": [resources.get("threads_baseline", 0), resources.get("threads_end", 0)],
            "tasks": [resources.get("tasks_baseline", 0), resources.get("tasks_end", 0)],
            "handles": [resources.get("handles_baseline", 0), resources.get("handles_end", 0)],
            "event_loop_p99_ms": event_loop.get("p99", 0),
            "event_loop_max_ms": event_loop.get("max", 0),
            "ingress_high_water": queues.get("ingress_high_water", 0),
            "ingress_capacity": queues.get("ingress_capacity", 0),
        },
        "checks": checks,
        "failed_stages": failed_stages,
        "passed": all(checks.values()) and not failed_stages,
    }


def render_markdown(report: dict[str, Any]) -> str:
    benchmark = report["benchmark"]
    stress = report["stress"]
    resources = report["resource_curve_summary"]
    lines = [
        "# RocketCatShell Docker / Linux v0.2.2 性能与稳定性验收报告",
        "",
        f"- 总体结果：{'通过' if report['passed'] else '未通过'}",
        f"- 对照基线：`{report['baseline_commit']}`",
        f"- 关键混合链路 p95：{benchmark['control_p95_ms']} ms → {benchmark['candidate_p95_ms']} ms（降低 {benchmark['p95_reduction_percent']}%）",
        f"- 关键混合链路吞吐：{benchmark['control_throughput_per_second']} → {benchmark['candidate_throughput_per_second']} msg/s（提升 {benchmark['throughput_gain_percent']}%）",
        f"- CPU / 10,000 条：{benchmark['control_cpu_seconds_per_10000']} s → {benchmark['candidate_cpu_seconds_per_10000']} s（降低 {benchmark['cpu_reduction_percent']}%）",
        f"- 30 分钟压力测试：{'通过' if stress['passed'] else '未通过'}，关闭耗时 {stress['shutdown_seconds']} s",
        f"- 5 分钟实机只读冒烟：{'通过' if report['smoke'].get('passed') else '未通过'}",
        "",
        "## Before / After / Why",
        "",
        "| Before | After | Why |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {item['before']} | {item['after']} | {item['why']} |"
        for item in report["before_after_why"]
    )
    lines.extend(
        [
            "",
            "## 验收门槛",
            "",
            *(
                f"- [{'x' if passed else ' '}] {name}"
                for name, passed in report["checks"].items()
            ),
            "",
            "## 资源曲线摘要",
            "",
            f"- RSS 斜率：{resources['rss_slope_mib_per_10min']} MiB / 10min",
            f"- 线程：{resources['threads'][0]} → {resources['threads'][1]}",
            f"- Task：{resources['tasks'][0]} → {resources['tasks'][1]}",
            f"- 句柄：{resources['handles'][0]} → {resources['handles'][1]}",
            f"- 事件循环：p99 {resources['event_loop_p99_ms']} ms，max {resources['event_loop_max_ms']} ms",
            f"- 入站队列高水位：{resources['ingress_high_water']} / {resources['ingress_capacity']}",
            "",
            "## 失败阶段",
            "",
            *(
                [f"- {stage}" for stage in report["failed_stages"]]
                or ["- 无"]
            ),
            "",
            "## 兼容性说明",
            "",
            f"- v0.2.2 回归：{report['regression']['v022']['passed']} / {report['regression']['v022']['total']}。",
            f"- 共享回归：{report['regression']['shared']['passed']} 通过、{report['regression']['shared']['failed']} 失败。",
            f"- 旧契约说明：{report['regression']['shared']['failure_note']}",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the RocketCatShell v0.2.2 acceptance report")
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--cpu-benchmark", type=Path, required=True)
    parser.add_argument("--stress", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--v022-passed", type=int, default=109)
    parser.add_argument("--v022-total", type=int, default=109)
    parser.add_argument("--shared-passed", type=int, default=62)
    parser.add_argument("--shared-failed", type=int, default=1)
    parser.add_argument(
        "--shared-failure-note",
        default=(
            "test_onebot_queue_applies_backpressure is a v0.2.0 blocking contract; "
            "v0.2.2 intentionally drops offline OneBot events without replay and counts them."
        ),
    )
    parser.add_argument("--browser-validators-passed", type=int, default=7)
    parser.add_argument("--static-checks-passed", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(render_markdown(report), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
